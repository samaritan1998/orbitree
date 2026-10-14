from __future__ import annotations

import ctypes
import fcntl
import json
import os
import pwd
import signal
import subprocess
import sys
from pathlib import Path

from .store import TeamStore, valid_username


def apply_filesystem_policy(home: Path, release: Path, shared_read: list[Path]) -> None:
    from py_landlock import AccessFs, Landlock, get_abi_version
    from py_landlock.abi import get_supported_fs

    abi = get_abi_version()
    if abi < 3:
        raise RuntimeError(
            "Team workers require Landlock ABI 3 or newer; refusing to run unisolated"
        )
    rules = Landlock().allow_all_network().allow_all_scope()
    readonly = [Path(p) for p in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/sys")]
    readonly += [release, Path(sys.base_prefix), *shared_read]
    for path in readonly:
        if path.exists():
            rules.allow_read(path).allow_execute(path)
    # Other users' command lines may contain submitted prompts.
    rules.add_path_rule("/proc", access=AccessFs.READ_DIR)
    rules.allow_read("/proc/self")
    for name in ("cpuinfo", "meminfo", "stat", "uptime", "loadavg", "version"):
        rules.add_path_rule(Path("/proc") / name, access=AccessFs.READ_FILE)
    # Do not grant /tmp, /mnt, /workspace, /root, or the other users' homes.
    rules.add_path_rule(home, access=get_supported_fs(abi))
    for path in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/tty", "/dev/ptmx"):
        if Path(path).exists():
            rules.add_path_rule(path, access=AccessFs.READ_FILE | AccessFs.WRITE_FILE)
    if Path("/dev/pts").exists():
        rules.allow_read_write("/dev/pts")
    rules.apply()


def linux_name(username: str) -> str:
    return f"rops_{valid_username(username)}"


def validate_release(release: Path) -> Path:
    release = release.resolve()
    if not (release / "backend/research_ops/team/runtime.py").is_file():
        raise ValueError("Release must contain the Orbitree backend")
    if (release / ".env").exists() or (release / "data/research-ops.db").exists():
        raise ValueError("Use a clean shared release, not the owner's live source/data directory")
    if release.stat().st_uid != 0 or release.stat().st_mode & 0o022:
        raise ValueError("Shared release must be root-owned and not group/world writable")
    return release


def _private_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as handle:
        temporary.chmod(0o600)
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def provision(root: Path, username: str, release: Path, shared_read: list[Path]) -> Path:
    valid_username(username)
    control = TeamStore(root).root / "control"
    with (control / f"{username}.provision.lock").open("a") as handle:
        os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Workspace provisioning is already in progress") from exc
        return _provision(root, username, release, shared_read)


def _provision(root: Path, username: str, release: Path, shared_read: list[Path]) -> Path:
    if os.geteuid() != 0:
        raise ValueError("Provisioning Linux users requires a server administrator")
    store = TeamStore(root)
    account = store.account(valid_username(username))
    if not account:
        raise ValueError("Create the account before provisioning its Linux user")
    release = validate_release(release)
    users = store.root / "users"
    users.mkdir(mode=0o711, exist_ok=True)
    home = users / username
    name = linux_name(username)
    for path in shared_read:
        if (
            not path.is_absolute()
            or not path.is_dir()
            or path.resolve()
            in {
                Path("/"),
                Path("/root"),
                Path("/mnt"),
                Path("/workspace"),
                store.root,
            }
        ):
            raise ValueError(
                "Shared paths must be explicit data directories, not system/workspace roots"
            )
    plan = {
        "username": username,
        "home": str(home),
        "release": str(release),
        "shared_read": [str(path.resolve()) for path in shared_read],
    }
    policy = store.root / "control" / f"{username}.json"
    journal = store.root / "control" / f"{username}.provision.json"
    try:
        identity = pwd.getpwnam(name)
    except KeyError:
        identity = None
    if policy.exists():
        previous = json.loads(policy.read_text())
        if any(previous.get(key) != value for key, value in plan.items()) or (
            not identity
            or identity.pw_uid == 0
            or previous.get("uid") != identity.pw_uid
            or previous.get("gid") != identity.pw_gid
            or identity.pw_dir != str(home)
            or home.is_symlink()
            or not (home / "config/settings.json").is_file()
        ):
            raise ValueError(
                "Existing workspace policy does not match; administrator review required"
            )
        return home
    # A root-only intent receipt makes a partial setup retryable without adopting
    # an unrelated Linux identity or overwriting an existing user's workspace.
    if journal.exists():
        previous = json.loads(journal.read_text())
        if any(previous.get(key) != value for key, value in plan.items()):
            raise ValueError("Provisioning settings changed; administrator review required")
        if identity is None and "uid" in previous:
            raise ValueError("Provisioned Linux identity is missing; administrator review required")
        if identity and (
            identity.pw_uid == 0
            or identity.pw_dir != str(home)
            or identity.pw_shell != "/usr/sbin/nologin"
            or previous.get("uid", identity.pw_uid) != identity.pw_uid
            or previous.get("gid", identity.pw_gid) != identity.pw_gid
        ):
            raise ValueError("Linux identity changed; refusing to reuse it")
    else:
        if home.exists() or home.is_symlink():
            raise ValueError("Workspace already exists; it will not be overwritten")
        if identity:
            raise ValueError("Linux user already exists; refusing to reuse an unrelated identity")
        _private_json(journal, plan)
    if identity is None:
        subprocess.run(
            [
                "/usr/sbin/useradd",
                "--user-group",
                "--no-create-home",
                "--home-dir",
                str(home),
                "--shell",
                "/usr/sbin/nologin",
                "--password",
                "!",
                name,
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        identity = pwd.getpwnam(name)
    policy_data = plan | {"uid": identity.pw_uid, "gid": identity.pw_gid}
    _private_json(journal, policy_data)
    if home.is_symlink():
        raise ValueError("Workspace must not be a symbolic link")
    home.mkdir(mode=0o700, exist_ok=True)
    managed = [home]
    for child in ("run", "data", "work", "tmp", ".codex", ".claude", ".pi", "skills", "config"):
        directory = home / child
        if directory.is_symlink():
            raise ValueError("Workspace directories must not be symbolic links during setup")
        directory.mkdir(mode=0o700, exist_ok=True)
        managed.append(directory)
    programs = home / "config/programs.yaml"
    if programs.is_symlink():
        raise ValueError("Workspace configuration must not be symbolic links")
    if not programs.exists():
        programs.write_text(
            "programs:\n  - code: FREE\n    slug: free\n    name: Free Work\n"
            "    objective: ''\n    color: '#32766d'\n    sort_order: 10\n"
        )
    settings = {
        "load_project_env": False,
        "require_access_token": True,
        "access_token": account["worker_token"],
        "data_dir": str(home / "data"),
        "runtime_dir": str(home / "data"),
        "workspace_root": str(home / "work"),
        "programs_file": str(programs),
        "frontend_dist": str(release / "web/dist"),
        "codex_home": str(home / ".codex"),
        "claude_home": str(home / ".claude"),
        "pi_session_dir": str(home / ".pi/agent/sessions"),
        "skill_source_root": str(home / "skills"),
        "skill_install_root": str(home / ".codex/skills"),
        "codex_enabled": True,
        "claude_enabled": True,
        "pi_enabled": False,
        "codex_registry_enabled": True,
        "infoflow_notifications_enabled": False,
        "infoflow_recipient": username,
        "ltp_monitor_users": [username],
        "ltp_control_directory_user": username,
        "ltp_monitor_enabled": False,
        "cluster_dashboard_enabled": False,
        "managed_jobs_enabled": False,
        "daily_reflection_enabled": False,
        "agentos_enabled": False,
        "agentos_job_sync_enabled": False,
        "dag_job_sync_enabled": False,
        "session_workflows_enabled": False,
        "session_workflow_analysis_enabled": False,
        "openreward_enabled": False,
        "goblin_enabled": False,
        "claude_permission_mode": "default",
        "claude_web_approvals_enabled": True,
        "web_terminal_enabled": True,
    }
    config = home / "config/settings.json"
    if programs.is_symlink() or config.is_symlink():
        raise ValueError("Workspace configuration must not be symbolic links")
    if not config.exists():
        _private_json(config, settings)
    config.chmod(0o600)
    for path in [*managed, programs, config]:
        os.chown(path, identity.pw_uid, identity.pw_gid)
    # Root-owned launch policy lives outside the writable user home.
    _private_json(policy, policy_data)
    return home


def run_worker(policy_file: Path, command: list[str] | None = None) -> None:
    """Drop identity, clear inherited credentials, then confine before importing the app."""
    if os.geteuid() != 0:
        raise ValueError("Run the launcher as the server administrator")
    if policy_file.stat().st_uid != 0 or policy_file.stat().st_mode & 0o077:
        raise ValueError("Launch policy must be root-owned with mode 0600")
    policy = json.loads(policy_file.read_text())
    identity = pwd.getpwnam(linux_name(policy["username"]))
    if identity.pw_uid != policy["uid"] or identity.pw_gid != policy["gid"] or not identity.pw_uid:
        raise ValueError("Linux account changed or unexpectedly has root privileges")
    home = Path(policy["home"]).resolve()
    release = Path(policy["release"]).resolve()
    os.environ.clear()
    os.environ.update(
        {
            "HOME": str(home),
            "USER": identity.pw_name,
            "LOGNAME": identity.pw_name,
            "PATH": f"{release}/tools/bin:{release}/.venv/bin:/usr/local/bin:/usr/bin:/bin",
            "TMPDIR": str(home / "tmp"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
            "PYTHONPATH": str(release / "backend"),
            "CODEX_HOME": str(home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "ROPS_ENV_FILE": str(home / "config/user.env"),
            "ROPS_LOAD_PROJECT_ENV": "false",
            "ROPS_TEAM_SETTINGS": str(home / "config/settings.json"),
        }
    )
    os.umask(0o077)
    os.setgroups([])
    os.setgid(identity.pw_gid)
    os.setuid(identity.pw_uid)
    os.chdir(home / "work")
    apply_filesystem_policy(home, release, [Path(path) for path in policy["shared_read"]])
    executable = release / ".venv/bin/python"
    if command:
        os.execvpe(command[0], command, os.environ)
    # Exec discards modules imported as root (including home-derived settings defaults).
    os.execve(executable, [str(executable), "-m", "research_ops.team.worker"], os.environ)


def start_worker(root: Path, username: str) -> int:
    valid_username(username)
    policy_file = root.resolve() / "control" / f"{username}.json"
    policy = json.loads(policy_file.read_text())
    socket = Path(policy["home"]) / "run/api.sock"
    # A live socket is not safe to unlink; start uses an advisory lock in the worker as well.
    pid_file = root / "control" / f"{username}.pid"
    if pid_file.exists():
        pid = int(pid_file.read_text())
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError(f"Worker is already running (PID {pid})")
    if socket.exists():
        socket.unlink()
    log_path = root / "control" / f"{username}.log"
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "research_ops.team.launch",
                str(policy_file),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        )
    log_path.chmod(0o600)
    pid_file.write_text(str(proc.pid))
    pid_file.chmod(0o600)
    return proc.pid


def stop_worker(root: Path, username: str) -> None:
    valid_username(username)
    pid_file = root / "control" / f"{username}.pid"
    policy = json.loads((root / "control" / f"{username}.json").read_text())
    pid = int(pid_file.read_text())
    proc = Path(f"/proc/{pid}")
    if not proc.exists():
        return
    # Some Python builds omit os.pidfd_open even on a supporting Linux host.
    # Bind libc's named functions instead of using architecture-specific syscalls.
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "pidfd_open") or not hasattr(libc, "pidfd_send_signal"):
        raise RuntimeError("Safe worker stop requires libc pidfd support")
    libc.pidfd_open.argtypes = [ctypes.c_int, ctypes.c_uint]
    libc.pidfd_open.restype = ctypes.c_int
    libc.pidfd_send_signal.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
    libc.pidfd_send_signal.restype = ctypes.c_int
    process_handle = libc.pidfd_open(pid, 0)
    if process_handle < 0:
        raise OSError(ctypes.get_errno(), "Could not open the worker process")
    try:
        command = (proc / "cmdline").read_bytes().split(b"\0")
        if proc.stat().st_uid != policy["uid"] or b"research_ops.team.worker" not in command:
            raise ValueError("PID is not this Orbitree worker; refusing to signal it")
        if libc.pidfd_send_signal(process_handle, signal.SIGTERM, None, 0) < 0:
            raise OSError(ctypes.get_errno(), "Could not signal the worker process")
    finally:
        os.close(process_handle)

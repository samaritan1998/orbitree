"""Root-owned routing for personal Claude sessions and an opt-in TUI account.

The existing claude-dmit wrapper supplies the proxy environment, then invokes
this launcher. Unregistered managed turns keep their original identity. An
administrator may select a default account for new interactive invocations.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import pwd
import stat
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any


def session_argument(args: list[str]) -> str | None:
    sessions: set[str] = set()
    for index, value in enumerate(args):
        if (
            value in {"--resume", "-r", "--session-id"}
            and index + 1 < len(args)
            and not args[index + 1].startswith("-")
        ):
            sessions.add(args[index + 1])
        elif value.startswith(("--resume=", "--session-id=")):
            sessions.add(value.split("=", 1)[1])
    if len(sessions) > 1:
        raise ValueError("Conflicting Claude session arguments")
    return next(iter(sessions), None)


def load_profile(registry: Path, session_id: str | None) -> dict[str, Any] | None:
    if not registry.exists():
        return None
    info = registry.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("Claude user registry must be a root-owned 0600 file")
    data = json.loads(registry.read_text())
    if data.get("version") != 1:
        raise ValueError("Unsupported Claude user registry version")
    profile = (
        data.get("sessions", {}).get(session_id)
        if session_id
        else data.get("interactive_default")
    )
    if profile is not None and not isinstance(profile, dict):
        raise ValueError("Invalid Claude user profile")
    return profile


def permission_arguments(args: list[str], mode: str) -> list[str]:
    if mode not in {"default", "acceptEdits", "bypassPermissions", "dontAsk", "plan"}:
        raise ValueError("Invalid Claude user permission mode")
    result: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--permission-mode":
            if index + 1 == len(args):
                raise ValueError("Missing Claude permission mode")
            index += 2
            continue
        if value.startswith("--permission-mode=") or value == "--dangerously-skip-permissions":
            index += 1
            continue
        result.append(value)
        index += 1
    return ["--permission-mode", mode, *result]


def profile_environment(profile: dict[str, Any], inherited: dict[str, str]) -> dict[str, str]:
    home = str(Path(profile["home"]))
    environment = {
        key: value
        for key, value in inherited.items()
        if key
        in {
            "http_proxy",
            "https_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "all_proxy",
            "ALL_PROXY",
            "no_proxy",
            "NO_PROXY",
            "TERM",
            "COLORTERM",
            "LANG",
            "LC_ALL",
            "TZ",
        }
    }
    environment.update(
        {
            "HOME": home,
            "USER": profile["user"],
            "LOGNAME": profile["user"],
            "SHELL": "/bin/bash",
            "LANG": environment.get("LANG") or "C.UTF-8",
            "PATH": (
                f"{home}/.local/bin:{home}/bin:/opt/miniforge3/bin:/usr/local/bin:/usr/bin:/bin"
            ),
            "CLAUDE_CONFIG_DIR": str(Path(profile["config_dir"])),
            "XDG_CACHE_HOME": f"{home}/.cache",
            "TMPDIR": f"{home}/tmp",
            "DISABLE_AUTOUPDATER": "1",
        }
    )
    return environment


def prepare_native_entrypoint(home: Path, command: Path) -> None:
    """Provision the CLI's expected entrypoint after dropping to its owner UID."""
    directory = home / ".local" / "bin"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # An existing user installation is not ours to replace.
    with suppress(FileExistsError):
        (directory / "claude").symlink_to(command)


def workspace_umask(profile: dict[str, Any]) -> int:
    value = profile.get("workspace_umask", "0077")
    if value not in {"0077", "0007"}:
        raise ValueError("Claude workspace umask must be 0077 or private-group 0007")
    return int(value, 8)


def _directory_fd(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Artifact grants require absolute paths without traversal")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def repair_artifact_access(
    profile: dict[str, Any], receipt, *, max_entries: int = 5000
) -> dict[str, int]:
    """Reconcile metadata only inside administrator-registered artifact roots.

    Directory FDs prevent user-controlled symlinks from redirecting privileged
    chmod/chown. Hardlinks and other users' files are deliberately excluded.
    """
    roots = profile.get("artifact_read_roots", [])
    if not isinstance(roots, list) or len(roots) > 20:
        raise ValueError("Invalid Claude artifact read grants")
    excluded = {
        "cache",
        "__pycache__",
        "node_modules",
        "venv",
        "data",
        "weights",
        "checkpoints",
        "torch_compile_cache",
    }
    extensions = {
        ".json",
        ".jsonl",
        ".ndjson",
        ".log",
        ".txt",
        ".md",
        ".csv",
        ".tsv",
        ".yaml",
        ".yml",
    }
    counts = {"visited": 0, "changed": 0, "skipped": 0, "limited": 0}
    uid, gid = profile["uid"], profile["gid"]
    deadline = time.monotonic() + 5

    def visit(descriptor: int, path: Path, depth: int):
        if counts["visited"] >= max_entries or time.monotonic() > deadline:
            counts["limited"] += 1
            return
        info = os.fstat(descriptor)
        directory = stat.S_ISDIR(info.st_mode)
        counts["visited"] += 1
        if (
            info.st_uid not in {0, uid}
            or info.st_gid not in {0, gid}
            or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))
        ):
            counts["skipped"] += 1
            return
        mode = stat.S_IMODE(info.st_mode)
        desired = mode | (0o2050 if directory else 0o040)
        if info.st_gid != gid or mode != desired:
            receipt.write(
                json.dumps(
                    {
                        "path": str(path),
                        "uid": info.st_uid,
                        "gid": info.st_gid,
                        "mode": mode,
                        "new_gid": gid,
                        "new_mode": desired,
                    }
                )
                + "\n"
            )
            receipt.flush()
            os.fchown(descriptor, -1, gid)
            os.fchmod(descriptor, desired)
            counts["changed"] += 1
        if not directory:
            return
        if depth >= 8:
            counts["limited"] += 1
            return
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if counts["visited"] >= max_entries or time.monotonic() > deadline:
                    counts["limited"] += 1
                    break
                if entry.name.startswith(".") or entry.name in excluded or entry.is_symlink():
                    counts["skipped"] += 1
                    continue
                is_directory = entry.is_dir(follow_symlinks=False)
                if not is_directory and Path(entry.name).suffix.lower() not in extensions:
                    counts["skipped"] += 1
                    continue
                try:
                    child = os.open(
                        entry.name,
                        os.O_RDONLY
                        | os.O_NOFOLLOW
                        | os.O_NONBLOCK
                        | (os.O_DIRECTORY if is_directory else 0),
                        dir_fd=descriptor,
                    )
                except OSError as exc:
                    if exc.errno not in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                        raise
                    counts["skipped"] += 1
                    continue
                try:
                    visit(child, path / entry.name, depth + 1)
                finally:
                    os.close(child)

    for root in roots:
        path = Path(root)
        if "artifacts" not in path.parts or path.name == "artifacts":
            raise ValueError("Artifact grants require a specific directory below artifacts")
        try:
            descriptor = _directory_fd(path)
        except FileNotFoundError:
            counts["skipped"] += 1
            continue
        try:
            visit(descriptor, path, 0)
        finally:
            os.close(descriptor)
    return counts


def prepare_workspace_access(profile: dict[str, Any]) -> None:
    if not profile.get("artifact_read_roots"):
        return
    directory = Path("/var/lib/research-ops/claude-permissions")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("Claude permission audit directory must be private and root-owned")
    descriptor = os.open(
        directory / f"{int(profile['uid'])}.jsonl",
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "a") as receipt:
        info = os.fstat(receipt.fileno())
        if info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("Claude permission audit must be private and root-owned")
        counts = repair_artifact_access(profile, receipt)
    if counts["limited"]:
        print(
            "Claude artifact access scan reached its bound; some grants need administrator review",
            file=sys.stderr,
        )


def launch(registry: Path, fallback: str, args: list[str]) -> None:
    session_id = session_argument(args)
    profile = load_profile(registry, session_id) if session_id else None
    interactive_default = False
    if profile is None and not any(
        value in {"-p", "--print", "--input-format", "--output-format"}
        or value.startswith(("--print=", "--input-format=", "--output-format="))
        for value in args
    ):
        profile = load_profile(registry, None)
        interactive_default = profile is not None
    if profile is None:
        os.execv(fallback, [fallback, *args])
        return
    if os.geteuid() != 0:
        raise ValueError("Managed Claude user switching requires the server launcher")
    identity = pwd.getpwnam(profile["user"])
    if identity.pw_uid == 0 or (
        identity.pw_uid != profile["uid"] or identity.pw_gid != profile["gid"]
    ):
        raise ValueError("Claude user identity changed or is root")
    home = Path(profile["home"]).resolve()
    config = Path(profile["config_dir"]).resolve()
    if home.stat().st_uid != identity.pw_uid or home.stat().st_mode & 0o077:
        raise ValueError("Claude user home must be private and owned by its user")
    if not config.is_relative_to(home) or str(home) != identity.pw_dir:
        raise ValueError("Claude user config must stay inside its registered home")
    command = Path(profile["command"]).resolve()
    info = command.stat()
    if info.st_uid != 0 or info.st_mode & 0o022 or not info.st_mode & 0o111:
        raise ValueError("Claude executable must be root-owned and not writable by users")
    if not interactive_default:
        args = permission_arguments(args, profile["permission_mode"])
    environment = profile_environment(profile, dict(os.environ))
    mask = workspace_umask(profile)
    prepare_workspace_access(profile)
    os.umask(mask)
    os.setgroups([])
    os.setgid(identity.pw_gid)
    os.setuid(identity.pw_uid)
    prepare_native_entrypoint(home, command)
    # A new TUI belongs to the directory chosen by the user, not the last
    # migrated Branch. No permission grants are inferred from this directory.
    cwd = os.getcwd() if interactive_default else profile["cwd"]
    os.chdir(cwd)
    os.execve(command, [str(command), *args], environment)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--fallback", required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    args = options.args[1:] if options.args[:1] == ["--"] else options.args
    try:
        launch(options.registry, options.fallback, args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Claude user launch failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

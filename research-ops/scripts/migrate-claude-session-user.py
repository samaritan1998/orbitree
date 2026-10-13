#!/usr/bin/env python3
"""Move one idle, personal Claude session to a dedicated non-root account.

Run as root. This does not start a conversation, restart Research Ops, or copy
workspaces. The root-owned session registry is activated only after verification.
"""

import argparse
import datetime
import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
from pathlib import Path


def private_copy(source, destination):
    if source.is_file():
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        destination.chmod(0o600)


def grant_group_tree(path, gid, writable, backup):
    """CFS has no POSIX ACL support; keep owners and grant only the dedicated group."""
    completed = set()
    if backup.exists():
        with backup.open() as prior:
            completed = {json.loads(line)["path"] for line in prior if line.strip()}
    excluded = {"cache", ".cache", "__pycache__", "node_modules", ".venv", "venv",
                "torch_compile_cache", "weights", "checkpoints"}
    with backup.open("a") as receipt:
        for directory, children, files in os.walk(path, followlinks=False):
            children[:] = [name for name in children if name not in excluded
                           and not (Path(directory) / name).is_symlink()]
            for target in [Path(directory), *(Path(directory) / name for name in files)]:
                info = target.lstat()
                if stat.S_ISLNK(info.st_mode):
                    continue
                mode = stat.S_IMODE(info.st_mode)
                group_mode = 0o040 | (0o020 if writable else 0)
                if stat.S_ISDIR(info.st_mode) or mode & 0o111:
                    group_mode |= 0o010
                if writable and stat.S_ISDIR(info.st_mode):
                    group_mode |= stat.S_ISGID
                if str(target) not in completed:
                    receipt.write(json.dumps({
                        "path": str(target), "uid": info.st_uid, "gid": info.st_gid, "mode": mode,
                    }) + "\n")
                    receipt.flush()
                if info.st_gid == gid and mode & group_mode == group_mode:
                    continue
                os.chown(target, -1, gid, follow_symlinks=False)
                os.chmod(target, mode | group_mode)


def configure_profile(receipt):
    home = Path(receipt["home"])
    profile_state = home / ".claude/.claude.json"
    original = Path(receipt["source_project"]).parent.parent / ".claude.json"
    private_copy(original, Path(receipt["backup"]) / "claude-config-state.json")
    state = json.loads(original.read_text()) if original.exists() else {}
    state["hasCompletedOnboarding"] = True
    projects = state.setdefault("projects", {})
    for path in receipt["write_directories"]:
        projects.setdefault(path, {})["hasTrustDialogAccepted"] = True
    profile_state.write_text(json.dumps(state, indent=2) + "\n")
    profile_state.chmod(0o600)
    os.chown(profile_state, receipt["uid"], receipt["gid"])


def finish_prepared(receipt_file):
    receipt = json.loads(receipt_file.read_text())
    identity = pwd.getpwnam(receipt["user"])
    if identity.pw_uid != receipt["uid"] or identity.pw_gid != receipt["gid"]:
        raise SystemExit("Prepared user identity changed")
    backup = Path(receipt["backup"])
    source_project = Path(receipt["source_project"])
    target_project = Path(receipt["target_project"])
    if source_project.is_symlink():
        raise SystemExit("Session has already been migrated; no changes made")
    session_file = f"{receipt['session_id']}.jsonl"
    if hashlib.sha256((source_project / session_file).read_bytes()).digest() != hashlib.sha256(
        (target_project / session_file).read_bytes()
    ).digest():
        raise SystemExit("Session changed after preparation; refusing to lose new messages")
    # Only non-traversable CFS parents need a group change. Public ancestors stay unchanged.
    roots = list(map(Path, receipt["write_directories"] + receipt["read_directories"]))
    for parent in dict.fromkeys(parent for root in roots for parent in root.parents):
        info = parent.stat()
        if not info.st_mode & stat.S_IXOTH:
            with (backup / "parent-permissions.before.jsonl").open("a") as handle:
                handle.write(json.dumps({"path": str(parent), "uid": info.st_uid,
                                         "gid": info.st_gid,
                                         "mode": stat.S_IMODE(info.st_mode)}) + "\n")
            os.chown(parent, -1, identity.pw_gid)
            os.chmod(parent, stat.S_IMODE(info.st_mode) | stat.S_IXGRP)
    for writable, key in [(True, "write_directories"), (False, "read_directories")]:
        for index, path in enumerate(receipt[key]):
            print(f"Granting dedicated-group {'write' if writable else 'read'}: {path}", flush=True)
            grant_group_tree(Path(path), identity.pw_gid, writable,
                             backup / f"permissions-{key}-{index}.before.jsonl")
    home = Path(receipt["home"])
    for path in receipt["write_directories"]:
        subprocess.run(["git", "config", "--file", str(home / ".gitconfig"),
                        "--add", "safe.directory", path], check=True)
    os.chown(home / ".gitconfig", identity.pw_uid, identity.pw_gid)
    configure_profile(receipt)
    shutil.move(str(source_project), backup / "project-original")
    source_project.symlink_to(target_project, target_is_directory=True)
    print(json.dumps({"status": "prepared", "receipt": str(receipt_file),
                      "user": receipt["user"], "uid": identity.pw_uid, "home": str(home)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--user", required=True)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--source-home", type=Path, default=Path("/root"))
    parser.add_argument("--write-dir", action="append", type=Path, default=[])
    parser.add_argument("--read-dir", action="append", type=Path, default=[])
    parser.add_argument("--command", required=True, type=Path)
    parser.add_argument("--finish-prepared", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Administrator access is required")
    if args.finish_prepared:
        finish_prepared(args.finish_prepared)
        return
    if not args.home.is_absolute() or args.home.exists():
        raise SystemExit("Use a new absolute home directory")
    try:
        pwd.getpwnam(args.user)
    except KeyError:
        pass
    else:
        raise SystemExit("Linux user already exists; it will not be reused")
    for path in [args.cwd, *args.write_dir, *args.read_dir]:
        if not path.is_absolute() or not path.is_dir() or path.resolve() in {
            Path("/"), Path("/root"), Path("/mnt"), Path("/workspace"), args.source_home,
        }:
            raise SystemExit("Workspace grants require specific existing directories")
    if not shutil.which("setfacl") or not shutil.which("getfacl"):
        raise SystemExit("Install the acl package first")
    source_config = args.source_home / ".claude"
    matches = list((source_config / "projects").glob(f"*/{args.session_id}.jsonl"))
    if len(matches) != 1 or matches[0].is_symlink():
        raise SystemExit("Expected exactly one unmigrated session JSONL")
    source_project = matches[0].parent
    other_sessions = [p for p in source_project.glob("*.jsonl") if p != matches[0]]
    if other_sessions:
        raise SystemExit("Project contains other sessions; migrate them separately")
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = args.source_home / ".local/state/research-ops/claude-migrations" / timestamp
    backup.mkdir(mode=0o700, parents=True)
    for source in [source_config / ".credentials.json", source_config / "settings.json",
                   args.source_home / ".claude.json", args.source_home / ".local/bin/claude-dmit"]:
        private_copy(source, backup / source.name)
    subprocess.run([
        "useradd", "--user-group", "--no-create-home", "--home-dir", str(args.home),
        "--shell", "/usr/sbin/nologin", "--password", "!", args.user,
    ], check=True)
    identity = pwd.getpwnam(args.user)
    args.home.mkdir(mode=0o700, parents=True)
    for name in [".claude", ".cache", "tmp", "bin", ".ltp", ".codex"]:
        (args.home / name).mkdir(mode=0o700)
    private_copy(source_config / ".credentials.json", args.home / ".claude/.credentials.json")
    private_copy(source_config / "settings.json", args.home / ".claude/settings.json")
    private_copy(args.source_home / ".claude.json", args.home / ".claude.json")
    private_copy(args.source_home / ".ltp/config.yaml", args.home / ".ltp/config.yaml")
    target_project = args.home / ".claude/projects" / source_project.name
    shutil.copytree(source_project, target_project, symlinks=True)
    for name in [".claude/skills", ".codex/skills"]:
        source = args.source_home / name
        if source.is_dir():
            (args.home / name).symlink_to(source, target_is_directory=True)
    for directory, children, files in os.walk(args.home, followlinks=False):
        os.chown(directory, identity.pw_uid, identity.pw_gid)
        for name in children + files:
            os.chown(
                Path(directory) / name, identity.pw_uid, identity.pw_gid, follow_symlinks=False
            )
    writes = list(dict.fromkeys([args.cwd, *args.write_dir]))
    reads = list(dict.fromkeys(args.read_dir))
    traverse = [args.source_home, source_config / "projects"]
    receipt = {
        "version": 1, "session_id": args.session_id, "user": args.user,
        "uid": identity.pw_uid, "gid": identity.pw_gid, "home": str(args.home),
        "config_dir": str(args.home / ".claude"), "cwd": str(args.cwd),
        "command": str(args.command.resolve()), "permission_mode": "bypassPermissions",
        "backup": str(backup), "source_project": str(source_project),
        "target_project": str(target_project),
        "write_directories": list(map(str, writes)), "read_directories": list(map(str, reads)),
    }
    receipt_file = backup / "migration.json"
    receipt_file.write_text(json.dumps(receipt, indent=2) + "\n")
    receipt_file.chmod(0o600)
    with (backup / "directory-acls.before").open("w") as handle:
        subprocess.run(["getfacl", "-p", *map(str, dict.fromkeys(traverse + writes + reads))],
                       stdout=handle, check=True)
    for path in traverse:
        if not os.access(path, os.X_OK):
            raise RuntimeError(f"Administrator cannot traverse {path}")
        subprocess.run(["setfacl", "-m", f"u:{args.user}:--x", str(path)], check=True)
    finish_prepared(receipt_file)


if __name__ == "__main__":
    main()

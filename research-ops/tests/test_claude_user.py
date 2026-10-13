import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from research_ops.codex import claude_user


@pytest.mark.parametrize(
    "args",
    [
        ["--resume", "thread"],
        ["-r", "thread"],
        ["--resume=thread"],
        ["--session-id", "thread"],
        ["--session-id=thread"],
    ],
)
def test_session_selection(args):
    assert claude_user.session_argument(args) == "thread"
    assert claude_user.session_argument(["--version"]) is None


def test_conflicting_sessions_fail_closed():
    with pytest.raises(ValueError, match="Conflicting"):
        claude_user.session_argument(["--resume", "one", "--session-id", "two"])


def test_resume_picker_does_not_treat_an_option_as_session_id():
    assert claude_user.session_argument(["--resume"]) is None
    assert claude_user.session_argument(["--resume", "--model", "opus"]) is None


def test_shared_workspace_umask_is_explicit_and_never_world_readable():
    assert claude_user.workspace_umask({}) == 0o077
    assert claude_user.workspace_umask({"workspace_umask": "0007"}) == 0o007
    with pytest.raises(ValueError):
        claude_user.workspace_umask({"workspace_umask": "0000"})


@pytest.mark.skipif(os.geteuid() != 0, reason="Verifies real filesystem ownership")
def test_artifact_grants_are_bounded_read_only_and_do_not_follow_links(tmp_path):
    root = tmp_path / "artifacts" / "probe"
    root.mkdir(parents=True)
    result = root / "acceptance.json"
    result.write_text("{}")
    result.chmod(0o600)
    outside = tmp_path / "credentials.json"
    outside.write_text("private")
    outside.chmod(0o600)
    (root / "escape.json").symlink_to(outside)
    (root / "hardlink.json").hardlink_to(outside)
    (root / "escape-dir").symlink_to(tmp_path, target_is_directory=True)
    cache = root / "cache"
    cache.mkdir()
    (cache / "private.json").write_text("{}")
    (cache / "private.json").chmod(0o600)
    (root / "model.safetensors").write_text("not copied")
    (root / "model.safetensors").chmod(0o600)
    identity = pwd.getpwnam("nobody")
    profile = {"uid": identity.pw_uid, "gid": identity.pw_gid, "artifact_read_roots": [str(root)]}
    receipt = io.StringIO()
    summary = claude_user.repair_artifact_access(profile, receipt)
    assert summary["changed"] == 2
    assert result.stat().st_gid == identity.pw_gid
    assert result.stat().st_mode & 0o777 == 0o640
    assert outside.stat().st_mode & 0o777 == 0o600
    assert outside.stat().st_gid == 0
    assert (cache / "private.json").stat().st_mode & 0o777 == 0o600
    assert (root / "model.safetensors").stat().st_mode & 0o777 == 0o600
    assert len(receipt.getvalue().splitlines()) == 2
    assert claude_user.repair_artifact_access(profile, receipt)["changed"] == 0
    assert claude_user.repair_artifact_access(profile, receipt, max_entries=1)["limited"] > 0
    profile["artifact_read_roots"] = [str(tmp_path)]
    with pytest.raises(ValueError, match="below artifacts"):
        claude_user.repair_artifact_access(profile, receipt)
    profile["artifact_read_roots"] = [str(root / "escape-dir")]
    with pytest.raises(OSError):
        claude_user.repair_artifact_access(profile, receipt)


def test_migrated_permission_mode_replaces_old_flags_without_changing_query():
    prompt = "  Original query, including --permission-mode text.\n"
    args = ["--permission-mode", "acceptEdits", "--model", "opus", "-p", prompt]
    updated = claude_user.permission_arguments(args, "bypassPermissions")
    assert updated == ["--permission-mode", "bypassPermissions", "--model", "opus", "-p", prompt]
    assert claude_user.permission_arguments(
        ["--permission-mode=plan", "--dangerously-skip-permissions", "--resume", "thread"],
        "bypassPermissions",
    ) == ["--permission-mode", "bypassPermissions", "--resume", "thread"]


def test_profile_environment_keeps_proxy_but_not_root_identity_or_api_credentials():
    profile = {"home": "/srv/claude/user", "config_dir": "/srv/claude/user/.claude", "user": "user"}
    environment = claude_user.profile_environment(
        profile,
        {
            "HOME": "/root",
            "CLAUDE_CONFIG_DIR": "/root/.claude",
            "PATH": "/root/bin",
            "ANTHROPIC_API_KEY": "do-not-inherit",
            "OPENAI_API_KEY": "do-not-inherit",
            "LTP_TOKEN": "do-not-inherit",
            "BASH_ENV": "/root/unsafe",
            "https_proxy": "http://127.0.0.1:1080",
            "no_proxy": "localhost,127.0.0.1",
        },
    )
    assert environment["HOME"] == "/srv/claude/user"
    assert environment["CLAUDE_CONFIG_DIR"] == profile["config_dir"]
    assert environment["https_proxy"] == "http://127.0.0.1:1080"
    assert environment["no_proxy"] == "localhost,127.0.0.1"
    assert not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LTP_TOKEN", "BASH_ENV"} & environment.keys()
    assert "/root" not in environment["PATH"]
    assert environment["PATH"].split(":")[0] == "/srv/claude/user/.local/bin"


def test_native_entrypoint_reuses_binary_and_is_idempotent(tmp_path):
    home = tmp_path / "home"
    command = tmp_path / "shared-claude"
    command.touch()
    command.chmod(0o755)
    claude_user.prepare_native_entrypoint(home, command)
    entry = home / ".local/bin/claude"
    assert entry.is_symlink()
    assert entry.resolve() == command
    assert os.access(entry, os.X_OK)
    claude_user.prepare_native_entrypoint(home, command)
    assert entry.resolve() == command


def test_native_entrypoint_preserves_existing_user_installation(tmp_path):
    directory = tmp_path / ".local/bin"
    directory.mkdir(parents=True)
    entry = directory / "claude"
    entry.write_text("existing user installation")
    claude_user.prepare_native_entrypoint(tmp_path, Path("/shared/claude"))
    assert entry.read_text() == "existing user installation"
    entry.unlink()
    entry.symlink_to("/user-managed/previous-version")
    claude_user.prepare_native_entrypoint(tmp_path, Path("/shared/claude"))
    assert entry.readlink() == Path("/user-managed/previous-version")


def test_unmapped_session_preserves_original_invocation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "execv", lambda *args: calls.append(args))
    args = ["--resume", "unmapped", "--permission-mode", "acceptEdits"]
    claude_user.launch(tmp_path / "missing.json", "/original/claude", args)
    assert calls == [("/original/claude", ["/original/claude", *args])]


def test_private_registry_and_exact_session_scope(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("Registry is intentionally owned by the administrator")
    registry = tmp_path / "users.json"
    registry.write_text(json.dumps({
        "version": 1,
        "sessions": {"thread": {"user": "alice"}},
        "interactive_default": {"user": "bob"},
    }))
    registry.chmod(0o600)
    assert claude_user.load_profile(registry, "thread") == {"user": "alice"}
    assert claude_user.load_profile(registry, "other") is None
    assert claude_user.load_profile(registry, None) == {"user": "bob"}
    registry.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        claude_user.load_profile(registry, "thread")
    with pytest.raises(ValueError, match="0600"):
        claude_user.load_profile(registry, None)


@pytest.mark.parametrize("args", [
    ["--print", "hello"],
    ["-p", "--session-id", "new-web-session", "hello"],
    ["--output-format=stream-json"],
    ["--input-format", "stream-json"],
])
def test_interactive_default_never_changes_unmapped_managed_turns(args, monkeypatch):
    calls = []
    lookups = []

    def profile(_registry, session_id):
        lookups.append(session_id)
        return {"user": "should-not-be-selected"} if session_id is None else None

    monkeypatch.setattr(claude_user, "load_profile", profile)
    monkeypatch.setattr(os, "execv", lambda *values: calls.append(values))
    claude_user.launch(None, "/original/claude", args)
    assert None not in lookups
    assert calls == [("/original/claude", ["/original/claude", *args])]


def test_profile_cannot_launch_as_root(monkeypatch):
    monkeypatch.setattr(claude_user, "load_profile", lambda *args: {"user": "root"})
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_user.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=0))
    with pytest.raises(ValueError, match="root"):
        claude_user.launch(None, "/original/claude", ["--resume", "thread"])


@pytest.mark.skipif(os.geteuid() != 0, reason="Verifies a real privilege drop")
@pytest.mark.parametrize("invocation", ["managed", "new", "resume", "status"])
def test_launcher_really_drops_uid_and_keeps_credentials_out_of_child_environment(invocation):
    identity = pwd.getpwnam("nobody")
    directory = Path(tempfile.mkdtemp(prefix="rops-claude-user-"))
    try:
        directory.chmod(0o755)
        home = directory / "home"
        home.mkdir(mode=0o700)
        os.chown(home, identity.pw_uid, identity.pw_gid)
        command = directory / "claude"
        command.write_text(
            "#!/usr/bin/python3\nimport json,os,sys\n"
            "print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'home':os.environ['HOME'],"
            "'groups':os.getgroups(),'args':sys.argv[1:],'cwd':os.getcwd(),"
            "'proxy':os.environ.get('https_proxy'),"
            "'native_entrypoint':os.path.realpath(os.environ['HOME']+'/.local/bin/claude'),"
            "'api_key_inherited':'ANTHROPIC_API_KEY' in os.environ}))\n"
        )
        command.chmod(0o755)
        registry = directory / "registry.json"
        profile = {
            "user": identity.pw_name,
            "uid": identity.pw_uid,
            "gid": identity.pw_gid,
            "home": str(home),
            "config_dir": str(home / ".claude"),
            "cwd": str(home),
            "command": str(command),
            "permission_mode": "bypassPermissions",
        }
        registry.write_text(json.dumps({
            "version": 1, "sessions": {"thread": profile}, "interactive_default": profile,
        }))
        registry.chmod(0o600)
        source = directory / "launcher.py"
        source.write_text(
            "import sys\nfrom types import SimpleNamespace\n"
            f"sys.path.insert(0, {str(Path(claude_user.__file__).parents[2])!r})\n"
            "from research_ops.codex import claude_user\n"
            "original=claude_user.pwd.getpwnam\n"
            "def account(name):\n"
            "    value=original(name)\n"
            "    return SimpleNamespace(pw_name=value.pw_name, pw_uid=value.pw_uid, "
            f"pw_gid=value.pw_gid, pw_dir={str(home)!r})\n"
            "claude_user.pwd.getpwnam=account\nclaude_user.main()\n"
        )
        args = {
            "managed": ["--resume", "thread", "--permission-mode", "acceptEdits"],
            "new": ["--model", "opus", "--permission-mode", "plan"],
            "resume": ["--resume", "a-personal-session"],
            "status": ["auth", "status", "--json"],
        }[invocation]
        result = subprocess.run(
            [
                sys.executable,
                str(source),
                "--registry",
                str(registry),
                "--fallback",
                "/unused",
                "--",
                *args,
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=directory,
            env={**os.environ, "ANTHROPIC_API_KEY": "must-not-inherit",
                 "https_proxy": "http://127.0.0.1:1080"},
        )
        child = json.loads(result.stdout)
        assert child["uid"] == identity.pw_uid != 0
        assert child["gid"] == identity.pw_gid
        assert child["groups"] == []
        assert child["home"] == str(home)
        assert child["args"] == (
            ["--permission-mode", "bypassPermissions", "--resume", "thread"]
            if invocation == "managed" else args
        )
        assert child["cwd"] == str(home if invocation == "managed" else directory)
        assert child["proxy"] == "http://127.0.0.1:1080"
        entry = home / ".local/bin/claude"
        assert child["native_entrypoint"] == str(command)
        assert entry.lstat().st_uid == identity.pw_uid
        assert child["api_key_inherited"] is False
    finally:
        shutil.rmtree(directory)

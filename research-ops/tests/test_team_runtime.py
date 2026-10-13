from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from research_ops.team import runtime
from research_ops.team.runtime import provision, stop_worker
from research_ops.team.store import TeamStore


def test_worker_refuses_live_owner_release(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("root-only provisioning validation")
    root = tmp_path / "team"
    TeamStore(root).add_account("alice", "Alice")
    source = tmp_path / "live-owner-release"
    source.mkdir()
    (source / "backend/research_ops").mkdir(parents=True)
    (source / "backend/research_ops/team").mkdir()
    (source / "backend/research_ops/team/runtime.py").write_text("# Synthetic release fixture\n")
    (source / ".env").write_text("EXAMPLE_LOCAL_SETTING=1\n")
    with pytest.raises(ValueError, match="clean shared release"):
        provision(root, "alice", source, [])


def test_clean_environment_and_landlock_in_real_child(tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux Landlock test")
    pytest.importorskip("py_landlock")
    # Files are deliberately world-readable: denial must come from the policy,
    # not incidental test directory permissions.
    home = tmp_path / "home"
    release = tmp_path / "release"
    shared = tmp_path / "shared"
    foreign = tmp_path / "other-user"
    for directory in (home, release, shared, foreign):
        directory.mkdir()
    secret = foreign / "credentials.txt"
    secret.write_text("DO_NOT_READ")
    (shared / "dataset.txt").write_text("shared sample")
    (home / "escape").symlink_to(secret)
    code = """
import json, os, pathlib, subprocess, sys
from research_ops.team.runtime import apply_filesystem_policy
home, release, shared, foreign = map(pathlib.Path, sys.argv[1:])
apply_filesystem_policy(home, release, [shared])
(home / 'own.txt').write_text('ok')
assert (shared / 'dataset.txt').read_text() == 'shared sample'
denied = []
for path in [foreign / 'credentials.txt', home / 'escape',
             pathlib.Path('/proc') / str(os.getppid()) / 'cmdline']:
    try:
        path.read_text()
    except PermissionError:
        denied.append(str(path))
    else:
        raise AssertionError('foreign read was allowed')
try:
    (shared / 'new.txt').write_text('bad')
except PermissionError:
    pass
else:
    raise AssertionError('shared write was allowed')
result = subprocess.run(['/bin/cat', str(foreign / 'credentials.txt')], capture_output=True)
assert result.returncode != 0
print(json.dumps({'own_write': True, 'shared_read': True,
                  'foreign_denied': len(denied), 'child_denied': True}))
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "backend"))
    result = subprocess.run(
        [sys.executable, "-c", code, str(home), str(release), str(shared), str(foreign)],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["foreign_denied"] == 3


def test_team_settings_do_not_load_owner_env(tmp_path):
    config = tmp_path / "settings.json"
    config.write_text(
        json.dumps(
            {
                "load_project_env": False,
                "data_dir": str(tmp_path / "data"),
                "workspace_root": str(tmp_path / "work"),
                "cluster_dashboard_enabled": False,
            }
        )
    )
    code = """
from research_ops.settings import get_settings
s = get_settings()
assert s.oneapi_api_key is None
assert not s.infoflow_notifications_enabled
assert not s.load_project_env
assert str(s.codex_home).startswith(__import__('os').environ['HOME'])
print('isolated')
"""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "ROPS_TEAM_SETTINGS": str(config),
        "PYTHONPATH": str(Path(__file__).parents[1] / "backend"),
    }
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, text=True, capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated"


def test_stop_does_not_signal_an_unrelated_process_with_same_uid(tmp_path):
    if not hasattr(ctypes.CDLL(None), "pidfd_open"):
        pytest.skip("Linux pidfd test")
    control = tmp_path / "control"
    control.mkdir()
    process = subprocess.Popen(["/bin/sleep", "30"])
    try:
        (control / "alice.pid").write_text(str(process.pid))
        (control / "alice.json").write_text(json.dumps({"uid": os.getuid()}))
        with pytest.raises(ValueError, match="not this Research Ops worker"):
            stop_worker(tmp_path, "alice")
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=3)


@pytest.fixture
def mocked_provision(tmp_path, monkeypatch):
    root = tmp_path / "team"
    release = tmp_path / "release"
    release.mkdir()
    store = TeamStore(root)
    store.add_account("alice", "Alice")
    users = {}
    calls = []

    def lookup(name):
        if name not in users:
            raise KeyError(name)
        return users[name]

    def useradd(args, **kwargs):
        calls.append(args)
        assert args[0] == "/usr/sbin/useradd"
        assert kwargs["timeout"] == 30
        users[args[-1]] = SimpleNamespace(
            pw_uid=12345,
            pw_gid=12345,
            pw_dir=str(root / "users/alice"),
            pw_shell="/usr/sbin/nologin",
        )

    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runtime.os, "chown", lambda *args: None)
    monkeypatch.setattr(runtime, "validate_release", lambda path: path.resolve())
    monkeypatch.setattr(runtime.pwd, "getpwnam", lookup)
    monkeypatch.setattr(runtime.subprocess, "run", useradd)
    return root, release, users, calls


def test_provision_reuses_only_owned_workspace(mocked_provision):
    root, release, _, calls = mocked_provision
    home = provision(root, "alice", release, [])
    settings = home / "config/settings.json"
    value = json.loads(settings.read_text())
    assert value["load_project_env"] is False
    assert value["infoflow_notifications_enabled"] is False
    (home / "work/keep.txt").write_text("own files")
    before = settings.read_bytes()
    assert provision(root, "alice", release, []) == home
    assert len(calls) == 1
    assert (home / "work/keep.txt").read_text() == "own files"
    assert settings.read_bytes() == before
    assert (root / "control/alice.json").stat().st_mode & 0o777 == 0o600


def test_partial_provision_resumes_without_replacing_files(mocked_provision, monkeypatch):
    root, release, _, calls = mocked_provision
    original = runtime._private_json

    def fail_config(path, value):
        if path.name == "settings.json":
            raise OSError("simulated disk failure")
        original(path, value)

    monkeypatch.setattr(runtime, "_private_json", fail_config)
    with pytest.raises(OSError, match="disk failure"):
        provision(root, "alice", release, [])
    home = root / "users/alice"
    (home / "work/keep.txt").write_text("do not replace")
    monkeypatch.setattr(runtime, "_private_json", original)
    assert provision(root, "alice", release, []) == home
    assert len(calls) == 1
    assert (home / "work/keep.txt").read_text() == "do not replace"


def test_provision_refuses_unrelated_home(mocked_provision):
    root, release, _, calls = mocked_provision
    home = root / "users/alice"
    home.mkdir(parents=True)
    with pytest.raises(ValueError, match="already exists"):
        provision(root, "alice", release, [])
    assert not calls


def test_provision_refuses_unrelated_linux_user(mocked_provision):
    root, release, users, calls = mocked_provision
    users["rops_alice"] = SimpleNamespace(pw_uid=45678)
    with pytest.raises(ValueError, match="unrelated identity"):
        provision(root, "alice", release, [])
    assert not calls


def test_provision_detects_identity_change(mocked_provision):
    root, release, users, _ = mocked_provision
    provision(root, "alice", release, [])
    users["rops_alice"].pw_uid = 67890
    with pytest.raises(ValueError, match="does not match"):
        provision(root, "alice", release, [])

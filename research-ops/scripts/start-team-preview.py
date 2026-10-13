"""Start a local-only acceptance environment with an actual non-root worker.

No owner database, model credentials or Agent requests are used. The activation
link is written to a private file rather than command output.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from research_ops.team.runtime import provision, start_worker
from research_ops.team.store import TeamStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path("/srv/rops-team-preview"))
    parser.add_argument("--port", type=int, default=8030)
    args = parser.parse_args()
    root = args.root.resolve()
    store = TeamStore(root)
    username = "preview_" + secrets.token_hex(3)
    store.add_account(username, "验收管理员", admin=True)
    home = provision(root, username, args.release, [])
    settings_file = home / "config/settings.json"
    values = json.loads(settings_file.read_text())
    for key in (
        "codex_enabled",
        "claude_enabled",
        "codex_result_sync_enabled",
        "codex_local_live_enabled",
        "codex_registry_enabled",
        "native_goal_supervision_enabled",
    ):
        values[key] = False
    settings_file.write_text(json.dumps(values, indent=2) + "\n")
    worker_pid = start_worker(root, username)
    token = store.invitation(username, "cli")
    origin = f"http://127.0.0.1:{args.port}"
    preview = root / "control/preview.json"
    preview.write_text(
        json.dumps(
            {"username": username, "worker_pid": worker_pid, "url": f"{origin}/#invite={token}"},
            indent=2,
        )
        + "\n"
    )
    preview.chmod(0o600)
    with (root / "control/gateway.log").open("ab") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "research_ops.team.server",
                "--root",
                str(root),
                "--frontend",
                str(args.release / "web/dist"),
                "--release",
                str(args.release),
                "--origin",
                origin,
                "--port",
                str(args.port),
            ],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "backend"),
            },
        )
    (root / "control/gateway.pid").write_text(str(proc.pid))
    os.chmod(root / "control/gateway.log", 0o600)
    print(f"Preview: {origin}; worker PID {worker_pid}; gateway PID {proc.pid}")
    print(f"Private activation receipt: {preview}")


if __name__ == "__main__":
    main()

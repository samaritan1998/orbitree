from __future__ import annotations

import fcntl
import os
from pathlib import Path

import uvicorn


def main() -> None:
    if os.geteuid() == 0:
        raise RuntimeError("New team workers must not run as root")
    home = Path.home()
    with (home / "run/worker.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from research_ops.api import create_app
        from research_ops.settings import get_settings

        settings = get_settings()
        uvicorn.run(
            create_app(settings),
            uds=str(home / "run/api.sock"),
            proxy_headers=False,
            access_log=False,
        )


if __name__ == "__main__":
    main()

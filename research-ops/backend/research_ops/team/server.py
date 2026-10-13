import argparse
from pathlib import Path

import uvicorn

from .gateway import TeamConfig, create_team_app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--frontend", type=Path, required=True)
    parser.add_argument(
        "--release", type=Path, help="Clean shared release for Web-created accounts"
    )
    parser.add_argument("--origin", required=True)
    parser.add_argument("--port", type=int, default=8030)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    uvicorn.run(
        create_team_app(TeamConfig(args.root, args.frontend, args.origin, args.release)),
        host=args.host,
        port=args.port,
        proxy_headers=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()

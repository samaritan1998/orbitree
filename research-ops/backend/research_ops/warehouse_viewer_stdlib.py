from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

_SERVICE_PATH = Path(__file__).with_name("services") / "warehouse_trajectories.py"
_SERVICE_SPEC = importlib.util.spec_from_file_location(
    "warehouse_trajectory_service", _SERVICE_PATH
)
if _SERVICE_SPEC is None or _SERVICE_SPEC.loader is None:
    raise RuntimeError(f"Cannot load {_SERVICE_PATH}")
_SERVICE_MODULE = importlib.util.module_from_spec(_SERVICE_SPEC)
sys.modules[_SERVICE_SPEC.name] = _SERVICE_MODULE
_SERVICE_SPEC.loader.exec_module(_SERVICE_MODULE)
DEFAULT_WAREHOUSE_ROOT = _SERVICE_MODULE.DEFAULT_WAREHOUSE_ROOT
WarehouseTrajectoryError = _SERVICE_MODULE.WarehouseTrajectoryError
WarehouseTrajectoryService = _SERVICE_MODULE.WarehouseTrajectoryService

STATIC_ROOT = Path(
    os.environ.get(
        "WAREHOUSE_VIEWER_STATIC_ROOT",
        Path(__file__).resolve().parents[2] / "web" / "warehouse-viewer",
    )
)
WAREHOUSE_ROOT = Path(os.environ.get("WAREHOUSE_ROOT", str(DEFAULT_WAREHOUSE_ROOT)))
SERVICE = WarehouseTrajectoryService(WAREHOUSE_ROOT)
SAMPLE_PATH = re.compile(r"^/api/sessions/([A-Za-z0-9]+) /samples/([A-Za-z0-9]+)$".replace(" ", ""))


class Handler(BaseHTTPRequestHandler):
    server_version = "WarehouseTrajectoryViewer/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._json({"status": "ok", "warehouse_root": str(SERVICE.warehouse_root)})
            return
        match = SAMPLE_PATH.match(parsed.path)
        if match:
            try:
                payload = SERVICE.get_sample(match.group(1), match.group(2))
            except WarehouseTrajectoryError as exc:
                self._json({"detail": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            self._json(payload)
            return
        if parsed.path in {"/", "/index.html"}:
            self._file(STATIC_ROOT / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/static/"):
            filename = unquote(parsed.path.removeprefix("/static/"))
            if "/" in filename or filename in {"", ".", ".."}:
                self._json({"detail": "invalid static path"}, HTTPStatus.BAD_REQUEST)
                return
            content_type = {
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
            }.get(Path(filename).suffix, "application/octet-stream")
            self._file(STATIC_ROOT / filename, content_type)
            return
        self._json({"detail": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/samples":
            self._json({"detail": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64 * 1024:
                raise WarehouseTrajectoryError("请求体大小无效")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise WarehouseTrajectoryError("请求体必须是 JSON 对象")
            dataset = str(payload.get("dataset") or "").strip()
            batch = str(payload.get("batch") or "").strip()
            per_batch = int(payload.get("per_batch", 10))
            seed = int(payload.get("seed", 42))
            if bool(dataset) == bool(batch):
                raise WarehouseTrajectoryError("dataset 和 batch 必须且只能提供一个")
            sampler = SERVICE.sample_dataset if dataset else SERVICE.sample_batch
            result = sampler(dataset or batch, per_batch=per_batch, seed=seed)
        except (
            ValueError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            WarehouseTrajectoryError,
        ) as exc:
            self._json({"detail": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._json(result)

    def _headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._headers()
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self._json({"detail": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._headers()
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    host = os.environ.get("WAREHOUSE_VIEWER_HOST", "0.0.0.0")
    port = int(os.environ.get("WAREHOUSE_VIEWER_PORT", "8315"))
    print(f"Warehouse Trajectory Viewer listening on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()

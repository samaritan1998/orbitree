#!/usr/bin/env python3
"""Serve the PPT trajectory canary viewer and lazy row payloads."""

from __future__ import annotations

import argparse
import json
import mimetypes
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class ViewerState:
    def __init__(self, data_root: Path, static_root: Path, run_root: Path) -> None:
        self.data_root = data_root.resolve()
        self.static_root = static_root.resolve()
        self.run_root = run_root.resolve()
        self.data_path = self.data_root / "data.jsonl"
        self.rows = list(self._read_jsonl(self.data_root / "rows_preview.jsonl"))
        self.index = {
            str(row["trajectory_id"]): row
            for row in self._read_jsonl(self.data_root / "row_index.jsonl")
        }
        self.validation = self._read_json(self.data_root / "validation_report.json")
        self.batch = self._read_json(self.data_root / "batch.json")
        self.schema = self._read_json(self.data_root / "schema_registry.json")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _read_jsonl(path: Path):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)

    def meta(self) -> dict[str, Any]:
        return {
            "batch": self.batch,
            "validation": self.validation,
            "schema": {
                "schema_version": self.schema.get("schema_version"),
                "producer_version": self.schema.get("producer_version"),
                "table_key": self.schema.get("table_key"),
                "display_name": self.schema.get("display_name"),
                "unique_key": self.schema.get("unique_key"),
                "field_count": len(self.schema.get("fields") or []),
                "fields": self.schema.get("fields") or [],
            },
        }

    def row_bytes(self, trajectory_id: str) -> bytes | None:
        entry = self.index.get(trajectory_id)
        if entry is None:
            return None
        with self.data_path.open("rb") as handle:
            handle.seek(int(entry["offset"]))
            raw = handle.read(int(entry["length"]))
        return raw.rstrip(b"\r\n")

    def allowed_media(self, raw_path: str) -> Path | None:
        try:
            path = Path(raw_path).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not path.is_file():
            return None
        for root in (self.data_root, self.run_root):
            try:
                path.relative_to(root)
                return path
            except ValueError:
                continue
        return None


class ViewerHandler(SimpleHTTPRequestHandler):
    state: ViewerState

    def log_message(self, format_string: str, *args: Any) -> None:
        if self.path == "/healthz":
            return
        super().log_message(format_string, *args)

    def send_bytes(
        self,
        payload: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
        status: int = 200,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, value: Any, status: int = 200) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_bytes(payload, "application/json; charset=utf-8", status=status)

    def send_file(self, path: Path, *, immutable: bool = False) -> None:
        try:
            payload = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        cache = "public, max-age=31536000, immutable" if immutable else "no-cache"
        self.send_bytes(payload, content_type, cache_control=cache)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            self.send_json({"status": "ok", "rows": len(self.state.rows)})
            return
        if path == "/api/meta":
            self.send_json(self.state.meta())
            return
        if path == "/api/rows":
            self.send_json(self.state.rows)
            return
        if path.startswith("/api/row/"):
            trajectory_id = urllib.parse.unquote(path.removeprefix("/api/row/"))
            payload = self.state.row_bytes(trajectory_id)
            if payload is None:
                self.send_json({"error": "row_not_found"}, status=404)
                return
            self.send_bytes(payload, "application/json; charset=utf-8")
            return
        if path.startswith("/assets/images/"):
            filename = Path(urllib.parse.unquote(path)).name
            if filename != urllib.parse.unquote(path).split("/")[-1]:
                self.send_error(404)
                return
            self.send_file(self.state.data_root / "assets" / "images" / filename, immutable=True)
            return
        if path == "/media":
            query = urllib.parse.parse_qs(parsed.query)
            requested = query.get("path", [""])[0]
            media = self.state.allowed_media(requested)
            if media is None:
                self.send_error(404)
                return
            self.send_file(media, immutable=True)
            return
        relative = path.lstrip("/") or "index.html"
        static_path = (self.state.static_root / relative).resolve()
        try:
            static_path.relative_to(self.state.static_root)
        except ValueError:
            self.send_error(404)
            return
        if static_path.is_dir():
            static_path = static_path / "index.html"
        if not static_path.is_file() and "." not in Path(relative).name:
            static_path = self.state.static_root / "index.html"
        self.send_file(static_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--static-root", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8878)
    args = parser.parse_args()
    ViewerHandler.state = ViewerState(args.data_root, args.static_root, args.run_root)
    server = ThreadingHTTPServer((args.bind, args.port), ViewerHandler)
    server.daemon_threads = True
    print(
        f"PPT trajectory canary viewer: http://{args.bind}:{args.port}/ "
        f"rows={len(ViewerHandler.state.rows)}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()

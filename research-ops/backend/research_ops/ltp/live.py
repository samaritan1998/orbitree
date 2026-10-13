from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


class SnapshotSourceError(RuntimeError):
    """Secret-safe failure from the read-only LTP snapshot boundary."""


@dataclass(frozen=True)
class SnapshotEnvelope:
    payload: dict[str, Any]
    source: Literal["live", "snapshot_file"]
    fetched_at: datetime
    refresh_error: str | None = None


class LtpControlLiveAdapter:
    """Run the existing LTP Control collector without importing its internals."""

    def __init__(
        self,
        *,
        enabled: bool,
        command: str,
        python_command: Path | None,
        source_root: Path | None,
        snapshot_path: Path | None,
        directory_user: str,
        allow_partial_users: bool,
        state_dir: Path,
        profiles_file: Path | None,
        query_timeout_seconds: int,
        process_timeout_seconds: float,
        max_response_bytes: int,
    ) -> None:
        if not command or any(character.isspace() for character in command):
            raise ValueError("ltp_control_command must be one executable path")
        if not directory_user or any(character.isspace() for character in directory_user):
            raise ValueError("ltp_control_directory_user must be one user name")
        self.enabled = enabled
        self.command = command
        self.python_command = python_command
        self.source_root = source_root
        self.snapshot_path = snapshot_path
        self.directory_user = directory_user
        self.allow_partial_users = allow_partial_users
        self.state_dir = state_dir
        self.profiles_file = profiles_file
        self.query_timeout_seconds = query_timeout_seconds
        self.process_timeout_seconds = process_timeout_seconds
        self.max_response_bytes = max_response_bytes

    def fetch(self) -> SnapshotEnvelope:
        live_error: str | None = None
        if self.enabled:
            try:
                return SnapshotEnvelope(
                    payload=self._query_live(),
                    source="live",
                    fetched_at=datetime.now(UTC),
                )
            except SnapshotSourceError:
                live_error = "实时查询失败，已尝试读取最近的只读快照。"

        if self.snapshot_path is not None:
            return SnapshotEnvelope(
                payload=self._read_snapshot_file(self.snapshot_path),
                source="snapshot_file",
                fetched_at=datetime.now(UTC),
                refresh_error=live_error,
            )

        if not self.enabled:
            raise SnapshotSourceError("集群大盘尚未配置实时数据源。")
        raise SnapshotSourceError("集群实时查询失败，且没有可用的最近快照。")

    def _query_live(self) -> dict[str, Any]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        if self.source_root is not None:
            argv = [str(self.python_command or sys.executable), "-m", "ltp_control"]
            current = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (
                f"{self.source_root}{os.pathsep}{current}" if current else str(self.source_root)
            )
            cwd = self.source_root
        else:
            argv = [self.command]
            cwd = None
        argv.extend(
            [
                "live",
                "--directory-user",
                self.directory_user,
                "--state-dir",
                str(self.state_dir),
                "--timeout-seconds",
                str(self.query_timeout_seconds),
                "--json",
            ]
        )
        if self.profiles_file is not None:
            argv.extend(["--profiles", str(self.profiles_file)])
        if self.allow_partial_users:
            argv.append("--allow-partial-users")
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=environment,
                capture_output=True,
                check=False,
                timeout=self.process_timeout_seconds,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise SnapshotSourceError("LTP Control 实时查询不可用。") from exc
        if completed.returncode != 0:
            raise SnapshotSourceError("LTP Control 实时查询返回失败。")
        if len(completed.stdout) > self.max_response_bytes:
            raise SnapshotSourceError("LTP Control 实时快照超过大小上限。")
        return self._decode_payload(completed.stdout)

    def _read_snapshot_file(self, path: Path) -> dict[str, Any]:
        try:
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise SnapshotSourceError("配置的集群快照不是普通文件。")
            if metadata.st_size > self.max_response_bytes:
                raise SnapshotSourceError("配置的集群快照超过大小上限。")
            return self._decode_payload(path.read_bytes())
        except SnapshotSourceError:
            raise
        except OSError as exc:
            raise SnapshotSourceError("无法读取配置的集群快照。") from exc

    @staticmethod
    def _decode_payload(raw: bytes) -> dict[str, Any]:
        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite JSON constant: {value}")

        try:
            payload = json.loads(raw, parse_constant=reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise SnapshotSourceError("LTP Control 返回了无效快照。") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("jobs"), list)
            or not isinstance(payload.get("pools"), list)
        ):
            raise SnapshotSourceError("LTP Control 快照合同不受支持。")
        return payload

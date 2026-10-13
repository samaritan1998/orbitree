from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

DEFAULT_WAREHOUSE_ROOT = Path(
    "/data/trajectory-warehouse"
)
WAREHOUSE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
ALLOWED_WAREHOUSE_HOSTS = {"warehouse.example.com"}
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|authorization|bearer|cookie|password|secret|token)(?:$|_)",
    re.IGNORECASE,
)


class WarehouseTrajectoryError(ValueError):
    pass


@dataclass(frozen=True)
class CachedSample:
    sample_id: str
    batch_id: str
    source_line: int
    record: Any
    redacted_paths: tuple[str, ...]


@dataclass
class CachedSession:
    created_at: float
    samples: dict[str, CachedSample]


def _parse_warehouse_id(value: str, *, resource: str) -> str:
    raw = value.strip()
    if not raw:
        raise WarehouseTrajectoryError(f"请输入 Warehouse {resource} URL 或 {resource} ID")

    if "://" in raw:
        parsed = urlparse(raw)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in ALLOWED_WAREHOUSE_HOSTS
        ):
            raise WarehouseTrajectoryError(
                f"只支持 warehouse.example.com 的 {resource} URL"
            )
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        expected_paths = {"datasets"} if resource == "dataset" else {"batch", "batches"}
        if len(parts) != 2 or parts[0] not in expected_paths:
            example = "/datasets/<dataset_id>" if resource == "dataset" else "/batch/<batch_id>"
            raise WarehouseTrajectoryError(f"Warehouse URL 必须形如 {example}")
        object_id = parts[1]
    else:
        object_id = raw

    if len(object_id) > 255 or not WAREHOUSE_ID_PATTERN.fullmatch(object_id):
        raise WarehouseTrajectoryError(
            f"{resource} ID 只能包含字母、数字、点、下划线和连字符，且不超过 255 字符"
        )
    return object_id


def parse_dataset_id(value: str) -> str:
    return _parse_warehouse_id(value, resource="dataset")


def parse_batch_id(value: str) -> str:
    return _parse_warehouse_id(value, resource="batch")


def _redact(value: Any, path: str = "$") -> tuple[Any, list[str]]:
    redacted_paths: list[str] = []
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if SENSITIVE_KEY_PATTERN.search(str(key)):
                result[key] = "[REDACTED]"
                redacted_paths.append(child_path)
                continue
            cleaned, nested = _redact(child, child_path)
            result[key] = cleaned
            redacted_paths.extend(nested)
        return result, redacted_paths
    if isinstance(value, list):
        result_list: list[Any] = []
        for index, child in enumerate(value):
            cleaned, nested = _redact(child, f"{path}[{index}]")
            result_list.append(cleaned)
            redacted_paths.extend(nested)
        return result_list, redacted_paths
    return value, redacted_paths


def _messages(record: Any) -> list[dict[str, Any]]:
    if not isinstance(record, dict):
        return []
    messages = record.get("messages")
    if isinstance(messages, str):
        try:
            messages = json.loads(messages)
        except json.JSONDecodeError:
            messages = None
    if not isinstance(messages, list):
        original = record.get("original_sample")
        if isinstance(original, dict):
            messages = original.get("messages")
    return [message for message in messages or [] if isinstance(message, dict)]


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(
            _text(item.get("text", item)) if isinstance(item, dict) else _text(item)
            for item in value
        )
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)


def summarize_record(record: Any, source_line: int, byte_size: int) -> dict[str, Any]:
    messages = _messages(record)
    role_counts: dict[str, int] = {}
    reasoning_chars = 0
    trained_steps = 0
    skipped_steps = 0
    explicit_masks = 0
    tool_calls = 0
    preview = ""
    for message in messages:
        role = str(message.get("role") or "unknown")
        role_counts[role] = role_counts.get(role, 0) + 1
        reasoning_chars += len(_text(message.get("reasoning_content")))
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            tool_calls += len(calls)
        if "step_loss_mask" in message:
            explicit_masks += 1
            if message.get("step_loss_mask") in {1, "1"}:
                trained_steps += 1
            else:
                skipped_steps += 1
        if not preview and role == "user":
            preview = " ".join(_text(message.get("content")).split())[:180]
    if not preview and messages:
        preview = " ".join(_text(messages[-1].get("content")).split())[:180]

    top_level_keys = sorted(record) if isinstance(record, dict) else []
    identity = {}
    if isinstance(record, dict):
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        source_meta = (
            record.get("source_meta") if isinstance(record.get("source_meta"), dict) else {}
        )
        for key in ("id", "app_id", "conv_id", "version_id", "sample_index"):
            value = record.get(key)
            if value is None:
                value = metadata.get(key)
            if value is None:
                value = source_meta.get(key)
            if value is not None:
                identity[key] = value

    return {
        "source_line": source_line,
        "byte_size": byte_size,
        "message_count": len(messages),
        "role_counts": role_counts,
        "reasoning_chars": reasoning_chars,
        "tool_call_count": tool_calls,
        "explicit_mask_count": explicit_masks,
        "trained_steps": trained_steps,
        "skipped_steps": skipped_steps,
        "top_level_keys": top_level_keys,
        "identity": identity,
        "preview": preview or "(empty trajectory)",
    }


class WarehouseTrajectoryService:
    def __init__(
        self,
        warehouse_root: Path = DEFAULT_WAREHOUSE_ROOT,
        *,
        max_sessions: int = 4,
        session_ttl_seconds: int = 3600,
        max_line_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.warehouse_root = warehouse_root.resolve()
        self.datasets_root = (self.warehouse_root / "datasets").resolve()
        self.sft_root = (self.warehouse_root / "sft").resolve()
        self.max_sessions = max_sessions
        self.session_ttl_seconds = session_ttl_seconds
        self.max_line_bytes = max_line_bytes
        self._sessions: OrderedDict[str, CachedSession] = OrderedDict()
        self._lock = threading.RLock()

    def sample_dataset(self, value: str, *, per_batch: int = 10, seed: int = 42) -> dict[str, Any]:
        if not 1 <= per_batch <= 20:
            raise WarehouseTrajectoryError("每个 batch 的采样数必须在 1 到 20 之间")
        dataset_id = parse_dataset_id(value)
        dataset_dir = self._dataset_dir(dataset_id)
        metadata = self._read_metadata(dataset_dir)
        batches = metadata.get("batches")
        if not isinstance(batches, list) or not batches:
            raise WarehouseTrajectoryError("dataset.json 中没有可用 batch")

        jobs: list[tuple[int, str, str, Path]] = []
        data_dir = (dataset_dir / "data").resolve()
        if not data_dir.is_dir():
            raise WarehouseTrajectoryError("dataset data 目录不存在")
        for index, batch in enumerate(batches):
            if not isinstance(batch, dict):
                raise WarehouseTrajectoryError(f"第 {index + 1} 个 batch 定义无效")
            batch_id = str(batch.get("batch_id") or "")
            table_type = str(batch.get("table_type") or "sft")
            if (
                not batch_id
                or len(batch_id) > 255
                or Path(batch_id).name != batch_id
                or batch_id in {".", ".."}
                or "\x00" in batch_id
            ):
                raise WarehouseTrajectoryError(f"第 {index + 1} 个 batch ID 无效")
            source = data_dir / f"{batch_id}.jsonl"
            if not source.is_file():
                raise WarehouseTrajectoryError(f"batch 数据文件不存在：{batch_id}.jsonl")
            resolved = source.resolve()
            if not resolved.is_relative_to(self.warehouse_root):
                raise WarehouseTrajectoryError(f"batch symlink 越过 Warehouse 根目录：{batch_id}")
            jobs.append((index, batch_id, table_type, resolved))

        worker_count = min(4, len(jobs))
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="warehouse-sample"
        ) as pool:
            sampled_batches = list(
                pool.map(
                    lambda item: self._sample_batch(*item, per_batch=per_batch, seed=seed),
                    jobs,
                )
            )
        sampled_batches.sort(key=lambda item: item["order"])

        session_id, response_batches = self._prepare_sample_response(sampled_batches)
        dataset_metadata, metadata_redactions = _redact(metadata)
        return {
            "session_id": session_id,
            "source_kind": "dataset",
            "source_id": dataset_id,
            "source": dataset_metadata,
            "dataset_id": dataset_id,
            "warehouse_url": f"https://warehouse.example.com/datasets/{dataset_id}",
            "dataset": dataset_metadata,
            "dataset_metadata_redactions": metadata_redactions,
            "per_batch": per_batch,
            "seed": seed,
            "batch_count": len(response_batches),
            "sample_count": sum(len(batch["samples"]) for batch in response_batches),
            "batches": response_batches,
        }

    def sample_batch(self, value: str, *, per_batch: int = 10, seed: int = 42) -> dict[str, Any]:
        if not 1 <= per_batch <= 20:
            raise WarehouseTrajectoryError("采样数必须在 1 到 20 之间")
        batch_id = parse_batch_id(value)
        batch_dir = self._batch_dir(batch_id)
        metadata = self._read_batch_metadata(batch_dir)
        source = (batch_dir / "data.jsonl").resolve()
        if not source.is_file():
            raise WarehouseTrajectoryError(f"batch 数据文件不存在：{batch_id}/data.jsonl")
        if not source.is_relative_to(self.warehouse_root):
            raise WarehouseTrajectoryError(f"batch 数据文件越过 Warehouse 根目录：{batch_id}")

        sampled_batches = [
            self._sample_batch(
                0,
                batch_id,
                "sft",
                source,
                per_batch=per_batch,
                seed=seed,
            )
        ]
        session_id, response_batches = self._prepare_sample_response(sampled_batches)
        batch_metadata, metadata_redactions = _redact(metadata)
        return {
            "session_id": session_id,
            "source_kind": "batch",
            "source_id": batch_id,
            "source": batch_metadata,
            "batch_id": batch_id,
            "warehouse_url": (
                "https://warehouse.example.com/"
                f"batch/{batch_id}?table=sft"
            ),
            "batch": batch_metadata,
            "batch_metadata_redactions": metadata_redactions,
            "per_batch": per_batch,
            "seed": seed,
            "batch_count": 1,
            "sample_count": len(response_batches[0]["samples"]),
            "batches": response_batches,
        }

    def get_sample(self, session_id: str, sample_id: str) -> dict[str, Any]:
        with self._lock:
            self._expire_sessions()
            session = self._sessions.get(session_id)
            if session is None:
                raise WarehouseTrajectoryError("采样会话已失效，请重新采样")
            sample = session.samples.get(sample_id)
            if sample is None:
                raise WarehouseTrajectoryError("采样记录不存在")
            self._sessions.move_to_end(session_id)
        return {
            "sample_id": sample.sample_id,
            "batch_id": sample.batch_id,
            "source_line": sample.source_line,
            "record": sample.record,
            "redacted_paths": list(sample.redacted_paths),
        }

    def _dataset_dir(self, dataset_id: str) -> Path:
        dataset_dir = (self.datasets_root / dataset_id).resolve()
        if not dataset_dir.is_relative_to(self.datasets_root) or not dataset_dir.is_dir():
            raise WarehouseTrajectoryError(f"Warehouse dataset 不存在：{dataset_id}")
        return dataset_dir

    def _batch_dir(self, batch_id: str) -> Path:
        batch_dir = (self.sft_root / batch_id).resolve()
        if not batch_dir.is_relative_to(self.sft_root) or not batch_dir.is_dir():
            raise WarehouseTrajectoryError(f"Warehouse SFT batch 不存在：{batch_id}")
        return batch_dir

    @staticmethod
    def _read_metadata(dataset_dir: Path) -> dict[str, Any]:
        path = dataset_dir / "dataset.json"
        if not path.is_file():
            raise WarehouseTrajectoryError("dataset.json 不存在")
        if path.stat().st_size > 2 * 1024 * 1024:
            raise WarehouseTrajectoryError("dataset.json 超过 2 MiB 安全上限")
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WarehouseTrajectoryError(f"dataset.json 无法解析：{exc}") from exc
        if not isinstance(metadata, dict):
            raise WarehouseTrajectoryError("dataset.json 顶层必须是对象")
        return metadata

    @staticmethod
    def _read_batch_metadata(batch_dir: Path) -> dict[str, Any]:
        path = batch_dir / "batch.json"
        if not path.is_file():
            raise WarehouseTrajectoryError("batch.json 不存在")
        if path.stat().st_size > 2 * 1024 * 1024:
            raise WarehouseTrajectoryError("batch.json 超过 2 MiB 安全上限")
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WarehouseTrajectoryError(f"batch.json 无法解析：{exc}") from exc
        if not isinstance(metadata, dict):
            raise WarehouseTrajectoryError("batch.json 顶层必须是对象")
        return metadata

    def _prepare_sample_response(
        self, sampled_batches: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        session_id = uuid.uuid4().hex
        cached: dict[str, CachedSample] = {}
        response_batches: list[dict[str, Any]] = []
        for batch in sampled_batches:
            response_samples = []
            for selected in batch.pop("records"):
                fingerprint = f"{session_id}:{batch['batch_id']}:{selected['source_line']}"
                sample_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:20]
                cleaned, redacted_paths = _redact(selected["record"])
                cached[sample_id] = CachedSample(
                    sample_id=sample_id,
                    batch_id=batch["batch_id"],
                    source_line=selected["source_line"],
                    record=cleaned,
                    redacted_paths=tuple(redacted_paths),
                )
                summary = summarize_record(
                    cleaned, selected["source_line"], selected["byte_size"]
                )
                summary.update(
                    {"sample_id": sample_id, "redacted_field_count": len(redacted_paths)}
                )
                response_samples.append(summary)
            batch["samples"] = response_samples
            response_batches.append(batch)

        self._store_session(session_id, cached)
        return session_id, response_batches

    def _sample_batch(
        self,
        order: int,
        batch_id: str,
        table_type: str,
        path: Path,
        *,
        per_batch: int,
        seed: int,
    ) -> dict[str, Any]:
        batch_seed = int.from_bytes(
            hashlib.sha256(f"{seed}:{batch_id}".encode()).digest()[:8], "big"
        )
        rng = random.Random(batch_seed)
        reservoir: list[dict[str, Any]] = []
        valid_records = 0
        invalid_records = 0
        with path.open("rb") as stream:
            for physical_line, raw in enumerate(stream, 1):
                if not raw.strip():
                    continue
                if len(raw) > self.max_line_bytes:
                    raise WarehouseTrajectoryError(
                        f"{batch_id} 第 {physical_line} 行超过 64 MiB 安全上限"
                    )
                try:
                    record = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    invalid_records += 1
                    continue
                valid_records += 1
                selected = {
                    "source_line": physical_line,
                    "byte_size": len(raw),
                    "record": record,
                }
                if len(reservoir) < per_batch:
                    reservoir.append(selected)
                    continue
                replacement = rng.randrange(valid_records)
                if replacement < per_batch:
                    reservoir[replacement] = selected
        reservoir.sort(key=lambda item: item["source_line"])
        return {
            "order": order,
            "batch_id": batch_id,
            "table_type": table_type,
            "record_count": valid_records,
            "invalid_record_count": invalid_records,
            "file_name": f"{batch_id}.jsonl",
            "file_size": path.stat().st_size,
            "records": reservoir,
        }

    def _store_session(self, session_id: str, samples: dict[str, CachedSample]) -> None:
        with self._lock:
            self._expire_sessions()
            self._sessions[session_id] = CachedSession(created_at=time.monotonic(), samples=samples)
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)

    def _expire_sessions(self) -> None:
        now = time.monotonic()
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.created_at > self.session_ttl_seconds
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)

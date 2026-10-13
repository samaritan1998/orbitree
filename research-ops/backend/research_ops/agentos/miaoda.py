"""Payload construction for Dashboard-visible Miaoda E2E batches.

The single-rollout EntryPoint adapter is useful for protocol smoke tests, but
it deliberately bypasses Datawheel's task/DB/Celery control plane.  This
module keeps the batch payload construction in the HTTP integration boundary
so a raw-query Miaoda canary can be submitted through ``/agent/batch/new``.

Only source provenance and query-derived hashes are copied into ``meta``;
replay URLs and PRD text are intentionally not accepted as inputs here.
Secrets (the Datawheel bearer and model API key) are supplied by the caller at
request time and are never written by this module.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any


class MiaodaBatchPayloadError(ValueError):
    """Raised when a raw-query batch would not be safe to submit."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _source_meta(record: Mapping[str, Any], query: str) -> dict[str, Any]:
    """Keep a small, non-secret provenance subset for each submitted row."""

    source = record.get("metadata")
    source = source if isinstance(source, Mapping) else {}
    metadata: dict[str, Any] = {
        "source_batch_id": _text(source.get("source_batch_id")),
        "source_line_no": source.get("source_line_no"),
        "source_record_id": _text(source.get("record_id") or record.get("id")),
        # Keep the stable episode/correlation identifiers that are returned by
        # Datawheel and may be used by Goblin when joining reward rows.  These
        # are identifiers only; query text and signed URLs are not copied here.
        "source_episode_id": _text(
            source.get("episode_id") or record.get("episode_id")
        ),
        "source_app_id": _text(source.get("app_id")),
        "source_conv_id": _text(source.get("conv_id")),
        "source_version_id": _text(source.get("version_id")),
        "query_sha256": _query_hash(query),
        "split": _text(source.get("split")) or "canary",
    }
    return {key: value for key, value in metadata.items() if value not in (None, "")}


def build_miaoda_e2e_batch_payload(
    records: Sequence[Mapping[str, Any]],
    *,
    task_name: str,
    generated_user_id: str,
    model_name: str,
    model_api_url: str,
    model_api_key: str,
    task_name_postfix: str = "-maasdefault-Miaoda123",
    provider: str = "qianfancoder_adapter_v4",
    coding_prompti_variant: str = "qianfan-coder",
    environment: str = "evaluation",
    query_mode: str = "deep_mode",
    default_agent: str = "AdaProAgent",
    concurrency: int = 2,
    max_tokens: int = 32_000,
    timeout_seconds: int = 1_200,
    message_interval_timeout_seconds: int = 1_500,
    temperature: float = 0.6,
    top_p: float = 0.1,
    request_logprobs: bool = False,
    top_logprobs: int = 0,
    remaining_auto_retry_count: int = 3,
) -> dict[str, Any]:
    """Build a Datawheel ``AgenticEndToEndPipeline`` request from raw queries.

    The function accepts rows produced by the Miaoda warehouse extractor.  A
    row must contain ``query``; optional ``metadata`` is used only for the
    allow-listed provenance fields above.  In particular, this builder never
    turns a historical ``prd``/``replay_url`` into the task input.
    """

    if not records:
        raise MiaodaBatchPayloadError("at least one raw-query record is required")
    if not _text(task_name):
        raise MiaodaBatchPayloadError("task_name must not be empty")
    if not _text(generated_user_id):
        raise MiaodaBatchPayloadError("generated_user_id must not be empty")
    if not _text(model_name) or not _text(model_api_url):
        raise MiaodaBatchPayloadError("model_name and model_api_url are required")
    if not _text(model_api_key):
        raise MiaodaBatchPayloadError("model_api_key must be supplied at request time")
    if concurrency < 1 or concurrency > 80:
        raise MiaodaBatchPayloadError("concurrency must be between 1 and 80")
    if remaining_auto_retry_count < 0:
        raise MiaodaBatchPayloadError("remaining auto retry count must be non-negative")
    if timeout_seconds < 1:
        raise MiaodaBatchPayloadError("model timeout must be positive")
    if message_interval_timeout_seconds <= timeout_seconds:
        raise MiaodaBatchPayloadError(
            "message interval timeout must exceed the model timeout"
        )

    api_records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, record in enumerate(records, start=1):
        query = _text(record.get("query") or record.get("user_query"))
        if not query:
            raise MiaodaBatchPayloadError(f"record {index} has an empty query")
        # Datawheel persists ``case_id`` as an integer.  Keep the warehouse's
        # composite id in ``meta.source_record_id`` and send a deterministic
        # one-based integer case id on the control-plane boundary.
        record_key = _text(record.get("id")) or f"miaoda-canary-{index:03d}"
        if record_key in seen_ids:
            raise MiaodaBatchPayloadError(f"duplicate record id: {record_key}")
        seen_ids.add(record_key)

        source = record.get("metadata")
        source = source if isinstance(source, Mapping) else {}
        app_type = _text(record.get("app_type") or source.get("app_type")) or "MINIPROGRAM"
        state_input = {
            "query": query,
            "app_type": app_type,
            "behavior": "generate_app",
            "has_code_template": "NONE",
            "has_research": False,
            "is_generated": False,
            "is_recommended_query": False,
            "lang_env": "zh-CN",
            "lang_user": "Chinese",
            "default_agent": default_agent,
            "wait_for_generate_complete": True,
        }
        api_records.append(
            {
                "id": index,
                # AgenticEndToEndPipeline consumes ``query`` from the record
                # itself.  ``state_input`` is retained as provenance-compatible
                # context for generic Datawheel adapters, but is not the only
                # source of the required pipeline argument.
                "query": query,
                "default_agent": default_agent,
                "tag": ["miaoda", "online-mopd", "canary"],
                "state_input": state_input,
                "meta": _source_meta(record, query),
            }
        )

    model_config = {
        "provider": _text(provider),
        "api_url": _text(model_api_url),
        "model_value": _text(model_name),
        "model": _text(model_name),
        "max_tokens": int(max_tokens),
        "api_key": model_api_key,
        "timeout": int(timeout_seconds),
        "temperature": float(temperature),
        "top_p": float(top_p),
        "thinking_config": {"thinking": {"type": "disabled"}},
    }
    if request_logprobs:
        model_config["logprobs"] = True
        model_config["top_logprobs"] = max(int(top_logprobs), 0)
    return {
        "taskName": _text(task_name),
        "taskNamePostfix": _text(task_name_postfix),
        "env": {"label": f"{_text(environment)}环境", "value": _text(environment)},
        "queryMode": _text(query_mode),
        "concurrency": int(concurrency),
        "remainingAutoRetryCount": int(remaining_auto_retry_count),
        "messageIntervalTimeoutSeconds": int(message_interval_timeout_seconds),
        "remainingQuota": None,
        "authCookie": False,
        "generatedUser": {"userId": _text(generated_user_id)},
        "shuffleOrder": False,
        "selectedDataset": {"value": "临时上传数据集", "hash": "**"},
        "selectedPipeline": "AgenticEndToEndPipeline",
        "activeCustomRemove": True,
        "forceContinue": True,
        "metadata": {
            "codingConfig": {
                "codingPromptiVariant": _text(coding_prompti_variant),
                "codingPromptiModelConfig": model_config,
            }
        },
        "records": api_records,
    }


def build_miaoda_dataset_batch_payload(
    records: Sequence[Mapping[str, Any]],
    *,
    task_name: str,
    dataset_name: str,
    generated_user_id: str,
    model_name: str,
    model_api_url: str,
    model_api_key: str,
    task_name_postfix: str = "-maasdefault-Miaoda123",
    provider: str = "openai",
    coding_prompti_variant: str = "qianfan-coder",
    environment: str = "evaluation",
    query_mode: str = "deep_mode",
    pipeline: str = "AgenticFirstCodingPipeline",
    concurrency: int = 8,
    max_tokens: int = 32_000,
    timeout_seconds: int = 600,
    temperature: float = 0.6,
    top_p: float = 0.95,
    top_k: int | None = 20,
    min_p: float | None = 0.0,
    presence_penalty: float | None = 0.0,
    repetition_penalty: float | None = 1.0,
    thinking_config: Mapping[str, Any] | None = None,
    reminder_variant_blocklist: Mapping[str, Any] | None = None,
    remaining_auto_retry_count: int = 3,
) -> dict[str, Any]:
    """Build a first-coding batch from a canonical Datawheel dataset.

    The records are already in Datawheel's canonical format and are passed
    through in memory.  Callers must not serialize the returned payload: it
    contains the runtime model key and dataset rows may contain signed URLs.
    """

    if not records:
        raise MiaodaBatchPayloadError("at least one dataset record is required")
    required = {
        "task_name": task_name,
        "dataset_name": dataset_name,
        "generated_user_id": generated_user_id,
        "model_name": model_name,
        "model_api_url": model_api_url,
        "model_api_key": model_api_key,
        "provider": provider,
        "coding_prompti_variant": coding_prompti_variant,
        "environment": environment,
        "query_mode": query_mode,
        "pipeline": pipeline,
    }
    missing = [name for name, value in required.items() if not _text(value)]
    if missing:
        raise MiaodaBatchPayloadError(
            f"required dataset batch fields are empty: {', '.join(missing)}"
        )
    if concurrency < 1 or concurrency > 80:
        raise MiaodaBatchPayloadError("concurrency must be between 1 and 80")
    if remaining_auto_retry_count < 0:
        raise MiaodaBatchPayloadError("remaining auto retry count must be non-negative")
    if max_tokens < 1 or timeout_seconds < 1:
        raise MiaodaBatchPayloadError("model token and timeout limits must be positive")

    api_records: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            raise MiaodaBatchPayloadError(f"dataset record {index} is not an object")
        api_records.append(dict(record))

    model_config: dict[str, Any] = {
        "provider": _text(provider),
        "api_url": _text(model_api_url),
        "model_value": _text(model_name),
        "model": _text(model_name),
        "max_tokens": int(max_tokens),
        "api_key": model_api_key,
        "timeout": int(timeout_seconds),
        "temperature": float(temperature),
        "top_p": float(top_p),
        "thinking_config": dict(
            thinking_config
            or {
                "chat_template_kwargs": {
                    "enable_thinking": True,
                    "preserve_thinking": True,
                }
            }
        ),
    }
    for key, value in (
        ("top_k", top_k),
        ("min_p", min_p),
        ("presence_penalty", presence_penalty),
        ("repetition_penalty", repetition_penalty),
    ):
        if value is not None:
            model_config[key] = value

    coding_config: dict[str, Any] = {
        "codingPromptiVariant": _text(coding_prompti_variant),
        "codingPromptiModelConfig": model_config,
    }
    if reminder_variant_blocklist:
        coding_config["reminderVariantBlocklist"] = dict(reminder_variant_blocklist)

    return {
        "taskName": _text(task_name),
        "taskNamePostfix": _text(task_name_postfix),
        "env": {"label": f"{_text(environment)}环境", "value": _text(environment)},
        "queryMode": _text(query_mode),
        "concurrency": int(concurrency),
        "remainingAutoRetryCount": int(remaining_auto_retry_count),
        "remainingQuota": None,
        "authCookie": False,
        "generatedUser": {"userId": _text(generated_user_id)},
        "shuffleOrder": False,
        "includeTags": [],
        "excludeTags": [],
        "selectedDataset": {"value": _text(dataset_name), "hash": "**"},
        "selectedPipeline": _text(pipeline),
        "activeCustomRemove": True,
        "forceContinue": True,
        "metadata": {"codingConfig": coding_config},
        "records": api_records,
    }


def miaoda_batch_identity(payload: Mapping[str, Any]) -> str:
    """Return a stable idempotency suffix without retaining model credentials."""

    def scrub(value: Any, key: str = "") -> Any:
        if key.casefold() in {"api_key", "apikey", "authorization", "token"}:
            return "<redacted>"
        if isinstance(value, Mapping):
            return {str(k): scrub(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    import json

    canonical = json.dumps(
        scrub(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

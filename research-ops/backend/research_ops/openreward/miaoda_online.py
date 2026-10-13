"""Async Datawheel/AgentOS orchestration for Miaoda ORS episodes.

The service models one *complete* Miaoda application rollout as an ORS
episode.  It deliberately separates three states that are often conflated:

``rollout``
    Datawheel/AgentOS is still executing the application.
``reward``
    In reward-backed mode, Goblin is evaluating the completed artifact. Pure
    MOPD skips this phase because teacher token log-probabilities are its
    learning signal.
``terminal``
    Reward-backed mode has joined a valid scalar reward or classified a reward
    infrastructure failure. Pure MOPD terminates after AgentOS with neutral
    schema reward ``0.0`` and ``task_reward_used=false``.

The implementation preserves one ORS session per training episode while an
optional in-process coordinator groups pure-OPD sessions into bounded
Datawheel batches. Reward-backed evaluation remains one-row-per-job so Goblin
semantics are unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import time
import zipfile
from collections import Counter, defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4
from xml.etree import ElementTree

from research_ops.agentos import AgentOSClient, AgentOSClientError
from research_ops.agentos.miaoda import build_miaoda_e2e_batch_payload
from research_ops.reward import (
    GoblinRewardClient,
    GoblinRewardError,
    load_reward_records,
    reward_map_from_file,
)

from .adapters import BackendResult
from .correlation import (
    episode_correlation_token,
    strip_episode_correlation,
    tag_miaoda_query,
)
from .types import JSONObject, TextBlock


class MiaodaOnlineError(RuntimeError):
    """Raised for a configuration or state-machine error."""


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MiaodaOnlineConfig:
    """Non-secret runtime configuration for one online rollout backend."""

    generated_user_id: str
    model_name: str
    model_api_url: str
    model_api_key_env: str = "AGENTOS_MODEL_API_KEY"
    task_name_prefix: str = "ors-miaoda"
    benchmark_dataset_name: str = "miaoda-online-mopd-train-v1"
    artifact_dir: Path = Path("data/miaoda-online-artifacts")
    reward_result_root: Path | None = None
    # Goblin deployments that expose only the JSON job endpoint consume a
    # path on shared CFS instead of a multipart upload.  Keep this separate
    # from ``artifact_dir`` so callers can choose the evaluator's shared
    # working root without changing where Datawheel artifacts are retained.
    reward_workdir_prefix: Path | None = None
    task_name_postfix: str = "-maasdefault-Miaoda123"
    concurrency: int = 1
    reward_upload: bool = True
    reward_scale: float = 1.0
    reward_image: str | None = None
    reward_prompt_version: str = "v1_6"
    reward_model: str = "gpt-5.5-agent"
    reward_query_round: str = "first_round"
    reward_eval_mm: bool = True
    reward_eval_miniprog: bool = True
    request_logprobs: bool = True
    request_top_logprobs: int = 0
    model_max_tokens: int = 32_000
    model_timeout_seconds: int = 1_200
    message_interval_timeout_seconds: int = 1_500
    policy_capture_dir: Path | None = None
    submission_batch_size: int = 1
    submission_batch_window_seconds: float = 0.0
    explicit_batch_wait_timeout_seconds: float = 120.0
    datawheel_poll_cache_seconds: float = 2.0
    policy_capture_join_timeout_seconds: float = 120.0
    trajectory_join_concurrency: int = 16

    def __post_init__(self) -> None:
        """Reject shared-path settings that a remote Goblin worker cannot expose.

        Relative paths are resolved inside the Reward worker's own process or
        container.  A job can therefore complete successfully while Research
        Ops has no way to read ``result.jsonl`` or ``rollout.msg``.  Requiring
        an absolute shared root turns that late, silent integration failure
        into an immediate configuration error.
        """

        if self.submission_batch_size < 1:
            raise MiaodaOnlineError("Miaoda submission batch size must be positive")
        if self.concurrency < 1 or self.concurrency > 80:
            raise MiaodaOnlineError("Miaoda Datawheel concurrency must be between 1 and 80")
        if self.submission_batch_window_seconds < 0:
            raise MiaodaOnlineError("Miaoda submission batch window must be non-negative")
        if self.explicit_batch_wait_timeout_seconds <= 0:
            raise MiaodaOnlineError("Miaoda explicit batch wait timeout must be positive")
        if self.datawheel_poll_cache_seconds < 0:
            raise MiaodaOnlineError("Miaoda poll cache duration must be non-negative")
        if self.policy_capture_join_timeout_seconds < 0:
            raise MiaodaOnlineError("Miaoda capture join timeout must be non-negative")
        if self.trajectory_join_concurrency < 1:
            raise MiaodaOnlineError("Miaoda trajectory join concurrency must be positive")
        if self.model_max_tokens < 1:
            raise MiaodaOnlineError("Miaoda model max tokens must be positive")
        if self.model_timeout_seconds < 1:
            raise MiaodaOnlineError("Miaoda model timeout must be positive")
        if self.message_interval_timeout_seconds <= self.model_timeout_seconds:
            raise MiaodaOnlineError(
                "Miaoda message interval timeout must exceed the model timeout"
            )
        if self.submission_batch_size > 1 and self.policy_capture_dir is None:
            raise MiaodaOnlineError("Miaoda batched pure OPD requires a policy capture directory")
        if self.policy_capture_dir is not None and not self.policy_capture_dir.is_absolute():
            raise MiaodaOnlineError("Miaoda policy capture directory must be absolute")
        if self.reward_upload:
            return
        roots = tuple(
            root
            for root in (self.reward_result_root, self.reward_workdir_prefix)
            if root is not None
        )
        if not roots:
            raise MiaodaOnlineError(
                "Miaoda shared-path Goblin mode requires an absolute reward result root"
            )
        if any(not root.is_absolute() for root in roots):
            raise MiaodaOnlineError(
                "Miaoda shared-path Goblin result roots must be absolute paths"
            )


_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_GOBLIN_RATING_LABELS = frozenset({"优秀", "基本可用", "不可用"})
_GOBLIN_RATING_PATTERNS = (
    re.compile(r'"rating"\s*:\s*"(?P<label>优秀|基本可用|不可用)"'),
    re.compile(r"最终评级\s*[:：]\s*(?P<label>优秀|基本可用|不可用)"),
)
_OPENAI_CHAT_COMPLETIONS_PATH = "/v1/chat/completions"


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _message_identity(value: Any) -> dict[str, Any]:
    message = value if isinstance(value, Mapping) else {}
    keys = ("role", "content", "name", "tool_call_id", "tool_calls", "function_call")
    return {key: message.get(key) for key in keys if key in message}


def _policy_call_from_capture(value: Mapping[str, Any]) -> dict[str, Any] | None:
    if value.get("schema_version") != "mopd-policy-capture.v2":
        return None
    prompt = value.get("prompt_token_ids")
    response = value.get("response_token_ids")
    behavior = value.get("behavior_logprobs")
    if not all(isinstance(items, list) for items in (prompt, response, behavior)):
        return None
    try:
        prompt_ids = [int(item) for item in prompt]
        response_ids = [int(item) for item in response]
        logprobs = [float(item) for item in behavior]
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not prompt_ids
        or not response_ids
        or len(response_ids) != len(logprobs)
        or any(item < 0 for item in prompt_ids + response_ids)
        or any(not math.isfinite(item) for item in logprobs)
    ):
        return None
    return {
        "capture_id": _safe_text(value.get("capture_id")),
        "prompt_token_ids": prompt_ids,
        "response_token_ids": response_ids,
        "behavior_logprobs": logprobs,
        "weight_version": _safe_text(value.get("weight_version")),
    }


def _load_policy_capture_records(
    path: Path,
    *,
    capture_episode_id: str,
    started_at: float,
    max_calls: int = 16_384,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if (
            not isinstance(value, dict)
            or _capture_timestamp(value) < started_at
            or _safe_text(value.get("episode_id")) != capture_episode_id
        ):
            continue
        capture_id = _safe_text(value.get("capture_id"))
        if not capture_id or capture_id in seen or _policy_call_from_capture(value) is None:
            continue
        seen.add(capture_id)
        records.append(value)
        if len(records) >= max_calls:
            break
    records.sort(key=_capture_timestamp)
    return records


def _load_policy_http_statuses(
    path: Path,
    *,
    capture_episode_id: str,
    started_at: float,
    maximum: int = 16_384,
) -> list[int]:
    if not path.is_file():
        return []
    statuses: list[int] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != "mopd-policy-http.v1"
            or _capture_timestamp(value) < started_at
            or _safe_text(value.get("episode_id")) != capture_episode_id
        ):
            continue
        status = value.get("status_code")
        if isinstance(status, int) and not isinstance(status, bool):
            statuses.append(status)
        if len(statuses) >= maximum:
            break
    return statuses


def _trajectory_policy_signatures(value: Any) -> list[tuple[str, str]]:
    """Return ordered prompt/assistant hashes from an AgentOS trajectory."""

    signatures: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def index_messages(messages: Any) -> None:
        if not isinstance(messages, list) or not all(
            isinstance(item, Mapping) for item in messages
        ):
            return
        with contextlib.suppress(ValueError):
            cleaned, _correlation_id = strip_episode_correlation(messages)
            if isinstance(cleaned, list):
                messages = cleaned
        for index, item in enumerate(messages):
            if str(item.get("role") or "").strip().casefold() != "assistant":
                continue
            signature = (
                _json_sha256(messages[:index]),
                _json_sha256(_message_identity(item)),
            )
            if signature not in seen:
                seen.add(signature)
                signatures.append(signature)

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            body = node.get("llm_request_body")
            if isinstance(body, Mapping):
                params = body.get("call_llm_params")
                params = params if isinstance(params, Mapping) else body
                index_messages(params.get("messages"))
            # Some exports store the complete history directly rather than
            # under llm_request_body.
            index_messages(node.get("messages"))
            for child in node.values():
                if isinstance(child, (Mapping, list)):
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return signatures


def _trajectory_app_ids(row: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("app_id", "generated_app_id"):
        value = _safe_text(row.get(key))
        if value and value not in values:
            values.append(value)
    raw_output = row.get("Pipeline输出结果")
    if isinstance(raw_output, str):
        with contextlib.suppress(TypeError, ValueError):
            raw_output = json.loads(raw_output)

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if str(key).casefold() in {"app_id", "generated_app_id"}:
                    candidate = _safe_text(value)
                    if candidate and candidate not in values:
                        values.append(candidate)
                elif isinstance(value, (Mapping, list)):
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(raw_output)
    return values


def _policy_url_for_episode(
    base_url: str,
    episode_id: str,
    *,
    require_record_correlation: bool = False,
) -> str:
    """Return the captured OpenAI completion endpoint for one episode.

    AgentOS treats ``codingPromptiModelConfig.api_url`` as the complete target
    URL. A bare gateway origin would therefore receive ``POST /`` rather than
    the policy proxy's OpenAI-compatible completion request. Preserve an
    explicit route, but make the standard bare gateway form safe.
    """

    parts = urlsplit(str(base_url).strip())
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["mopd_episode_id"] = episode_id
    if require_record_correlation:
        query["mopd_require_record_correlation"] = "1"
    path = parts.path.rstrip("/") or _OPENAI_CHAT_COMPLETIONS_PATH
    return urlunsplit(
        (parts.scheme, parts.netloc, path, urlencode(query), parts.fragment)
    )


def _xlsx_reward_input(source: Path) -> Path:
    """Normalize a Datawheel workbook for Goblin's shared-path JSONL API.

    The upload-backed Goblin deployment accepts ``result_raw.xlsx`` directly,
    while the JSON-only deployment describes ``file`` as one JSON object per
    line.  Keep the original workbook for provenance and create an adjacent,
    atomically-written JSONL view only when the shared-path mode is selected.
    The standard aliases (``app_type``/``case_id``/generated IDs) bridge the
    Chinese Datawheel column names without changing the source artifact.
    """

    if source.suffix.casefold() not in {".xlsx", ".xlsm"}:
        return source
    destination = source.with_suffix(".jsonl")
    temporary = destination.with_suffix(destination.suffix + ".part")
    namespace = {"m": _XLSX_MAIN_NS}
    with zipfile.ZipFile(source) as workbook:
        names = set(workbook.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            shared_root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
            for item in shared_root.findall("m:si", namespace):
                shared.append(
                    "".join(text.text or "" for text in item.iter(f"{{{_XLSX_MAIN_NS}}}t"))
                )
        sheet_root = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    rows: list[list[str | None]] = []
    for row in sheet_root.findall(".//m:sheetData/m:row", namespace):
        values: list[str | None] = []
        for cell in row.findall("m:c", namespace):
            reference = str(cell.get("r") or "")
            column = "".join(character for character in reference if character.isalpha())
            index = 0
            for character in column:
                index = index * 26 + ord(character.upper()) - 64
            while len(values) < index:
                values.append(None)
            value = cell.find("m:v", namespace)
            text = (value.text if value is not None else "") or ""
            if cell.get("t") == "s":
                with contextlib.suppress(ValueError, IndexError):
                    text = shared[int(text)]
            values[index - 1] = text
        rows.append(values)
    if not rows:
        raise GoblinRewardError(f"Datawheel workbook has no rows: {source}")
    headers = [str(value or "") for value in rows[0]]

    def maybe_json(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    records: list[dict[str, Any]] = []
    for values in rows[1:]:
        record = {
            header: value
            for header, value in zip(headers, values, strict=False)
            if header and value not in (None, "")
        }
        for key in ("state_input", "meta", "Pipeline请求参数", "Pipeline输出结果", "Pipeline输入"):
            if key in record:
                record[key] = maybe_json(record[key])
        record.setdefault("app_type", record.get("应用类型") or "MINIPROGRAM")
        record.setdefault("case_id", record.get("id"))
        record.setdefault("generated_app_id", record.get("app_id"))
        record.setdefault("generated_conv_id", record.get("conv_id"))
        records.append(record)
    temporary.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return destination


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _capture_file_name(value: str) -> str:
    text = _safe_text(value)
    safe = "".join(char if (char.isalnum() or char in "._:-") else "_" for char in text)
    return (safe[:160] or "unattributed") + ".jsonl"


def _capture_timestamp(value: Mapping[str, Any]) -> float:
    raw = str(value.get("captured_at") or "")
    try:
        from datetime import datetime

        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _load_policy_calls(
    path: Path,
    *,
    episode_id: str,
    started_at: float,
    max_calls: int = 256,
) -> list[dict[str, Any]]:
    """Load bounded, text-free v2 policy captures for one managed episode."""

    calls: list[dict[str, Any]] = []
    for value in _load_policy_capture_records(
        path,
        capture_episode_id=episode_id,
        started_at=started_at,
        max_calls=max_calls,
    ):
        call = _policy_call_from_capture(value)
        if call is not None:
            calls.append(call)
        if len(calls) >= max_calls:
            break
    return calls


def _episode_id(task_spec: Mapping[str, Any], state: Mapping[str, Any]) -> str:
    for key in ("episode_id", "case_id", "id", "record_id"):
        value = _safe_text(task_spec.get(key))
        if value:
            return value
    value = _safe_text(state.get("episode_id"))
    if value:
        return value
    query = _safe_text(task_spec.get("query") or task_spec.get("prompt"))
    if not query:
        raise MiaodaOnlineError("Miaoda task requires query or episode_id")
    return f"query-{hashlib.sha256(query.encode('utf-8')).hexdigest()[:16]}"


def _query(task_spec: Mapping[str, Any]) -> str:
    value = _safe_text(task_spec.get("query") or task_spec.get("prompt"))
    if not value:
        raise MiaodaOnlineError("Miaoda task requires a non-empty query")
    return value


def _task_reward_required(task_spec: Mapping[str, Any]) -> bool:
    """Return whether this episode must block on a scalar task reward.

    Pure on-policy distillation uses a neutral scalar only to satisfy Slime's
    generic RL batch schema; its learning signal comes from teacher token
    log-probabilities.  Keep reward-backed behavior as the default so existing
    evaluation/RL callers remain unchanged, while an explicit task flag lets
    online MOPD finish immediately after the AgentOS artifact is available.
    """

    # ``training_objective`` is the canonical, positive opt-in used by the
    # materializer.  Honor it even if an older proxy drops the auxiliary
    # boolean while forwarding the task object.
    objective = str(
        task_spec.get("training_objective")
        or task_spec.get("openreward_training_objective")
        or ""
    ).strip().casefold()
    if objective == "pure_opd":
        return False
    value = task_spec.get("task_reward_required", True)
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise MiaodaOnlineError("task_reward_required must be a boolean")


_EXPLICIT_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


def _explicit_batch_fields(
    task_spec: Mapping[str, Any], *, maximum_size: int
) -> tuple[str, int, int] | None:
    """Validate the all-or-none trainer-owned Miaoda batch envelope."""

    raw_values = (
        task_spec.get("optimizer_batch_id"),
        task_spec.get("optimizer_batch_expected_count"),
        task_spec.get("optimizer_batch_member_index"),
    )
    if all(value is None for value in raw_values):
        return None
    if any(value is None for value in raw_values):
        raise MiaodaOnlineError("explicit Miaoda batch fields must be provided together")
    batch_id = str(raw_values[0]).strip()
    if not _EXPLICIT_BATCH_ID_RE.fullmatch(batch_id):
        raise MiaodaOnlineError("optimizer_batch_id has an invalid format")
    if isinstance(raw_values[1], bool) or isinstance(raw_values[2], bool):
        raise MiaodaOnlineError("explicit Miaoda batch counts must be integers")
    try:
        expected_count = int(raw_values[1])
        member_index = int(raw_values[2])
    except (TypeError, ValueError) as exc:
        raise MiaodaOnlineError("explicit Miaoda batch counts must be integers") from exc
    if expected_count < 1 or expected_count > maximum_size:
        raise MiaodaOnlineError("explicit Miaoda batch size is outside the configured limit")
    if member_index < 0 or member_index >= expected_count:
        raise MiaodaOnlineError("optimizer_batch_member_index is outside the batch")
    return batch_id, expected_count, member_index


def _first_row_for_episode(
    rows: list[Mapping[str, Any]],
    *,
    task_spec: Mapping[str, Any],
    state: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Select the Datawheel row belonging to this one-row ORS episode.

    The online backend submits one record per ORS session.  Datawheel's
    downloaded workbook nevertheless contains several possible identifiers
    (numeric ``id``, source IDs, and a generated UUID).  Prefer an explicit
    id/query match and fall back to the sole row; never guess among multiple
    rows.
    """

    if not rows:
        return None
    candidates = {
        _safe_text(state.get("miaoda_record_id")),
        _safe_text(state.get("episode_id")),
        _safe_text(task_spec.get("episode_id")),
        _safe_text(task_spec.get("case_id")),
        _safe_text(task_spec.get("id")),
    }
    for row in rows:
        row_ids = {
            _safe_text(row.get(key))
            for key in (
                "id",
                "case_id",
                "episode_id",
                "record_id",
                "跑批单条记录id",
            )
        }
        if candidates.intersection(item for item in row_ids if item):
            return row
        meta = row.get("meta") or row.get("metadata")
        if isinstance(meta, str):
            with contextlib.suppress(TypeError, ValueError):
                meta = json.loads(meta)
        if isinstance(meta, Mapping):
            source_ids = {
                _safe_text(meta.get(key))
                for key in ("source_episode_id", "source_record_id", "record_id")
            }
            if candidates.intersection(item for item in source_ids if item):
                return row
    query = _safe_text(task_spec.get("query") or task_spec.get("prompt"))
    query_matches = [row for row in rows if query and _safe_text(row.get("query")) == query]
    if len(query_matches) == 1:
        return query_matches[0]
    return rows[0] if len(rows) == 1 else None


def _capture_datawheel_ids(
    destination: Path,
    *,
    task_spec: Mapping[str, Any],
    state: dict[str, Any],
) -> None:
    """Copy only allow-listed Datawheel IDs into ephemeral backend state.

    Parsing is best-effort: a malformed/empty workbook should be handled by
    the normal Goblin/reward error path, not turn an otherwise useful rollout
    into a credential or query leak.  The IDs are retained only long enough
    to correlate the Goblin row and are never copied to task text.
    """

    try:
        rows = load_reward_records(destination)
    except Exception:  # optional correlation metadata must never break rollout
        return
    _capture_datawheel_ids_from_rows(rows, task_spec=task_spec, state=state)


def _capture_datawheel_ids_from_rows(
    rows: list[Mapping[str, Any]],
    *,
    task_spec: Mapping[str, Any],
    state: dict[str, Any],
) -> None:
    row = _first_row_for_episode(rows, task_spec=task_spec, state=state)
    if row is None:
        return
    aliases = {
        "miaoda_app_id": ("app_id", "appId", "generated_app_id"),
        "miaoda_conv_id": (
            "conv_id",
            "conversation_id",
            "conversationId",
            "generated_conv_id",
        ),
        "miaoda_record_uuid": (
            "跑批单条记录id",
            "record_id",
            "recordId",
            "id",
        ),
    }
    for state_key, keys in aliases.items():
        for key in keys:
            value = _safe_text(row.get(key))
            if value:
                state[state_key] = value
                break


def _rating_from_text(value: str) -> str | None:
    """Return Goblin's documented textual rating without inventing a score."""

    matches: list[tuple[int, str]] = []
    for pattern in _GOBLIN_RATING_PATTERNS:
        for match in pattern.finditer(value):
            label = match.group("label")
            if label in _GOBLIN_RATING_LABELS:
                matches.append((match.start(), label))
    return max(matches, default=(0, ""))[1] or None


def _rating_evidence(
    *,
    result_file: str | Path | None,
    workdir: str | Path | None,
) -> tuple[str, Path] | None:
    """Find a bounded, local Goblin rating artifact.

    Current Goblin jobs may copy the Datawheel rows to ``result.jsonl`` while
    writing the actual rubric conclusion to ``<case>/rollout.msg``.  Search
    only the configured per-batch directory and small terminal artifacts.  A
    label is preserved as metadata; this function deliberately performs no
    numeric mapping.
    """

    candidates: list[Path] = []
    if result_file:
        candidates.append(Path(result_file))
    if workdir:
        root = Path(workdir)
        candidates.extend(sorted(root.glob("*/rollout.msg")))
        candidates.extend(sorted(root.glob("*/rollout.md")))
        candidates.extend(sorted(root.glob("work/*/rollout.msg")))
        candidates.extend(sorted(root.glob("work/*/rollout.md")))
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        try:
            size = candidate.stat().st_size
            if size <= 0:
                continue
            with candidate.open("rb") as stream:
                if size > 1_000_000:
                    stream.seek(-1_000_000, os.SEEK_END)
                text = stream.read(1_000_000).decode("utf-8", errors="replace")
        except OSError:
            continue
        label = _rating_from_text(text)
        if label is not None:
            return label, candidate
    return None


@dataclass
class _PendingSubmission:
    task_spec: JSONObject
    state: dict[str, Any]
    future: asyncio.Future[BackendResult]


@dataclass
class _ExplicitPendingGroup:
    expected_count: int
    items: dict[int, _PendingSubmission] = field(default_factory=dict)
    timeout_handle: asyncio.TimerHandle | None = None


@dataclass
class _BatchMember:
    episode_id: str
    task_spec: JSONObject
    state: dict[str, Any]
    record_id: str
    capture_episode_id: str


@dataclass
class _BatchRuntime:
    group_id: str
    batch_task_id: str
    members: list[_BatchMember]
    capture_file: Path
    capture_started_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    latest_snapshot: Any = None
    latest_snapshot_at: float = 0.0
    artifact: Path | None = None
    rows: list[Mapping[str, Any]] | None = None
    policy_calls: dict[str, list[dict[str, Any]]] | None = None
    join_started_at: float = 0.0
    active_episode_ids: set[str] = field(default_factory=set)


class MiaodaDatawheelGoblinBackend:
    """Backend implementing canonical ``start_rollout``/``poll_rollout`` tools.

    AgentOS credentials and the model API key are resolved by their respective
    clients at request time.  They are never copied into ``state`` or result
    metadata.  ``state`` contains only correlation IDs and phase information.
    """

    def __init__(
        self,
        datawheel: AgentOSClient,
        goblin: GoblinRewardClient,
        config: MiaodaOnlineConfig,
    ) -> None:
        if config.concurrency < 1:
            raise MiaodaOnlineError("Miaoda online concurrency must be positive")
        self.datawheel = datawheel
        self.goblin = goblin
        self.config = config
        self._submission_lock: asyncio.Lock | None = None
        self._pending_submissions: list[_PendingSubmission] = []
        self._submission_timer: asyncio.TimerHandle | None = None
        self._explicit_submission_groups: dict[str, _ExplicitPendingGroup] = {}
        self._batch_runtimes: dict[str, _BatchRuntime] = {}

    def setup(
        self,
        *,
        task_spec: JSONObject,
        secrets: Mapping[str, Any],
        state: dict[str, Any],
    ) -> None:
        del secrets
        state["episode_id"] = _episode_id(task_spec, state)
        state["phase"] = "not_started"
        state["pure_opd"] = not _task_reward_required(task_spec)
        explicit = _explicit_batch_fields(
            task_spec, maximum_size=self.config.submission_batch_size
        )
        if explicit is not None:
            batch_id, expected_count, member_index = explicit
            state.update(
                {
                    "miaoda_optimizer_batch_id": batch_id,
                    "miaoda_optimizer_batch_expected_count": expected_count,
                    "miaoda_optimizer_batch_member_index": member_index,
                }
            )

    async def execute(
        self,
        *,
        action: str,
        payload: JSONObject,
        task_spec: JSONObject,
        state: dict[str, Any],
    ) -> BackendResult:
        del payload
        normalized = action.strip().casefold()
        if normalized in {"start_rollout", "run_rollout"}:
            return await self._start(task_spec, state)
        if normalized in {"poll_rollout", "get_status", "status", "submit"}:
            return await self._poll(task_spec, state)
        raise KeyError(action)

    def teardown(self, *, task_spec: JSONObject, state: dict[str, Any]) -> None:
        del task_spec
        batch_id = _safe_text(state.get("miaoda_batch_task_id"))
        episode_id = _safe_text(state.get("episode_id"))
        runtime = self._batch_runtimes.get(batch_id)
        if runtime is not None:
            runtime.active_episode_ids.discard(episode_id)
            if not runtime.active_episode_ids:
                self._batch_runtimes.pop(batch_id, None)
        # Keep no local paths or IDs beyond the session lifetime.  The remote
        # jobs themselves remain queryable by their durable IDs.
        for key in tuple(state):
            if key.startswith(("miaoda_", "goblin_")) or key in {
                "phase",
                "episode_id",
                "pure_opd",
                "policy_capture_started_at",
                "mopd_capture_file",
                "policy_calls",
            }:
                state.pop(key, None)

    async def _start(self, task_spec: JSONObject, state: dict[str, Any]) -> BackendResult:
        if state.get("phase") not in {None, "not_started"}:
            return self._state_output(state, "rollout already started")
        explicit = _explicit_batch_fields(
            task_spec, maximum_size=self.config.submission_batch_size
        )
        if not _task_reward_required(task_spec) and explicit is not None:
            return await self._enqueue_explicit_batch_start(
                task_spec,
                state,
                batch_id=explicit[0],
                expected_count=explicit[1],
                member_index=explicit[2],
            )
        if not _task_reward_required(task_spec) and self.config.submission_batch_size > 1:
            return await self._enqueue_batched_start(task_spec, state)
        return await self._start_single(task_spec, state)

    async def _start_single(
        self, task_spec: JSONObject, state: dict[str, Any]
    ) -> BackendResult:
        query = _query(task_spec)
        episode_id = _episode_id(task_spec, state)
        model_key = os.environ.get(self.config.model_api_key_env, "").strip()
        if not model_key:
            raise MiaodaOnlineError(
                "Miaoda model API key is unavailable; configure environment variable "
                f"{self.config.model_api_key_env}"
            )
        task_name = (
            f"{self.config.task_name_prefix}-"
            f"{hashlib.sha256(episode_id.encode('utf-8')).hexdigest()[:12]}"
        )
        state["policy_capture_started_at"] = time.time()
        record = {
            "id": episode_id,
            "query": query,
            "app_type": task_spec.get("app_type") or "MINIPROGRAM",
            "metadata": {
                "episode_id": episode_id,
                "split": task_spec.get("split") or "train",
                "source_record_id": episode_id,
            },
        }
        payload = build_miaoda_e2e_batch_payload(
            [record],
            task_name=task_name,
            generated_user_id=self.config.generated_user_id,
            model_name=self.config.model_name,
            model_api_url=_policy_url_for_episode(self.config.model_api_url, episode_id),
            model_api_key=model_key,
            task_name_postfix=self.config.task_name_postfix,
            concurrency=self.config.concurrency,
            max_tokens=self.config.model_max_tokens,
            timeout_seconds=self.config.model_timeout_seconds,
            message_interval_timeout_seconds=(
                self.config.message_interval_timeout_seconds
            ),
            request_logprobs=self.config.request_logprobs,
            top_logprobs=self.config.request_top_logprobs,
            remaining_auto_retry_count=(
                3 if _task_reward_required(task_spec) else 0
            ),
        )
        try:
            snapshot = await asyncio.to_thread(
                self.datawheel.submit_batch,
                payload,
                idempotency_key=(
                    f"ors-miaoda-{hashlib.sha256(episode_id.encode('utf-8')).hexdigest()[:32]}"
                ),
            )
        except AgentOSClientError as exc:
            raise MiaodaOnlineError("Datawheel rollout submission failed") from exc
        state.update(
            {
                "phase": "rollout",
                "miaoda_batch_task_id": snapshot.batch_task_id,
                "miaoda_task_name": task_name,
                "miaoda_record_id": "1",
            }
        )
        if self.config.policy_capture_dir is not None:
            state["mopd_capture_file"] = str(
                self.config.policy_capture_dir / f"capture-{_capture_file_name(episode_id)}"
            )
        return BackendResult(
            blocks=[TextBlock(text="Miaoda rollout submitted; waiting for AgentOS")],
            finished=False,
            metadata=self._metadata(state, status=snapshot.status),
        )

    def _get_submission_lock(self) -> asyncio.Lock:
        if self._submission_lock is None:
            self._submission_lock = asyncio.Lock()
        return self._submission_lock

    async def _enqueue_explicit_batch_start(
        self,
        task_spec: JSONObject,
        state: dict[str, Any],
        *,
        batch_id: str,
        expected_count: int,
        member_index: int,
    ) -> BackendResult:
        """Wait for the trainer-declared batch instead of flushing on a timer."""

        _query(task_spec)
        episode_id = _episode_id(task_spec, state)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[BackendResult] = loop.create_future()
        pending = _PendingSubmission(dict(task_spec), state, future)
        state["policy_capture_started_at"] = time.time()
        submit_now: list[_PendingSubmission] = []
        async with self._get_submission_lock():
            group = self._explicit_submission_groups.get(batch_id)
            if group is None:
                group = _ExplicitPendingGroup(expected_count=expected_count)
                group.timeout_handle = loop.call_later(
                    self.config.explicit_batch_wait_timeout_seconds,
                    lambda: asyncio.create_task(
                        self._expire_explicit_submission_group(batch_id)
                    ),
                )
                self._explicit_submission_groups[batch_id] = group
            elif group.expected_count != expected_count:
                raise MiaodaOnlineError(
                    "optimizer batch members disagree on expected count"
                )
            if member_index in group.items:
                raise MiaodaOnlineError("duplicate optimizer batch member index")
            if any(
                _episode_id(item.task_spec, item.state) == episode_id
                for item in group.items.values()
            ):
                raise MiaodaOnlineError("duplicate optimizer batch episode")
            group.items[member_index] = pending
            if len(group.items) == expected_count:
                submit_now = [group.items[index] for index in range(expected_count)]
                if group.timeout_handle is not None:
                    group.timeout_handle.cancel()
                self._explicit_submission_groups.pop(batch_id, None)
        if submit_now:
            asyncio.create_task(
                self._submit_pending_group(
                    submit_now,
                    optimizer_batch_id=batch_id,
                )
            )
        return await asyncio.shield(future)

    async def _expire_explicit_submission_group(self, batch_id: str) -> None:
        async with self._get_submission_lock():
            group = self._explicit_submission_groups.pop(batch_id, None)
        if group is None:
            return
        error = MiaodaOnlineError(
            "explicit Miaoda optimizer batch did not reach its expected count"
        )
        for item in group.items.values():
            if not item.future.done():
                item.future.set_exception(error)

    async def _enqueue_batched_start(
        self, task_spec: JSONObject, state: dict[str, Any]
    ) -> BackendResult:
        # Validate before taking a queue slot so one malformed session cannot
        # poison otherwise valid members of the same Datawheel submission.
        _query(task_spec)
        _episode_id(task_spec, state)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[BackendResult] = loop.create_future()
        state["policy_capture_started_at"] = time.time()
        pending = _PendingSubmission(dict(task_spec), state, future)
        submit_now: list[_PendingSubmission] = []
        async with self._get_submission_lock():
            self._pending_submissions.append(pending)
            if len(self._pending_submissions) >= self.config.submission_batch_size:
                submit_now = self._take_pending_submissions()
            elif self._submission_timer is None:
                self._submission_timer = loop.call_later(
                    self.config.submission_batch_window_seconds,
                    lambda: asyncio.create_task(self._flush_pending_submissions()),
                )
        if submit_now:
            asyncio.create_task(self._submit_pending_group(submit_now))
        return await asyncio.shield(future)

    def _take_pending_submissions(self) -> list[_PendingSubmission]:
        items = self._pending_submissions[: self.config.submission_batch_size]
        del self._pending_submissions[: len(items)]
        if self._submission_timer is not None:
            self._submission_timer.cancel()
            self._submission_timer = None
        return items

    async def _flush_pending_submissions(self) -> None:
        items: list[_PendingSubmission] = []
        async with self._get_submission_lock():
            self._submission_timer = None
            if self._pending_submissions:
                items = self._take_pending_submissions()
                if self._pending_submissions:
                    loop = asyncio.get_running_loop()
                    self._submission_timer = loop.call_later(
                        self.config.submission_batch_window_seconds,
                        lambda: asyncio.create_task(self._flush_pending_submissions()),
                    )
        if items:
            await self._submit_pending_group(items)

    async def _submit_pending_group(
        self,
        items: list[_PendingSubmission],
        *,
        optimizer_batch_id: str | None = None,
    ) -> None:
        group_id = (
            f"batch-{hashlib.sha256(optimizer_batch_id.encode()).hexdigest()[:32]}"
            if optimizer_batch_id
            else f"batch-{uuid4().hex}"
        )
        model_key = os.environ.get(self.config.model_api_key_env, "").strip()
        if not model_key:
            error = MiaodaOnlineError(
                "Miaoda model API key is unavailable; configure environment variable "
                f"{self.config.model_api_key_env}"
            )
            for item in items:
                if not item.future.done():
                    item.future.set_exception(error)
            return
        task_name = (
            f"{self.config.task_name_prefix}-"
            f"{hashlib.sha256(group_id.encode('utf-8')).hexdigest()[:12]}"
        )
        records: list[dict[str, Any]] = []
        members: list[_BatchMember] = []
        for index, item in enumerate(items, start=1):
            episode_id = _episode_id(item.task_spec, item.state)
            capture_episode_id = episode_correlation_token(
                batch_id=group_id,
                episode_id=episode_id,
                member_index=index - 1,
            )
            records.append(
                {
                    "id": f"{episode_id}:batch:{index}",
                    "episode_id": episode_id,
                    "query": tag_miaoda_query(
                        _query(item.task_spec), capture_episode_id
                    ),
                    "app_type": item.task_spec.get("app_type") or "MINIPROGRAM",
                    "metadata": {
                        "episode_id": episode_id,
                        "split": item.task_spec.get("split") or "train",
                        "source_record_id": episode_id,
                    },
                }
            )
            members.append(
                _BatchMember(
                    episode_id=episode_id,
                    task_spec=item.task_spec,
                    state=item.state,
                    record_id=str(index),
                    capture_episode_id=capture_episode_id,
                )
            )
        payload = build_miaoda_e2e_batch_payload(
            records,
            task_name=task_name,
            generated_user_id=self.config.generated_user_id,
            model_name=self.config.model_name,
            model_api_url=_policy_url_for_episode(
                self.config.model_api_url,
                group_id,
                require_record_correlation=True,
            ),
            model_api_key=model_key,
            task_name_postfix=self.config.task_name_postfix,
            concurrency=self.config.concurrency,
            max_tokens=self.config.model_max_tokens,
            timeout_seconds=self.config.model_timeout_seconds,
            message_interval_timeout_seconds=(
                self.config.message_interval_timeout_seconds
            ),
            request_logprobs=self.config.request_logprobs,
            top_logprobs=self.config.request_top_logprobs,
            remaining_auto_retry_count=0,
        )
        try:
            snapshot = await asyncio.to_thread(
                self.datawheel.submit_batch,
                payload,
                idempotency_key=f"ors-miaoda-{group_id.removeprefix('batch-')}",
            )
        except (AgentOSClientError, OSError, ValueError) as exc:
            logger.error(
                "miaoda_datawheel_batch_submit_failed record_count=%d "
                "explicit_batch=%s error_type=%s http_status=%s",
                len(items),
                bool(optimizer_batch_id),
                type(exc).__name__,
                getattr(exc, "status_code", None),
            )
            error = MiaodaOnlineError("Datawheel batched rollout submission failed")
            error.__cause__ = exc
            for item in items:
                if not item.future.done():
                    item.future.set_exception(error)
            return
        assert self.config.policy_capture_dir is not None
        capture_file = self.config.policy_capture_dir / f"capture-{_capture_file_name(group_id)}"
        runtime = _BatchRuntime(
            group_id=group_id,
            batch_task_id=snapshot.batch_task_id,
            members=members,
            capture_file=capture_file,
            capture_started_at=min(
                float(member.state.get("policy_capture_started_at") or time.time())
                for member in members
            ),
            latest_snapshot=snapshot,
            latest_snapshot_at=time.monotonic(),
            active_episode_ids={member.episode_id for member in members},
        )
        self._batch_runtimes[snapshot.batch_task_id] = runtime
        for item, member in zip(items, members, strict=True):
            member_capture_file = self.config.policy_capture_dir / (
                f"capture-{_capture_file_name(member.capture_episode_id)}"
            )
            member.state.update(
                {
                    "phase": "rollout",
                    "miaoda_batch_task_id": snapshot.batch_task_id,
                    "miaoda_task_name": task_name,
                    "miaoda_record_id": member.record_id,
                    "miaoda_submission_group_id": group_id,
                    "miaoda_capture_episode_id": member.capture_episode_id,
                    "mopd_capture_file": str(member_capture_file),
                }
            )
            if optimizer_batch_id:
                member.state["miaoda_optimizer_batch_id"] = optimizer_batch_id
            if not item.future.done():
                item.future.set_result(
                    BackendResult(
                        blocks=[
                            TextBlock(
                                text=(
                                    "Miaoda rollout joined a batched Datawheel submission; "
                                    "waiting for AgentOS"
                                )
                            )
                        ],
                        finished=False,
                        metadata=self._metadata(member.state, status=snapshot.status),
                    )
                )

    async def _poll(self, task_spec: JSONObject, state: dict[str, Any]) -> BackendResult:
        if state.get("phase") == "terminal":
            return self._terminal_output(state)
        if state.get("miaoda_submission_group_id"):
            return await self._poll_batched(task_spec, state)
        batch_id = _safe_text(state.get("miaoda_batch_task_id"))
        if not batch_id:
            raise MiaodaOnlineError("call start_rollout before poll_rollout")
        if state.get("phase") == "rollout":
            try:
                batch = await asyncio.to_thread(self.datawheel.get_batch, batch_id)
            except AgentOSClientError as exc:
                raise MiaodaOnlineError("Datawheel status query failed") from exc
            status = batch.status.casefold()
            if status not in {
                "completed",
                "failed",
                "cancelled",
                "canceled",
                "terminated",
                "error",
            }:
                return BackendResult(
                    blocks=[TextBlock(text=f"Miaoda rollout status: {batch.status}")],
                    finished=False,
                    metadata=self._metadata(state, status=batch.status),
                )
            if status != "completed":
                return self._finish(
                    state,
                    reward=0.0,
                    usable=True,
                    reason="agentos_execution_failure",
                    text=f"Miaoda rollout ended with status {batch.status}",
                )
            destination = self.config.artifact_dir / f"datawheel-{batch_id}.xlsx"
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                await asyncio.to_thread(self.datawheel.download_result, batch_id, destination)
            except (AgentOSClientError, OSError) as exc:
                # In pure OPD the artifact is a join aid, not a terminal
                # reward gate.  The LTP-side materializer can retry the same
                # signed download (or a sidecar can place it on the shared
                # CFS path) after ORS has returned the durable batch id.
                # Keep the episode usable and preserve the expected path;
                # reward-backed callers retain the strict failure behavior.
                if not _task_reward_required(task_spec):
                    state["miaoda_artifact"] = str(destination)
                    return self._finish(
                        state,
                        reward=0.0,
                        usable=True,
                        task_reward_used=False,
                        reason="pure_opd_artifact_deferred",
                        text=(
                            "AgentOS completed; pure OPD deferred artifact download "
                            "to the materializer"
                        ),
                        error_type=type(exc).__name__,
                    )
                return self._finish(
                    state,
                    reward=None,
                    usable=False,
                    reason="rollout_artifact_unavailable",
                    text="AgentOS completed but its result artifact is unavailable",
                    error_type=type(exc).__name__,
                )
            _capture_datawheel_ids(destination, task_spec=task_spec, state=state)
            state["miaoda_artifact"] = str(destination)
            if not _task_reward_required(task_spec):
                return self._finish(
                    state,
                    reward=0.0,
                    usable=True,
                    task_reward_used=False,
                    reason="pure_opd_no_task_reward",
                    text=(
                        "AgentOS rollout artifact is ready; pure OPD does not "
                        "wait for a task reward"
                    ),
                )
            try:
                reward_options: dict[str, Any] = {
                    "concurrency": self.config.concurrency,
                    "prompt_version": self.config.reward_prompt_version,
                    "reward_model": self.config.reward_model,
                    "query_round": self.config.reward_query_round,
                    "eval_mm": self.config.reward_eval_mm,
                    "eval_miniprog": self.config.reward_eval_miniprog,
                }
                if self.config.reward_image:
                    reward_options["image"] = self.config.reward_image
                if not self.config.reward_upload:
                    workdir_root = (
                        self.config.reward_workdir_prefix
                        or self.config.reward_result_root
                        or self.config.artifact_dir
                    )
                    # Goblin writes per-case files directly below
                    # ``workdir_prefix``.  Isolate every Datawheel batch so a
                    # resumed job cannot consume another episode's
                    # ``rollout.msg`` or stale rating.
                    workdir = workdir_root / f"goblin-{batch_id}"
                    workdir.mkdir(parents=True, exist_ok=True)
                    reward_options["workdir_prefix"] = str(workdir)
                    reward_options["result_file"] = str(workdir / "result.jsonl")
                    reward_options["log_dir"] = str(workdir / "logs")
                # Goblin's current JSON job endpoint accepts the original
                # Datawheel workbook.  Keeping XLSX preserves the published
                # app/replay URL columns that its prevalidator needs; the
                # older JSONL normalization drops those aliases and is only a
                # compatibility helper for deployments that explicitly need
                # it.
                reward_input = destination
                job = await asyncio.to_thread(
                    self.goblin.submit_file,
                    reward_input,
                    rollout_model=self.config.model_name,
                    benchmark_dataset_name=self.config.benchmark_dataset_name,
                    upload=self.config.reward_upload,
                    **reward_options,
                )
            except GoblinRewardError as exc:
                return self._finish(
                    state,
                    reward=None,
                    usable=False,
                    reason="reward_submission_failure",
                    text="AgentOS completed but Goblin submission failed",
                    error_type=type(exc).__name__,
                )
            state.update(
                {
                    "phase": "reward",
                    "goblin_job_id": job.job_id,
                    "goblin_workdir": str(workdir) if not self.config.reward_upload else "",
                }
            )
            return BackendResult(
                blocks=[TextBlock(text="AgentOS completed; Goblin reward submitted")],
                finished=False,
                metadata=self._metadata(state, status=job.status),
            )

        if state.get("phase") == "reward":
            job_id = _safe_text(state.get("goblin_job_id"))
            try:
                job = await asyncio.to_thread(self.goblin.get_job, job_id)
            except GoblinRewardError as exc:
                return self._finish(
                    state,
                    reward=None,
                    usable=False,
                    reason="reward_status_failure",
                    text="Goblin status query failed",
                    error_type=type(exc).__name__,
                )
            if job.status in {"pending", "running"}:
                return BackendResult(
                    blocks=[TextBlock(text=f"Goblin reward status: {job.status}")],
                    finished=False,
                    metadata=self._metadata(state, status=job.status),
                )
            if job.status in {"failed", "cancelled"}:
                # Evaluator infrastructure failure is invalid, not a zero
                # quality score; the trainer must drop/retry this episode.
                return self._finish(
                    state,
                    reward=None,
                    usable=False,
                    reason="reward_infrastructure_failure",
                    text=f"Goblin reward ended with status {job.status}",
                )
            return self._resolve_reward(task_spec, state, job.result_file)

        raise MiaodaOnlineError(f"unknown Miaoda phase: {state.get('phase')!r}")

    async def _poll_batched(
        self, task_spec: JSONObject, state: dict[str, Any]
    ) -> BackendResult:
        batch_id = _safe_text(state.get("miaoda_batch_task_id"))
        runtime = self._batch_runtimes.get(batch_id)
        if runtime is None:
            raise MiaodaOnlineError("Miaoda batched rollout state is unavailable")

        async with runtime.lock:
            now = time.monotonic()
            snapshot = runtime.latest_snapshot
            terminal = snapshot is not None and str(snapshot.status).casefold() in {
                "completed",
                "failed",
                "cancelled",
                "canceled",
                "terminated",
                "error",
            }
            if (
                not terminal
                and now - runtime.latest_snapshot_at
                >= self.config.datawheel_poll_cache_seconds
            ):
                try:
                    snapshot = await asyncio.to_thread(self.datawheel.get_batch, batch_id)
                except AgentOSClientError as exc:
                    raise MiaodaOnlineError("Datawheel batched status query failed") from exc
                runtime.latest_snapshot = snapshot
                runtime.latest_snapshot_at = now

        assert snapshot is not None
        status = str(snapshot.status).casefold()
        if status not in {
            "completed",
            "failed",
            "cancelled",
            "canceled",
            "terminated",
            "error",
        }:
            return BackendResult(
                blocks=[TextBlock(text=f"Miaoda batched rollout status: {snapshot.status}")],
                finished=False,
                metadata=self._metadata(state, status=snapshot.status),
            )
        if status != "completed":
            return self._finish(
                state,
                reward=0.0,
                usable=False,
                task_reward_used=False,
                reason="agentos_execution_failure",
                text=f"Miaoda batched rollout ended with status {snapshot.status}",
            )

        await self._prepare_batched_artifact(runtime)
        if runtime.rows is not None:
            _capture_datawheel_ids_from_rows(
                runtime.rows, task_spec=task_spec, state=state
            )
        if runtime.artifact is not None:
            state["miaoda_artifact"] = str(runtime.artifact)

        calls = (runtime.policy_calls or {}).get(_safe_text(state.get("episode_id")), [])
        if calls:
            state["policy_calls"] = calls
            return self._finish(
                state,
                reward=0.0,
                usable=True,
                task_reward_used=False,
                reason="pure_opd_no_task_reward",
                text="AgentOS batched rollout and policy-token join are ready",
            )

        if runtime.join_started_at <= 0:
            runtime.join_started_at = time.monotonic()
        elapsed = time.monotonic() - runtime.join_started_at
        if elapsed < self.config.policy_capture_join_timeout_seconds:
            # Policy writes and AgentOS trajectory materialization can lag the
            # Datawheel terminal status briefly. Keep the ORS episode alive so
            # the next poll retries the deterministic join.
            runtime.policy_calls = None
            return BackendResult(
                blocks=[TextBlock(text="Miaoda rollout completed; policy-token join pending")],
                finished=False,
                metadata=self._metadata(state, status="capture_join_pending"),
            )
        return self._finish(
            state,
            reward=0.0,
            usable=False,
            task_reward_used=False,
            reason="policy_capture_join_failure",
            text="AgentOS completed but policy tokens could not be joined to this trajectory",
        )

    async def _prepare_batched_artifact(self, runtime: _BatchRuntime) -> None:
        async with runtime.lock:
            if runtime.rows is None:
                list_records = getattr(self.datawheel, "list_batch_records", None)
                if callable(list_records):
                    try:
                        records = await asyncio.to_thread(
                            list_records,
                            runtime.batch_task_id,
                        )
                    except (AgentOSClientError, OSError, TypeError, ValueError):
                        records = []
                    if len(records) == len(runtime.members):
                        runtime.rows = records
            if runtime.rows is None:
                destination = (
                    self.config.artifact_dir
                    / f"datawheel-{runtime.batch_task_id}.xlsx"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if not destination.is_file() or destination.stat().st_size <= 0:
                        await asyncio.to_thread(
                            self.datawheel.download_result,
                            runtime.batch_task_id,
                            destination,
                        )
                    rows = await asyncio.to_thread(load_reward_records, destination)
                except (AgentOSClientError, GoblinRewardError, OSError, ValueError):
                    if runtime.join_started_at <= 0:
                        runtime.join_started_at = time.monotonic()
                    return
                runtime.artifact = destination
                runtime.rows = rows
            if runtime.policy_calls is not None:
                return
            runtime.policy_calls = await self._join_batched_policy_calls(runtime)

    async def _join_batched_policy_calls(
        self, runtime: _BatchRuntime
    ) -> dict[str, list[dict[str, Any]]]:
        assert runtime.rows is not None
        assert self.config.policy_capture_dir is not None
        direct_candidates: dict[
            str,
            tuple[list[dict[str, Any]], list[dict[str, Any]]],
        ] = {}
        for member in runtime.members:
            path = self.config.policy_capture_dir / (
                f"capture-{_capture_file_name(member.capture_episode_id)}"
            )
            http_path = self.config.policy_capture_dir / (
                f"http-{_capture_file_name(member.capture_episode_id)}"
            )
            records = _load_policy_capture_records(
                path,
                capture_episode_id=member.capture_episode_id,
                started_at=runtime.capture_started_at,
            )
            calls = [
                call
                for record in records
                if (call := _policy_call_from_capture(record)) is not None
            ]
            statuses = _load_policy_http_statuses(
                http_path,
                capture_episode_id=member.capture_episode_id,
                started_at=runtime.capture_started_at,
            )
            member.state["miaoda_policy_http_statuses"] = statuses
            if (
                calls
                and len(statuses) == len(calls)
                and all(200 <= value < 300 for value in statuses)
            ):
                direct_candidates[member.episode_id] = (records, calls)
        if direct_candidates:
            semaphore = asyncio.Semaphore(self.config.trajectory_join_concurrency)

            async def exact_direct_calls(
                member: _BatchMember,
            ) -> list[dict[str, Any]]:
                candidate = direct_candidates.get(member.episode_id)
                if candidate is None:
                    return []
                records, calls = candidate
                capture_signatures = Counter(
                    (
                        _safe_text(record.get("prompt_sha256")),
                        _safe_text(record.get("response_message_sha256")),
                    )
                    for record in records
                )
                if not capture_signatures or any(
                    not all(signature) for signature in capture_signatures
                ):
                    return []
                row = _first_row_for_episode(
                    runtime.rows or [],
                    task_spec=member.task_spec,
                    state={
                        "episode_id": member.episode_id,
                        "miaoda_record_id": member.record_id,
                    },
                )
                if row is None:
                    return []
                record_status = _safe_text(
                    row.get("status") or row.get("状态")
                ).casefold()
                if record_status and record_status not in {
                    "completed",
                    "complete",
                    "success",
                    "succeeded",
                }:
                    return []
                for app_id in _trajectory_app_ids(row):
                    async with semaphore:
                        try:
                            trajectory = await asyncio.to_thread(
                                self.datawheel.get_trajectory,
                                app_id,
                            )
                        except (AgentOSClientError, OSError, TypeError, ValueError):
                            continue
                    trajectory_signatures = Counter(
                        _trajectory_policy_signatures(trajectory)
                    )
                    if all(
                        trajectory_signatures[signature] >= count
                        for signature, count in capture_signatures.items()
                    ):
                        member.state["miaoda_policy_join_method"] = "exact_hash"
                        return calls
                    # Datawheel's exported trajectory decorates historical
                    # messages with fields that were not present in the
                    # actual policy request.  That makes a full-message hash
                    # differ even though the per-record correlation marker
                    # reached the policy proxy correctly.  For an explicitly
                    # completed record, the deterministic case-id mapping and
                    # exact call-count parity still form an unambiguous join.
                    # Retry pollution remains rejected because it necessarily
                    # adds capture calls that are absent from the final app
                    # trajectory.
                    if (
                        record_status
                        in {"completed", "complete", "success", "succeeded"}
                        and len(trajectory_signatures) == len(records)
                    ):
                        member.state["miaoda_policy_join_method"] = (
                            "record_correlation_call_count"
                        )
                        return calls
                return []

            direct_values = await asyncio.gather(
                *(exact_direct_calls(member) for member in runtime.members)
            )
            direct = {
                member.episode_id: calls
                for member, calls in zip(
                    runtime.members, direct_values, strict=True
                )
                if calls
            }
            # Per-record correlation makes partial environment failure
            # unambiguous. Preserve usable siblings and let only members with
            # no exact capture time out as untrainable episodes.
            if direct:
                return direct

        # Compatibility fallback for historical batches created before the
        # per-record transport marker existed.
        records = _load_policy_capture_records(
            runtime.capture_file,
            capture_episode_id=runtime.group_id,
            started_at=runtime.capture_started_at,
        )
        if not records:
            return {}

        semaphore = asyncio.Semaphore(self.config.trajectory_join_concurrency)

        async def signatures(member: _BatchMember) -> list[tuple[str, str]]:
            row = _first_row_for_episode(
                runtime.rows or [],
                task_spec=member.task_spec,
                state={
                    "episode_id": member.episode_id,
                    "miaoda_record_id": member.record_id,
                },
            )
            if row is None:
                return []
            for app_id in _trajectory_app_ids(row):
                async with semaphore:
                    try:
                        trajectory = await asyncio.to_thread(
                            self.datawheel.get_trajectory, app_id
                        )
                    except (AgentOSClientError, OSError, TypeError, ValueError):
                        continue
                found = _trajectory_policy_signatures(trajectory)
                if found:
                    return found
            return []

        member_signatures = await asyncio.gather(
            *(signatures(member) for member in runtime.members)
        )
        buckets: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
        for record in records:
            signature = (
                _safe_text(record.get("prompt_sha256")),
                _safe_text(record.get("response_message_sha256")),
            )
            if all(signature):
                buckets[signature].append(record)

        assigned: dict[str, list[dict[str, Any]]] = {}
        for member, signatures_for_member in zip(
            runtime.members, member_signatures, strict=True
        ):
            calls: list[dict[str, Any]] = []
            for signature in signatures_for_member:
                if not buckets[signature]:
                    continue
                call = _policy_call_from_capture(buckets[signature].popleft())
                if call is not None:
                    calls.append(call)
            if calls:
                assigned[member.episode_id] = calls
        return assigned

    def _resolve_reward(
        self,
        task_spec: JSONObject,
        state: dict[str, Any],
        result_file: str | None,
    ) -> BackendResult:
        if not result_file:
            return self._finish(
                state,
                reward=None,
                usable=False,
                reason="reward_result_file_missing",
                text="Goblin completed without a result file",
            )
        source: str | Path = result_file
        result_path = Path(result_file)
        if not result_path.is_absolute() and self.config.reward_result_root is not None:
            source = self.config.reward_result_root / result_path
        try:
            rewards = reward_map_from_file(
                source,
                normalize_scale=self.config.reward_scale,
            )
        except (GoblinRewardError, OSError) as exc:
            rating = _rating_evidence(
                result_file=source,
                workdir=_safe_text(state.get("goblin_workdir")) or None,
            )
            if rating is not None:
                label, evidence_path = rating
                state["goblin_rating_evidence"] = str(evidence_path)
                return self._finish(
                    state,
                    reward=None,
                    reward_label=label,
                    usable=False,
                    reason="reward_label_only",
                    text="Miaoda rollout has a Goblin rating but no scalar reward",
                )
            return self._finish(
                state,
                reward=None,
                usable=False,
                reason="reward_result_unparseable",
                text="Goblin completed but its result could not be parsed",
                error_type=type(exc).__name__,
            )
        candidates = [
            _safe_text(state.get("episode_id")),
            _safe_text(task_spec.get("episode_id")),
            _safe_text(task_spec.get("case_id")),
            _safe_text(task_spec.get("app_id")),
            _safe_text(state.get("miaoda_app_id")),
            _safe_text(state.get("miaoda_conv_id")),
            _safe_text(state.get("miaoda_record_uuid")),
            _safe_text(state.get("miaoda_record_id")),
        ]
        for candidate in dict.fromkeys(item for item in candidates if item):
            if candidate in rewards:
                return self._finish(
                    state,
                    reward=rewards[candidate],
                    usable=True,
                    reason="goblin_reward",
                    text="Miaoda rollout reward is ready",
                )
        rating = _rating_evidence(
            result_file=source,
            workdir=_safe_text(state.get("goblin_workdir")) or None,
        )
        if rating is not None:
            label, evidence_path = rating
            state["goblin_rating_evidence"] = str(evidence_path)
            return self._finish(
                state,
                reward=None,
                reward_label=label,
                usable=False,
                reason="reward_label_only",
                text="Miaoda rollout has a Goblin rating but no scalar reward",
            )
        return self._finish(
            state,
            reward=None,
            usable=False,
            reason="reward_join_mismatch",
            text="Goblin result has no row matching this episode",
        )

    def _finish(
        self,
        state: dict[str, Any],
        *,
        reward: float | None,
        reward_label: str | None = None,
        usable: bool,
        task_reward_used: bool = True,
        reason: str,
        text: str,
        error_type: str | None = None,
    ) -> BackendResult:
        state.update(
            {
                "phase": "terminal",
                "terminal_reward": reward,
                "terminal_reward_label": reward_label,
                "terminal_usable": usable,
                "terminal_task_reward_used": task_reward_used,
                "terminal_reason": reason,
                "terminal_text": text,
                "terminal_error_type": error_type,
            }
        )
        return self._terminal_output(state)

    @staticmethod
    def _terminal_output(state: Mapping[str, Any]) -> BackendResult:
        metadata: dict[str, Any] = {
            "phase": "terminal",
            "usable": bool(state.get("terminal_usable", False)),
            "terminal_reason": _safe_text(state.get("terminal_reason")),
            "scalar_reward_available": state.get("terminal_reward") is not None,
            "task_reward_used": bool(state.get("terminal_task_reward_used", True)),
        }
        reward_label = _safe_text(state.get("terminal_reward_label"))
        if reward_label:
            metadata["reward_label"] = reward_label
        for key in (
            "episode_id",
            "miaoda_batch_task_id",
            "miaoda_task_name",
            "miaoda_record_id",
            "miaoda_app_id",
            "miaoda_conv_id",
            "miaoda_record_uuid",
            "miaoda_submission_group_id",
            "miaoda_capture_episode_id",
            "miaoda_optimizer_batch_id",
            "goblin_job_id",
            "goblin_rating_evidence",
            "miaoda_artifact",
            "goblin_workdir",
            "mopd_capture_file",
            "terminal_error_type",
        ):
            value = _safe_text(state.get(key))
            if value:
                metadata[key] = value
        for key in (
            "miaoda_optimizer_batch_expected_count",
            "miaoda_optimizer_batch_member_index",
        ):
            value = state.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                metadata[key] = value
        statuses = state.get("miaoda_policy_http_statuses")
        if isinstance(statuses, list) and all(
            isinstance(value, int) and not isinstance(value, bool) for value in statuses
        ):
            metadata["miaoda_policy_http_statuses"] = list(statuses)
        capture_path = _safe_text(state.get("mopd_capture_file"))
        if bool(state.get("pure_opd")) and capture_path:
            embedded_calls = state.get("policy_calls")
            policy_calls = (
                list(embedded_calls)
                if isinstance(embedded_calls, list)
                else _load_policy_calls(
                    Path(capture_path),
                    episode_id=(
                        _safe_text(state.get("miaoda_capture_episode_id"))
                        or _safe_text(state.get("episode_id"))
                    ),
                    started_at=float(state.get("policy_capture_started_at") or 0.0),
                )
            )
            if policy_calls:
                metadata["policy_calls"] = policy_calls
                metadata["policy_call_count"] = len(policy_calls)
        return BackendResult(
            blocks=[
                TextBlock(text=_safe_text(state.get("terminal_text")) or "Miaoda episode terminal")
            ],
            reward=state.get("terminal_reward"),
            finished=True,
            metadata=metadata,
        )

    @staticmethod
    def _state_output(state: Mapping[str, Any], text: str) -> BackendResult:
        return BackendResult(
            blocks=[TextBlock(text=text)],
            finished=state.get("phase") == "terminal",
            reward=state.get("terminal_reward") if state.get("phase") == "terminal" else None,
            metadata={
                "phase": _safe_text(state.get("phase")),
                "episode_id": _safe_text(state.get("episode_id")),
            },
        )

    @staticmethod
    def _metadata(state: Mapping[str, Any], *, status: str) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "phase": _safe_text(state.get("phase")),
            "status": _safe_text(status),
        }
        for key in (
            "episode_id",
            "miaoda_batch_task_id",
            "miaoda_task_name",
            "miaoda_record_id",
            "miaoda_app_id",
            "miaoda_conv_id",
            "miaoda_record_uuid",
            "miaoda_submission_group_id",
            "miaoda_capture_episode_id",
            "miaoda_optimizer_batch_id",
            "goblin_job_id",
            "mopd_capture_file",
        ):
            value = _safe_text(state.get(key))
            if value:
                metadata[key] = value
        for key in (
            "miaoda_optimizer_batch_expected_count",
            "miaoda_optimizer_batch_member_index",
        ):
            value = state.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                metadata[key] = value
        statuses = state.get("miaoda_policy_http_statuses")
        if isinstance(statuses, list) and all(
            isinstance(value, int) and not isinstance(value, bool) for value in statuses
        ):
            metadata["miaoda_policy_http_statuses"] = list(statuses)
        return metadata

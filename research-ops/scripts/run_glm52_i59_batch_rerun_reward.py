#!/usr/bin/env python3
"""Monitor attached GLM-5.2 replicas, rerun one batch, then submit Reward.

The workflow is intentionally attach-only.  It never submits or deletes an
LTP job and it never removes a gateway endpoint.  Running the file without
``--execute`` only validates the manifest and prints a redacted plan.  The
``--execute`` mode is durable and uses a local state file plus an advisory
lock; external mutations are preceded by a state boundary so an interrupted
request is reconciled instead of replayed automatically.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from research_ops.agentos import (  # noqa: E402
    AgentOSBatchSnapshot,
    AgentOSClient,
    AgentOSClientError,
)
from research_ops.reward import (  # noqa: E402
    GoblinJobSnapshot,
    GoblinRewardClient,
    GoblinRewardError,
)

DEFAULT_MANIFEST = PROJECT_ROOT / "config/glm52-i59-batch-rerun-reward.yaml"
SCHEMA_VERSION = "miaoda.glm52.batch_rerun_reward.v1"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
SECRET_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|authorization|cookie|password|secret|token)(?:_|$)",
    flags=re.IGNORECASE,
)

BATCH_TERMINAL_STATUSES = {
    "completed",
    "complete",
    "succeeded",
    "success",
    "done",
    "failed",
    "failure",
    "error",
    "cancelled",
    "canceled",
    "stopped",
    "terminated",
}
BATCH_SUCCESS_STATUSES = {"completed", "complete", "succeeded", "success", "done"}
BATCH_FAILURE_STATUSES = {
    "failed",
    "failure",
    "error",
    "cancelled",
    "canceled",
    "stopped",
    "terminated",
}
RECORD_TERMINAL_STATUSES = {
    "completed",
    "complete",
    "succeeded",
    "success",
    "done",
    "failed",
    "failure",
    "error",
    "cancelled",
    "canceled",
    "stopped",
    "terminated",
    "skipped",
    "quarantined",
}
REWARD_SUCCESS_STATUSES = {"completed", "complete", "succeeded", "success", "done"}
REWARD_FAILURE_STATUSES = {"failed", "failure", "error", "cancelled", "canceled", "stopped"}


class WorkflowConfigurationError(ValueError):
    """Raised when a versioned workflow manifest is unsafe or incomplete."""


class WorkflowIneligible(RuntimeError):
    """Raised when an external terminal state is not formally retryable."""


class AmbiguousMutationError(RuntimeError):
    """Raised when a process died around a mutating remote request."""


class ServiceNotReady(RuntimeError):
    """Raised internally for a transient serving readiness mismatch."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_status(value: Any) -> str:
    return str(value or "unknown").strip().lower().replace("-", "_").replace(" ", "_")


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowConfigurationError(f"{name} must be an object")
    return dict(value)


def _text(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise WorkflowConfigurationError(f"{name} must not be empty")
    return result


def _positive_int(value: Any, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowConfigurationError(f"{name} must be an integer") from exc
    if result < 1:
        raise WorkflowConfigurationError(f"{name} must be positive")
    return result


def _nonnegative_int(value: Any, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowConfigurationError(f"{name} must be an integer") from exc
    if result < 0:
        raise WorkflowConfigurationError(f"{name} must not be negative")
    return result


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise WorkflowConfigurationError(f"{name} must be boolean")
    return value


def _absolute_path(value: Any, name: str) -> Path:
    path = Path(_text(value, name)).expanduser()
    if not path.is_absolute():
        raise WorkflowConfigurationError(f"{name} must be absolute")
    return path


def _url(value: Any, name: str) -> str:
    text = _text(value, name).rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WorkflowConfigurationError(f"{name} must be an http(s) URL")
    if parsed.username or parsed.password:
        raise WorkflowConfigurationError(f"{name} must not contain URL credentials")
    return text


def _env_name(value: Any, name: str) -> str:
    text = _text(value, name)
    if not ENV_NAME.fullmatch(text):
        raise WorkflowConfigurationError(f"{name} must name an uppercase environment variable")
    return text


def _reject_inline_secrets(value: Any, path: str = "manifest") -> None:
    """Reject secret-looking manifest values while allowing ``*_env`` refs."""

    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.casefold().replace("-", "_")
            if normalized.endswith("_env") and SECRET_KEY_PATTERN.search(normalized):
                if not isinstance(child, str) or not ENV_NAME.fullmatch(child.strip()):
                    raise WorkflowConfigurationError(
                        f"{path}.{key} must name an uppercase environment variable"
                    )
            elif (
                (normalized in SECRET_KEYS or SECRET_KEY_PATTERN.search(normalized))
                and child not in (None, "")
            ):
                raise WorkflowConfigurationError(
                    f"{path}.{key} must be an environment reference, not a value"
                )
            _reject_inline_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_inline_secrets(child, f"{path}[{index}]")


def load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    """Load and validate the attach-only workflow manifest."""

    source = Path(path).expanduser()
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise WorkflowConfigurationError(f"manifest cannot be read: {source}") from exc
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise WorkflowConfigurationError(f"manifest is not valid YAML: {source}") from exc
    manifest = _mapping(document, "manifest")
    _reject_inline_secrets(manifest)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise WorkflowConfigurationError("unsupported workflow schema_version")
    _text(manifest.get("pipeline_name"), "pipeline_name")

    runtime = _mapping(manifest.get("runtime"), "runtime")
    model = _mapping(manifest.get("model"), "model")
    serving = _mapping(manifest.get("serving"), "serving")
    rollout = _mapping(manifest.get("rollout"), "rollout")
    reward = _mapping(manifest.get("reward"), "reward")
    teardown = _mapping(manifest.get("teardown"), "teardown")

    output_root = _absolute_path(runtime.get("output_root"), "runtime.output_root")
    state_file = _absolute_path(
        runtime.get("state_file") or output_root / "state.json",
        "runtime.state_file",
    )
    result_file = _absolute_path(
        runtime.get("result_file") or output_root / "rollout_result.xlsx",
        "runtime.result_file",
    )
    for key in (
        "poll_interval_seconds",
        "service_timeout_seconds",
        "batch_timeout_seconds",
        "reward_timeout_seconds",
        "http_timeout_seconds",
        "ltp_command_timeout_seconds",
    ):
        _positive_int(runtime.get(key), f"runtime.{key}")
    runtime.update(
        {
            "output_root": str(output_root),
            "state_file": str(state_file),
            "result_file": str(result_file),
        }
    )

    model_name = _text(model.get("name"), "model.name")
    _absolute_path(model.get("artifact_path"), "model.artifact_path")
    model["artifact_success_marker"] = str(
        _absolute_path(model.get("artifact_success_marker"), "model.artifact_success_marker")
    )
    if _text(model.get("provider"), "model.provider") != "openai":
        raise WorkflowConfigurationError("model.provider must be openai for this GLM workflow")
    if (
        _text(model.get("coding_prompti_variant"), "model.coding_prompti_variant")
        != "qianfan-coder"
    ):
        raise WorkflowConfigurationError("model.coding_prompti_variant must be qianfan-coder")
    _env_name(model.get("api_key_env"), "model.api_key_env")
    model["gateway_control_url"] = _url(
        model.get("gateway_control_url"), "model.gateway_control_url"
    )
    model["gateway_api_url"] = _url(model.get("gateway_api_url"), "model.gateway_api_url")
    model["name"] = model_name

    ltp_cli = _text(serving.get("ltp_cli"), "serving.ltp_cli")
    ltp_user = _text(serving.get("ltp_user"), "serving.ltp_user")
    per_endpoint = _positive_int(
        serving.get("per_endpoint_concurrency"), "serving.per_endpoint_concurrency"
    )
    min_ready = _positive_int(serving.get("min_ready_endpoints"), "serving.min_ready_endpoints")
    max_ready = _positive_int(serving.get("max_ready_endpoints"), "serving.max_ready_endpoints")
    if min_ready > max_ready:
        raise WorkflowConfigurationError("serving.min_ready_endpoints exceeds max_ready_endpoints")
    raw_jobs = serving.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise WorkflowConfigurationError("serving.jobs must be a non-empty list")
    jobs: list[dict[str, Any]] = []
    seen_jobs: set[str] = set()
    for index, raw_job in enumerate(raw_jobs):
        job = _mapping(raw_job, f"serving.jobs[{index}]")
        name = _text(job.get("name"), f"serving.jobs[{index}].name")
        if not SAFE_NAME.fullmatch(name):
            raise WorkflowConfigurationError(
                f"serving.jobs[{index}].name contains unsafe characters"
            )
        if name in seen_jobs:
            raise WorkflowConfigurationError(f"duplicate serving job: {name}")
        seen_jobs.add(name)
        job["cluster"] = _text(job.get("cluster"), f"serving.jobs[{index}].cluster")
        job["driver_log"] = str(
            _absolute_path(job.get("driver_log"), f"serving.jobs[{index}].driver_log")
        )
        job["name"] = name
        jobs.append(job)
    if max_ready > len(jobs):
        raise WorkflowConfigurationError("serving.max_ready_endpoints exceeds serving.jobs")
    serving.update(
        {
            "ltp_cli": ltp_cli,
            "ltp_user": ltp_user,
            "per_endpoint_concurrency": per_endpoint,
            "min_ready_endpoints": min_ready,
            "max_ready_endpoints": max_ready,
            "jobs": jobs,
        }
    )
    smoke = _mapping(serving.get("smoke"), "serving.smoke")
    smoke["prompt"] = _text(smoke.get("prompt"), "serving.smoke.prompt")
    smoke["max_tokens"] = _positive_int(smoke.get("max_tokens"), "serving.smoke.max_tokens")
    try:
        smoke_temperature = float(smoke.get("temperature"))
    except (TypeError, ValueError) as exc:
        raise WorkflowConfigurationError("serving.smoke.temperature must be numeric") from exc
    if smoke_temperature < 0:
        raise WorkflowConfigurationError("serving.smoke.temperature must not be negative")
    smoke["temperature"] = smoke_temperature
    smoke["chat_template_kwargs"] = dict(
        _mapping(smoke.get("chat_template_kwargs"), "serving.smoke.chat_template_kwargs")
    )
    serving["smoke"] = smoke

    rollout["datawheel_url"] = _url(rollout.get("datawheel_url"), "rollout.datawheel_url")
    rollout["datawheel_token_env"] = _env_name(
        rollout.get("datawheel_token_env"), "rollout.datawheel_token_env"
    )
    rollout["batch_task_id"] = _text(rollout.get("batch_task_id"), "rollout.batch_task_id")
    if _bool(rollout.get("restart_all_records"), "rollout.restart_all_records") is not True:
        raise WorkflowConfigurationError("rollout.restart_all_records must be true")
    if _bool(rollout.get("skip_check"), "rollout.skip_check"):
        raise WorkflowConfigurationError("rollout.skip_check must be false")
    group_size = _positive_int(rollout.get("group_size"), "rollout.group_size")
    if group_size > 100:
        raise WorkflowConfigurationError("rollout.group_size exceeds the bounded limit of 100")
    page_size = _positive_int(rollout.get("record_page_size"), "rollout.record_page_size")
    if page_size > 1_000:
        raise WorkflowConfigurationError("rollout.record_page_size exceeds 1000")
    configured_max = _positive_int(
        rollout.get("configured_max_concurrency"), "rollout.configured_max_concurrency"
    )
    capacity_limit = per_endpoint * max_ready
    if configured_max < per_endpoint or configured_max > capacity_limit:
        raise WorkflowConfigurationError(
            "rollout.configured_max_concurrency must be within the serving capacity range"
        )
    rollout["group_size"] = group_size
    rollout["record_page_size"] = page_size
    rollout["configured_max_concurrency"] = configured_max
    rollout["resume_after_restart"] = _bool(
        rollout.get("resume_after_restart"), "rollout.resume_after_restart"
    )

    reward_enabled = _bool(reward.get("enabled"), "reward.enabled")
    if reward_enabled:
        reward["goblin_url"] = _url(reward.get("goblin_url"), "reward.goblin_url")
        reward["goblin_token_env"] = _env_name(
            reward.get("goblin_token_env"), "reward.goblin_token_env"
        )
        reward["benchmark_dataset_name"] = _text(
            reward.get("benchmark_dataset_name"), "reward.benchmark_dataset_name"
        )
        reward["rollout_model"] = _text(reward.get("rollout_model"), "reward.rollout_model")
        if reward["rollout_model"] != model_name:
            raise WorkflowConfigurationError("reward.rollout_model must equal model.name")
        reward["query_round"] = _text(reward.get("query_round"), "reward.query_round")
        reward["prompt_version"] = _text(reward.get("prompt_version"), "reward.prompt_version")
        reward["reward_model"] = _text(reward.get("reward_model"), "reward.reward_model")
        reward["image"] = _text(reward.get("image"), "reward.image")
        reward["concurrency"] = _positive_int(reward.get("concurrency"), "reward.concurrency")
        reward["timeout_seconds"] = _positive_int(
            reward.get("timeout_seconds"), "reward.timeout_seconds"
        )
        reward["max_retries"] = _nonnegative_int(reward.get("max_retries"), "reward.max_retries")
        reward["pod_retry"] = _nonnegative_int(reward.get("pod_retry"), "reward.pod_retry")
        reward["eval_mm"] = _bool(reward.get("eval_mm"), "reward.eval_mm")
        reward["eval_miniprog"] = _bool(reward.get("eval_miniprog"), "reward.eval_miniprog")
        reward["wait_for_completion"] = _bool(
            reward.get("wait_for_completion"), "reward.wait_for_completion"
        )
        reward["api_env"] = _text(reward.get("api_env"), "reward.api_env")
    if _bool(teardown.get("enabled"), "teardown.enabled") is not False:
        raise WorkflowConfigurationError(
            "teardown.enabled must be false; this attach-only workflow never tears down serving"
        )

    manifest["runtime"] = runtime
    manifest["model"] = model
    manifest["serving"] = serving
    manifest["rollout"] = rollout
    manifest["reward"] = reward
    manifest["teardown"] = teardown
    return manifest, hashlib.sha256(raw).hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_field(output: str, field: str) -> str | None:
    match = re.search(rf"^{re.escape(field)}:\s*(.+?)\s*$", output, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


@dataclass(frozen=True)
class LTPObservation:
    status: str
    submission_time: datetime | None
    error: str | None = None


def parse_ltp_observation(output: str, *, returncode: int = 0) -> LTPObservation:
    status = normalize_status(_parse_field(output, "status"))
    submission = _parse_datetime(_parse_field(output, "submission_time"))
    error = None if returncode == 0 else f"ltp_show_exit_{returncode}"
    return LTPObservation(status=status, submission_time=submission, error=error)


def _clean_ltp_env() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        environment.pop(name, None)
    return environment


class LTPStatusClient:
    """Read one exact LTP job identity without invoking lifecycle commands."""

    def __init__(
        self,
        *,
        command: str,
        user: str,
        timeout_seconds: int,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.command = command
        self.user = user
        self.timeout_seconds = timeout_seconds
        self.runner = runner or subprocess.run

    def show(self, job_name: str) -> LTPObservation:
        command = [self.command, "show", job_name, "--user", self.user]
        try:
            completed = self.runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=_clean_ltp_env(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return LTPObservation("unknown", None, "ltp_show_timeout")
        except (OSError, subprocess.SubprocessError) as exc:
            return LTPObservation("unknown", None, f"ltp_show_{type(exc).__name__}")
        return parse_ltp_observation(completed.stdout or "", returncode=completed.returncode)


@dataclass(frozen=True)
class DriverEvidence:
    endpoint: str | None
    ready_at: datetime | None
    reason: str | None = None


_DRIVER_HEADER = re.compile(
    r"stage=serving\s+replica=\S+\s+job=(?P<job>\S+)\s+"
    r"model=(?P<model>\S+)\s+endpoint=(?P<endpoint>\S+)"
)
_READY_MARKER = re.compile(r"stage=serving\s+status=ready\s+endpoint=(?P<endpoint>\S+)")


def _valid_endpoint(value: Any) -> str | None:
    text = str(value or "").strip().rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    return text


def _read_tail(path: Path, *, max_bytes: int = 2_000_000) -> str:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - max_bytes))
        return stream.read().decode("utf-8", errors="replace")


def _json_line(line: str) -> Mapping[str, Any] | None:
    start = line.find("{")
    end = line.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(line[start : end + 1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def inspect_driver_log(
    path: Path,
    *,
    job_name: str,
    model_name: str,
    submitted_at: datetime,
) -> DriverEvidence:
    """Find a fresh per-job ready marker and its preceding smoke evidence.

    The shared ``dev1/SERVING_READY.json`` is intentionally ignored.  Current
    run.sh writes a JSON smoke receipt followed by ``status=ready`` in each
    job-specific driver log; requiring the receipt timestamp to be newer than
    the exact LTP submission rejects stale logs from reclaimed jobs.
    """

    try:
        text = _read_tail(Path(path))
    except FileNotFoundError:
        return DriverEvidence(None, None, "driver_log_missing")
    except (OSError, UnicodeError):
        return DriverEvidence(None, None, "driver_log_unreadable")

    header_endpoint: str | None = None
    ready_markers: list[tuple[int, str]] = []
    evidence: list[tuple[int, Mapping[str, Any]]] = []
    stale_seen = False
    for line_number, line in enumerate(text.splitlines()):
        header = _DRIVER_HEADER.search(line)
        if header and header.group("job") == job_name and header.group("model") == model_name:
            header_endpoint = _valid_endpoint(header.group("endpoint"))
        ready = _READY_MARKER.search(line)
        if ready:
            endpoint = _valid_endpoint(ready.group("endpoint"))
            if endpoint:
                ready_markers.append((line_number, endpoint))
        item = _json_line(line)
        if item is not None:
            evidence.append((line_number, item))

    if not ready_markers:
        return DriverEvidence(header_endpoint, None, "ready_marker_missing")

    candidates: list[tuple[datetime, str, Mapping[str, Any]]] = []
    for evidence_line, item in evidence:
        if str(item.get("actual_job") or "").strip() != job_name:
            continue
        if str(item.get("model_name") or "").strip() != model_name:
            continue
        endpoint = _valid_endpoint(item.get("endpoint"))
        observed_at = _parse_datetime(item.get("observed_at_utc") or item.get("ready_at_utc"))
        matching_markers = [
            marker_line
            for marker_line, marker_endpoint in ready_markers
            if marker_endpoint == endpoint and marker_line >= evidence_line
        ]
        if endpoint is None or observed_at is None or not matching_markers:
            continue
        if observed_at < submitted_at:
            stale_seen = True
            continue
        if header_endpoint and endpoint != header_endpoint:
            continue
        for key in ("local_generation_ok", "gateway_generation_ok"):
            if key in item and not bool(item.get(key)):
                break
        else:
            if "gateway_endpoint_count" in item:
                try:
                    if int(item["gateway_endpoint_count"]) < 1:
                        continue
                except (TypeError, ValueError):
                    continue
            candidates.append((observed_at, endpoint, item))

    if not candidates:
        reason = "ready_evidence_predates_submission" if stale_seen else "ready_evidence_missing"
        return DriverEvidence(header_endpoint, None, reason)
    ready_at, endpoint, _ = max(candidates, key=lambda item: item[0])
    return DriverEvidence(endpoint, ready_at, None)


class GatewayProbe:
    """Read-only health, endpoint-list, and bounded generation checks."""

    def __init__(
        self,
        *,
        model_name: str,
        control_url: str,
        api_url: str,
        smoke: Mapping[str, Any],
        timeout_seconds: int,
        api_key: str = "",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model_name = model_name
        self.control_url = control_url.rstrip("/")
        self.api_url = api_url.rstrip("/")
        self.smoke = dict(smoke)
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key.strip()
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            token = self.api_key
            if token.casefold().startswith("bearer "):
                token = token.split(None, 1)[1].strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers = {**self._headers(), **headers}
        with httpx.Client(
            timeout=self.timeout_seconds,
            verify=True,
            trust_env=False,
            transport=self.transport,
        ) as client:
            return client.request(method, url, headers=headers, **kwargs)

    def endpoints(self) -> list[str]:
        try:
            response = self._request(
                "GET",
                f"{self.control_url}/proxy/endpoint/list",
                params={"model_name": self.model_name},
            )
        except httpx.HTTPError as exc:
            raise ServiceNotReady(f"gateway_endpoint_list_{type(exc).__name__}") from exc
        if response.status_code == 400 and any(
            marker in response.text.casefold() for marker in ("not exists", "not found")
        ):
            return []
        if response.status_code < 200 or response.status_code >= 300:
            raise ServiceNotReady(f"gateway_endpoint_list_http_{response.status_code}")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise ServiceNotReady("gateway_endpoint_list_invalid_json") from exc
        if not isinstance(payload, Mapping):
            raise ServiceNotReady("gateway_endpoint_list_invalid_payload")
        data = payload.get("data")
        if isinstance(data, Mapping):
            items = data.get("endpoints") or data.get("items") or []
        else:
            items = payload.get("endpoints") or []
        if not isinstance(items, list):
            raise ServiceNotReady("gateway_endpoint_list_invalid_items")
        result: list[str] = []
        for item in items:
            value = item.get("endpoint") if isinstance(item, Mapping) else item
            endpoint = _valid_endpoint(value)
            if endpoint:
                result.append(endpoint)
        return list(dict.fromkeys(result))

    def probe(self, endpoint: str) -> dict[str, Any]:
        endpoint = _valid_endpoint(endpoint) or ""
        if not endpoint:
            raise ServiceNotReady("endpoint_invalid")
        try:
            health = self._request("GET", f"{endpoint}/health")
        except httpx.HTTPError as exc:
            raise ServiceNotReady(f"direct_health_{type(exc).__name__}") from exc
        if health.status_code != 200:
            raise ServiceNotReady(f"direct_health_http_{health.status_code}")
        registered = self.endpoints()
        if endpoint not in registered:
            raise ServiceNotReady("gateway_endpoint_missing")
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": self.smoke["prompt"]}],
            "max_tokens": int(self.smoke["max_tokens"]),
            "temperature": float(self.smoke["temperature"]),
            "chat_template_kwargs": dict(self.smoke["chat_template_kwargs"]),
        }
        try:
            generated = self._request(
                "POST",
                f"{self.api_url}/v1/chat/completions",
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise ServiceNotReady(f"gateway_generation_{type(exc).__name__}") from exc
        if generated.status_code != 200:
            raise ServiceNotReady(f"gateway_generation_http_{generated.status_code}")
        try:
            message = generated.json()["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ServiceNotReady("gateway_generation_invalid") from exc
        if not isinstance(message, Mapping) or not (
            message.get("content") or message.get("reasoning_content")
        ):
            raise ServiceNotReady("gateway_generation_empty")
        return {
            "direct_health_http_status": health.status_code,
            "gateway_generation_http_status": generated.status_code,
            "registered_endpoint_count": len(registered),
        }


class ServingMonitor:
    """Poll the exact attached jobs and return credential-free observations."""

    def __init__(
        self,
        manifest: Mapping[str, Any],
        *,
        gateway: GatewayProbe,
        ltp: LTPStatusClient,
    ) -> None:
        self.manifest = dict(manifest)
        self.model = _mapping(manifest["model"], "model")
        self.serving = _mapping(manifest["serving"], "serving")
        self.gateway = gateway
        self.ltp = ltp

    def poll(self) -> dict[str, Any]:
        jobs: list[dict[str, Any]] = []
        ready_endpoints: set[str] = set()
        for configured in self.serving["jobs"]:
            job = dict(configured)
            name = str(job["name"])
            observation: dict[str, Any] = {
                "job_name": name,
                "cluster": str(job["cluster"]),
                "ready": False,
                "endpoint": None,
                "ready_at_utc": None,
                "reason": None,
            }
            ltp = self.ltp.show(name)
            observation["status"] = ltp.status
            observation["submission_time_utc"] = _format_datetime(ltp.submission_time)
            if ltp.error:
                observation["reason"] = ltp.error
                jobs.append(observation)
                continue
            if ltp.status != "running":
                observation["reason"] = f"ltp_status_{ltp.status}"
                jobs.append(observation)
                continue
            if ltp.submission_time is None:
                observation["reason"] = "ltp_submission_time_missing"
                jobs.append(observation)
                continue
            evidence = inspect_driver_log(
                Path(str(job["driver_log"])),
                job_name=name,
                model_name=str(self.model["name"]),
                submitted_at=ltp.submission_time,
            )
            observation["endpoint"] = evidence.endpoint
            if evidence.ready_at is None:
                observation["reason"] = evidence.reason or "ready_evidence_missing"
                jobs.append(observation)
                continue
            try:
                gateway_evidence = self.gateway.probe(str(evidence.endpoint))
            except (ServiceNotReady, OSError, httpx.HTTPError) as exc:
                observation["reason"] = str(exc)[:180] or type(exc).__name__
                jobs.append(observation)
                continue
            observation.update(
                {
                    "ready": True,
                    "ready_at_utc": _format_datetime(evidence.ready_at),
                    "reason": None,
                    "gateway": gateway_evidence,
                }
            )
            ready_endpoints.add(str(evidence.endpoint))
            jobs.append(observation)
        return {
            "observed_at_utc": _now(),
            "model_name": str(self.model["name"]),
            "ready_count": len(ready_endpoints),
            "ready_endpoints": sorted(ready_endpoints),
            "ready_job_count": sum(1 for item in jobs if item.get("ready")),
            "requested_job_count": len(jobs),
            "degraded": bool(ready_endpoints) and len(ready_endpoints) < len(jobs),
            "jobs": jobs,
        }


def batch_phase(snapshot: AgentOSBatchSnapshot, *, expected_total: int | None = None) -> str:
    """Classify a batch using record counts, never only the provider label."""

    counts = {
        normalize_status(key): int(value)
        for key, value in snapshot.status_counts.items()
        if isinstance(value, (int, float))
    }
    active = sum(value for key, value in counts.items() if key not in RECORD_TERMINAL_STATUSES)
    provider_status = normalize_status(snapshot.status)
    provider_terminal = provider_status in BATCH_TERMINAL_STATUSES or provider_status.startswith(
        ("completed_", "succeeded_", "failed_", "cancelled_", "canceled_", "terminated_")
    )
    if provider_terminal and active > 0:
        return "mixed_terminal"
    if active > 0:
        return "running"
    total = snapshot.total_size or sum(counts.values())
    terminal_count = sum(value for key, value in counts.items() if key in RECORD_TERMINAL_STATUSES)
    if total <= 0 or terminal_count < total:
        return "running"
    if expected_total is not None and total != expected_total:
        return "count_mismatch"
    if provider_status in BATCH_FAILURE_STATUSES or provider_status.startswith(
        ("failed_", "error_", "cancelled_", "canceled_", "terminated_")
    ):
        return "failed"
    if provider_status in BATCH_SUCCESS_STATUSES or provider_status == "running":
        return "completed"
    return "running"


def _record_id(record: Mapping[str, Any]) -> str:
    for key in ("id", "record_id", "recordId"):
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _safe_mapping_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"response_type": type(value).__name__}
    return {
        "response_type": type(value).__name__,
        "response_keys": sorted(str(key) for key in value)[:80],
    }


def _safe_goblin_job(snapshot: GoblinJobSnapshot) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "job_id": snapshot.job_id,
            "status": snapshot.status,
            "workdir_prefix": snapshot.workdir_prefix,
            "log_dir": snapshot.log_dir,
            "result_file": snapshot.result_file,
            "total": snapshot.total,
            "completed": snapshot.completed,
            "succeeded": snapshot.succeeded,
            "failed": snapshot.failed,
        }.items()
        if value is not None
    }


def _remote_concurrency(snapshot: AgentOSBatchSnapshot) -> int | None:
    for key in ("concurrency", "current_concurrency", "max_concurrency"):
        value = snapshot.raw.get(key)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


class BatchRerunRewardWorkflow:
    """Durable state machine for readiness -> rerun-all -> Reward."""

    def __init__(
        self,
        manifest: Mapping[str, Any],
        manifest_sha256: str,
        *,
        datawheel: AgentOSClient,
        goblin: GoblinRewardClient | None,
        monitor: ServingMonitor,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        self.manifest = dict(manifest)
        self.manifest_sha256 = manifest_sha256
        self.runtime = _mapping(manifest["runtime"], "runtime")
        self.model = _mapping(manifest["model"], "model")
        self.serving = _mapping(manifest["serving"], "serving")
        self.rollout = _mapping(manifest["rollout"], "rollout")
        self.reward = _mapping(manifest["reward"], "reward")
        self.datawheel = datawheel
        self.goblin = goblin
        self.monitor = monitor
        self.sleep = sleep
        self.log = log
        self.state_path = Path(str(self.runtime["state_file"]))
        self.state = self._load_state()

    def _initial_state(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "pipeline_name": str(self.manifest["pipeline_name"]),
            "manifest_sha256": self.manifest_sha256,
            "batch_task_id": str(self.rollout["batch_task_id"]),
            "model_name": str(self.model["name"]),
            "phase": "waiting_for_service",
            "rerun_started": False,
            "record_ids": [],
            "record_count": 0,
            "restart_next_index": 0,
            "restart_groups": [],
            "applied_concurrency": 0,
            "max_ready_count": 0,
            "created_at_utc": _now(),
            "updated_at_utc": _now(),
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return self._initial_state()
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid workflow state file: {self.state_path}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("workflow state must be a JSON object")
        for key, expected in (
            ("schema_version", SCHEMA_VERSION),
            ("pipeline_name", str(self.manifest["pipeline_name"])),
            ("manifest_sha256", self.manifest_sha256),
            ("batch_task_id", str(self.rollout["batch_task_id"])),
            ("model_name", str(self.model["name"])),
        ):
            if str(value.get(key) or "") != expected:
                raise RuntimeError(f"workflow state {key} does not match the manifest")
        return value

    def _save(self) -> None:
        self.state["updated_at_utc"] = _now()
        _atomic_write_json(self.state_path, self.state)

    def _log_state(self, message: str) -> None:
        self.log(message[:500])

    def _require_runtime_token(self, env_name: str) -> None:
        if not os.environ.get(env_name, "").strip():
            raise WorkflowConfigurationError(
                f"required runtime token is unavailable; configure environment variable {env_name}"
            )

    def _validate_artifact_receipt(self) -> None:
        marker = Path(str(self.model["artifact_success_marker"]))
        try:
            payload = marker.read_bytes()
        except OSError as exc:
            raise WorkflowConfigurationError(
                f"validated artifact marker is unavailable: {marker}"
            ) from exc
        if not payload:
            raise WorkflowConfigurationError(f"validated artifact marker is empty: {marker}")
        self.state["artifact_receipt"] = {
            "path": str(marker),
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "observed_at_utc": _now(),
        }
        self._save()

    def _record_control_error(self, exc: BaseException) -> None:
        self.state["last_control_error"] = {
            "error_type": type(exc).__name__,
            "observed_at_utc": _now(),
        }
        self._save()
        self._log_state(f"transient AgentOS control error: {type(exc).__name__}")

    def _service_snapshot(self) -> dict[str, Any]:
        snapshot = self.monitor.poll()
        ready_count = int(snapshot.get("ready_count") or 0)
        previous_max = int(self.state.get("max_ready_count") or 0)
        self.state["service"] = snapshot
        self.state["ready_count"] = ready_count
        self.state["max_ready_count"] = min(
            int(self.serving["max_ready_endpoints"]), max(previous_max, ready_count)
        )
        self.state["service_polls"] = int(self.state.get("service_polls") or 0) + 1
        self._save()
        self._log_state(
            f"services ready={ready_count} cumulative={self.state['max_ready_count']} "
            f"endpoints={json.dumps(snapshot.get('ready_endpoints') or [])}"
        )
        return snapshot

    def _target_capacity(self) -> int:
        cumulative = int(self.state.get("max_ready_count") or 0)
        return min(
            int(self.rollout["configured_max_concurrency"]),
            cumulative * int(self.serving["per_endpoint_concurrency"]),
        )

    def _ensure_capacity(self) -> None:
        target = self._target_capacity()
        if target < 1:
            return
        applied = int(self.state.get("applied_concurrency") or 0)
        if target <= applied:
            return
        snapshot = self.datawheel.get_batch(str(self.rollout["batch_task_id"]))
        remote = _remote_concurrency(snapshot)
        if remote is not None and remote >= target:
            self.state["applied_concurrency"] = remote
            self._save()
            return
        pending = {
            "kind": "update_concurrency",
            "target": target,
            "started_at_utc": _now(),
        }
        self.state["pending_mutation"] = pending
        self.state["phase"] = "updating_concurrency"
        self._save()
        try:
            response = self.datawheel.update_concurrency(
                str(self.rollout["batch_task_id"]), target
            )
        except Exception as exc:
            raise AmbiguousMutationError(
                f"update_concurrency outcome is ambiguous for target {target}"
            ) from exc
        self.state.pop("pending_mutation", None)
        self.state["applied_concurrency"] = target
        self.state["last_concurrency_update"] = {
            "target": target,
            "accepted_at_utc": _now(),
            **_safe_mapping_metadata(response),
        }
        self._save()
        self._log_state(f"batch concurrency increased to {target}")

    def _freeze_record_ids(self) -> list[str]:
        existing = [str(value).strip() for value in self.state.get("record_ids") or []]
        if existing:
            if len(existing) != len(set(existing)) or any(not value for value in existing):
                raise RuntimeError("workflow state contains invalid duplicate record IDs")
            self.state["record_count"] = len(existing)
            self._save()
            return existing
        records = self.datawheel.list_batch_records(
            str(self.rollout["batch_task_id"]),
            page_size=int(self.rollout["record_page_size"]),
        )
        if not records:
            raise RuntimeError("Datawheel returned no records for the requested batch")
        ids = [_record_id(record) for record in records]
        if any(not value for value in ids):
            raise RuntimeError("Datawheel returned a record without an id")
        if len(ids) != len(set(ids)):
            raise RuntimeError("Datawheel returned duplicate record IDs")
        self.state["record_ids"] = ids
        self.state["record_count"] = len(ids)
        self._save()
        return ids

    def _start_rerun(self) -> None:
        batch_id = str(self.rollout["batch_task_id"])
        snapshot = self.datawheel.get_batch(batch_id)
        pre_phase = batch_phase(snapshot)
        if pre_phase == "mixed_terminal":
            self.state["phase"] = "rollout_ineligible"
            self.state["formal_metric_eligible"] = False
            self.state["ineligible_reason"] = "mixed_terminal_before_rerun"
            self._save()
            raise WorkflowIneligible("batch is terminal while records remain active")
        self._require_runtime_token(str(self.rollout["datawheel_token_env"]))
        record_ids = self._freeze_record_ids()
        expected_total = snapshot.total_size
        if expected_total and int(expected_total) != len(record_ids):
            self.state["phase"] = "rollout_ineligible"
            self.state["formal_metric_eligible"] = False
            self.state["ineligible_reason"] = "batch_record_count_mismatch"
            self._save()
            raise WorkflowIneligible(
                f"batch total_size={expected_total} does not match record_count={len(record_ids)}"
            )
        self.state["rerun_started"] = True
        self.state["rerun_started_at_utc"] = _now()
        self.state["phase"] = "rerun_submitting"
        self._save()
        self._ensure_capacity()

        group_size = int(self.rollout["group_size"])
        next_index = int(self.state.get("restart_next_index") or 0)
        if next_index < 0 or next_index > len(record_ids):
            raise RuntimeError("workflow restart index is outside the frozen record list")
        if self.state.get("restart_inflight"):
            raise AmbiguousMutationError("restart_inflight exists; reconcile before resuming")
        while next_index < len(record_ids):
            group = record_ids[next_index : next_index + group_size]
            self.state["restart_inflight"] = {
                "record_ids": group,
                "start_index": next_index,
                "started_at_utc": _now(),
                "skip_check": False,
            }
            self._save()
            try:
                response = self.datawheel.restart_records(
                    group,
                    skip_check=bool(self.rollout["skip_check"]),
                )
            except Exception as exc:
                raise AmbiguousMutationError(
                    "restart outcome is ambiguous for records "
                    f"{next_index}:{next_index + len(group)}"
                ) from exc
            next_index += len(group)
            self.state.pop("restart_inflight", None)
            self.state["restart_next_index"] = next_index
            groups = list(self.state.get("restart_groups") or [])
            groups.append(
                {
                    "start_index": next_index - len(group),
                    "record_count": len(group),
                    "accepted_at_utc": _now(),
                    **_safe_mapping_metadata(response),
                }
            )
            self.state["restart_groups"] = groups
            self._save()
            self._log_state(f"restarted records {next_index - len(group)}:{next_index}")

        if bool(self.rollout["resume_after_restart"]):
            self.state["pending_mutation"] = {"kind": "resume_batch", "started_at_utc": _now()}
            self.state["phase"] = "resuming_batch"
            self._save()
            try:
                response = self.datawheel.resume_batch(batch_id)
            except Exception as exc:
                raise AmbiguousMutationError("resume_batch outcome is ambiguous") from exc
            self.state.pop("pending_mutation", None)
            self.state["resume_response"] = _safe_mapping_metadata(response)
            self._save()
        self.state["phase"] = "rollout_running"
        self._save()

    def _poll_batch(self) -> str:
        snapshot = self.datawheel.get_batch(str(self.rollout["batch_task_id"]))
        phase = batch_phase(snapshot, expected_total=int(self.state["record_count"]))
        self.state.update(
            {
                "batch_status": snapshot.status,
                "status_counts": dict(snapshot.status_counts),
                "total_size": snapshot.total_size,
                "rollout_phase": phase,
                "remote_concurrency": _remote_concurrency(snapshot),
                "batch_polls": int(self.state.get("batch_polls") or 0) + 1,
            }
        )
        self._save()
        self._log_state(
            f"batch={self.rollout['batch_task_id']} phase={phase} "
            f"counts={json.dumps(snapshot.status_counts, sort_keys=True)}"
        )
        return phase

    def _submit_reward(self) -> int:
        if not bool(self.reward.get("enabled")):
            self.state["phase"] = "complete"
            self._save()
            return 0
        if self.goblin is None:
            raise WorkflowConfigurationError("Reward is enabled but no Goblin client is configured")
        existing_job_id = str(self.state.get("goblin_job_id") or "").strip()
        if self.state.get("phase") == "reward_submitting" and not existing_job_id:
            raise AmbiguousMutationError("Reward submission boundary exists without a job id")
        if not existing_job_id:
            token_env = str(self.reward["goblin_token_env"])
            self._require_runtime_token(token_env)
            artifact = Path(str(self.runtime["result_file"]))
            self.state["phase"] = "downloading_result"
            self._save()
            downloaded = self.datawheel.download_result(
                str(self.rollout["batch_task_id"]), artifact
            )
            artifact = Path(downloaded)
            if not artifact.is_file() or artifact.stat().st_size == 0:
                raise RuntimeError("downloaded Datawheel result is empty")
            self.state["result_file"] = str(artifact)
            self.state["phase"] = "reward_submitting"
            self.state["reward_submission_started_at_utc"] = _now()
            self._save()
            try:
                job = self.goblin.submit_file(
                    artifact,
                    rollout_model=str(self.reward["rollout_model"]),
                    benchmark_dataset_name=str(self.reward["benchmark_dataset_name"]),
                    upload=True,
                    image=str(self.reward["image"]),
                    concurrency=int(self.reward["concurrency"]),
                    prompt_version=str(self.reward["prompt_version"]),
                    reward_model=str(self.reward["reward_model"]),
                    query_round=str(self.reward["query_round"]),
                    eval_mm=bool(self.reward["eval_mm"]),
                    eval_miniprog=bool(self.reward["eval_miniprog"]),
                    timeout=int(self.reward["timeout_seconds"]),
                    max_retries=int(self.reward["max_retries"]),
                    pod_retry=int(self.reward["pod_retry"]),
                    resume=True,
                    template_mode=True,
                    api_env=str(self.reward["api_env"]),
                    description=(
                        f"{self.reward['rollout_model']}; batch {self.rollout['batch_task_id']}; "
                        f"{self.reward['benchmark_dataset_name']}; "
                        f"reward {self.reward['prompt_version']}"
                    ),
                )
            except Exception as exc:
                raise AmbiguousMutationError("Reward submission outcome is ambiguous") from exc
            self.state["goblin_job_id"] = job.job_id
            self.state["goblin"] = _safe_goblin_job(job)
            self.state["phase"] = "reward_submitted"
            self._save()
            self._log_state(f"submitted Goblin job={job.job_id}")

        if not bool(self.reward["wait_for_completion"]):
            return 0
        deadline = time.monotonic() + int(self.runtime["reward_timeout_seconds"])
        while True:
            observed = self.goblin.get_job(existing_job_id or str(self.state["goblin_job_id"]))
            status = normalize_status(observed.status)
            self.state["goblin"] = _safe_goblin_job(observed)
            self.state["phase"] = "waiting_reward"
            self._save()
            if status in REWARD_FAILURE_STATUSES:
                self.state["phase"] = "reward_failed"
                self._save()
                return 2
            if status in REWARD_SUCCESS_STATUSES:
                self.state["phase"] = "complete"
                self.state["completed_at_utc"] = _now()
                self._save()
                return 0
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Reward polling timed out: {self.state['goblin_job_id']}")
            self.sleep(float(self.runtime["poll_interval_seconds"]))

    def run(self) -> int:
        phase = str(self.state.get("phase") or "")
        if phase == "complete":
            self._log_state("workflow already complete")
            return 0
        if phase in {"rollout_ineligible", "reward_failed"}:
            self._log_state(f"workflow is terminal and ineligible: {phase}")
            return 2
        if self.state.get("pending_mutation") or self.state.get("restart_inflight"):
            raise AmbiguousMutationError(
                "workflow contains an unfinished remote mutation; reconcile state before resuming"
            )
        if phase == "reward_submitting" and not self.state.get("goblin_job_id"):
            raise AmbiguousMutationError("Reward submission boundary exists without a job id")

        self._validate_artifact_receipt()

        service_deadline = time.monotonic() + int(self.runtime["service_timeout_seconds"])
        batch_deadline = time.monotonic() + int(self.runtime["batch_timeout_seconds"])
        while True:
            if not bool(self.state.get("rerun_started")):
                if time.monotonic() >= service_deadline:
                    self.state["phase"] = "service_timeout"
                    self._save()
                    return 1
                service = self._service_snapshot()
                if int(service.get("ready_count") or 0) >= int(
                    self.serving["min_ready_endpoints"]
                ):
                    try:
                        self._start_rerun()
                    except WorkflowIneligible:
                        return 2
                    except AgentOSClientError as exc:
                        self._record_control_error(exc)
                        self.sleep(float(self.runtime["poll_interval_seconds"]))
                        continue
                    continue
                self.sleep(float(self.runtime["poll_interval_seconds"]))
                continue

            # A process may have exited after one or more restart groups were
            # accepted but before the final phase write.  Continue only from
            # the durable index; never replay an already accepted group.
            frozen_ids = [str(value).strip() for value in self.state.get("record_ids") or []]
            next_index = int(self.state.get("restart_next_index") or 0)
            if frozen_ids and next_index < len(frozen_ids):
                self._start_rerun()
                continue
            if not frozen_ids:
                raise RuntimeError("rerun_started state has no frozen record IDs")

            if time.monotonic() >= batch_deadline:
                self.state["phase"] = "batch_timeout"
                self._save()
                return 1
            self._service_snapshot()
            try:
                self._ensure_capacity()
                phase = self._poll_batch()
            except AgentOSClientError as exc:
                self._record_control_error(exc)
                self.sleep(float(self.runtime["poll_interval_seconds"]))
                continue
            if phase == "completed":
                return self._submit_reward()
            if phase in {"failed", "mixed_terminal", "count_mismatch"}:
                self.state["phase"] = "rollout_ineligible"
                self.state["formal_metric_eligible"] = False
                self.state["ineligible_reason"] = phase
                self._save()
                return 2
            self.sleep(float(self.runtime["poll_interval_seconds"]))


def safe_plan(manifest: Mapping[str, Any], manifest_sha256: str) -> dict[str, Any]:
    model = _mapping(manifest["model"], "model")
    serving = _mapping(manifest["serving"], "serving")
    rollout = _mapping(manifest["rollout"], "rollout")
    reward = _mapping(manifest["reward"], "reward")
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_sha256": manifest_sha256,
        "pipeline_name": manifest["pipeline_name"],
        "mode": "dry_run",
        "execute_required_for_mutations": True,
        "model": model["name"],
        "artifact_success_marker": model["artifact_success_marker"],
        "serving_jobs": [
            {"name": job["name"], "cluster": job["cluster"]}
            for job in serving["jobs"]
        ],
        "readiness": {
            "min_ready_endpoints": serving["min_ready_endpoints"],
            "per_endpoint_concurrency": serving["per_endpoint_concurrency"],
            "configured_max_concurrency": rollout["configured_max_concurrency"],
            "formula": "min(configured_max_concurrency, cumulative_ready_endpoints * 20)",
        },
        "batch": {
            "batch_task_id": rollout["batch_task_id"],
            "restart_all_records": rollout["restart_all_records"],
            "group_size": rollout["group_size"],
            "skip_check": rollout["skip_check"],
            "resume_after_restart": rollout["resume_after_restart"],
        },
        "reward": {
            "enabled": reward["enabled"],
            "benchmark_dataset_name": reward.get("benchmark_dataset_name"),
            "rollout_model": reward.get("rollout_model"),
            "query_round": reward.get("query_round"),
            "prompt_version": reward.get("prompt_version"),
            "reward_model": reward.get("reward_model"),
            "concurrency": reward.get("concurrency"),
            "wait_for_completion": reward.get("wait_for_completion"),
        },
        "teardown_enabled": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--execute", action="store_true", help="enable remote batch/reward mutations"
    )
    modes.add_argument(
        "--probe-only",
        action="store_true",
        help="perform one read-only serving probe and print its evidence",
    )
    modes.add_argument("--dry-run", action="store_true", help="validate and print the plan")
    return parser


def _build_monitor(manifest: Mapping[str, Any]) -> ServingMonitor:
    runtime = _mapping(manifest["runtime"], "runtime")
    model = _mapping(manifest["model"], "model")
    serving = _mapping(manifest["serving"], "serving")
    api_key = os.environ.get(str(model["api_key_env"]), "")
    gateway = GatewayProbe(
        model_name=str(model["name"]),
        control_url=str(model["gateway_control_url"]),
        api_url=str(model["gateway_api_url"]),
        smoke=_mapping(serving["smoke"], "serving.smoke"),
        timeout_seconds=int(runtime["http_timeout_seconds"]),
        api_key=api_key,
    )
    ltp = LTPStatusClient(
        command=str(serving["ltp_cli"]),
        user=str(serving["ltp_user"]),
        timeout_seconds=int(runtime["ltp_command_timeout_seconds"]),
    )
    return ServingMonitor(manifest, gateway=gateway, ltp=ltp)


def _write_execution_receipts(
    manifest: Mapping[str, Any], manifest_sha256: str, output_root: Path
) -> None:
    """Persist immutable, credential-free ownership receipts for inspection."""

    serving = _mapping(manifest["serving"], "serving")
    _atomic_write_json(
        output_root / "manifest.normalized.json",
        {"manifest_sha256": manifest_sha256, "manifest": dict(manifest)},
    )
    _atomic_write_json(
        output_root / "serving_jobs.json",
        {
            "manifest_sha256": manifest_sha256,
            "recorded_at_utc": _now(),
            "jobs": [
                {
                    "job_name": job["name"],
                    "cluster": job["cluster"],
                    "driver_log": job["driver_log"],
                }
                for job in serving["jobs"]
            ],
        },
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest, manifest_sha256 = load_manifest(args.manifest)
        if args.probe_only:
            print(json.dumps(_build_monitor(manifest).poll(), ensure_ascii=False, indent=2))
            return 0
        if not args.execute:
            print(json.dumps(safe_plan(manifest, manifest_sha256), ensure_ascii=False, indent=2))
            return 0

        runtime = _mapping(manifest["runtime"], "runtime")
        rollout = _mapping(manifest["rollout"], "rollout")
        reward = _mapping(manifest["reward"], "reward")
        state_path = Path(str(runtime["state_file"]))
        lock_path = state_path.with_suffix(state_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(f"workflow already running for state={state_path}", file=sys.stderr)
                return 0
            try:
                _write_execution_receipts(
                    manifest,
                    manifest_sha256,
                    Path(str(runtime["output_root"])),
                )
                datawheel = AgentOSClient(
                    str(rollout["datawheel_url"]),
                    token_env=str(rollout["datawheel_token_env"]),
                    timeout_seconds=float(runtime["http_timeout_seconds"]),
                    trust_env=False,
                )
                goblin = None
                if bool(reward.get("enabled")):
                    goblin = GoblinRewardClient(
                        str(reward["goblin_url"]),
                        token_env=str(reward["goblin_token_env"]),
                        timeout_seconds=float(runtime["http_timeout_seconds"]),
                        trust_env=False,
                    )
                workflow = BatchRerunRewardWorkflow(
                    manifest,
                    manifest_sha256,
                    datawheel=datawheel,
                    goblin=goblin,
                    monitor=_build_monitor(manifest),
                )
                return workflow.run()
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except KeyboardInterrupt:
        print("workflow interrupted; durable state was left for reconciliation", file=sys.stderr)
        return 130
    except (
        AgentOSClientError,
        GoblinRewardError,
        WorkflowConfigurationError,
        WorkflowIneligible,
        AmbiguousMutationError,
        OSError,
        RuntimeError,
        TimeoutError,
        httpx.HTTPError,
    ) as exc:
        print(f"workflow failed: {type(exc).__name__}: {str(exc)[:500]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run a durable Qwen3.8 serving, rollout, optional Reward workflow."""

from __future__ import annotations

import argparse
import contextlib
import csv
import fcntl
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from research_ops.agentos import (  # noqa: E402
    AgentOSBatchSnapshot,
    AgentOSClient,
    AgentOSClientError,
    build_miaoda_dataset_batch_payload,
    miaoda_batch_identity,
)
from research_ops.reward import (  # noqa: E402
    GoblinJobSnapshot,
    GoblinRewardClient,
    GoblinRewardError,
)

DEFAULT_MANIFEST = PROJECT_ROOT / "config/qw38-inhouse300-rollout-reward.yaml"
SUPPORTED_B_CLUSTERS = {
    "cluster-a",
    "cluster-b",
    "cluster-c",
    "cluster-reserved-a",
    "cluster-reserved-b",
    "cluster-reserved-c",
}
MM200_H100_CLUSTERS = {"cluster-d"}
MM200_JOB_BASE_PREFIXES = ("dep-q38-on553-", "dep-qw38-opxml-")
MM200_PROVIDERS = {"openai", "qianfancoder_adapter_v6"}
SECOND_ROUND_H100_CLUSTERS = {"cluster-d"}
SECOND_ROUND_PROVIDERS = {"openai", "qianfancoder_adapter_v6"}
DFLASH2_PERFORMANCE_PROFILE = "qwen38_dflash2_performance"
DFLASH2_PERFORMANCE_CLUSTERS = {"cluster-dev"}
DFLASH2_PERFORMANCE_MAX_RUNNING_REQUESTS = {8, 16, 32, 48}
TERMINAL_ROLLOUT_KEYS = {
    "completed",
    "failed",
    "stopped",
    "terminated",
    "cancelled",
    "canceled",
}
COMPLETED_STATUSES = {"completed", "complete", "succeeded", "success", "done"}
FAILED_STATUSES = {"failed", "failure", "error", "stopped", "terminated", "cancelled", "canceled"}
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
    r"(?:^|_)(?:api_?key|authorization|cookie|password|secret|token)(?:$|_)",
    flags=re.IGNORECASE,
)
ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
JOB_LEDGER_COLUMNS = (
    "logical_job_name",
    "actual_job_name",
    "user",
    "cluster",
    "total_nodes",
    "gpus_per_node",
    "submission_time_utc",
    "log_dir",
    "readiness_file",
    "gateway_preclean",
    "attached_at_utc",
)


class WorkflowConfigurationError(ValueError):
    """Raised when the versioned manifest violates the workflow contract."""


class ServiceNotReady(RuntimeError):
    """Raised for a transient serving-readiness mismatch."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


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


def _reject_inline_secrets(value: Any, path: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key)
            normalized = name.casefold()
            if normalized.endswith("_env") and SECRET_KEY_PATTERN.search(normalized):
                if not isinstance(child, str) or not ENVIRONMENT_NAME_PATTERN.fullmatch(child):
                    raise WorkflowConfigurationError(
                        f"{path}.{name} must name an uppercase environment variable"
                    )
            elif (
                normalized in SECRET_KEYS or SECRET_KEY_PATTERN.search(normalized)
            ) and child not in (None, ""):
                raise WorkflowConfigurationError(
                    f"{path}.{name} must be an environment reference, not a value"
                )
            _reject_inline_secrets(child, f"{path}.{name}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_inline_secrets(child, f"{path}[{index}]")


def load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    document = yaml.safe_load(raw)
    manifest = _mapping(document, "manifest")
    _reject_inline_secrets(manifest)
    if manifest.get("schema_version") != "miaoda.rollout_reward_workflow.v1":
        raise WorkflowConfigurationError("unsupported workflow schema_version")

    runtime = _mapping(manifest.get("runtime"), "runtime")
    serving = _mapping(manifest.get("serving"), "serving")
    rollout = _mapping(manifest.get("rollout"), "rollout")
    teardown = _mapping(manifest.get("teardown"), "teardown")
    reward = _mapping(manifest.get("reward"), "reward")
    coding = _mapping(rollout.get("coding_config"), "rollout.coding_config")
    model = _mapping(
        coding.get("codingPromptiModelConfig"),
        "rollout.coding_config.codingPromptiModelConfig",
    )

    output_root = Path(_text(runtime.get("output_root"), "runtime.output_root"))
    readiness_file = Path(_text(serving.get("readiness_file"), "serving.readiness_file"))
    readiness_dir = Path(str(serving.get("readiness_dir") or readiness_file.parent / "readiness"))
    if (
        not output_root.is_absolute()
        or not readiness_file.is_absolute()
        or not readiness_dir.is_absolute()
    ):
        raise WorkflowConfigurationError("runtime and readiness paths must be absolute")
    serving["readiness_dir"] = str(readiness_dir)
    manifest["serving"] = serving
    dataset_name = str(rollout.get("dataset_name") or "")
    workflow_profile = str(manifest.get("workflow_profile") or "formal")
    candidate_clusters = set(serving.get("candidate_clusters") or [])
    requested_replicas = _positive_int(
        serving.get("requested_replicas"), "serving.requested_replicas"
    )
    min_ready_endpoints = _positive_int(
        serving.get("min_ready_endpoints"), "serving.min_ready_endpoints"
    )
    if min_ready_endpoints > requested_replicas:
        raise WorkflowConfigurationError("serving.min_ready_endpoints exceeds requested_replicas")
    job_base = _text(serving.get("job_base"), "serving.job_base")
    if teardown.get("enabled") is not True:
        raise WorkflowConfigurationError("serving teardown must be enabled")
    if int(rollout.get("selective_retry_rounds") or 0) != 0:
        raise WorkflowConfigurationError("this workflow does not authorize selective reruns")
    if model.get("model") != model.get("model_value") or model.get("model") != serving.get(
        "model_name"
    ):
        raise WorkflowConfigurationError("serving and rollout model identities differ")
    expected_api_url = f"{str(serving.get('gateway_api_url')).rstrip('/')}/v1/chat/completions"
    if model.get("api_url") != expected_api_url:
        raise WorkflowConfigurationError("rollout api_url must use the configured gateway")
    if model.get("api_key_env") != serving.get("model_api_key_env"):
        raise WorkflowConfigurationError("serving and rollout model key references differ")
    per_endpoint_concurrency = _positive_int(
        rollout.get("per_endpoint_concurrency"),
        "rollout.per_endpoint_concurrency",
    )
    configured_max_concurrency = _positive_int(
        rollout.get("configured_max_concurrency"),
        "rollout.configured_max_concurrency",
    )
    if configured_max_concurrency < per_endpoint_concurrency:
        raise WorkflowConfigurationError("rollout.configured_max_concurrency is below one endpoint")
    if int(rollout.get("concurrency") or 0) != min(
        configured_max_concurrency,
        min_ready_endpoints * per_endpoint_concurrency,
    ):
        raise WorkflowConfigurationError("rollout concurrency violates the endpoint capacity cap")
    expected_runtime = _mapping(serving.get("expected_runtime"), "serving.expected_runtime")
    if dataset_name == "example-app-300":
        if int(rollout.get("expected_records") or 0) != 300:
            raise WorkflowConfigurationError(
                "inhouse-300 workflow must require exactly 300 records"
            )
        if workflow_profile == DFLASH2_PERFORMANCE_PROFILE:
            if candidate_clusters != DFLASH2_PERFORMANCE_CLUSTERS:
                raise WorkflowConfigurationError(
                    "DFlash2 performance serving must use cluster-dev"
                )
            if not job_base.startswith("q38i53-b-df2-"):
                raise WorkflowConfigurationError(
                    "serving.job_base is not a Qwen3.8 B-card DFlash2 deployment"
                )
            if model.get("provider") != "openai":
                raise WorkflowConfigurationError(
                    "DFlash2 performance rollout provider must be openai"
                )
            if reward.get("enabled") is not False:
                raise WorkflowConfigurationError(
                    "DFlash2 performance workflow does not authorize Reward"
                )
            required_runtime_keys = {
                "tp_size",
                "dp_size",
                "context_length",
                "cuda_graph_max_bs",
                "max_running_requests",
            }
            if set(expected_runtime) != required_runtime_keys:
                raise WorkflowConfigurationError(
                    "DFlash2 performance expected runtime fields are incomplete"
                )
            max_running_requests = _positive_int(
                expected_runtime.get("max_running_requests"),
                "serving.expected_runtime.max_running_requests",
            )
            required_runtime = {
                "tp_size": 8,
                "dp_size": 1,
                "context_length": 131072,
                "cuda_graph_max_bs": max_running_requests,
                "max_running_requests": max_running_requests,
            }
            if max_running_requests not in DFLASH2_PERFORMANCE_MAX_RUNNING_REQUESTS:
                raise WorkflowConfigurationError(
                    "DFlash2 performance max_running_requests is not an approved profile"
                )
        elif workflow_profile == "formal":
            if candidate_clusters != SUPPORTED_B_CLUSTERS:
                raise WorkflowConfigurationError(
                    "serving.candidate_clusters must list every B-card alias"
                )
            if not job_base.startswith(("demo-base-rollout", "demo-tuned-rollout")):
                raise WorkflowConfigurationError("serving.job_base is not a formal Qwen3.8 job")
            if reward.get("enabled") is not True:
                raise WorkflowConfigurationError("formal workflow requires Reward")
            if (reward.get("query_round"), reward.get("prompt_version")) != (
                "first_round",
                "v1_6",
            ):
                raise WorkflowConfigurationError("inhouse-300 Reward must use first_round + v1_6")
            required_runtime = {
                "tp_size": 8,
                "dp_size": 1,
                "context_length": 131072,
                "cuda_graph_max_bs": 48,
                "max_running_requests": 48,
            }
        else:
            raise WorkflowConfigurationError(
                f"unsupported inhouse-300 workflow profile: {workflow_profile}"
            )
    elif dataset_name == "example-multimodal-200":
        if candidate_clusters not in (MM200_H100_CLUSTERS, SUPPORTED_B_CLUSTERS):
            raise WorkflowConfigurationError(
                "multimodal-200 serving must use cluster-d or list every B-card alias"
            )
        if not job_base.startswith(MM200_JOB_BASE_PREFIXES):
            raise WorkflowConfigurationError(
                "serving.job_base is not an approved multimodal-200 deployment"
            )
        if int(rollout.get("expected_records") or 0) != 200:
            raise WorkflowConfigurationError(
                "multimodal-200 workflow must require exactly 200 records"
            )
        if model.get("provider") not in MM200_PROVIDERS:
            raise WorkflowConfigurationError(
                "multimodal-200 rollout provider must be openai or qianfancoder_adapter_v6"
            )
        if reward.get("enabled") is not False:
            raise WorkflowConfigurationError("multimodal-200 workflow does not authorize Reward")
        required_runtime = {
            "tp_size": 8,
            "dp_size": 1,
            "context_length": 131072,
            "cuda_graph_max_bs": 16,
            "max_running_requests": 16,
        }
    elif dataset_name == "miaoda-second-rounds-maas-0615":
        if candidate_clusters != SECOND_ROUND_H100_CLUSTERS:
            raise WorkflowConfigurationError("second-round serving must use cluster-d")
        if not job_base.startswith("dep-qw38-s53-sr0615-"):
            raise WorkflowConfigurationError(
                "serving.job_base is not the Qwen3.8 SFT53 second-round deployment"
            )
        if int(rollout.get("expected_records") or 0) != 435:
            raise WorkflowConfigurationError(
                "second-round workflow must require exactly 435 records"
            )
        if rollout.get("pipeline") != "LuiPipeline":
            raise WorkflowConfigurationError("second-round rollout must use LuiPipeline")
        if model.get("provider") not in SECOND_ROUND_PROVIDERS:
            raise WorkflowConfigurationError(
                "second-round rollout provider must be openai or qianfancoder_adapter_v6"
            )
        if reward.get("enabled") is not True:
            raise WorkflowConfigurationError("second-round workflow requires Reward")
        if (reward.get("query_round"), reward.get("prompt_version")) != (
            "second_round",
            "v1_3",
        ):
            raise WorkflowConfigurationError("second-round Reward must use second_round + v1_3")
        if reward.get("benchmark_dataset_name") != dataset_name:
            raise WorkflowConfigurationError(
                "second-round Reward benchmark must match the rollout dataset"
            )
        required_runtime = {
            "tp_size": 8,
            "dp_size": 1,
            "context_length": 131072,
            "cuda_graph_max_bs": 48,
            "max_running_requests": 48,
        }
    else:
        raise WorkflowConfigurationError(f"unsupported formal dataset: {dataset_name}")
    if expected_runtime != required_runtime:
        raise WorkflowConfigurationError(
            "serving expected runtime does not match the selected workflow profile"
        )

    for section_name, section in (
        ("runtime", runtime),
        ("serving", serving),
        ("rollout", rollout),
        ("reward", reward),
    ):
        for key in (
            "poll_interval_seconds",
            "service_timeout_seconds",
            "rollout_timeout_seconds",
            "teardown_timeout_seconds",
            "reward_timeout_seconds",
            "concurrency",
            "timeout_seconds",
        ):
            if key in section:
                _positive_int(section[key], f"{section_name}.{key}")

    return manifest, hashlib.sha256(raw).hexdigest()


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _environment_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise WorkflowConfigurationError(f"runtime environment variable is empty: {name}")
    return value


def _normalize_status(value: Any) -> str:
    return str(value or "unknown").strip().lower().replace("-", "_").replace(" ", "_")


def _job_belongs_to_base(actual_job_name: str, job_base: str) -> bool:
    return actual_job_name == job_base or actual_job_name.startswith(f"{job_base}-")


def _job_readiness_path(serving: Mapping[str, Any], actual_job_name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", actual_job_name):
        raise WorkflowConfigurationError("actual LTP job name contains unsafe characters")
    return Path(str(serving["readiness_dir"])) / f"{actual_job_name}.json"


def _read_job_ledger(path: Path) -> list[dict[str, str]]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if not reader.fieldnames or "actual_job_name" not in reader.fieldnames:
            raise RuntimeError("submitted job ledger has an invalid header")
        rows = [
            {str(key): str(value or "") for key, value in row.items() if key is not None}
            for row in reader
            if str(row.get("actual_job_name") or "").strip()
        ]
    identities = [row["actual_job_name"].strip() for row in rows]
    if len(identities) != len(set(identities)):
        raise RuntimeError("submitted job ledger contains duplicate actual identities")
    return rows


def _write_job_ledger(path: Path, rows: list[dict[str, str]]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(JOB_LEDGER_COLUMNS),
        delimiter="\t",
        lineterminator="\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in JOB_LEDGER_COLUMNS})
    _atomic_write_text(path, buffer.getvalue())


@contextlib.contextmanager
def _locked_job_ledger(output_root: Path):
    lock_path = output_root / "submitted_jobs.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield output_root / "submitted_jobs.tsv"
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _max_ledger_jobs(rollout: Mapping[str, Any]) -> int:
    per_endpoint = _positive_int(
        rollout.get("per_endpoint_concurrency"),
        "rollout.per_endpoint_concurrency",
    )
    configured_max = _positive_int(
        rollout.get("configured_max_concurrency"),
        "rollout.configured_max_concurrency",
    )
    return max(1, (configured_max + per_endpoint - 1) // per_endpoint)


def ensure_primary_job_ledger(
    manifest: Mapping[str, Any],
    actual_job_name: str,
) -> list[dict[str, str]]:
    runtime = _mapping(manifest["runtime"], "runtime")
    serving = _mapping(manifest["serving"], "serving")
    rollout = _mapping(manifest["rollout"], "rollout")
    job_base = str(serving["job_base"])
    if not _job_belongs_to_base(actual_job_name, job_base):
        raise WorkflowConfigurationError("actual LTP job does not belong to serving.job_base")
    output_root = Path(str(runtime["output_root"]))
    with _locked_job_ledger(output_root) as ledger_path:
        rows = _read_job_ledger(ledger_path)
        matching = [row for row in rows if row["actual_job_name"] == actual_job_name]
        if not matching:
            rows.append(
                {
                    "logical_job_name": job_base,
                    "actual_job_name": actual_job_name,
                    "user": str(serving["ltp_user"]),
                    "gpus_per_node": str(
                        _mapping(serving["expected_runtime"], "expected_runtime")["tp_size"]
                    ),
                    "readiness_file": str(serving["readiness_file"]),
                    "gateway_preclean": "true",
                    "attached_at_utc": _now(),
                }
            )
        for index, row in enumerate(rows):
            identity = row["actual_job_name"].strip()
            if not _job_belongs_to_base(identity, job_base):
                raise RuntimeError(f"ledger job does not belong to pipeline: {identity}")
            if identity == actual_job_name:
                # The primary service may have a fixed legacy receipt path. Replica
                # receipts are the only ones derived from readiness_dir.
                row["readiness_file"] = str(serving["readiness_file"])
            elif not row.get("readiness_file"):
                row["readiness_file"] = str(_job_readiness_path(serving, identity))
            if not row.get("gateway_preclean"):
                row["gateway_preclean"] = "true" if index == 0 else "false"
            if not row.get("attached_at_utc"):
                row["attached_at_utc"] = row.get("submission_time_utc") or _now()
        if len(rows) > _max_ledger_jobs(rollout):
            raise RuntimeError("submitted job ledger exceeds configured concurrency capacity")
        _write_job_ledger(ledger_path, rows)
        return rows


def attach_job_to_ledger(
    manifest: Mapping[str, Any],
    actual_job_name: str,
    ltp_observation: Mapping[str, str],
) -> tuple[dict[str, str], bool]:
    runtime = _mapping(manifest["runtime"], "runtime")
    serving = _mapping(manifest["serving"], "serving")
    rollout = _mapping(manifest["rollout"], "rollout")
    job_base = str(serving["job_base"])
    identity = actual_job_name.strip()
    if not _job_belongs_to_base(identity, job_base):
        raise WorkflowConfigurationError("attached LTP job does not belong to serving.job_base")
    output_root = Path(str(runtime["output_root"]))
    state_path = output_root / "state.json"
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("teardown_complete") or state.get("phase") in {
            "tearing_down",
            "serving_released",
            "rollout_ineligible",
            "complete",
        }:
            raise RuntimeError("cannot attach serving after teardown has started")
    with _locked_job_ledger(output_root) as ledger_path:
        rows = _read_job_ledger(ledger_path)
        for row in rows:
            if row["actual_job_name"] == identity:
                return row, False
        if not rows:
            raise RuntimeError("primary serving job must be registered before attach-job")
        if len(rows) >= _max_ledger_jobs(rollout):
            raise RuntimeError("attach-job would exceed configured concurrency capacity")
        row = {
            "logical_job_name": job_base,
            "actual_job_name": identity,
            "user": str(serving["ltp_user"]),
            "cluster": str(ltp_observation.get("cluster") or ""),
            "total_nodes": str(ltp_observation.get("total_nodes") or ""),
            "gpus_per_node": str(
                _mapping(serving["expected_runtime"], "expected_runtime")["tp_size"]
            ),
            "submission_time_utc": str(ltp_observation.get("submission_time") or ""),
            "log_dir": str(Path(str(serving["readiness_dir"])).parent),
            "readiness_file": str(_job_readiness_path(serving, identity)),
            "gateway_preclean": "false",
            "attached_at_utc": _now(),
        }
        rows.append(row)
        _write_job_ledger(ledger_path, rows)
        return row, True


def rollout_phase(
    snapshot: AgentOSBatchSnapshot,
    *,
    expected_total: int | None = None,
) -> str:
    counts = {_normalize_status(key): int(value) for key, value in snapshot.status_counts.items()}
    pending = counts.get("pending", 0)
    running = counts.get("running", 0)
    status = _normalize_status(snapshot.status)
    terminal_label = status in COMPLETED_STATUSES | FAILED_STATUSES or status.startswith(
        ("completed_", "succeeded_", "failed_", "cancelled_", "terminated_")
    )
    if terminal_label and pending + running > 0:
        return "mixed_terminal"
    if pending + running > 0:
        return "running"
    total = snapshot.total_size or sum(counts.values())
    terminal = sum(counts.get(key, 0) for key in TERMINAL_ROLLOUT_KEYS)
    settled = total > 0 and terminal >= total
    if settled and expected_total is not None and total != expected_total:
        return "count_mismatch"
    if status in FAILED_STATUSES and settled:
        return "failed"
    completed_label = status in COMPLETED_STATUSES or status.startswith(
        ("completed_", "succeeded_")
    )
    if settled and completed_label:
        return "completed"
    if settled and status == "running":
        return "completed"
    return "running"


def _safe_goblin(snapshot: GoblinJobSnapshot) -> dict[str, Any]:
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


class ServingRuntime:
    """Bounded LTP and gateway operations for one exact serving job."""

    def __init__(
        self,
        serving: Mapping[str, Any],
        teardown: Mapping[str, Any],
        *,
        actual_job_name: str,
        model_api_key: str,
        readiness_file: Path | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = dict(serving)
        self.teardown_config = dict(teardown)
        self.actual_job_name = actual_job_name
        self.model_api_key = model_api_key
        self.readiness_file = readiness_file or Path(str(self.config["readiness_file"]))
        self.transport = transport

    @staticmethod
    def _ltp_env() -> dict[str, str]:
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

    def _ltp(self, action: str, *, logs: bool = False) -> subprocess.CompletedProcess[str]:
        command = ["ltp-cli", action, self.actual_job_name, "--user", self.config["ltp_user"]]
        if logs:
            command.append("--logs")
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=25,
            env=self._ltp_env(),
            check=False,
        )

    @staticmethod
    def _ltp_status(output: str) -> str | None:
        match = re.search(r"^status:\s*(\S+)\s*$", output, flags=re.MULTILINE)
        return match.group(1) if match else None

    @staticmethod
    def _ltp_submission_time(output: str) -> datetime | None:
        match = re.search(r"^submission_time:\s*(.+?)\s*$", output, flags=re.MULTILINE)
        if not match:
            return None
        try:
            observed = datetime.fromisoformat(match.group(1))
        except ValueError:
            return None
        return observed.replace(tzinfo=UTC) if observed.tzinfo is None else observed.astimezone(UTC)

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(
            timeout=30.0,
            trust_env=False,
            transport=self.transport,
        ) as client:
            return client.request(method, url, **kwargs)

    def _gateway_endpoints(self) -> list[str]:
        response = self._request(
            "GET",
            f"{str(self.config['gateway_control_url']).rstrip('/')}/proxy/endpoint/list",
            params={"model_name": self.config["model_name"]},
        )
        if response.status_code == 400 and any(
            marker in response.text.casefold() for marker in ("not exists", "not found")
        ):
            return []
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("gateway endpoint list was not an object")
        items = ((payload.get("data") or {}).get("endpoints")) or []
        endpoints: list[str] = []
        for item in items:
            endpoint = item.get("endpoint") if isinstance(item, Mapping) else item
            if str(endpoint or "").strip():
                endpoints.append(str(endpoint).strip())
        return endpoints

    def probe(self) -> dict[str, Any]:
        shown = self._ltp("show")
        if shown.returncode != 0:
            raise ServiceNotReady("ltp_show_failed")
        status = self._ltp_status(shown.stdout)
        if _normalize_status(status) != "running":
            raise ServiceNotReady(f"ltp_status_{_normalize_status(status)}")

        readiness_path = self.readiness_file
        try:
            receipt = json.loads(readiness_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ServiceNotReady("readiness_receipt_unavailable") from exc
        if not isinstance(receipt, Mapping):
            raise ServiceNotReady("readiness_receipt_invalid")
        if receipt.get("model_name") != self.config["model_name"]:
            raise ServiceNotReady("readiness_model_mismatch")
        receipt_job = str(receipt.get("job_name") or "").strip()
        job_base = str(self.config["job_base"])
        if self.actual_job_name != job_base and not self.actual_job_name.startswith(f"{job_base}-"):
            raise ServiceNotReady("actual_job_name_mismatch")
        if receipt.get("job_base") != job_base:
            raise ServiceNotReady("readiness_job_base_mismatch")
        receipt_job_matches = receipt_job in {"unknown", job_base, self.actual_job_name}
        receipt_job_matches = receipt_job_matches or self.actual_job_name.startswith(
            f"{receipt_job}-"
        )
        if not receipt_job_matches:
            raise ServiceNotReady("readiness_job_mismatch")
        ready_at = str(receipt.get("ready_at_utc") or "").strip()
        try:
            ready_time = datetime.fromisoformat(ready_at.replace("Z", "+00:00"))
            age_seconds = (datetime.now(UTC) - ready_time).total_seconds()
        except (TypeError, ValueError) as exc:
            raise ServiceNotReady("readiness_timestamp_invalid") from exc
        max_age = int(self.config["readiness_max_age_seconds"])
        if age_seconds < -300 or age_seconds > max_age:
            raise ServiceNotReady("readiness_receipt_stale")
        submitted_at = self._ltp_submission_time(shown.stdout)
        if submitted_at is None:
            raise ServiceNotReady("ltp_submission_time_missing")
        if ready_time < submitted_at:
            raise ServiceNotReady("readiness_predates_ltp_submission")
        expected_runtime = _mapping(
            self.config.get("expected_runtime"),
            "serving.expected_runtime",
        )
        for key, expected in expected_runtime.items():
            if receipt.get(key) != expected:
                raise ServiceNotReady(f"readiness_{key}_mismatch")
        if str(receipt.get("gateway_api_url") or "").rstrip("/") != str(
            self.config["gateway_api_url"]
        ).rstrip("/"):
            raise ServiceNotReady("readiness_gateway_mismatch")
        endpoint = str(receipt.get("endpoint") or "").strip()
        if not endpoint:
            raise ServiceNotReady("readiness_endpoint_missing")

        health = self._request("GET", f"{endpoint.rstrip('/')}/health")
        if health.status_code != 200:
            raise ServiceNotReady(f"direct_health_http_{health.status_code}")
        if endpoint not in self._gateway_endpoints():
            raise ServiceNotReady("gateway_endpoint_missing")

        smoke = _mapping(self.config.get("smoke"), "serving.smoke")
        payload = {
            "model": self.config["model_name"],
            "messages": [{"role": "user", "content": smoke["prompt"]}],
            "max_tokens": int(smoke["max_tokens"]),
            "temperature": float(smoke["temperature"]),
            "chat_template_kwargs": dict(smoke["chat_template_kwargs"]),
        }
        headers = {"Content-Type": "application/json"}
        if self.model_api_key:
            headers["Authorization"] = f"Bearer {self.model_api_key}"
        generated = self._request(
            "POST",
            f"{str(self.config['gateway_api_url']).rstrip('/')}/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        if generated.status_code != 200:
            raise ServiceNotReady(f"gateway_generation_http_{generated.status_code}")
        try:
            message = generated.json()["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ServiceNotReady("gateway_generation_invalid") from exc
        if not (message.get("content") or message.get("reasoning_content")):
            raise ServiceNotReady("gateway_generation_empty")
        return {
            "observed_at": _now(),
            "ltp_status": status,
            "job_name": self.actual_job_name,
            "model_name": self.config["model_name"],
            "endpoint": endpoint,
            "direct_health_http_status": 200,
            "gateway_generation_http_status": 200,
            "readiness_file": str(readiness_path),
        }

    def teardown(
        self,
        *,
        endpoint: str | None,
        timeout_seconds: int,
        poll_interval: float,
    ) -> dict[str, Any]:
        control_url = str(self.config["gateway_control_url"]).rstrip("/")
        target = str(endpoint or "").strip()
        registered = self._gateway_endpoints()
        if target and target in registered:
            response = self._request(
                "POST",
                f"{control_url}/proxy/endpoint/delete",
                headers={"Content-Type": "application/json"},
                json={"model_name": self.config["model_name"], "endpoints": [target]},
            )
            response.raise_for_status()
        remaining = self._gateway_endpoints()
        if target and target in remaining:
            raise RuntimeError("owned gateway endpoint remains after teardown")
        if not remaining and self.teardown_config.get("remove_gateway_model") is True:
            response = self._request(
                "POST",
                f"{control_url}/proxy/delete",
                headers={"Content-Type": "application/json"},
                json={"model_name": self.config["model_name"]},
            )
            if response.status_code not in {200, 404}:
                response.raise_for_status()

        delete_output = "skipped"
        verification_status = "skipped"
        verification_pod_absent = False
        if self.teardown_config.get("delete_ltp_job") is True:
            deleted = self._ltp("delete")
            delete_output = "accepted" if deleted.returncode == 0 else "not_found"
            if deleted.returncode != 0 and "not found" not in (
                f"{deleted.stdout}\n{deleted.stderr}".casefold()
            ):
                raise RuntimeError("ltp_delete_failed")

            deadline = time.monotonic() + timeout_seconds
            while True:
                shown = self._ltp("show", logs=True)
                combined = f"{shown.stdout}\n{shown.stderr}"
                status = _normalize_status(self._ltp_status(shown.stdout))
                pod_absent = "notfound" in combined.casefold() or "not found" in combined.casefold()
                verification_status = status
                verification_pod_absent = pod_absent
                if shown.returncode != 0:
                    if pod_absent:
                        break
                    raise RuntimeError("ltp_teardown_verification_failed")
                if (
                    status
                    in {
                        "deleted",
                        "failed",
                        "succeeded",
                        "completed",
                    }
                    or pod_absent
                ):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("ltp teardown verification timed out")
                time.sleep(max(poll_interval, 0.1))
        return {
            "completed_at": _now(),
            "model_name": self.config["model_name"],
            "removed_endpoints": [target] if target and target in registered else [],
            "remaining_endpoints": remaining,
            "ltp_job_name": self.actual_job_name,
            "ltp_delete": delete_output,
            "ltp_verification_status": verification_status,
            "ltp_verification_pod_absent": verification_pod_absent,
        }


class ServingFleet:
    """Reload and verify every serving job owned by one rollout pipeline."""

    def __init__(
        self,
        manifest: Mapping[str, Any],
        *,
        model_api_key: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.manifest = dict(manifest)
        self.runtime_config = _mapping(manifest["runtime"], "runtime")
        self.config = _mapping(manifest["serving"], "serving")
        self.teardown_config = _mapping(manifest["teardown"], "teardown")
        self.output_root = Path(str(self.runtime_config["output_root"]))
        self.model_api_key = model_api_key
        self.transport = transport

    def _jobs(self) -> list[dict[str, str]]:
        with _locked_job_ledger(self.output_root) as ledger_path:
            rows = _read_job_ledger(ledger_path)
        if not rows:
            raise RuntimeError("submitted job ledger is empty")
        return rows

    def _runtime(self, row: Mapping[str, str]) -> ServingRuntime:
        return ServingRuntime(
            self.config,
            self.teardown_config,
            actual_job_name=str(row["actual_job_name"]),
            model_api_key=self.model_api_key,
            readiness_file=Path(str(row["readiness_file"])),
            transport=self.transport,
        )

    def probe(self) -> dict[str, Any]:
        ready: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        rows = self._jobs()
        for row in rows:
            identity = row["actual_job_name"]
            try:
                ready.append(self._runtime(row).probe())
            except (
                ServiceNotReady,
                OSError,
                httpx.HTTPError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as exc:
                reason = str(exc).strip() or type(exc).__name__
                errors[identity] = reason[:160]
        endpoints = sorted(
            {
                str(receipt["endpoint"])
                for receipt in ready
                if str(receipt.get("endpoint") or "").strip()
            }
        )
        return {
            "observed_at": _now(),
            "model_name": self.config["model_name"],
            "requested_replicas": len(rows),
            "ready_replicas": len(ready),
            "endpoint_count": len(endpoints),
            "endpoints": endpoints,
            "jobs": ready,
            "job_errors": errors,
            "degraded": bool(ready) and len(ready) < len(rows),
        }

    @staticmethod
    def _snapshot_endpoints(snapshot: Mapping[str, Any] | None) -> dict[str, str]:
        if not snapshot:
            return {}
        jobs = snapshot.get("jobs")
        if isinstance(jobs, list):
            return {
                str(item.get("job_name")): str(item.get("endpoint"))
                for item in jobs
                if isinstance(item, Mapping)
                and str(item.get("job_name") or "").strip()
                and str(item.get("endpoint") or "").strip()
            }
        job_name = str(snapshot.get("job_name") or "").strip()
        endpoint = str(snapshot.get("endpoint") or "").strip()
        return {job_name: endpoint} if job_name and endpoint else {}

    def _receipt_endpoint(self, row: Mapping[str, str]) -> str:
        try:
            receipt = json.loads(Path(row["readiness_file"]).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ""
        if not isinstance(receipt, Mapping):
            return ""
        if receipt.get("model_name") != self.config["model_name"]:
            return ""
        receipt_job = str(receipt.get("job_name") or "").strip()
        identity = row["actual_job_name"]
        if receipt_job not in {"unknown", identity, self.config["job_base"]}:
            return ""
        return str(receipt.get("endpoint") or "").strip()

    def teardown(
        self,
        *,
        serving_snapshot: Mapping[str, Any] | None,
        timeout_seconds: int,
        poll_interval: float,
    ) -> dict[str, Any]:
        rows = self._jobs()
        known_endpoints = self._snapshot_endpoints(serving_snapshot)
        receipts: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        for row in rows:
            identity = row["actual_job_name"]
            endpoint = known_endpoints.get(identity) or self._receipt_endpoint(row)
            try:
                receipts.append(
                    self._runtime(row).teardown(
                        endpoint=endpoint,
                        timeout_seconds=timeout_seconds,
                        poll_interval=poll_interval,
                    )
                )
            except (
                OSError,
                httpx.HTTPError,
                RuntimeError,
                subprocess.SubprocessError,
                TimeoutError,
            ) as exc:
                errors[identity] = (str(exc).strip() or type(exc).__name__)[:160]

        remaining = self._runtime(rows[0])._gateway_endpoints()
        if remaining:
            errors["gateway"] = "owned model still has registered endpoints"
        if errors:
            details = ", ".join(f"{key}:{value}" for key, value in sorted(errors.items()))
            raise RuntimeError(f"fleet teardown incomplete: {details}")
        return {
            "schema_version": "miaoda.serving_teardown.v2",
            "completed_at": _now(),
            "model_name": self.config["model_name"],
            "removed_endpoints": sorted(
                {
                    endpoint
                    for receipt in receipts
                    for endpoint in receipt.get("removed_endpoints", [])
                }
            ),
            "remaining_endpoints": remaining,
            "jobs": receipts,
        }


class RolloutRewardWorkflow:
    """Idempotent coordinator with injectable provider adapters for tests."""

    def __init__(
        self,
        manifest: Mapping[str, Any],
        manifest_hash: str,
        *,
        actual_job_name: str,
        datawheel: AgentOSClient,
        goblin: GoblinRewardClient | None,
        serving: ServingFleet,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        self.manifest = dict(manifest)
        self.manifest_hash = manifest_hash
        self.actual_job_name = actual_job_name
        self.datawheel = datawheel
        self.goblin = goblin
        self.serving = serving
        self.sleep = sleep
        self.log = log
        self.runtime = _mapping(manifest["runtime"], "runtime")
        self.rollout = _mapping(manifest["rollout"], "rollout")
        self.reward = _mapping(manifest["reward"], "reward")
        self.teardown = _mapping(manifest["teardown"], "teardown")
        self.output_root = Path(str(self.runtime["output_root"]))
        self.state_path = self.output_root / "state.json"

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.is_file():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise RuntimeError("workflow state is not an object")
            if state.get("manifest_sha256") != self.manifest_hash:
                raise RuntimeError("workflow manifest changed after execution began")
            if state.get("actual_job_name") != self.actual_job_name:
                raise RuntimeError("workflow state belongs to a different LTP job")
            return state
        return {
            "schema_version": "miaoda.rollout_reward_state.v2",
            "pipeline_name": self.manifest["pipeline_name"],
            "manifest_sha256": self.manifest_hash,
            "actual_job_name": self.actual_job_name,
            "phase": "waiting_service",
            "created_at": _now(),
            "updated_at": _now(),
        }

    def _save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _now()
        _atomic_write(self.state_path, state)

    def _wait_for_service(self, state: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + float(self.runtime["service_timeout_seconds"])
        failures = 0
        while True:
            try:
                receipt = self._refresh_serving_capacity(state)
                minimum = int(_mapping(self.manifest["serving"], "serving")["min_ready_endpoints"])
                if int(receipt["endpoint_count"]) >= minimum:
                    state["phase"] = "service_ready"
                    state["serving_health_failures"] = 0
                    self._save(state)
                    self.log(
                        "service ready "
                        f"ready={receipt['ready_replicas']}/"
                        f"{receipt['requested_replicas']} "
                        f"endpoints={receipt['endpoint_count']} "
                        f"concurrency={state['effective_concurrency']}"
                    )
                    return receipt
                raise ServiceNotReady(
                    f"healthy_endpoints_{receipt['endpoint_count']}_below_{minimum}"
                )
            except (ServiceNotReady, OSError, httpx.HTTPError, subprocess.SubprocessError) as exc:
                failures += 1
                state["phase"] = "waiting_service"
                state["serving_health_failures"] = failures
                state["last_serving_error"] = type(exc).__name__
                self._save(state)
                if time.monotonic() >= deadline:
                    raise TimeoutError("serving readiness timed out") from exc
                self.sleep(float(self.runtime["poll_interval_seconds"]))

    def _refresh_serving_capacity(
        self,
        state: dict[str, Any],
        *,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        receipt = self.serving.probe()
        ready_replicas = int(receipt["ready_replicas"])
        healthy_endpoints = int(receipt["endpoint_count"])
        per_endpoint = int(self.rollout["per_endpoint_concurrency"])
        configured_max = int(self.rollout["configured_max_concurrency"])
        effective = min(configured_max, healthy_endpoints * per_endpoint)
        state["serving"] = receipt
        state["requested_replicas"] = int(receipt["requested_replicas"])
        state["ready_replicas"] = ready_replicas
        state["endpoint_count"] = healthy_endpoints
        state["effective_concurrency"] = effective
        state["configured_max_concurrency"] = configured_max
        state["per_endpoint_concurrency"] = per_endpoint
        state.pop("concurrency_update_deferred", None)
        if batch_id and state.get("applied_concurrency") != effective:
            if effective == 0:
                state["concurrency_update_deferred"] = (
                    "zero healthy endpoints; concurrency=0 is not applied automatically "
                    "because it can terminate the batch"
                )
            else:
                try:
                    self.datawheel.update_concurrency(batch_id, effective)
                except AgentOSClientError as exc:
                    state["last_concurrency_update_error"] = type(exc).__name__
                    state["concurrency_update_attempted_at"] = _now()
                else:
                    state["applied_concurrency"] = effective
                    state["concurrency_updated_at"] = _now()
                    state.pop("last_concurrency_update_error", None)
                    self.log(
                        f"batch={batch_id} concurrency={effective} "
                        f"healthy_endpoints={healthy_endpoints}"
                    )
        self._save(state)
        return receipt

    def _matching_batches(self) -> list[str]:
        task_name = str(self.rollout["task_name"])
        matches: list[str] = []
        for page in range(1, 11):
            rows = self.datawheel.list_batches(page=page, page_size=100)
            for row in rows:
                name = str(row.get("task_name") or row.get("taskName") or row.get("name") or "")
                deleted = row.get("deleted")
                if name == task_name and deleted not in {True, "1", "true", "True"}:
                    identifier = str(
                        row.get("batch_task_id") or row.get("batchTaskId") or row.get("id") or ""
                    ).strip()
                    if identifier:
                        matches.append(identifier)
            if len(rows) < 100:
                break
        return sorted(set(matches))

    def _ensure_batch(self, state: dict[str, Any]) -> str:
        if state.get("batch_task_id"):
            return str(state["batch_task_id"])
        matches = self._matching_batches()
        if len(matches) > 1:
            raise RuntimeError("multiple Datawheel batches share the formal task name")
        if matches:
            state["batch_task_id"] = matches[0]
            state["phase"] = "rollout_submitted"
            state["rollout_recovered"] = True
            self._save(state)
            return matches[0]

        records = self.datawheel.get_dataset_records(str(self.rollout["dataset_name"]))
        expected = int(self.rollout["expected_records"])
        if len(records) != expected:
            raise RuntimeError(f"dataset record count is {len(records)}, expected {expected}")
        coding = _mapping(self.rollout["coding_config"], "rollout.coding_config")
        model = _mapping(coding["codingPromptiModelConfig"], "codingPromptiModelConfig")
        model_key = _environment_value(str(model["api_key_env"]))
        generated_user = _environment_value(str(self.rollout["generated_user_id_env"]))
        effective_concurrency = int(state.get("effective_concurrency") or 0)
        if effective_concurrency < 1:
            raise ServiceNotReady("rollout submission requires a healthy endpoint")
        payload = build_miaoda_dataset_batch_payload(
            records,
            task_name=str(self.rollout["task_name"]),
            task_name_postfix=str(self.rollout["task_name_postfix"]),
            dataset_name=str(self.rollout["dataset_name"]),
            generated_user_id=generated_user,
            model_name=str(model["model"]),
            model_api_url=str(model["api_url"]),
            model_api_key=model_key,
            provider=str(model["provider"]),
            coding_prompti_variant=str(coding["codingPromptiVariant"]),
            environment=str(self.rollout["environment"]),
            query_mode=str(self.rollout["query_mode"]),
            pipeline=str(self.rollout["pipeline"]),
            concurrency=effective_concurrency,
            max_tokens=int(model["max_tokens"]),
            timeout_seconds=int(model["timeout"]),
            temperature=float(model["temperature"]),
            top_p=float(model["top_p"]),
            top_k=int(model["top_k"]),
            min_p=float(model["min_p"]),
            presence_penalty=float(model["presence_penalty"]),
            repetition_penalty=float(model["repetition_penalty"]),
            thinking_config=_mapping(model["thinking_config"], "thinking_config"),
            remaining_auto_retry_count=int(self.rollout["remaining_auto_retry_count"]),
        )
        identity = miaoda_batch_identity(payload)
        state["phase"] = "submitting_rollout"
        state["rollout_idempotency_key"] = identity
        state["dataset_record_count"] = expected
        self._save(state)
        submitted = self.datawheel.submit_batch(payload, idempotency_key=identity)
        state["batch_task_id"] = submitted.batch_task_id
        state["phase"] = "rollout_submitted"
        state["batch_status"] = submitted.status
        state["applied_concurrency"] = effective_concurrency
        state["rollout_submitted_at"] = _now()
        self._save(state)
        self.log(f"submitted rollout batch={submitted.batch_task_id}")
        return submitted.batch_task_id

    def _poll_rollout(self, state: dict[str, Any], batch_id: str) -> str:
        deadline = time.monotonic() + float(self.runtime["rollout_timeout_seconds"])
        while True:
            snapshot = self.datawheel.get_batch(batch_id)
            phase = rollout_phase(
                snapshot,
                expected_total=int(self.rollout["expected_records"]),
            )
            state["phase"] = "waiting_rollout" if phase == "running" else "rollout_terminal"
            state["batch_status"] = snapshot.status
            state["status_counts"] = snapshot.status_counts
            state["total_size"] = snapshot.total_size
            state["rollout_phase"] = phase
            remote_concurrency = snapshot.raw.get("concurrency")
            try:
                observed_concurrency = int(remote_concurrency)
            except (TypeError, ValueError):
                observed_concurrency = None
            if observed_concurrency is not None and observed_concurrency >= 0:
                state["applied_concurrency"] = observed_concurrency
                state["observed_remote_concurrency"] = observed_concurrency
            self._save(state)
            self.log(
                f"batch={batch_id} phase={phase} counts="
                f"{json.dumps(snapshot.status_counts, sort_keys=True)}"
            )
            if phase != "running":
                state["formal_metric_eligible"] = phase == "completed"
                state["rollout_terminal_at"] = _now()
                submitted_at = str(state.get("rollout_submitted_at") or "")
                try:
                    submitted_time = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
                    terminal_time = datetime.fromisoformat(
                        state["rollout_terminal_at"].replace("Z", "+00:00")
                    )
                except ValueError:
                    pass
                else:
                    state["rollout_elapsed_seconds"] = round(
                        (terminal_time - submitted_time).total_seconds(), 3
                    )
                self._save(state)
                return phase
            self._refresh_serving_capacity(state, batch_id=batch_id)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"rollout polling timed out: {batch_id}")
            self.sleep(float(self.runtime["poll_interval_seconds"]))

    def _ensure_teardown(self, state: dict[str, Any]) -> None:
        if state.get("teardown_complete"):
            return
        while True:
            state["phase"] = "tearing_down"
            state["teardown_state"] = "retrying"
            self._save(state)
            snapshot = state.get("serving")
            try:
                receipt = self.serving.teardown(
                    serving_snapshot=snapshot if isinstance(snapshot, Mapping) else None,
                    timeout_seconds=int(self.runtime["teardown_timeout_seconds"]),
                    poll_interval=float(self.runtime["poll_interval_seconds"]),
                )
            except (
                OSError,
                httpx.HTTPError,
                RuntimeError,
                subprocess.SubprocessError,
                TimeoutError,
            ) as exc:
                state["last_teardown_error"] = (str(exc).strip() or type(exc).__name__)[:300]
                state["teardown_attempted_at"] = _now()
                self._save(state)
                self.sleep(float(self.runtime["poll_interval_seconds"]))
                continue
            state["teardown_complete"] = True
            state["teardown_state"] = "complete"
            state["teardown"] = receipt
            state["phase"] = "serving_released"
            state.pop("last_teardown_error", None)
            self._save(state)
            _atomic_write(self.output_root / "TEARDOWN_COMPLETE.json", receipt)
            return

    def _ensure_reward(self, state: dict[str, Any], batch_id: str) -> int:
        if not self.reward.get("enabled"):
            state["phase"] = "complete"
            self._save(state)
            return 0
        if self.goblin is None:
            raise WorkflowConfigurationError(
                "Reward is enabled but no Reward client was configured"
            )
        if state.get("phase") == "reward_submitting" and not state.get("goblin_job_id"):
            raise RuntimeError("ambiguous Reward submission requires receipt reconciliation")

        artifact = self.output_root / "rollout_result.xlsx"
        if not state.get("goblin_job_id"):
            state["phase"] = "downloading_rollout_result"
            self._save(state)
            self.datawheel.download_result(batch_id, artifact)
            state["phase"] = "reward_submitting"
            state["rollout_artifact"] = str(artifact)
            self._save(state)
            job = self.goblin.submit_file(
                artifact,
                rollout_model=str(self.reward["rollout_model"]),
                benchmark_dataset_name=str(self.reward["benchmark_dataset_name"]),
                upload=True,
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
                api_env=str(self.rollout["environment"]),
                description=(
                    f"{self.reward['rollout_model']}; batch {batch_id}; "
                    f"{self.reward['benchmark_dataset_name']}; reward "
                    f"{self.reward['prompt_version']}; c{self.reward['concurrency']}"
                ),
            )
            state["goblin_job_id"] = job.job_id
            state["goblin"] = _safe_goblin(job)
            state["phase"] = "reward_submitted"
            self._save(state)
            self.log(f"submitted reward job={job.job_id}")

        if not self.reward.get("wait_for_completion"):
            return 0
        deadline = time.monotonic() + float(self.runtime["reward_timeout_seconds"])
        while True:
            observed = self.goblin.get_job(str(state["goblin_job_id"]))
            state["goblin"] = _safe_goblin(observed)
            state["phase"] = "reward_completed" if observed.successful else "waiting_reward"
            self._save(state)
            if observed.status in {"failed", "cancelled"}:
                state["phase"] = "reward_failed"
                self._save(state)
                return 2
            if observed.successful:
                state["phase"] = "complete"
                state["completed_at"] = _now()
                self._save(state)
                return 0
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Reward polling timed out: {observed.job_id}")
            self.sleep(float(self.runtime["poll_interval_seconds"]))

    def run(self) -> int:
        state = self._load_state()
        if state.get("phase") == "complete":
            self.log("workflow already complete")
            return 0

        batch_id = str(state.get("batch_task_id") or "")
        terminal_phase: str | None = None
        if batch_id:
            snapshot = self.datawheel.get_batch(batch_id)
            terminal_phase = rollout_phase(
                snapshot,
                expected_total=int(self.rollout["expected_records"]),
            )
            if terminal_phase == "running":
                terminal_phase = None
            else:
                state["batch_status"] = snapshot.status
                state["status_counts"] = snapshot.status_counts
                state["rollout_phase"] = terminal_phase
                state["formal_metric_eligible"] = terminal_phase == "completed"
                self._save(state)

        if terminal_phase is None:
            self._wait_for_service(state)
            batch_id = self._ensure_batch(state)
            terminal_phase = self._poll_rollout(state, batch_id)

        self._ensure_teardown(state)
        if terminal_phase != "completed":
            state["phase"] = "rollout_ineligible"
            state["formal_metric_eligible"] = False
            self._save(state)
            return 2
        return self._ensure_reward(state, batch_id)


def _safe_dry_run(manifest: Mapping[str, Any], manifest_hash: str, job_name: str) -> dict[str, Any]:
    rollout = _mapping(manifest["rollout"], "rollout")
    reward = _mapping(manifest["reward"], "reward")
    serving = _mapping(manifest["serving"], "serving")
    model = _mapping(
        _mapping(rollout["coding_config"], "coding_config")["codingPromptiModelConfig"],
        "codingPromptiModelConfig",
    )
    return {
        "manifest_sha256": manifest_hash,
        "pipeline_name": manifest["pipeline_name"],
        "actual_job_name": job_name,
        "serving_model": serving["model_name"],
        "readiness_file": serving["readiness_file"],
        "readiness_dir": serving["readiness_dir"],
        "requested_replicas": serving["requested_replicas"],
        "min_ready_endpoints": serving["min_ready_endpoints"],
        "dataset": rollout["dataset_name"],
        "expected_records": rollout["expected_records"],
        "rollout_concurrency": rollout["concurrency"],
        "per_endpoint_concurrency": rollout["per_endpoint_concurrency"],
        "configured_max_concurrency": rollout["configured_max_concurrency"],
        "coding_config": {
            "codingPromptiVariant": rollout["coding_config"]["codingPromptiVariant"],
            "codingPromptiModelConfig": {
                key: value for key, value in model.items() if key != "api_key_env"
            }
            | {"api_key_env": model["api_key_env"]},
        },
        "reward": (
            {
                key: reward[key]
                for key in (
                    "benchmark_dataset_name",
                    "rollout_model",
                    "query_round",
                    "prompt_version",
                    "reward_model",
                    "concurrency",
                    "eval_mm",
                    "eval_miniprog",
                )
            }
            if reward.get("enabled")
            else {"enabled": False}
        ),
        "teardown_serving_after_rollout": True,
    }


def _recover_primary_job(manifest: Mapping[str, Any], supplied: str | None) -> str:
    if str(supplied or "").strip():
        return str(supplied).strip()
    runtime = _mapping(manifest["runtime"], "runtime")
    output_root = Path(str(runtime["output_root"]))
    state_path = output_root / "state.json"
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        identity = str(state.get("actual_job_name") or "").strip()
        if identity:
            return identity
    rows = _read_job_ledger(output_root / "submitted_jobs.tsv")
    return rows[0]["actual_job_name"] if rows else ""


def _inspect_ltp_job(serving: Mapping[str, Any], actual_job_name: str) -> dict[str, str]:
    command = [
        "ltp-cli",
        "show",
        actual_job_name,
        "--user",
        str(serving["ltp_user"]),
    ]
    observed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=25,
        env=ServingRuntime._ltp_env(),
        check=False,
    )
    if observed.returncode != 0:
        raise RuntimeError("attached LTP job could not be verified")
    status = _normalize_status(ServingRuntime._ltp_status(observed.stdout))
    if status not in {"created", "submitted", "pending", "running"}:
        raise RuntimeError(f"attached LTP job is not live or queued: {status}")
    result = {"status": status}
    for key in ("cluster", "total_nodes", "submission_time"):
        match = re.search(rf"^{key}:\s*(.+?)\s*$", observed.stdout, flags=re.MULTILINE)
        if match:
            result[key] = match.group(1).strip()
    return result


def migrate_workflow_state(
    manifest: Mapping[str, Any],
    manifest_hash: str,
    actual_job_name: str,
) -> dict[str, Any]:
    runtime = _mapping(manifest["runtime"], "runtime")
    output_root = Path(str(runtime["output_root"]))
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root / "workflow.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("stop the running supervisor before state migration") from exc
        rows = ensure_primary_job_ledger(manifest, actual_job_name)
        state_path = output_root / "state.json"
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("pipeline_name") != manifest["pipeline_name"]:
                raise RuntimeError("workflow state belongs to a different pipeline")
            if state.get("actual_job_name") != actual_job_name:
                raise RuntimeError("workflow state belongs to a different primary LTP job")
            previous_hash = str(state.get("manifest_sha256") or "")
            history = list(state.get("manifest_sha256_history") or [])
            if previous_hash and previous_hash != manifest_hash and previous_hash not in history:
                history.append(previous_hash)
            state["manifest_sha256_history"] = history
            state["manifest_sha256"] = manifest_hash
            state["schema_version"] = "miaoda.rollout_reward_state.v2"
            state["requested_replicas"] = len(rows)
            state["migrated_at"] = _now()
            state["updated_at"] = _now()
        else:
            state = {
                "schema_version": "miaoda.rollout_reward_state.v2",
                "pipeline_name": manifest["pipeline_name"],
                "manifest_sha256": manifest_hash,
                "manifest_sha256_history": [],
                "actual_job_name": actual_job_name,
                "requested_replicas": len(rows),
                "phase": "waiting_service",
                "created_at": _now(),
                "migrated_at": _now(),
                "updated_at": _now(),
            }
        _atomic_write(state_path, state)
        return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--job-name", help="Actual suffixed LTP job identity")
    parser.add_argument("--attach-job", help="Attach an additional exact LTP job identity")
    parser.add_argument("--migrate-state", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest_path = args.manifest.expanduser().resolve(strict=True)
    manifest, manifest_hash = load_manifest(manifest_path)
    serving_config = _mapping(manifest["serving"], "serving")
    job_env = str(serving_config["actual_job_name_env"])
    actual_job_name = _recover_primary_job(
        manifest,
        str(args.job_name or os.environ.get(job_env) or ""),
    )
    if args.dry_run:
        print(
            json.dumps(
                _safe_dry_run(
                    manifest,
                    manifest_hash,
                    actual_job_name or f"<from {job_env}>",
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.attach_job:
        observation = _inspect_ltp_job(serving_config, str(args.attach_job).strip())
        row, added = attach_job_to_ledger(
            manifest,
            str(args.attach_job).strip(),
            observation,
        )
        print(
            json.dumps(
                {
                    "attached": added,
                    "actual_job_name": row["actual_job_name"],
                    "cluster": row["cluster"],
                    "readiness_file": row["readiness_file"],
                    "gateway_preclean": row["gateway_preclean"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not actual_job_name:
        raise SystemExit(f"--job-name or {job_env} is required")
    if args.migrate_state:
        state = migrate_workflow_state(manifest, manifest_hash, actual_job_name)
        print(
            json.dumps(
                {
                    "pipeline_name": state["pipeline_name"],
                    "phase": state["phase"],
                    "actual_job_name": state["actual_job_name"],
                    "requested_replicas": state["requested_replicas"],
                    "manifest_sha256": state["manifest_sha256"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    runtime = _mapping(manifest["runtime"], "runtime")
    rollout = _mapping(manifest["rollout"], "rollout")
    reward = _mapping(manifest["reward"], "reward")
    output_root = Path(str(runtime["output_root"]))
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root / "workflow.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"workflow already running: {output_root}", file=sys.stderr)
            return 0
        try:
            ensure_primary_job_ledger(manifest, actual_job_name)
            model_key = _environment_value(str(serving_config["model_api_key_env"]))
            datawheel = AgentOSClient(
                str(rollout["datawheel_url"]),
                token_env=str(rollout["datawheel_token_env"]),
                timeout_seconds=180.0,
                trust_env=False,
            )
            goblin = None
            if reward.get("enabled"):
                goblin = GoblinRewardClient(
                    str(reward["goblin_url"]),
                    token_env=str(reward["goblin_token_env"]),
                    timeout_seconds=180.0,
                    trust_env=False,
                )
            serving_fleet = ServingFleet(
                manifest,
                model_api_key=model_key,
            )
            return RolloutRewardWorkflow(
                manifest,
                manifest_hash,
                actual_job_name=actual_job_name,
                datawheel=datawheel,
                goblin=goblin,
                serving=serving_fleet,
            ).run()
        except (
            AgentOSClientError,
            GoblinRewardError,
            WorkflowConfigurationError,
            ServiceNotReady,
            httpx.HTTPError,
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            TimeoutError,
        ) as exc:
            print(f"workflow failed: {type(exc).__name__}: {str(exc)[:500]}", file=sys.stderr)
            return 1
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    raise SystemExit(main())

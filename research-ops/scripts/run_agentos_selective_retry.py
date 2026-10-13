#!/usr/bin/env python3
"""Supervise evidence-based Datawheel record retries and serving teardown."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from research_ops.agentos import AgentOSClient, AgentOSClientError

ACTIVE_STATUSES = {"pending", "running"}
TERMINAL_STATUSES = {
    "completed",
    "failed",
    "stopped",
    "terminated",
    "cancelled",
    "canceled",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize(value: Any) -> str:
    return str(value or "unknown").strip().casefold().replace("-", "_")


def eligible_failure_category(record: Mapping[str, Any]) -> str | None:
    """Return the retryable service/transport class for one failed record."""

    if _normalize(record.get("status")) != "failed":
        return None
    exception = str(record.get("exception_type") or "").strip()
    message = str(record.get("error_msg") or "")
    lowered = message.casefold()
    service_markers = (
        "请求后端转发实例失败",
        "no endpoint",
        "underlying forwarding instance",
        "connection refused",
        "connection reset",
        "disconnected",
    )
    if (
        exception == "PromptiCallError"
        and "http 500" in lowered
        and any(marker in lowered or marker in message for marker in service_markers)
    ):
        return "model_service_http_500_transport"
    timeout_markers = ("stream", "request", "read", "connect", "pipeline")
    if exception == "PipelineStreamTimeoutError" or (
        "timeout" in lowered and any(marker in lowered for marker in timeout_markers)
    ):
        return "pipeline_stream_timeout"
    return None


def residual_failure_category(record: Mapping[str, Any]) -> str:
    message = str(record.get("error_msg") or "")
    lowered = message.casefold()
    if "maximum iteration" in lowered or "最大迭代" in message:
        return "iteration_limit"
    if "401" in lowered or "authentication" in lowered or "publish" in lowered:
        return "authentication_or_publish"
    if "not valid json" in lowered or "非合法 json" in lowered:
        return "application_invalid_json"
    if "未知错误" in message:
        return "unclassified_agent_failure"
    exception = str(record.get("exception_type") or "unknown").strip()
    return f"ineligible_{exception}"


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_object(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid {name}: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} is not an object: {path}")
    return value


def _require_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    return text


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = _load_object(path, "selective retry manifest")
    if manifest.get("schema_version") != "miaoda.selective_retry_manifest.v1":
        raise ValueError("unsupported selective retry manifest schema")
    for section in ("runtime", "datawheel", "service", "retry", "teardown"):
        if not isinstance(manifest.get(section), dict):
            raise ValueError(f"manifest.{section} must be an object")
    runtime = manifest["runtime"]
    output_root = Path(_require_text(runtime.get("output_root"), "runtime.output_root"))
    audit_file = Path(_require_text(runtime.get("audit_file"), "runtime.audit_file"))
    readiness_file = Path(
        _require_text(manifest["service"].get("readiness_file"), "service.readiness_file")
    )
    if (
        not output_root.is_absolute()
        or not audit_file.is_absolute()
        or not readiness_file.is_absolute()
    ):
        raise ValueError("runtime, audit, and readiness paths must be absolute")
    token_env = _require_text(manifest["datawheel"].get("token_env"), "datawheel.token_env")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", token_env):
        raise ValueError("datawheel.token_env must be an environment reference")
    retry = manifest["retry"]
    for key in (
        "concurrency",
        "group_size",
        "poll_interval_seconds",
        "canary_timeout_seconds",
        "rollout_timeout_seconds",
        "max_retry_rounds",
    ):
        if int(retry.get(key) or 0) < 1:
            raise ValueError(f"retry.{key} must be positive")
    if int(retry["concurrency"]) > 80 or int(retry["group_size"]) > 100:
        raise ValueError("retry concurrency or group size exceeds the bounded limit")
    if manifest["teardown"].get("enabled") is not True:
        raise ValueError("serving teardown must be enabled")
    if manifest.get("reward", {}).get("enabled") is not False:
        raise ValueError("selective retry does not authorize Reward")
    return manifest


class SelectiveRetrySupervisor:
    def __init__(self, manifest: Mapping[str, Any], *, log=print) -> None:
        self.manifest = dict(manifest)
        self.runtime = dict(manifest["runtime"])
        self.datawheel_config = dict(manifest["datawheel"])
        self.service = dict(manifest["service"])
        self.retry = dict(manifest["retry"])
        self.teardown_config = dict(manifest["teardown"])
        self.output_root = Path(self.runtime["output_root"])
        self.audit_path = Path(self.runtime["audit_file"])
        self.state_path = self.output_root / "runtime/state.json"
        self.log = log
        self.client = AgentOSClient(
            self.datawheel_config["base_url"],
            token_env=self.datawheel_config["token_env"],
            timeout_seconds=120,
            trust_env=False,
        )
        self.audit = _load_object(self.audit_path, "failure audit")
        self.state = _load_object(self.state_path, "selective retry state")
        if self.audit.get("batch_task_id") != self.datawheel_config.get("batch_task_id"):
            raise RuntimeError("failure audit belongs to a different batch")
        if self.state.get("batch_task_id") != self.datawheel_config.get("batch_task_id"):
            raise RuntimeError("retry state belongs to a different batch")
        self.selected_cases = sorted(
            {int(item["case_id"]) for item in self.audit["selected_records"]}
        )
        if len(self.selected_cases) != int(self.audit["selected_count"]):
            raise RuntimeError("failure audit contains duplicate selected case IDs")

    def _save(self) -> None:
        self.state["updated_at_utc"] = _now()
        _atomic_write(self.state_path, self.state)

    def _ltp(self, action: str, *, logs: bool = False) -> subprocess.CompletedProcess[str]:
        command = [
            "ltp-cli",
            action,
            self.service["ltp_job"],
            "--user",
            self.service["ltp_user"],
        ]
        if logs:
            command.append("--logs")
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
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=25,
            env=environment,
            check=False,
        )

    @staticmethod
    def _ltp_status(output: str) -> str:
        match = re.search(r"^status:\s*(\S+)\s*$", output, flags=re.MULTILINE)
        return _normalize(match.group(1) if match else "unknown")

    def _gateway_endpoints(self, model_name: str) -> list[str]:
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.get(
                f"{self.service['gateway_control_url'].rstrip('/')}/proxy/endpoint/list",
                params={"model_name": model_name},
            )
        if response.status_code == 400 and "not exists" in response.text.casefold():
            return []
        response.raise_for_status()
        items = ((response.json().get("data") or {}).get("endpoints")) or []
        return [
            str(item.get("endpoint") if isinstance(item, Mapping) else item).strip()
            for item in items
            if str(item.get("endpoint") if isinstance(item, Mapping) else item).strip()
        ]

    def verify_service(self) -> dict[str, Any]:
        shown = self._ltp("show")
        if shown.returncode != 0 or self._ltp_status(shown.stdout) != "running":
            raise RuntimeError("serving LTP job is not Running")
        readiness = _load_object(Path(self.service["readiness_file"]), "readiness receipt")
        if readiness.get("job_name") != self.service["ltp_job"]:
            raise RuntimeError("readiness receipt belongs to a different LTP job")
        if readiness.get("endpoint") != self.service["endpoint"]:
            raise RuntimeError("readiness receipt endpoint mismatch")
        if readiness.get("model_name") != self.service["primary_model"]:
            raise RuntimeError("readiness receipt primary model mismatch")
        if any(
            readiness.get(key) != 200
            for key in (
                "health_http_status",
                "local_generation_http_status",
                "gateway_generation_http_status",
            )
        ):
            raise RuntimeError("readiness receipt lacks successful generation evidence")
        alias = self.service["batch_model_alias"]
        endpoint = self.service["endpoint"]
        endpoints = self._gateway_endpoints(alias)
        if endpoints != [endpoint]:
            raise RuntimeError("batch model alias does not have the exact serving endpoint")
        payload = {
            "model": alias,
            "messages": [{"role": "user", "content": "2+2等于几？只回答数字。"}],
            "max_tokens": 128,
            "temperature": 0,
            "chat_template_kwargs": {
                "enable_thinking": True,
                "preserve_thinking": True,
            },
        }
        with httpx.Client(timeout=300, trust_env=False) as client:
            health = client.get(f"{endpoint.rstrip('/')}/health")
            health.raise_for_status()
            generated = client.post(
                f"{self.service['gateway_api_url'].rstrip('/')}/v1/chat/completions",
                json=payload,
            )
            generated.raise_for_status()
        body = generated.json()
        message = body["choices"][0]["message"]
        if body.get("model") != alias or not (
            message.get("content") or message.get("reasoning_content")
        ):
            raise RuntimeError("batch model alias gateway generation was invalid")
        evidence = {
            "verified_at_utc": _now(),
            "ltp_status": "Running",
            "endpoint": endpoint,
            "batch_model_alias": alias,
            "registered_endpoints": endpoints,
            "direct_health_http_status": health.status_code,
            "gateway_generation_http_status": generated.status_code,
            "gateway_finish_reason": body["choices"][0].get("finish_reason"),
        }
        self.state["last_service_gate"] = evidence
        self._save()
        return evidence

    def _records_by_case(self) -> dict[int, dict[str, Any]]:
        records = self.client.list_batch_records(
            self.datawheel_config["batch_task_id"],
            page_size=200,
        )
        expected = int(self.datawheel_config["expected_records"])
        if len(records) != expected:
            raise RuntimeError(f"Datawheel returned {len(records)} records, expected {expected}")
        by_case: dict[int, dict[str, Any]] = {}
        for record in records:
            case_id = record.get("case_id")
            if not isinstance(case_id, int) or isinstance(case_id, bool):
                raise RuntimeError("Datawheel record has an invalid case_id")
            if case_id in by_case:
                raise RuntimeError("Datawheel returned duplicate case_id values")
            by_case[case_id] = record
        return by_case

    def _selected_status_counts(
        self,
        records: Mapping[int, Mapping[str, Any]],
    ) -> dict[str, int]:
        return dict(
            sorted(
                Counter(
                    _normalize(records[case_id].get("status")) for case_id in self.selected_cases
                ).items()
            )
        )

    def _heartbeat(
        self,
        records: Mapping[int, Mapping[str, Any]],
        *,
        phase: str,
    ) -> None:
        snapshot = self.client.get_batch(self.datawheel_config["batch_task_id"])
        counts = self._selected_status_counts(records)
        self.state.update(
            {
                "phase": phase,
                "batch_status": snapshot.status,
                "batch_status_counts": snapshot.status_counts,
                "selected_status_counts": counts,
                "concurrency": snapshot.raw.get("concurrency"),
                "heartbeat_at_utc": _now(),
            }
        )
        self._save()
        self.log(
            f"phase={phase} batch={snapshot.status} selected={json.dumps(counts, sort_keys=True)}"
        )

    def _ensure_concurrency(self) -> None:
        expected = int(self.retry["concurrency"])
        snapshot = self.client.get_batch(self.datawheel_config["batch_task_id"])
        if int(snapshot.raw.get("concurrency") or 0) != expected:
            self.client.update_concurrency(self.datawheel_config["batch_task_id"], expected)
        self.state["concurrency"] = expected
        self._save()

    def _restart_cases(self, case_ids: list[int], *, label: str) -> list[int]:
        records = self._records_by_case()
        candidates: list[tuple[int, str, str]] = []
        for case_id in case_ids:
            record = records[case_id]
            category = eligible_failure_category(record)
            if category:
                candidates.append((case_id, str(record["id"]), category))
        receipt: dict[str, Any] = {
            "schema_version": "miaoda.selective_retry_submission.v1",
            "label": label,
            "submitted_at_utc": _now(),
            "batch_task_id": self.datawheel_config["batch_task_id"],
            "skip_check": False,
            "requested_case_count": len(case_ids),
            "eligible_case_count": len(candidates),
            "group_size": int(self.retry["group_size"]),
            "groups": [],
        }
        submitted: list[int] = []
        group_size = int(self.retry["group_size"])
        for offset in range(0, len(candidates), group_size):
            group = candidates[offset : offset + group_size]
            record_ids = [item[1] for item in group]
            case_group = [item[0] for item in group]
            self.state["restart_inflight"] = {
                "label": label,
                "case_ids": case_group,
                "record_ids": record_ids,
                "started_at_utc": _now(),
            }
            self._save()
            response = self.client.restart_records(record_ids, skip_check=False)
            submitted.extend(case_group)
            receipt["groups"].append(
                {
                    "case_ids": case_group,
                    "record_ids": record_ids,
                    "categories": [item[2] for item in group],
                    "response_type": type(response).__name__,
                    "response_keys": sorted(response) if isinstance(response, dict) else [],
                    "accepted_at_utc": _now(),
                }
            )
            self.state.pop("restart_inflight", None)
            self.state["last_restart_group"] = {
                "label": label,
                "case_ids": case_group,
                "accepted_at_utc": _now(),
            }
            self._save()
        receipt["submitted_case_count"] = len(submitted)
        receipt["submitted_case_ids"] = submitted
        _atomic_write(self.output_root / f"retry/{label}-submission.json", receipt)
        return submitted

    def _wait_for_canary(self) -> None:
        case_id = int(self.retry["canary_case_id"])
        deadline = time.monotonic() + int(self.retry["canary_timeout_seconds"])
        while True:
            records = self._records_by_case()
            record = records[case_id]
            status = _normalize(record.get("status"))
            self.state["canary_current_record_id"] = str(record.get("id") or "")
            self.state["canary_status"] = status
            self._heartbeat(records, phase="canary_running")
            if status == "completed":
                _atomic_write(
                    self.output_root / "retry/canary-result.json",
                    {
                        "schema_version": "miaoda.selective_retry_canary_result.v1",
                        "completed_at_utc": _now(),
                        "case_id": case_id,
                        "record_id": record.get("id"),
                        "status": status,
                        "generated_app_id_present": bool(record.get("generated_app_id")),
                    },
                )
                return
            if status in TERMINAL_STATUSES - {"completed"}:
                category = eligible_failure_category(record)
                raise RuntimeError(
                    f"canary retry failed with {category or residual_failure_category(record)}"
                )
            if status not in ACTIVE_STATUSES:
                raise RuntimeError(f"canary entered unexpected status: {status}")
            if time.monotonic() >= deadline:
                raise TimeoutError("canary retry timed out")
            time.sleep(int(self.retry["poll_interval_seconds"]))

    def _wait_for_round(self, *, label: str) -> dict[int, dict[str, Any]]:
        deadline = time.monotonic() + int(self.retry["rollout_timeout_seconds"])
        while True:
            records = self._records_by_case()
            counts = self._selected_status_counts(records)
            self._heartbeat(records, phase=f"{label}_running")
            if not any(status in ACTIVE_STATUSES for status in counts):
                return records
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{label} selective retry timed out")
            time.sleep(int(self.retry["poll_interval_seconds"]))

    def _write_final(self, records: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
        selected = []
        for case_id in self.selected_cases:
            record = records[case_id]
            status = _normalize(record.get("status"))
            selected.append(
                {
                    "case_id": case_id,
                    "record_id": record.get("id"),
                    "status": status,
                    "failure_category": (
                        eligible_failure_category(record)
                        or (residual_failure_category(record) if status == "failed" else None)
                    ),
                }
            )
        residual = []
        for item in self.audit["residual_records"]:
            case_id = int(item["case_id"])
            record = records[case_id]
            residual.append(
                {
                    "case_id": case_id,
                    "record_id": record.get("id"),
                    "status": _normalize(record.get("status")),
                    "failure_category": residual_failure_category(record),
                }
            )
        payload = {
            "schema_version": "miaoda.selective_retry_final.v1",
            "completed_at_utc": _now(),
            "batch_task_id": self.datawheel_config["batch_task_id"],
            "retry_rounds_used": self.state.get("retry_round"),
            "selected_status_counts": dict(
                sorted(Counter(item["status"] for item in selected).items())
            ),
            "selected_records": selected,
            "original_residual_records": residual,
            "reward_submitted": False,
        }
        _atomic_write(self.output_root / "retry/final-result.json", payload)
        return payload

    def _remove_owned_model(self, model_name: str) -> dict[str, Any]:
        endpoint = self.service["endpoint"]
        control = self.service["gateway_control_url"].rstrip("/")
        before = self._gateway_endpoints(model_name)
        unexpected = [value for value in before if value != endpoint]
        if unexpected:
            raise RuntimeError(f"model {model_name} has endpoints not owned by this pipeline")
        with httpx.Client(timeout=30, trust_env=False) as client:
            if endpoint in before:
                removed = client.post(
                    f"{control}/proxy/endpoint/delete",
                    json={"model_name": model_name, "endpoints": [endpoint]},
                )
                removed.raise_for_status()
            after = self._gateway_endpoints(model_name)
            if after:
                raise RuntimeError(f"model {model_name} still has registered endpoints")
            deleted = client.post(
                f"{control}/proxy/delete",
                json={"model_name": model_name},
            )
            if deleted.status_code not in {200, 400, 404}:
                deleted.raise_for_status()
        return {
            "model_name": model_name,
            "removed_endpoints": [endpoint] if endpoint in before else [],
            "remaining_endpoints": [],
        }

    def teardown(self) -> dict[str, Any]:
        self.state["phase"] = "teardown"
        self._save()
        models = []
        for model_name in dict.fromkeys(
            [self.service["batch_model_alias"], self.service["primary_model"]]
        ):
            models.append(self._remove_owned_model(model_name))

        deleted = self._ltp("delete")
        if deleted.returncode != 0 and "not found" not in (
            f"{deleted.stdout}\n{deleted.stderr}".casefold()
        ):
            raise RuntimeError("ltp delete failed")
        deadline = time.monotonic() + int(self.teardown_config["timeout_seconds"])
        verification_status = "unknown"
        pod_absent = False
        while True:
            shown = self._ltp("show", logs=True)
            combined = f"{shown.stdout}\n{shown.stderr}"
            verification_status = self._ltp_status(shown.stdout)
            pod_absent = "notfound" in combined.casefold() or "not found" in combined.casefold()
            if shown.returncode != 0 and pod_absent:
                break
            if verification_status in {"deleted", "failed", "completed", "succeeded"} or pod_absent:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("ltp teardown verification timed out")
            time.sleep(min(int(self.retry["poll_interval_seconds"]), 30))
        receipt = {
            "schema_version": "miaoda.serving_teardown.v2",
            "completed_at": _now(),
            "models": models,
            "remaining_endpoints": [],
            "ltp_job_name": self.service["ltp_job"],
            "ltp_delete": "accepted" if deleted.returncode == 0 else "not_found",
            "ltp_verification_status": verification_status,
            "ltp_verification_pod_absent": pod_absent,
        }
        _atomic_write(self.output_root / "runtime/TEARDOWN_COMPLETE.json", receipt)
        self.state["teardown_complete"] = True
        self.state["teardown"] = receipt
        self._save()
        return receipt

    def run(self) -> int:
        if self.state.get("phase") == "complete" and self.state.get("teardown_complete"):
            self.log("selective retry already complete")
            return 0
        self._wait_for_canary()
        self.verify_service()
        self._ensure_concurrency()
        bulk_cases = [
            case_id
            for case_id in self.selected_cases
            if case_id != int(self.retry["canary_case_id"])
        ]
        submitted = self._restart_cases(bulk_cases, label="round-1-bulk")
        self.state.update(
            {
                "phase": "round_1_running",
                "retry_round": 1,
                "round_1_submitted_case_count": len(submitted) + 1,
            }
        )
        self._save()

        records = self._wait_for_round(label="round_1")
        round_number = 1
        max_rounds = int(self.retry["max_retry_rounds"])
        while round_number < max_rounds:
            retryable = [
                case_id
                for case_id in self.selected_cases
                if eligible_failure_category(records[case_id])
            ]
            if not retryable:
                break
            round_number += 1
            self.verify_service()
            self._ensure_concurrency()
            submitted = self._restart_cases(
                retryable,
                label=f"round-{round_number}",
            )
            self.state.update(
                {
                    "phase": f"round_{round_number}_running",
                    "retry_round": round_number,
                    f"round_{round_number}_submitted_case_count": len(submitted),
                }
            )
            self._save()
            records = self._wait_for_round(label=f"round_{round_number}")

        final = self._write_final(records)
        self.state["final_selected_status_counts"] = final["selected_status_counts"]
        self.state["phase"] = "finalized"
        self._save()
        self.teardown()
        self.state["phase"] = "complete"
        self._save()
        self.log(
            "selective retry complete "
            f"counts={json.dumps(final['selected_status_counts'], sort_keys=True)}"
        )
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = load_manifest(args.manifest.resolve(strict=True))
    if args.dry_run:
        print(
            json.dumps(
                {
                    "batch_task_id": manifest["datawheel"]["batch_task_id"],
                    "output_root": manifest["runtime"]["output_root"],
                    "ltp_job": manifest["service"]["ltp_job"],
                    "batch_model_alias": manifest["service"]["batch_model_alias"],
                    "concurrency": manifest["retry"]["concurrency"],
                    "max_retry_rounds": manifest["retry"]["max_retry_rounds"],
                    "reward_enabled": manifest.get("reward", {}).get("enabled"),
                    "teardown_enabled": manifest["teardown"]["enabled"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    output_root = Path(manifest["runtime"]["output_root"])
    lock_path = output_root / "runtime/supervisor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"selective retry supervisor already running: {lock_path}")
            return 0
        supervisor: SelectiveRetrySupervisor | None = None
        try:
            supervisor = SelectiveRetrySupervisor(manifest)
            return supervisor.run()
        except (
            AgentOSClientError,
            httpx.HTTPError,
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
        ) as exc:
            if supervisor is not None:
                supervisor.state["phase"] = "error"
                supervisor.state["last_error_type"] = type(exc).__name__
                supervisor.state["last_error_http_status"] = getattr(exc, "status_code", None)
                supervisor._save()
            print(f"selective retry supervisor failed: {type(exc).__name__}", file=sys.stderr)
            return 1
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    raise SystemExit(main())

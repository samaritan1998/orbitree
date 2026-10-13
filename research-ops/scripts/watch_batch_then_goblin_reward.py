#!/usr/bin/env python3
"""Wait for one Datawheel batch and submit its first-round Goblin reward once.

The watcher is intentionally a one-shot process: it polls the Datawheel batch,
downloads the result after a successful terminal status, submits one Goblin
job, records a credential-free receipt, and exits. A state file and an
advisory lock make restarts safe and prevent two copies from submitting the
same batch concurrently.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_ops.agentos import AgentOSClient, AgentOSClientError
from research_ops.reward import GoblinJobSnapshot, GoblinRewardClient, GoblinRewardError

DEFAULT_DATAWHEEL_URL = "http://198.51.100.250:80/api/v1/datawheel"
DEFAULT_GOBLIN_URL = "http://198.51.100.139:8118"
DEFAULT_BENCHMARK = "example-app-300"
DEFAULT_PROMPT_VERSION = "v1_6"
DEFAULT_REWARD_MODEL = "gpt-5.5-agent"
DEFAULT_QUERY_ROUND = "first_round"
DEFAULT_CONCURRENCY = 50
TERMINAL_BATCH_STATUSES = {"completed", "complete", "succeeded", "success", "done"}
FAILED_BATCH_STATUSES = {
    "failed",
    "failure",
    "error",
    "cancelled",
    "canceled",
    "stopped",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_status(value: Any) -> str:
    return str(value or "unknown").strip().lower().replace("-", "_").replace(" ", "_")


def batch_phase(status: Any) -> str:
    normalized = normalize_status(status)
    if normalized in TERMINAL_BATCH_STATUSES or normalized.startswith(
        ("completed_", "succeeded_", "success_", "done_")
    ):
        return "completed"
    if normalized in FAILED_BATCH_STATUSES or normalized.startswith(
        ("failed_", "error_", "cancelled_", "canceled_")
    ):
        return "failed"
    return "running"


def _safe_job(snapshot: GoblinJobSnapshot) -> dict[str, Any]:
    """Persist only stable job fields; raw provider payloads are not needed."""

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


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_state(path: Path, *, batch_id: str, rollout_model: str, benchmark: str) -> dict[str, Any]:
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid watcher state file: {path}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"watcher state is not an object: {path}")
        if str(value.get("batch_task_id")) != batch_id:
            raise RuntimeError("watcher state belongs to a different batch")
        if str(value.get("rollout_model")) != rollout_model:
            raise RuntimeError("watcher state belongs to a different rollout model")
        if str(value.get("benchmark")) != benchmark:
            raise RuntimeError("watcher state belongs to a different benchmark")
        return value
    return {
        "version": 1,
        "batch_task_id": batch_id,
        "rollout_model": rollout_model,
        "benchmark": benchmark,
        "phase": "waiting",
        "created_at": _now(),
        "updated_at": _now(),
        "polls": 0,
    }


def _save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _atomic_write(path, state)


def _description(
    model: str,
    batch_id: str,
    benchmark: str,
    prompt_version: str,
    concurrency: int,
) -> str:
    return f"{model}; batch {batch_id}; {benchmark}; reward {prompt_version}; c{concurrency}"


class BatchGoblinWatcher:
    """Stateful coordinator split out so the transition logic is testable."""

    def __init__(
        self,
        args: argparse.Namespace,
        *,
        datawheel: AgentOSClient,
        goblin: GoblinRewardClient,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        self.args = args
        self.datawheel = datawheel
        self.goblin = goblin
        self.sleep = sleep
        self.log = log
        self.state_path = Path(args.state_file).expanduser()
        self.artifact_path = Path(args.artifact_file).expanduser()

    def _poll_batch(self, state: dict[str, Any]) -> Any:
        deadline = time.monotonic() + max(float(self.args.poll_timeout), 0.1)
        while True:
            snapshot = self.datawheel.get_batch(self.args.batch_id)
            state["polls"] = int(state.get("polls") or 0) + 1
            state["batch_status"] = snapshot.status
            state["status_counts"] = snapshot.status_counts
            state["total_size"] = snapshot.total_size
            _save_state(self.state_path, state)
            phase = batch_phase(snapshot.status)
            self.log(
                f"batch={self.args.batch_id} status={normalize_status(snapshot.status)} "
                f"counts={json.dumps(snapshot.status_counts, sort_keys=True)}"
            )
            if phase != "running":
                return snapshot
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"batch polling timed out: {self.args.batch_id}")
            self.sleep(min(max(self.args.poll_interval, 0.1), remaining))

    def run(self) -> int:
        state = _load_state(
            self.state_path,
            batch_id=self.args.batch_id,
            rollout_model=self.args.rollout_model,
            benchmark=self.args.benchmark,
        )
        if state.get("phase") in {"submitted", "completed"}:
            self.log(
                f"batch={self.args.batch_id} already has Goblin job "
                f"{state.get('goblin_job_id', '<unknown>')}; nothing to do"
            )
            return 0
        if state.get("phase") == "failed":
            self.log(
                f"batch={self.args.batch_id} is marked failed; "
                "refusing a duplicate submission"
            )
            return 2
        if state.get("phase") == "submitting":
            self.log(
                f"batch={self.args.batch_id} has an ambiguous Goblin submission; "
                "inspect the receipt before retrying"
            )
            return 3

        if self.args.dry_run:
            print(
                json.dumps(
                    {
                        "batch_task_id": self.args.batch_id,
                        "rollout_model": self.args.rollout_model,
                        "benchmark": self.args.benchmark,
                        "query_round": self.args.query_round,
                        "prompt_version": self.args.prompt_version,
                        "reward_model": self.args.reward_model,
                        "concurrency": self.args.concurrency,
                        "eval_mm": self.args.eval_mm,
                        "eval_miniprog": self.args.eval_miniprog,
                        "state_file": str(self.state_path),
                        "artifact_file": str(self.artifact_path),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        snapshot = self._poll_batch(state)
        phase = batch_phase(snapshot.status)
        if phase == "failed":
            state["phase"] = "failed"
            state["failure_reason"] = f"datawheel_status:{normalize_status(snapshot.status)}"
            _save_state(self.state_path, state)
            self.log(
                f"batch={self.args.batch_id} ended with {snapshot.status}; "
                "Goblin not submitted"
            )
            return 2

        # Always resolve a fresh workbook after the batch reaches a terminal
        # state. A file left by a probe or an interrupted prior run may be a
        # non-terminal snapshot and must never be sent as the final result.
        state["phase"] = "downloading"
        _save_state(self.state_path, state)
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.datawheel.download_result(self.args.batch_id, self.artifact_path)
        self.log(f"downloaded fresh artifact={self.artifact_path}")

        # Mark the boundary before the remote POST. If the process dies after
        # this point, a human can inspect the receipt instead of silently
        # creating a second reward job.
        state["phase"] = "submitting"
        state["artifact_file"] = str(self.artifact_path)
        _save_state(self.state_path, state)
        job = self.goblin.submit_file(
            self.artifact_path,
            rollout_model=self.args.rollout_model,
            benchmark_dataset_name=self.args.benchmark,
            upload=True,
            concurrency=self.args.concurrency,
            prompt_version=self.args.prompt_version,
            reward_model=self.args.reward_model,
            query_round=self.args.query_round,
            eval_mm=self.args.eval_mm,
            eval_miniprog=self.args.eval_miniprog,
            timeout=self.args.reward_timeout,
            max_retries=self.args.max_retries,
            pod_retry=self.args.pod_retry,
            resume=True,
            template_mode=True,
            api_env=self.args.api_env,
            description=_description(
                self.args.rollout_model,
                self.args.batch_id,
                self.args.benchmark,
                self.args.prompt_version,
                self.args.concurrency,
            ),
        )
        state["phase"] = "submitted"
        state["goblin_job_id"] = job.job_id
        state["goblin"] = _safe_job(job)
        _save_state(self.state_path, state)
        self.log(f"submitted goblin_job_id={job.job_id} status={job.status}")

        # One read gives the caller a current status without making the
        # scheduler hold a process open for the whole asynchronous evaluation.
        with contextlib.suppress(GoblinRewardError):
            observed = self.goblin.get_job(job.job_id)
            state["goblin"] = _safe_job(observed)
            _save_state(self.state_path, state)
            self.log(f"observed goblin_job_id={observed.job_id} status={observed.status}")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument(
        "--rollout-model",
        default=os.environ.get("GOBLIN_ROLLOUT_MODEL", "").strip(),
        help="Reward rollout_model; may also be set with GOBLIN_ROLLOUT_MODEL",
    )
    parser.add_argument(
        "--benchmark",
        default=os.environ.get("GOBLIN_BENCHMARK", DEFAULT_BENCHMARK),
    )
    parser.add_argument(
        "--datawheel-url",
        default=os.environ.get("ROPS_AGENTOS_DATAWHEEL_BASE_URL", DEFAULT_DATAWHEEL_URL),
    )
    parser.add_argument(
        "--goblin-url",
        default=os.environ.get("ROPS_GOBLIN_BASE_URL", DEFAULT_GOBLIN_URL),
    )
    parser.add_argument("--datawheel-token-env", default="DATAWHEEL_TOKEN")
    parser.add_argument("--goblin-token-env", default="GOBLIN_REWARD_API_TOKEN")
    parser.add_argument("--poll-interval", type=float, default=30.0)
    parser.add_argument("--poll-timeout", type=float, default=7 * 24 * 3600.0)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--artifact-file", required=True)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION)
    parser.add_argument("--reward-model", default=DEFAULT_REWARD_MODEL)
    parser.add_argument("--query-round", default=DEFAULT_QUERY_ROUND)
    parser.add_argument("--reward-timeout", type=int, default=1200)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--pod-retry", type=int, default=1)
    parser.add_argument("--api-env", default="evaluation")
    parser.add_argument("--no-eval-mm", action="store_true")
    parser.add_argument("--no-eval-miniprog", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.batch_id = str(args.batch_id).strip()
    args.rollout_model = str(args.rollout_model).strip()
    args.benchmark = str(args.benchmark).strip()
    args.eval_mm = not args.no_eval_mm
    args.eval_miniprog = not args.no_eval_miniprog
    if not args.batch_id:
        raise SystemExit("--batch-id must not be empty")
    if not args.rollout_model:
        raise SystemExit("--rollout-model or GOBLIN_ROLLOUT_MODEL is required")
    if not args.benchmark:
        raise SystemExit("--benchmark must not be empty")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be positive")

    state_path = Path(args.state_file).expanduser()
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"watcher already running for state={state_path}", file=sys.stderr)
            return 0
        try:
            datawheel = AgentOSClient(
                args.datawheel_url,
                token_env=args.datawheel_token_env,
                timeout_seconds=120.0,
                trust_env=False,
            )
            goblin = GoblinRewardClient(
                args.goblin_url,
                token_env=args.goblin_token_env,
                timeout_seconds=120.0,
                trust_env=False,
            )
            return BatchGoblinWatcher(args, datawheel=datawheel, goblin=goblin).run()
        except (AgentOSClientError, GoblinRewardError, OSError, TimeoutError, RuntimeError) as exc:
            print(f"batch-goblin watcher failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    raise SystemExit(main())

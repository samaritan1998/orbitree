from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

from research_ops.agentos import AgentOSBatchSnapshot
from research_ops.reward import GoblinJobSnapshot

from scripts.watch_batch_then_goblin_reward import BatchGoblinWatcher, batch_phase


def _args(tmp_path: Path, **overrides: object) -> Namespace:
    values = {
        "batch_id": "batch-1",
        "rollout_model": "qwen-test",
        "benchmark": "example-app-300",
        "poll_timeout": 10.0,
        "poll_interval": 0.0,
        "state_file": str(tmp_path / "state.json"),
        "artifact_file": str(tmp_path / "result.xlsx"),
        "dry_run": False,
        "query_round": "first_round",
        "prompt_version": "v1_6",
        "reward_model": "gpt-5.5-agent",
        "concurrency": 50,
        "eval_mm": True,
        "eval_miniprog": True,
        "reward_timeout": 1200,
        "max_retries": 1,
        "pod_retry": 1,
        "api_env": "evaluation",
    }
    values.update(overrides)
    return Namespace(**values)


def test_batch_phase_accepts_provider_terminal_variants() -> None:
    assert batch_phase("Running") == "running"
    assert batch_phase("completed_with_errors") == "completed"
    assert batch_phase("failed") == "failed"


class _Datawheel:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = iter(statuses)
        self.downloads = 0

    def get_batch(self, batch_id: str) -> AgentOSBatchSnapshot:
        return AgentOSBatchSnapshot(
            batch_task_id=batch_id,
            status=next(self.statuses),
            status_counts={"completed": 1},
            total_size=1,
        )

    def download_result(self, batch_id: str, destination: Path) -> Path:
        self.downloads += 1
        destination.write_bytes(b"xlsx")
        return destination


class _Goblin:
    def __init__(self) -> None:
        self.submissions = 0
        self.observations = 0

    def submit_file(self, path: Path, **kwargs: object) -> GoblinJobSnapshot:
        self.submissions += 1
        assert path.read_bytes() == b"xlsx"
        assert kwargs["rollout_model"] == "qwen-test"
        assert kwargs["benchmark_dataset_name"] == "example-app-300"
        assert kwargs["query_round"] == "first_round"
        return GoblinJobSnapshot(job_id="goblin-1", status="pending")

    def get_job(self, job_id: str) -> GoblinJobSnapshot:
        self.observations += 1
        return GoblinJobSnapshot(job_id=job_id, status="queued")


def test_completed_batch_is_downloaded_and_submitted_once(tmp_path: Path) -> None:
    datawheel = _Datawheel(["running", "completed"])
    goblin = _Goblin()
    args = _args(tmp_path)
    logs: list[str] = []
    watcher = BatchGoblinWatcher(
        args,
        datawheel=datawheel,  # type: ignore[arg-type]
        goblin=goblin,  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
        log=logs.append,
    )

    assert watcher.run() == 0
    assert datawheel.downloads == 1
    assert goblin.submissions == 1
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "submitted"
    assert state["goblin_job_id"] == "goblin-1"
    assert state["goblin"]["status"] == "queued"

    # A restart sees the durable terminal phase and does not poll or submit again.
    assert watcher.run() == 0
    assert goblin.submissions == 1


def test_failed_batch_is_not_sent_to_goblin(tmp_path: Path) -> None:
    datawheel = _Datawheel(["failed"])
    goblin = _Goblin()
    args = _args(tmp_path)
    watcher = BatchGoblinWatcher(
        args,
        datawheel=datawheel,  # type: ignore[arg-type]
        goblin=goblin,  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
        log=lambda _message: None,
    )

    assert watcher.run() == 2
    assert goblin.submissions == 0
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "failed"


def test_ambiguous_submission_is_not_replayed(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "batch_task_id": "batch-1",
                "rollout_model": "qwen-test",
                "benchmark": "example-app-300",
                "phase": "submitting",
            }
        ),
        encoding="utf-8",
    )
    goblin = _Goblin()
    watcher = BatchGoblinWatcher(
        _args(tmp_path),
        datawheel=_Datawheel(["completed"]),  # type: ignore[arg-type]
        goblin=goblin,  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
        log=lambda _message: None,
    )

    assert watcher.run() == 3
    assert goblin.submissions == 0

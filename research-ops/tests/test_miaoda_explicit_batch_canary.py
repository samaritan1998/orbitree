from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_miaoda_explicit_batch_canary.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("run_miaoda_explicit_batch_canary", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
canary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canary)
VERIFY_SCRIPT = SCRIPT.with_name("verify_miaoda_explicit_batch_canary.py")
VERIFY_SPEC = importlib.util.spec_from_file_location(
    "verify_miaoda_explicit_batch_canary", VERIFY_SCRIPT
)
assert VERIFY_SPEC is not None and VERIFY_SPEC.loader is not None
verifier = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(verifier)


def test_read_tasks_adds_one_exact_explicit_batch_envelope(tmp_path: Path) -> None:
    source = tmp_path / "tasks.jsonl"
    source.write_text(
        "".join(
            json.dumps(
                {
                    "domain": "miaoda",
                    "task": {
                        "id": f"source-{index}",
                        "episode_id": f"source-{index}",
                        "query": f"build app {index}",
                    },
                }
            )
            + "\n"
            for index in range(8)
        )
    )

    tasks = canary._read_tasks(source, count=8, optimizer_batch_id="canary-batch")

    assert len(tasks) == 8
    assert {task["optimizer_batch_id"] for task in tasks} == {"canary-batch"}
    assert {task["optimizer_batch_expected_count"] for task in tasks} == {8}
    assert [task["optimizer_batch_member_index"] for task in tasks] == list(range(8))
    assert len({task["episode_id"] for task in tasks}) == 8
    assert all(task["training_objective"] == "pure_opd" for task in tasks)
    assert all(task["task_reward_required"] is False for task in tasks)


def test_policy_summary_requires_all_2xx_and_exact_logprob_join() -> None:
    summary = canary._policy_summary(
        {
            "miaoda_policy_http_statuses": [200, 201],
            "policy_calls": [
                {
                    "prompt_token_ids": [1, 2],
                    "response_token_ids": [3],
                    "behavior_logprobs": [-0.1],
                    "weight_version": "iter-0",
                },
                {
                    "prompt_token_ids": [1, 2, 3],
                    "response_token_ids": [4, 5],
                    "behavior_logprobs": [-0.2, -0.3],
                    "weight_version": "iter-0",
                },
            ],
        }
    )
    assert summary["policy_http_all_2xx"] is True
    assert summary["response_token_count"] == 3
    assert summary["behavior_logprob_count"] == 3
    assert summary["weight_versions"] == ["iter-0"]

    with pytest.raises(RuntimeError, match="policy_http_non_2xx"):
        canary._policy_summary(
            {
                "miaoda_policy_http_statuses": [200, 500],
                "policy_calls": [
                    {
                        "prompt_token_ids": [1],
                        "response_token_ids": [2],
                        "behavior_logprobs": [-0.1],
                    },
                    {
                        "prompt_token_ids": [1],
                        "response_token_ids": [2],
                        "behavior_logprobs": [-0.1],
                    },
                ],
            }
        )


def test_trajectory_message_count_never_serializes_message_text() -> None:
    assert canary._trajectory_message_count(
        {"data": {"coding_messages_all": [{"role": "user"}, {"role": "assistant"}]}}
    ) == 2


def test_recovery_verifier_requires_one_batch_and_eight_capture_ids(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "start.json"
    evidence.write_text(
        json.dumps(
            {
                "started_at": "2026-08-10T12:00:00+00:00",
                "records": [
                    {
                        "member_index": index,
                        "episode_id": f"episode-{index}",
                        "capture_episode_id": f"ep-{index:032x}",
                        "batch_task_id": "batch-one",
                    }
                    for index in range(8)
                ],
            }
        )
    )
    batch_id, _started_at, records = verifier._read_start_evidence(evidence)
    assert batch_id == "batch-one"
    assert len(records) == 8


def test_recovery_verifier_accepts_terminal_top_level_batch_id(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "terminal.json"
    evidence.write_text(
        json.dumps(
            {
                "batch_task_id": "batch-terminal",
                "started_at": "2026-08-11T04:00:00+00:00",
                "records": [
                    {
                        "member_index": index,
                        "episode_id": f"episode-{index}",
                        "capture_episode_id": f"ep-{index:032x}",
                    }
                    for index in range(8)
                ],
            }
        )
    )

    batch_id, _started_at, records = verifier._read_start_evidence(evidence)

    assert batch_id == "batch-terminal"
    assert len(records) == 8

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_openreward_live_prompts.py"
SPEC = importlib.util.spec_from_file_location("build_openreward_live_prompts", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
prompts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prompts)


def _pool() -> dict[str, object]:
    return {
        "environments": {
            domain: {
                "train": [
                    {"task_id": f"{domain}-0", "query": f"do {domain} zero"},
                    {"task_id": f"{domain}-1", "query": f"do {domain} one"},
                ]
            }
            for domain in prompts.DOMAIN_ORDER
        }
    }


def test_build_balanced_mixed_domain_batch_is_interleaved() -> None:
    tasks = prompts._pool_tasks(_pool())
    rows, quotas = prompts.build_rows(
        tasks,
        total_samples=16,
        weights={domain: 1 for domain in prompts.DOMAIN_ORDER},
        pure_opd=True,
    )

    assert quotas == {domain: 4 for domain in prompts.DOMAIN_ORDER}
    assert [row["metadata"]["openreward_env"] for row in rows[:4]] == list(
        prompts.DOMAIN_ORDER
    )
    assert len({row["metadata"]["openreward_task_id"] for row in rows}) == 16
    for row in rows:
        metadata = row["metadata"]
        assert metadata["openreward_pure_opd"] is True
        assert metadata["openreward_task"]["task_reward_required"] is False
        assert metadata["openreward_task"]["training_objective"] == "pure_opd"
    assert rows[0]["metadata"]["openreward_mode"] == "managed"
    assert rows[1]["metadata"]["openreward_mode"] == "interactive"
    miaoda = [row for row in rows if row["metadata"]["openreward_env"] == "miaoda"]
    assert len(
        {row["metadata"]["openreward_task"]["episode_id"] for row in miaoda}
    ) == len(miaoda)
    assert all(
        row["metadata"]["openreward_task"]["source_episode_id"].startswith("miaoda-")
        for row in miaoda
    )


def test_weighted_quota_uses_exact_total() -> None:
    quotas = prompts._allocate_quotas(11, {"miaoda": 1, "tooluse": 2})
    assert quotas == {"miaoda": 4, "tooluse": 7}
    assert len(prompts._interleaved_domains(quotas)) == 11


def test_task_pool_defaults_to_authoritative_source_proportions() -> None:
    pool = _pool()
    pool["source_provenance"] = {
        "miaoda": {"available_train_rows": 64},
        "ppt": {"available_train_rows": 350},
        "swe": {"available_train_rows": 4578},
        "tooluse": {"available_train_rows": 503},
    }
    tasks = prompts._pool_tasks(pool)
    weights = prompts._parse_domain_weights(
        [], tasks, default_weights=prompts._pool_source_counts(pool, tasks)
    )
    rows, quotas = prompts.build_rows(
        tasks, total_samples=4096, weights=weights, pure_opd=True
    )

    assert quotas == {"miaoda": 48, "ppt": 261, "swe": 3412, "tooluse": 375}
    assert len(rows) == 4096


def test_cli_keeps_single_tooluse_task_compatibility(tmp_path: Path) -> None:
    task = tmp_path / "task.json"
    output = tmp_path / "prompts.jsonl"
    task.write_text(json.dumps({"task_id": "one", "query": "do one"}))

    assert prompts.main(
        ["--task-spec", str(task), "--output", str(output), "--copies", "3"]
    ) == 0
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) == 3
    assert {row["metadata"]["openreward_env"] for row in rows} == {"tooluse"}


def test_cli_materializes_4096_equal_domain_rows(tmp_path: Path) -> None:
    pool = tmp_path / "pool.json"
    output = tmp_path / "prompts.jsonl"
    pool.write_text(json.dumps(_pool()))

    assert prompts.main(
        [
            "--task-pool",
            str(pool),
            "--output",
            str(output),
            "--total-samples",
            "4096",
        ]
    ) == 0
    counts = {domain: 0 for domain in prompts.DOMAIN_ORDER}
    with output.open(encoding="utf-8") as stream:
        for line in stream:
            counts[json.loads(line)["metadata"]["openreward_env"]] += 1
    assert counts == {domain: 1024 for domain in prompts.DOMAIN_ORDER}


def test_secret_like_task_key_is_rejected(tmp_path: Path) -> None:
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"task_id": "unsafe", "api_key": "not-for-disk"}))
    with pytest.raises(SystemExit, match="credential-like"):
        prompts.main(
            ["--task-spec", str(task), "--output", str(tmp_path / "out.jsonl")]
        )

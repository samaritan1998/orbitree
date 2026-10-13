import json
from pathlib import Path

import pytest
from research_ops.services.warehouse_trajectories import (
    WarehouseTrajectoryError,
    WarehouseTrajectoryService,
    parse_batch_id,
    parse_dataset_id,
)


def _dataset(root: Path) -> Path:
    warehouse = root / "qianfan-warehouse"
    dataset = warehouse / "datasets" / "ds-example"
    source = warehouse / "sft" / "batch-a" / "data.jsonl"
    source.parent.mkdir(parents=True)
    rows = [
        {
            "messages": [
                {"role": "user", "content": f"request {index}"},
                {
                    "role": "assistant",
                    "content": f"answer {index}",
                    "reasoning_content": "reason",
                    "step_loss_mask": index % 2,
                },
            ],
            "metadata": {"api_key": "must-not-leak", "index": index},
        }
        for index in range(12)
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (source.parent / "batch.json").write_text(
        json.dumps(
            {
                "batch_id": "batch-a",
                "name": "Example batch",
                "status": "pending",
                "record_count": 12,
                "tags": ["test"],
            }
        ),
        encoding="utf-8",
    )
    data = dataset / "data"
    data.mkdir(parents=True)
    (data / "batch-a.jsonl").symlink_to(source)
    (dataset / "dataset.json").write_text(
        json.dumps(
            {
                "dataset_id": "ds-example",
                "name": "Example",
                "batches": [{"table_type": "sft", "batch_id": "batch-a"}],
                "total_records": 12,
            }
        ),
        encoding="utf-8",
    )
    return warehouse


def test_dataset_url_parser_accepts_only_warehouse_dataset_urls() -> None:
    assert parse_dataset_id("ds-example") == "ds-example"
    assert (
        parse_dataset_id("https://warehouse.example.com/datasets/ds-example")
        == "ds-example"
    )
    with pytest.raises(WarehouseTrajectoryError):
        parse_dataset_id("https://example.com/datasets/ds-example")
    with pytest.raises(WarehouseTrajectoryError):
        parse_dataset_id("../outside")


def test_batch_url_parser_accepts_warehouse_batch_urls() -> None:
    assert parse_batch_id("batch-a") == "batch-a"
    assert (
        parse_batch_id("https://warehouse.example.com/batch/batch-a?table=sft")
        == "batch-a"
    )
    with pytest.raises(WarehouseTrajectoryError):
        parse_batch_id("https://example.com/batch/batch-a")
    with pytest.raises(WarehouseTrajectoryError):
        parse_batch_id("../outside")


def test_sampling_is_reproducible_and_returns_complete_redacted_records(tmp_path: Path) -> None:
    service = WarehouseTrajectoryService(_dataset(tmp_path))
    first = service.sample_dataset("ds-example", per_batch=4, seed=7)
    second = service.sample_dataset("ds-example", per_batch=4, seed=7)

    first_lines = [item["source_line"] for item in first["batches"][0]["samples"]]
    second_lines = [item["source_line"] for item in second["batches"][0]["samples"]]
    assert first_lines == second_lines
    assert first["sample_count"] == 4
    assert first["batches"][0]["record_count"] == 12

    sample = first["batches"][0]["samples"][0]
    detail = service.get_sample(first["session_id"], sample["sample_id"])
    assert detail["record"]["metadata"]["api_key"] == "[REDACTED]"
    assert detail["record"]["metadata"]["index"] >= 0
    assert detail["redacted_paths"] == ["$.metadata.api_key"]


def test_single_pending_batch_sampling_uses_same_complete_record_view(tmp_path: Path) -> None:
    service = WarehouseTrajectoryService(_dataset(tmp_path))
    result = service.sample_batch("batch-a", per_batch=5, seed=11)

    assert result["source_kind"] == "batch"
    assert result["source_id"] == "batch-a"
    assert result["batch"]["status"] == "pending"
    assert result["batch_count"] == 1
    assert result["sample_count"] == 5
    assert result["batches"][0]["record_count"] == 12
    sample = result["batches"][0]["samples"][0]
    detail = service.get_sample(result["session_id"], sample["sample_id"])
    assert detail["record"]["metadata"]["api_key"] == "[REDACTED]"


def test_dataset_symlink_cannot_escape_warehouse_root(tmp_path: Path) -> None:
    warehouse = _dataset(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"messages": []}\n', encoding="utf-8")
    link = warehouse / "datasets" / "ds-example" / "data" / "batch-a.jsonl"
    link.unlink()
    link.symlink_to(outside)

    with pytest.raises(WarehouseTrajectoryError, match="越过 Warehouse 根目录"):
        WarehouseTrajectoryService(warehouse).sample_dataset("ds-example")

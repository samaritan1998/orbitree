import json
from pathlib import Path

import pytest

from scripts.build_dsv4flash_thinking_mode_batches import (
    INSTRUCTION,
    REPLACEMENTS,
    TransformError,
    resolve_plan,
    transform_record,
)


def record_with_system(content: str) -> dict:
    return {
        "messages": [
            {"role": "system", "content": content},
            {"role": "user", "content": "request"},
            {"role": "assistant", "content": "answer", "reasoning_content": "reason"},
        ],
        "metadata": {"keep": True},
    }


@pytest.mark.parametrize(
    ("anchor", "strategy"),
    [
        ("You have access to the following functions:", "before_tool_schema_list"),
        ("We have access to the following functions:", "before_tool_schema_list"),
        ("If you need to call one or more tools,", "before_tool_call_format"),
    ],
)
def test_instruction_is_inserted_before_supported_anchor(anchor: str, strategy: str) -> None:
    original = record_with_system(
        f"Base instructions.\n\nParameters: type notes.\n\n{anchor}\nTool A"
    )
    transformed, actual_strategy, markers = transform_record(original)
    system = transformed["messages"][0]["content"]

    assert actual_strategy == strategy
    assert system.index("Parameters: type notes.") < system.index(INSTRUCTION)
    assert system.index(INSTRUCTION) < system.index(anchor)
    assert markers["has_parameters_description"] is True
    assert transformed["messages"][1:] == original["messages"][1:]
    assert transformed["metadata"] == original["metadata"]


def test_instruction_is_appended_when_system_has_no_tool_anchors() -> None:
    original = record_with_system("A system prompt without tools.")
    transformed, strategy, _ = transform_record(original)

    assert strategy == "append_existing_system"
    assert transformed["messages"][0]["content"].endswith(INSTRUCTION)


def test_instruction_creates_system_when_source_has_none() -> None:
    original = {
        "messages": [
            {"role": "user", "content": "request"},
            {"role": "assistant", "content": "answer", "reasoning_content": "reason"},
        ]
    }
    transformed, strategy, _ = transform_record(original)

    assert strategy == "create_system_message"
    assert transformed["messages"][0] == {"role": "system", "content": INSTRUCTION}
    assert transformed["messages"][1:] == original["messages"]


def test_existing_instruction_is_rejected() -> None:
    with pytest.raises(TransformError, match="already present"):
        transform_record(record_with_system(INSTRUCTION))


def test_dataset_plan_replaces_usersim_batch_exactly_once(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    dataset_id = "ds-test"
    old_batch = next(iter(REPLACEMENTS))
    replacement = REPLACEMENTS[old_batch]
    other = "20260731_other-batch"
    dataset_dir = warehouse / "datasets" / dataset_id
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "dataset.json").write_text(
        json.dumps(
            {
                "dataset_id": dataset_id,
                "batches": [
                    {"table_type": "sft", "batch_id": old_batch},
                    {"table_type": "sft", "batch_id": other},
                ],
                "total_records": 2,
            }
        ),
        encoding="utf-8",
    )
    for batch_id in (replacement, other):
        batch_dir = warehouse / "sft" / batch_id
        batch_dir.mkdir(parents=True)
        (batch_dir / "data.jsonl").write_text('{"messages": []}\n', encoding="utf-8")
        (batch_dir / "batch.json").write_text(
            json.dumps({"batch_id": batch_id, "record_count": 1, "status": "confirmed"}),
            encoding="utf-8",
        )

    _, plans = resolve_plan(warehouse, dataset_id, tmp_path / "output")

    assert [plan.source_batch_id for plan in plans] == [replacement, other]
    assert len({plan.derived_name for plan in plans}) == 2

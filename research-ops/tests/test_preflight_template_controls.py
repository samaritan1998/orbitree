from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(
    "/root/.codex/skills/miaoda-glm52-workflow/scripts/build_training_preflight.py"
)


def _load_builder():
    spec = importlib.util.spec_from_file_location("training_preflight", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reservoir_sampling_collects_full_merge_controls_and_manifest_fallback(
    tmp_path: Path,
) -> None:
    builder = _load_builder()
    merged = tmp_path / "merged.jsonl"
    rows = [
        {
            "messages": [],
            "metadata": {
                "source_batch_id": "batch-a",
                "clear_thinking": False,
                "enable_thinking": True,
                "reasoning_effort": "high",
                "preserve_thinking": True,
            },
        },
        {
            "messages": [],
            "metadata": {
                "source_batch_id": "batch-a",
                "clear_thinking": True,
                "enable_thinking": True,
                "reasoning_effort": "high",
                "preserve_thinking": True,
            },
        },
        {
            "messages": [],
            "metadata": {
                "source_batch_id": "batch-b",
                "enable_thinking": True,
            },
        },
    ]
    merged.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    selected, counts, total, controls = builder.reservoir_samples(
        merged,
        ["batch-a", "batch-b"],
        1,
        42,
        "metadata",
        {"batch-a": {}, "batch-b": {"metadata": {"clear_thinking": False}}},
    )

    assert total == 3
    assert counts == {"batch-a": 2, "batch-b": 1}
    assert len(selected["batch-a"]) == 1
    assert controls["batch-a"]["clear_thinking"]["values"] == [
        {"value": "false", "count": 1, "source": "merged"},
        {"value": "true", "count": 1, "source": "merged"},
    ]
    assert controls["batch-a"]["clear_thinking"]["missing"] == 0
    assert controls["batch-b"]["clear_thinking"]["values"] == [
        {"value": "false", "count": 0, "source": "manifest"}
    ]
    assert controls["batch-b"]["clear_thinking"]["missing"] == 1


def test_template_controls_html_and_mode_are_explicit() -> None:
    builder = _load_builder()
    flags = {
        key: {"values": [], "missing": 2}
        for key in builder.TEMPLATE_CONTROL_KEYS
    }
    flags["enable_thinking"] = {
        "values": [
            {"value": "true", "count": 3, "source": "merged"},
            {"value": "false", "count": 1, "source": "merged"},
        ],
        "missing": 0,
    }
    html = builder.template_controls_html(flags)
    assert "clear_thinking" in html
    assert "enable_thinking" in html
    assert "true × 3" in html
    assert builder.mode_from_template_controls(flags, {}) == "MIXED"

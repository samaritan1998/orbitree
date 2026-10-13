from __future__ import annotations

from pathlib import Path

from research_ops.openreward import load_task_catalogs
from research_ops.openreward.ppt_manifest import build_ppt_online_task
from research_ops.openreward.toolathlon_manifest import build_toolathlon_task

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLUSE_CATALOG = PROJECT_ROOT / "config" / "openreward-tooluse-canary.json"
PPT_CATALOG = PROJECT_ROOT / "config" / "openreward-ppt-canary.json"


def test_toolathlon_task_keeps_evaluator_content_in_remote_environment() -> None:
    task = build_toolathlon_task(
        task_name="meal-plan",
        query="Create a meal plan.",
        task_config={"needed_mcp_servers": ["calendar", "excel"]},
        repository="EnvCommons/toolathlon-gym",
        revision="a" * 40,
        preprocess_sha256="b" * 64,
        evaluator_sha256="c" * 64,
    )

    assert task["task_name"] == "meal-plan"
    assert task["tool_servers"] == ["calendar", "excel"]
    assert task["reward_spec"]["terminal_action"] == "claim_done"
    assert "evaluation_source" not in task
    assert "preprocess_source" not in task


def test_ppt_prompt_maps_assets_output_and_slide_verifier() -> None:
    record = {
        "messages": [
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": (
                    "请制作演示文稿，页数控制在 12 至 18 页。\n"
                    "- /inputs/style_refs/style.pptx\n"
                    "- /inputs/references/reference.md\n"
                    "Save the slide deck to: /workspace/slides.pptx"
                ),
            },
        ],
        "metadata": {
            "original_id": "ppt-task-1",
            "source_line": 7,
            "prompt_sha256": "d" * 64,
        },
    }

    task = build_ppt_online_task(record)
    assert task["output_path"] == "/workspace/slides.pptx"
    assert task["verifier"]["min_slides"] == 12
    assert task["verifier"]["max_slides"] == 18
    assert task["assets"] == [
        {"path": "/inputs/style_refs/style.pptx", "role": "style_reference"},
        {"path": "/inputs/references/reference.md", "role": "content_reference"},
    ]
    assert task["source"]["asset_status"] == "requires_original_task_bundle"


def test_ppt_prompt_maps_colon_slide_ranges_and_current_presentations() -> None:
    task = build_ppt_online_task(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "请编辑当前演示文稿，页数：12-16页。\n"
                        "- /inputs/current_pptx/current.pptx\n"
                        "Save the slide deck to: /workspace/slides.pptx"
                    ),
                }
            ],
            "metadata": {
                "original_id": "ppt-edit-1",
                "source_line": 8,
                "prompt_sha256": "e" * 64,
            },
        }
    )

    assert task["verifier"]["min_slides"] == 12
    assert task["verifier"]["max_slides"] == 16
    assert task["assets"] == [
        {"path": "/inputs/current_pptx/current.pptx", "role": "current_presentation"}
    ]


def test_canary_catalogs_cover_eight_diverse_tasks() -> None:
    tooluse = load_task_catalogs(TOOLUSE_CATALOG)["tooluse"]["train"]
    ppt = load_task_catalogs(PPT_CATALOG)["ppt"]["train"]

    assert len(tooluse) == len(ppt) == 8
    assert len({task["task_id"] for task in tooluse}) == 8
    assert len({server for task in tooluse for server in task["tool_servers"]}) >= 8
    expected_revision = "9c82859d05bc1f0376150d749cd2e3d43471d583"
    assert all(task["source"]["revision"] == expected_revision for task in tooluse)
    assert len({task["task_id"] for task in ppt}) == 8
    assert all(task["output_path"] == "/workspace/slides.pptx" for task in ppt)
    assert all(task["verifier"]["render_required"] for task in ppt)
    assert all(task["verifier"]["min_slides"] > 1 for task in ppt)
    assert all(task["verifier"]["max_slides"] < 100 for task in ppt)
    assert all(task["source"]["asset_status"] == "requires_local_assets" for task in ppt)
    axes = {
        (task["task_axes"]["content_mode"], task["task_axes"]["interaction_mode"])
        for task in ppt
    }
    assert ("grounded", "create") in axes
    assert all(task["source"]["provenance_status"] == "synthetic_example" for task in ppt)
    assert all("historical_reward" not in task["source"] for task in ppt)

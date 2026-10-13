from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from research_ops.openreward.ppt_runtime import HttpPptRubric, PptTask, inspect_pptx
from research_ops.openreward.slideforge_reward import (
    SlideForgeCommandRunner,
    SlideForgeRewardConfig,
    SlideForgeRewardService,
    create_slideforge_reward_app,
    map_slideforge_evaluation,
)

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


def _pptx(text: str = "Editable evidence") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("ppt/presentation.xml", "<p:presentation xmlns:p='urn:p' />")
        archive.writestr(
            "ppt/slides/slide1.xml",
            (
                "<p:sld xmlns:p='urn:p' xmlns:a='urn:a'>"
                f"<p:cSld><a:t>{text}</a:t></p:cSld></p:sld>"
            ),
        )
    return output.getvalue()


def _rubric() -> dict[str, Any]:
    return {
        "core_rubric": [
            {
                "item_id": "REQ_STRUCTURE",
                "domain": "Requirement Compliance",
                "area": "Section Coverage",
            },
            {
                "item_id": "REQ_GROUNDING",
                "domain": "Requirement Compliance",
                "area": "Prohibited Content",
            },
            {
                "item_id": "SUB_FAITHFUL",
                "domain": "Substance Quality",
                "area": "Source Faithfulness",
            },
            {
                "item_id": "DES_VISUAL",
                "domain": "Presentation Design Quality",
                "area": "Visual Hierarchy",
            },
        ]
    }


def _evaluation(*, training_usable: bool = True) -> dict[str, Any]:
    return {
        "reward_score": 0.73,
        "domain_scores": {
            "Requirement Compliance": 0.99,
            "Substance Quality": 0.98,
            "Presentation Design Quality": 0.97,
        },
        "evaluation_confidence": {
            "training_usable": training_usable,
            "evaluation_valid": True,
        },
        "item_results": [
            {
                "item_id": "SYS_OPENABLE_FILE",
                "domain": "Requirement Compliance",
                "evaluation_method": "script",
                "status": "pass",
                "score": 1,
                "quality_scoring_excluded": True,
            },
            {
                "item_id": "SYS_EDITABILITY",
                "domain": "Requirement Compliance",
                "evaluation_method": "script",
                "status": "pass",
                "score": 1,
                "quality_scoring_excluded": True,
            },
            {
                "item_id": "REQ_STRUCTURE",
                "domain": "Requirement Compliance",
                "evaluation_method": "llm_text",
                "status": "pass",
                "score": 0.6,
            },
            {
                "item_id": "REQ_GROUNDING",
                "domain": "Requirement Compliance",
                "evaluation_method": "llm_text",
                "status": "fail",
                "score": 0.0,
            },
            {
                "item_id": "SUB_FAITHFUL",
                "domain": "Substance Quality",
                "evaluation_method": "llm_text",
                "status": "pass",
                "score": 0.8,
            },
            {
                "item_id": "DES_VISUAL",
                "domain": "Presentation Design Quality",
                "evaluation_method": "vlm_visual",
                "status": "fail",
                "score": 0.2,
            },
        ],
    }


def _inspection(raw: bytes) -> dict[str, Any]:
    return {
        "slides": 1,
        "text_runs": 1,
        "characters": 17,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def test_dimension_mapping_uses_item_evidence_and_preserves_domains() -> None:
    result = map_slideforge_evaluation(
        _evaluation(),
        _rubric(),
        _inspection(_pptx()),
        cache_hit=False,
    )

    assert result["reward"] == 0.73
    assert result["usable"] is True
    assert result["rubric"] == {
        "content_grounding": 0.4,
        "structure": 0.6,
        "visual_quality": 0.2,
        "editability": 1.0,
    }
    assert result["metadata"]["slideforge_domain_scores"]["Requirement Compliance"] == 0.99
    assert result["metadata"]["dimension_derivation"]["content_grounding_item_ids"] == [
        "REQ_GROUNDING",
        "SUB_FAITHFUL",
    ]


def test_training_unusable_is_preserved_even_with_valid_dimensions() -> None:
    result = map_slideforge_evaluation(
        _evaluation(training_usable=False),
        _rubric(),
        _inspection(_pptx()),
        cache_hit=True,
    )

    assert result["reward"] == 0.73
    assert result["usable"] is False
    assert result["metadata"]["cache_hit"] is True


def test_command_runner_invokes_existing_slideforge_stages(tmp_path: Path) -> None:
    pipeline_root = tmp_path / "pipeline-root"
    reward_stage = pipeline_root / "pipeline" / "stages" / "reward"
    reward_stage.mkdir(parents=True)
    build_script = reward_stage / "build_candidate_bundle.py"
    build_script.write_text(
        """\
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--result_dir')
parser.add_argument('--pptx')
parser.add_argument('--output')
parser.add_argument('--reference_ppt')
args = parser.parse_args()
Path(args.output).write_text(json.dumps({'ppt_metadata': {'openable': True}}))
""",
        encoding="utf-8",
    )
    reward_script = reward_stage / "run_reward_with_claude_skill.sh"
    reward_script.write_text(
        """\
#!/bin/sh
set -eu
mkdir -p "$OUTPUT_DIR"
cp "$RUBRIC_PATH.expected.json" "$OUTPUT_DIR/evaluation.json"
""",
        encoding="utf-8",
    )
    reward_script.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("ccrelay", "claude"):
        executable = fake_bin / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)

    bundle = tmp_path / "bundles" / "task-1"
    bundle.mkdir(parents=True)
    (bundle / "task.json").write_text(json.dumps({"assets": []}), encoding="utf-8")
    (bundle / "rubric.json").write_text(json.dumps(_rubric()), encoding="utf-8")
    (bundle / "rubric_input.json").write_text("{}", encoding="utf-8")
    (bundle / "rubric.json.expected.json").write_text(
        json.dumps(_evaluation()),
        encoding="utf-8",
    )
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    artifact = artifact_root / "slides.pptx"
    artifact.write_bytes(_pptx())
    render = artifact_root / "slide-001.png"
    render.write_bytes(_PNG)
    work_root = tmp_path / "work"
    work_root.mkdir()
    runner = SlideForgeCommandRunner(
        SlideForgeRewardConfig(
            pipeline_root=pipeline_root,
            bundle_root=tmp_path / "bundles",
            artifact_root=artifact_root,
            work_root=work_root,
        ),
        environ={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "MDRW_LLM_ENDPOINT": "http://model.test",
            "MDRW_LLM_API_KEY": "not-a-real-secret",
            "MDRW_LLM_MODEL": "judge-model",
        },
    )

    result = runner.evaluate(
        task_id="task-1",
        artifact_path=artifact,
        render_paths=[render],
        bundle_dir=bundle,
        work_dir=work_root / "request",
    )

    assert result["reward_score"] == 0.73
    assert (work_root / "request" / "candidate_bundle.json").is_file()
    assert (work_root / "request" / "build_candidate_bundle.log").is_file()
    assert (work_root / "request" / "run_reward.log").is_file()


class FakeRunner:
    def __init__(self, *, training_usable: bool = True) -> None:
        self.calls = 0
        self.training_usable = training_usable

    def evaluate(self, **_: Any) -> dict[str, Any]:
        self.calls += 1
        return _evaluation(training_usable=self.training_usable)


def _service(tmp_path: Path) -> tuple[SlideForgeRewardService, FakeRunner, dict[str, Any]]:
    pipeline_root = tmp_path / "pipeline"
    bundle_root = tmp_path / "bundles"
    artifact_root = tmp_path / "artifacts"
    work_root = tmp_path / "work"
    task_id = "slideforge-task-1"
    inputs = bundle_root / task_id / "inputs"
    inputs.mkdir(parents=True)
    artifact_root.mkdir()
    work_root.mkdir()
    pipeline_root.mkdir()
    (bundle_root / task_id / "rubric.json").write_text(json.dumps(_rubric()))

    raw = _pptx()
    artifact = artifact_root / "slides.pptx"
    artifact.write_bytes(raw)
    render = artifact_root / "slide-001.png"
    render.write_bytes(_PNG)
    payload = {
        "task_id": task_id,
        "artifact_path": str(artifact),
        "render_paths": [str(render)],
        "inputs_path": str(inputs),
        "inspection": _inspection(raw),
    }
    runner = FakeRunner()
    service = SlideForgeRewardService(
        SlideForgeRewardConfig(
            pipeline_root=pipeline_root,
            bundle_root=bundle_root,
            artifact_root=artifact_root,
            work_root=work_root,
        ),
        runner=runner,
    )
    return service, runner, payload


def test_http_adapter_authenticates_validates_shared_paths_and_caches(tmp_path: Path) -> None:
    service, runner, payload = _service(tmp_path)
    app = create_slideforge_reward_app(
        service,
        auth_token="reward-secret",
        environ={"ORS_SLIDEFORGE_REWARD_REQUIRE_AUTH": "true"},
    )
    headers = {"Authorization": "Bearer reward-secret"}

    with TestClient(app) as client:
        assert client.post("/v1/evaluate", json=payload).status_code == 401
        first = client.post("/v1/evaluate", headers=headers, json=payload)
        second = client.post("/v1/evaluate", headers=headers, json=payload)
        escaped = dict(payload)
        escaped["artifact_path"] = str(tmp_path / "outside.pptx")
        (tmp_path / "outside.pptx").write_bytes(_pptx("outside"))
        rejected = client.post("/v1/evaluate", headers=headers, json=escaped)

    assert first.status_code == 200
    assert first.json()["metadata"]["cache_hit"] is False
    assert second.json()["metadata"]["cache_hit"] is True
    assert rejected.status_code == 400
    assert runner.calls == 1


async def test_ppt_http_rubric_preserves_slideforge_unusable_result(tmp_path: Path) -> None:
    service, _, payload = _service(tmp_path)
    service.runner = FakeRunner(training_usable=False)
    app = create_slideforge_reward_app(
        service,
        auth_token="reward-secret",
        environ={"ORS_SLIDEFORGE_REWARD_REQUIRE_AUTH": "true"},
    )
    client = HttpPptRubric(
        "http://slideforge.test",
        environ={"ORS_PPT_RUBRIC_TOKEN": "reward-secret"},
        transport=httpx.ASGITransport(app=app),
    )
    raw = Path(payload["artifact_path"]).read_bytes()
    inspection = inspect_pptx(raw)
    task = PptTask.from_spec(
        {
            "task_id": payload["task_id"],
            "query": "Create an editable PPT.",
            "benchmark_family": "ppt_online",
            "output_path": "/workspace/slides.pptx",
            "assets": [],
            "verifier": {
                "min_slides": 1,
                "max_slides": 1,
                "editable_pptx_required": True,
                "render_required": True,
                "content_grounding_required": False,
                "visual_qa_required": True,
            },
        }
    )

    result = await client.evaluate(
        task=task,
        task_spec={"task_id": payload["task_id"]},
        artifact_path=Path(payload["artifact_path"]),
        render_paths=[Path(item) for item in payload["render_paths"]],
        inputs_path=Path(payload["inputs_path"]),
        inspection=inspection,
    )

    assert result.reward == 0.73
    assert result.usable is False
    assert result.metadata["slideforge_domain_scores"]["Substance Quality"] == 0.98

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from research_ops.openreward import (
    PptOfficeBackend,
    RemoteServiceConfig,
    build_default_environments,
    create_openreward_app,
)
from research_ops.openreward.ppt_runtime import (
    CommandResult,
    PptInspection,
    PptRuntimeService,
    PptTask,
    RubricResult,
    create_ppt_runtime_app,
    inspect_pptx,
    presentation_path,
)

_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


def _pptx(*slides: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("ppt/presentation.xml", "<p:presentation xmlns:p='urn:p' />")
        for index, text in enumerate(slides, start=1):
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                (
                    "<p:sld xmlns:p='urn:p' xmlns:a='urn:a'>"
                    f"<p:cSld><a:t>{text}</a:t></p:cSld></p:sld>"
                ),
            )
    return output.getvalue()


def _task_spec(task_id: str = "ppt-task-1") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "query": "Create an editable two-slide deck grounded in reference.md.",
        "benchmark_family": "ppt_online",
        "output_path": "/workspace/slides.pptx",
        "assets": [{"path": "/inputs/references/reference.md"}],
        "verifier": {
            "min_slides": 2,
            "max_slides": 2,
            "editable_pptx_required": True,
            "render_required": True,
            "content_grounding_required": True,
            "visual_qa_required": True,
        },
    }


class FakeEngine:
    def __init__(self, deck: bytes) -> None:
        self.deck = deck
        self.removed: list[dict[str, Any]] = []

    def ping(self) -> bool:
        return True

    def image_id(self, image_ref: str) -> str:
        assert image_ref == "policy"
        return "sha256:" + "a" * 64

    def start(
        self,
        task: PptTask,
        inputs: Path,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        del task, inputs, session_id
        return {"files": {}}

    def remove(self, container: dict[str, Any] | None) -> None:
        if container is not None:
            self.removed.append(container)

    def exec_shell(self, container: dict[str, Any], command: str) -> CommandResult:
        if command == "make-deck":
            container["files"]["/workspace/slides.pptx"] = self.deck
        return CommandResult(f"ran: {command}", 0)

    def read_file(self, container: dict[str, Any], path: Any) -> bytes:
        value = container["files"].get(str(path))
        if value is None:
            raise RuntimeError("missing fixture file")
        return bytes(value)

    def write_file(self, container: dict[str, Any], path: Any, raw: bytes) -> int:
        container["files"][str(path)] = raw
        return len(raw)

    def render(self, raw: bytes, *, session_id: str) -> list[bytes]:
        del session_id
        return [_PNG] * inspect_pptx(raw).slides


class FakeRubric:
    async def evaluate(
        self,
        *,
        task: PptTask,
        task_spec: dict[str, Any],
        artifact_path: Path,
        render_paths: list[Path],
        inputs_path: Path,
        inspection: PptInspection,
    ) -> RubricResult:
        del task, task_spec, inputs_path
        assert artifact_path.is_file()
        assert all(item.is_file() for item in render_paths)
        assert inspection.slides == 2
        return RubricResult(
            0.75,
            {
                "content_grounding": 0.8,
                "structure": 0.9,
                "visual_quality": 0.6,
                "editability": 1.0,
            },
            "fake-rubric-v1",
            metadata={"slideforge_domain_scores": {"Requirement Compliance": 0.8}},
        )


class UnusableRubric(FakeRubric):
    async def evaluate(self, **kwargs: Any) -> RubricResult:
        result = await super().evaluate(**kwargs)
        return RubricResult(
            result.reward,
            result.rubric,
            result.grader_id,
            usable=False,
            metadata={"evaluation_confidence": {"training_usable": False}},
        )


def _bundle(tmp_path: Path, task_id: str = "ppt-task-1") -> Path:
    root = tmp_path / "bundles"
    references = root / task_id / "inputs" / "references"
    references.mkdir(parents=True)
    (references / "reference.md").write_text("ground truth", encoding="utf-8")
    return root


def _service(
    tmp_path: Path,
    *,
    deck: bytes,
    rubric: FakeRubric | None,
) -> tuple[PptRuntimeService, FakeEngine]:
    engine = FakeEngine(deck)
    service = PptRuntimeService(
        engine,
        bundle_root=_bundle(tmp_path),
        artifact_root=tmp_path / "artifacts",
        rubric=rubric,
        ttl_seconds=3600,
    )
    return service, engine


def test_pptx_inspection_and_paths_require_editable_ooxml() -> None:
    inspection = inspect_pptx(_pptx("Title", "Evidence"))

    assert inspection.slides == 2
    assert inspection.text_runs == 2
    assert inspection.editable is True
    assert str(presentation_path("build.js")) == "/workspace/build.js"
    assert str(presentation_path("/inputs/references/reference.md", allow_inputs=True)).startswith(
        "/inputs/"
    )

    for unsafe in ("../host", "/etc/passwd", "/workspace"):
        try:
            presentation_path(unsafe)
        except Exception as exc:
            assert "path" in str(exc)
        else:  # pragma: no cover - assertion helper
            raise AssertionError(f"unsafe path accepted: {unsafe}")


def test_ppt_runtime_returns_real_rubric_reward_and_caches_terminal_result(
    tmp_path: Path,
) -> None:
    service, engine = _service(
        tmp_path,
        deck=_pptx("Title", "Grounded content"),
        rubric=FakeRubric(),
    )
    app = create_ppt_runtime_app(service, auth_token="runtime-secret")
    headers = {"Authorization": "Bearer runtime-secret"}

    with TestClient(app) as client:
        assert client.post("/v1/presentations", json={}).status_code == 401
        created = client.post(
            "/v1/presentations",
            headers=headers,
            json={"task_id": "ppt-task-1", "task_spec": _task_spec()},
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        assert created.json()["rubric_ready"] is True

        blocked = client.post(
            f"/v1/presentations/{session_id}/exec",
            headers=headers,
            json={"input": {"command": "find / -name '*.pptx'"}},
        )
        assert blocked.status_code == 400
        assert "unbounded filesystem-root scans" in blocked.json()["detail"]

        edit_task = _task_spec("ppt-edit-task")
        edit_task["task_axes"] = {"interaction_mode": "edit"}
        edit_bundle = _bundle(tmp_path, "ppt-edit-task")
        assert edit_bundle.is_dir()
        edit_created = client.post(
            "/v1/presentations",
            headers=headers,
            json={"task_id": "ppt-edit-task", "task_spec": edit_task},
        )
        assert edit_created.status_code == 200
        edit_session_id = edit_created.json()["session_id"]
        clean_blocked = client.post(
            f"/v1/presentations/{edit_session_id}/exec",
            headers=headers,
            json={
                "input": {
                    "command": (
                        "python3 /opt/claude-skills/pptx/scripts/clean.py "
                        "/workspace/unpacked/"
                    )
                }
            },
        )
        assert clean_blocked.status_code == 400
        assert "clean.py is disabled" in clean_blocked.json()["detail"]

        clean_read_allowed = client.post(
            f"/v1/presentations/{edit_session_id}/exec",
            headers=headers,
            json={
                "input": {
                    "command": "cat /opt/claude-skills/pptx/scripts/clean.py"
                }
            },
        )
        assert clean_read_allowed.status_code == 200

        generated = client.post(
            f"/v1/presentations/{session_id}/exec",
            headers=headers,
            json={"input": {"command": "make-deck"}},
        )
        assert generated.json()["output"]["metadata"]["exit_code"] == 0
        rendered = client.post(
            "/v1/presentations/render",
            headers=headers,
            json={
                "session_id": session_id,
                "input": {"path": "/workspace/slides.pptx"},
            },
        )
        assert len(rendered.json()["output"]["blocks"]) == 3

        payload = {
            "session_id": session_id,
            "input": {"path": "/workspace/slides.pptx"},
        }
        first = client.post("/v1/presentations/verify", headers=headers, json=payload)
        second = client.post("/v1/presentations/verify", headers=headers, json=payload)
        assert first.json() == second.json()
        output = first.json()["output"]
        assert output["reward"] == 0.75
        assert output["finished"] is True
        assert output["metadata"]["usable"] is True
        assert output["metadata"]["reward_source"] == "ppt-rubric-service"
        assert output["metadata"]["rubric_content_grounding"] == 0.8
        assert output["metadata"]["rubric_visual_quality"] == 0.6
        assert output["metadata"]["grader_metadata"]["slideforge_domain_scores"] == {
            "Requirement Compliance": 0.8
        }
        assert "artifact_path" not in first.text

    assert engine.removed


def test_unusable_rubric_is_terminal_without_a_training_reward(tmp_path: Path) -> None:
    service, _ = _service(
        tmp_path,
        deck=_pptx("Title", "Grounded content"),
        rubric=UnusableRubric(),
    )
    app = create_ppt_runtime_app(service)

    with TestClient(app) as client:
        session_id = client.post(
            "/v1/presentations",
            json={"task_id": "ppt-task-1", "task_spec": _task_spec()},
        ).json()["session_id"]
        client.post(
            f"/v1/presentations/{session_id}/exec",
            json={"input": {"command": "make-deck"}},
        )
        output = client.post(
            "/v1/presentations/verify",
            json={
                "session_id": session_id,
                "input": {"path": "/workspace/slides.pptx"},
            },
        ).json()["output"]

    assert output["finished"] is True
    assert output["reward"] is None
    assert output["metadata"]["usable"] is False
    assert output["metadata"]["diagnostic_reward"] == 0.75
    assert output["metadata"]["reward_source"] == "ppt-rubric-unusable"


def test_valid_ppt_without_rubric_is_not_silently_usable(tmp_path: Path) -> None:
    service, _ = _service(
        tmp_path,
        deck=_pptx("Title", "Content"),
        rubric=None,
    )
    app = create_ppt_runtime_app(service)

    with TestClient(app) as client:
        session_id = client.post(
            "/v1/presentations",
            json={"task_id": "ppt-task-1", "task_spec": _task_spec()},
        ).json()["session_id"]
        client.post(
            f"/v1/presentations/{session_id}/exec",
            json={"input": {"command": "make-deck"}},
        )
        result = client.post(
            "/v1/presentations/verify",
            json={
                "session_id": session_id,
                "input": {"path": "/workspace/slides.pptx"},
            },
        ).json()["output"]

    assert result["finished"] is True
    assert result["reward"] is None
    assert result["metadata"]["usable"] is False
    assert result["metadata"]["reward_source"] == "rubric-unavailable"


def test_invalid_ppt_is_a_deterministic_zero_reward(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, deck=b"not-a-pptx", rubric=FakeRubric())
    app = create_ppt_runtime_app(service)

    with TestClient(app) as client:
        session_id = client.post(
            "/v1/presentations",
            json={"task_id": "ppt-task-1", "task_spec": _task_spec()},
        ).json()["session_id"]
        client.post(
            f"/v1/presentations/{session_id}/exec",
            json={"input": {"command": "make-deck"}},
        )
        result = client.post(
            "/v1/presentations/verify",
            json={
                "session_id": session_id,
                "input": {"path": "/workspace/slides.pptx"},
            },
        ).json()["output"]

    assert result["reward"] == 0.0
    assert result["metadata"]["usable"] is True
    assert result["metadata"]["structural_pass"] is False
    diagnostic_id = result["metadata"]["diagnostic_artifact_id"]
    diagnostic_path = (tmp_path / "artifacts" / diagnostic_id).resolve()
    assert diagnostic_path.is_relative_to((tmp_path / "artifacts").resolve())
    assert diagnostic_path.read_bytes() == b"not-a-pptx"


def test_ppt_runtime_round_trips_through_unified_ors(tmp_path: Path) -> None:
    service, _ = _service(
        tmp_path,
        deck=_pptx("Title", "Grounded content"),
        rubric=FakeRubric(),
    )
    sidecar = create_ppt_runtime_app(service)
    backend = PptOfficeBackend(
        RemoteServiceConfig(
            environment="ppt",
            base_url_env="PPT_CANARY_URL",
            create_operation="POST /v1/presentations",
            action_operations={
                "bash": "POST /v1/presentations/{session_id}/exec",
                "read_file": "POST /v1/presentations/{session_id}/files/read",
                "write_file": "POST /v1/presentations/{session_id}/files/write",
                "edit_file": "POST /v1/presentations/{session_id}/files/edit",
                "ppt_read": "POST /v1/presentations/read",
                "ppt_write": "POST /v1/presentations/write",
                "render": "POST /v1/presentations/render",
                "submit_deliverable": "POST /v1/presentations/verify",
            },
            destroy_operation="DELETE /v1/presentations/{session_id}",
            terminal_actions=frozenset({"submit_deliverable"}),
        ),
        environ={"PPT_CANARY_URL": "http://ppt-runtime.local"},
        transport=httpx.ASGITransport(app=sidecar),
    )
    ors = create_openreward_app(
        build_default_environments(
            backends={"ppt": backend},
            task_catalogs={"ppt": {"train": [_task_spec()]}},
        )
    )

    with TestClient(ors) as client:
        sid = client.post("/create_session").json()["sid"]
        headers = {"X-Session-ID": sid}
        created = client.post(
            "/create",
            headers=headers,
            json={"env_name": "ppt", "split": "train", "index": 0},
        )
        assert created.status_code == 200
        tools = client.get("/ppt/task_tools", headers=headers).json()["tools"]
        names = {item["name"] for item in tools}
        assert {"bash", "read_file", "write_file", "edit_file", "render"} <= names
        blocked = client.post(
            "/ppt/call",
            headers=headers,
            json={
                "name": "bash",
                "input": {"command": "find / -name '*.pptx'"},
            },
        )
        assert '"ok":true' in blocked.text
        assert "unbounded filesystem-root scans" in blocked.text
        assert '"tool_error":true' in blocked.text
        generated = client.post(
            "/ppt/call",
            headers=headers,
            json={"name": "bash", "input": {"command": "make-deck"}},
        )
        assert '"exit_code":0' in generated.text
        submitted = client.post(
            "/ppt/call",
            headers=headers,
            json={
                "name": "submit_deliverable",
                "input": {"path": "/workspace/slides.pptx"},
            },
        )
        assert '"reward":0.75' in submitted.text
        assert '"usable":true' in submitted.text
        client.post("/delete", headers=headers)

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from research_ops.openreward import load_task_catalogs
from research_ops.openreward.swe_runtime import (
    CommandResult,
    EvaluationResult,
    GradeResult,
    HarnessPlan,
    R2eHarness,
    SweBenchTask,
    SwegymHarness,
    SweRuntimeService,
    create_swe_runtime_app,
    workspace_path,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANARY_CATALOG = PROJECT_ROOT / "config" / "openreward-swe-canary.json"
FORMAL_POOL = PROJECT_ROOT / "config" / "openreward-four-domain-formal-r1-task-pool.json"


def _task_spec() -> dict[str, Any]:
    return {
        "task_id": "astropy__astropy-13306",
        "query": "fix structured table stacking",
        "benchmark_family": "swebench",
        "repo_ref": "astropy/astropy",
        "base_commit": "b3fa7702635b260b008d391705c521fca7283761",
        "setup": {"version": "5.0"},
        "verifier": {
            "test_patch": "diff --git a/test.py b/test.py\n",
            "fail_to_pass": ["test_regression"],
            "pass_to_pass": ["test_existing"],
        },
    }


def _r2e_task_spec() -> dict[str, Any]:
    return {
        "task_id": "coveragepy_final_05fbe9c95352ad7536c1f8ebd53a6739714a8af9",
        "query": "Fix the coverage URL generation regression.",
        "benchmark_family": "r2e",
        "repo_ref": "coveragepy",
        "base_commit": "05fbe9c95352ad7536c1f8ebd53a6739714a8af9",
        "sandbox": {
            "image_ref": (
                "registry.example.com/acg-airec/r2e_gym/"
                "namanjain12/coveragepy_final:05fbe9c95352ad7536c1f8ebd53a6739714a8af9"
            )
        },
        "verifier": {
            "test_file_names": ["test_1.py", "helpers/conftest.py"],
            "test_file_codes": ["def test_url():\n    assert True\n", ""],
            "expected_tests": [
                {"name": "VersionTest.test_make_url", "status": "PASSED"},
                {"name": "VersionTest.test_make_version", "status": "PASSED"},
            ],
        },
    }


class FakeHarness:
    version = "4.0.4-test"

    def plan(self, task: SweBenchTask) -> HarnessPlan:
        return HarnessPlan(
            task=task,
            image_ref="swebench/fake:latest",
            eval_script="echo test",
            harness_version=self.version,
            test_spec=object(),
        )

    def grade(self, plan: HarnessPlan, test_output: str, *, patch_applied: bool) -> GradeResult:
        del plan, test_output, patch_applied
        return GradeResult(True, True, 1.0, 1.0, 1, 1, 1, 1)


class FakeEngine:
    def __init__(self) -> None:
        self.removed: list[dict[str, Any]] = []

    def ping(self) -> bool:
        return True

    def image_id(self, image_ref: str) -> str:
        del image_ref
        return "sha256:" + "b" * 64

    def start(self, plan: HarnessPlan, *, session_id: str, purpose: str) -> dict[str, Any]:
        del plan, session_id
        return {"purpose": purpose, "files": {}}

    def remove(self, container: dict[str, Any] | None) -> None:
        if container is not None:
            self.removed.append(container)

    def exec_shell(self, container: dict[str, Any], command: str) -> CommandResult:
        del container
        return CommandResult(f"ran: {command}", 0)

    def read_file(self, container: dict[str, Any], path: Any) -> CommandResult:
        return CommandResult(container["files"].get(str(path), ""), 0)

    def write_file(self, container: dict[str, Any], path: Any, content: str) -> int:
        container["files"][str(path)] = content
        return len(content.encode())

    def extract_patch(self, container: dict[str, Any]) -> bytes:
        del container
        return b"diff --git a/a.py b/a.py\n"

    def verify(
        self,
        plan: HarnessPlan,
        patch: bytes,
        *,
        session_id: str,
    ) -> EvaluationResult:
        del plan, session_id
        return EvaluationResult(
            patch_applied=True,
            patch_sha256="a" * 64,
            patch_bytes=len(patch),
            test_output="tests passed",
            eval_exit_code=0,
            eval_timed_out=False,
        )


def test_swe_task_mapping_is_strict_and_keeps_verifier_out_of_harness_prompt() -> None:
    task = SweBenchTask.from_spec(_task_spec())
    instance = task.harness_instance()

    assert task.task_id == "astropy__astropy-13306"
    assert instance["repo"] == "astropy/astropy"
    assert instance["FAIL_TO_PASS"] == ["test_regression"]
    assert "gold_patch" not in instance


def test_r2e_task_builds_isolated_evaluator_and_matches_expected_statuses() -> None:
    task = SweBenchTask.from_spec(_r2e_task_spec())
    harness = R2eHarness()
    plan = harness.plan(task)

    assert task.benchmark_family == "r2e"
    assert plan.image_ref == _r2e_task_spec()["sandbox"]["image_ref"]
    assert plan.requires_head_match is False
    assert "/testbed/run_tests.sh" in plan.eval_script
    assert "Fix the coverage" not in plan.eval_script

    grade = harness.grade(
        plan,
        "\n".join(
            [
                "PASSED r2e_tests/test_1.py::VersionTest::test_make_url",
                "PASSED r2e_tests/test_1.py::VersionTest::test_make_version",
            ]
        ),
        patch_applied=True,
    )
    assert grade.resolved is True
    assert grade.fail_to_pass == 1.0

    mismatch = harness.grade(
        plan,
        "FAILED r2e_tests/test_1.py::VersionTest::test_make_url",
        patch_applied=True,
    )
    assert mismatch.resolved is False


def test_r2e_task_rejects_unsafe_evaluator_filename() -> None:
    task = _r2e_task_spec()
    task["verifier"]["test_file_names"] = ["../escape.py"]
    task["verifier"]["test_file_codes"] = ["pass\n"]
    try:
        SweBenchTask.from_spec(task)
    except Exception as exc:
        assert "relative safe path" in str(exc)
    else:  # pragma: no cover - assertion helper
        raise AssertionError("unsafe R2E evaluator filename was accepted")


def test_workspace_paths_are_confined_to_testbed() -> None:
    assert str(workspace_path("src/module.py")) == "/testbed/src/module.py"
    assert str(workspace_path("/testbed/src/module.py")) == "/testbed/src/module.py"

    for value in ("../host", "/etc/passwd", "/testbed"):
        try:
            workspace_path(value)
        except Exception as exc:
            assert "workspace path" in str(exc)
        else:  # pragma: no cover - assertion helper
            raise AssertionError(f"unsafe path accepted: {value}")


def test_real_swe_canary_catalog_contains_eight_diverse_joined_tasks() -> None:
    catalog = load_task_catalogs(CANARY_CATALOG)
    tasks = catalog["swe"]["train"]

    assert len(tasks) == 8
    assert tasks[0]["task_id"] == "astropy__astropy-13306"
    assert len({item["repo_ref"] for item in tasks}) == 8
    assert all(item["verifier"]["test_patch"] for item in tasks)


def test_swe_runtime_http_contract_executes_and_caches_terminal_reward() -> None:
    engine = FakeEngine()
    service = SweRuntimeService(engine, FakeHarness(), ttl_seconds=3600)  # type: ignore[arg-type]
    app = create_swe_runtime_app(service, auth_token="runtime-secret")
    headers = {"Authorization": "Bearer runtime-secret"}

    with TestClient(app) as client:
        assert client.post("/v1/sandboxes", json={}).status_code == 401
        created = client.post(
            "/v1/sandboxes",
            headers=headers,
            json={"task_id": _task_spec()["task_id"], "task_spec": _task_spec()},
        )
        assert created.status_code == 200
        sandbox_id = created.json()["sandbox_id"]
        assert created.json()["image_id"] == "sha256:" + "b" * 64
        assert "test_patch" not in created.text

        written = client.post(
            f"/v1/sandboxes/{sandbox_id}/files/write",
            headers=headers,
            json={"input": {"path": "fix.py", "content": "value = 1\n"}},
        )
        assert written.json()["output"]["metadata"]["path"] == "/testbed/fix.py"
        read = client.post(
            f"/v1/sandboxes/{sandbox_id}/files/read",
            headers=headers,
            json={"input": {"path": "/testbed/fix.py"}},
        )
        assert read.json()["output"]["text"] == "value = 1\n"
        ran = client.post(
            f"/v1/sandboxes/{sandbox_id}/exec",
            headers=headers,
            json={"input": {"command": "pytest -q"}},
        )
        assert ran.json()["output"]["metadata"]["exit_code"] == 0

        first = client.post(f"/v1/sandboxes/{sandbox_id}/verify", headers=headers)
        second = client.post(f"/v1/sandboxes/{sandbox_id}/verify", headers=headers)
        assert first.json() == second.json()
        assert first.json()["output"]["reward"] == 1.0
        assert first.json()["output"]["finished"] is True

        deleted = client.delete(f"/v1/sandboxes/{sandbox_id}", headers=headers)
        assert deleted.json() == {"deleted": True}

    assert engine.removed


@pytest.mark.skipif(not (PROJECT_ROOT / "data/swe_gym_harness").is_dir(), reason="Optional upstream SWE harness not installed")
def test_swegym_training_task_builds_pinned_image_and_eval_script() -> None:
    payload = json.loads(FORMAL_POOL.read_text(encoding="utf-8"))
    task_spec = payload["environments"]["swe"]["train"][0]
    task = SweBenchTask.from_spec(task_spec)
    harness = SwegymHarness(
        harness_root=PROJECT_ROOT / "data" / "swe_gym_harness",
    )
    plan = harness.plan(task)

    assert task.benchmark_family == "swegym"
    assert plan.image_ref == task_spec["sandbox"]["image_ref"]
    assert "git apply" in plan.eval_script
    assert plan.harness_version.startswith("2.0.13+git.")


@pytest.mark.skipif(not (PROJECT_ROOT / "data/swe_gym_harness").is_dir(), reason="Optional upstream SWE harness not installed")
def test_swegym_grader_uses_task_scoped_log_path() -> None:
    payload = json.loads(FORMAL_POOL.read_text(encoding="utf-8"))
    task = SweBenchTask.from_spec(payload["environments"]["swe"]["train"][0])
    harness = SwegymHarness(harness_root=PROJECT_ROOT / "data" / "swe_gym_harness")
    plan = harness.plan(task)
    output = "\n".join(
        [
            "applied patch (pred)",
            *(f"PASSED {name}" for name in task.fail_to_pass + task.pass_to_pass),
        ]
    )

    grade = harness.grade(plan, output, patch_applied=True)

    assert grade.resolved is True
    assert grade.fail_to_pass == 1.0
    assert grade.pass_to_pass == 1.0

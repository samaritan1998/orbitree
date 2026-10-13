from __future__ import annotations

import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from research_ops import models
from test_api_workflow import create_work


def _presentbench_fixture(tmp_path: Path) -> tuple[Path, Path]:
    task_file = tmp_path / "tasks.jsonl"
    result_root = tmp_path / "results"
    domain_root = tmp_path / "cases" / "fixture"
    domain_root.mkdir(parents=True)
    (domain_root / "common_judge_prompt.json").write_text(
        json.dumps(
            {
                "material_independent_prefix": "Independent",
                "material_independent_checklist_1": [],
                "material_independent_checklist_2": [],
                "material_dependent_prefix": "Dependent",
                "material_dependent_checklist_3": [],
            }
        ),
        encoding="utf-8",
    )
    records: list[dict[str, object]] = []
    for case_name, questions in (("case-a", ["Rubric A1", "Rubric A2"]), ("case-b", ["Rubric B1"])):
        case_dir = domain_root / case_name
        generation_dir = case_dir / "generation_task"
        generation_dir.mkdir(parents=True)
        (generation_dir / "judge_prompt.json").write_text(
            json.dumps(
                {
                    "material_dependent_checklist_1": questions,
                    "material_dependent_checklist_2": [],
                }
            ),
            encoding="utf-8",
        )
        output_dir = Path(case_name) / "generation_task" / "results"
        candidate_dir = result_root / output_dir
        candidate_dir.mkdir(parents=True)
        with zipfile.ZipFile(candidate_dir / "slides.pptx", "w") as archive:
            archive.writestr("ppt/slides/slide1.xml", f"<slide>{case_name}</slide>")
            archive.writestr("ppt/slides/slide2.xml", f"<slide>{case_name}</slide>")
        (candidate_dir / "slides.pdf").write_bytes(f"rendered:{case_name}".encode())
        record: dict[str, object] = {
            "task_id": case_name,
            "source": "fixture",
            "query_path": str(generation_dir / "instructions.md"),
            "reference_paths": [str(case_dir / "material.pdf")],
            "output_dir": str(output_dir),
            "presentbench_case_dir": str(case_dir),
            "domains": ["fixture"],
        }
        if case_name == "case-b":
            record.pop("presentbench_case_dir")
        records.append(record)
    task_file.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return task_file, result_root


def _create_run(client, work_id: str, *, tool_parser: bool = True) -> dict:
    response = client.post(
        "/api/eval-runs",
        json={
            "work_item_id": work_id,
            "name": "PresentBench fixture",
            "adapter": "presentbench",
            "mode": "fast",
            "candidate_id": "candidate-r1",
            "tool_parser": tool_parser,
            "batch_size": 2,
            "max_attempts": 2,
            "config_json": {"judge_model": "fixture-judge"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _import_run(client, run_id: str, task_file: Path, result_root: Path) -> dict:
    response = client.post(
        f"/api/eval-runs/{run_id}/import-presentbench",
        json={"task_file": str(task_file), "result_root": str(result_root)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _complete(client, unit: dict, score: float = 0.75) -> dict:
    response = client.post(
        f"/api/eval-units/{unit['id']}/complete",
        json={
            "lease_token": unit["lease_token"],
            "score": score,
            "result_json": {"answer": "yes", "evidence": [unit["rubric_key"]]},
            "duration_ms": 25,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_eval_harness_claim_retry_requeue_audit_and_cache(client, tmp_path: Path) -> None:
    work, _branch = create_work(client)
    task_file, result_root = _presentbench_fixture(tmp_path)
    run = _create_run(client, work["id"])
    imported = _import_run(client, run["id"], task_file, result_root)
    assert imported["run"]["status"] == "running"
    assert imported["imported_cases"] == 2
    assert imported["imported_units"] == 3
    assert imported["summary"]["status_counts"] == {"pending": 3}

    first = client.post(
        f"/api/eval-runs/{run['id']}/claim",
        json={"worker_id": "train-z-1", "limit": 2, "lease_seconds": 60},
    )
    second = client.post(
        f"/api/eval-runs/{run['id']}/claim",
        json={"worker_id": "train-z-2", "limit": 2, "lease_seconds": 60},
    )
    assert first.status_code == second.status_code == 200
    first_units = first.json()["units"]
    second_units = second.json()["units"]
    assert len(first_units) == 2
    assert len({unit["case_key"] for unit in first_units}) == 1
    assert len(second_units) == 1
    assert {unit["id"] for unit in first_units}.isdisjoint(
        {unit["id"] for unit in second_units}
    )

    failed = first_units[0]
    failure = client.post(
        f"/api/eval-units/{failed['id']}/fail",
        json={
            "lease_token": failed["lease_token"],
            "category": "judge_timeout",
            "message": "transient timeout",
            "retryable": True,
        },
    )
    assert failure.status_code == 200
    assert failure.json()["status"] == "retryable"
    _complete(client, first_units[1])
    _complete(client, second_units[0])

    retry = client.post(
        f"/api/eval-runs/{run['id']}/claim",
        json={"worker_id": "train-z-3", "limit": 2, "lease_seconds": 60},
    ).json()["units"]
    assert len(retry) == 1
    assert retry[0]["id"] == failed["id"]
    assert retry[0]["attempt_count"] == 2
    quarantined = client.post(
        f"/api/eval-units/{retry[0]['id']}/fail",
        json={
            "lease_token": retry[0]["lease_token"],
            "category": "judge_timeout",
            "message": "timeout again",
            "retryable": True,
        },
    )
    assert quarantined.json()["status"] == "quarantined"
    assert client.get(f"/api/eval-runs/{run['id']}").json()["run"]["status"] == "needs_review"

    requeued = client.post(f"/api/eval-runs/{run['id']}/requeue", json={})
    assert requeued.status_code == 200, requeued.text
    assert requeued.json()["requeued_units"] == 1
    final_claim = client.post(
        f"/api/eval-runs/{run['id']}/claim",
        json={"worker_id": "train-z-4", "limit": 2, "lease_seconds": 60},
    ).json()["units"]
    assert len(final_claim) == 1
    _complete(client, final_claim[0], score=1.0)
    detail = client.get(f"/api/eval-runs/{run['id']}").json()
    assert detail["run"]["status"] == "completed"
    assert detail["summary"]["mean_score"] == 0.8333333333333334
    assert detail["summary"]["aggregate_score"] == 0.8125
    assert detail["summary"]["aggregate_cases"] == 2
    audit = client.get(f"/api/eval-runs/{run['id']}/audit").json()
    assert audit["valid"] is True
    assert audit["issues"] == []

    cached_run = _create_run(client, work["id"])
    cached = _import_run(client, cached_run["id"], task_file, result_root)
    assert cached["reused_units"] == 3
    assert cached["run"]["status"] == "completed"
    assert cached["summary"]["cache_hits"] == 3

    parser_off_run = _create_run(client, work["id"], tool_parser=False)
    parser_off = _import_run(client, parser_off_run["id"], task_file, result_root)
    assert parser_off["reused_units"] == 0
    warning_codes = {
        issue["code"]
        for issue in client.get(f"/api/eval-runs/{parser_off_run['id']}/audit").json()["issues"]
    }
    assert "tool_parser_disabled" in warning_codes


def test_eval_claim_is_atomic_and_stale_lease_cannot_complete(client, tmp_path: Path) -> None:
    work, _branch = create_work(client)
    task_file, result_root = _presentbench_fixture(tmp_path)
    run = _create_run(client, work["id"])
    _import_run(client, run["id"], task_file, result_root)

    def claim(worker: str) -> list[dict]:
        response = client.post(
            f"/api/eval-runs/{run['id']}/claim",
            json={"worker_id": worker, "limit": 2, "lease_seconds": 60},
        )
        assert response.status_code == 200, response.text
        return response.json()["units"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ["worker-a", "worker-b"]))
    claimed_ids = [unit["id"] for batch in claims for unit in batch]
    assert len(claimed_ids) == len(set(claimed_ids)) == 3

    stale = claims[0][0]
    state = client.app.state.rops
    with state.database.session_factory() as session:
        unit = session.get(models.EvalUnit, stale["id"])
        assert unit is not None
        unit.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    stale_response = client.post(
        f"/api/eval-units/{stale['id']}/complete",
        json={"lease_token": stale["lease_token"], "score": 1.0},
    )
    assert stale_response.status_code == 409


def test_eval_config_rejects_credentials_and_missing_artifacts_are_quarantined(
    client, tmp_path: Path
) -> None:
    work, _branch = create_work(client)
    rejected = client.post(
        "/api/eval-runs",
        json={
            "work_item_id": work["id"],
            "name": "unsafe",
            "adapter": "presentbench",
            "candidate_id": "candidate",
            "config_json": {"api_key": "do-not-store"},
        },
    )
    assert rejected.status_code == 409

    task_file, result_root = _presentbench_fixture(tmp_path)
    (result_root / "case-b" / "generation_task" / "results" / "slides.pptx").unlink()
    run = _create_run(client, work["id"])
    imported = _import_run(client, run["id"], task_file, result_root)
    assert imported["quarantined_units"] == 1
    assert imported["summary"]["error_counts"] == {"missing_artifact": 1}


def test_generic_eval_units_use_canonical_single_unit_claims(client) -> None:
    work, _branch = create_work(client)
    created = client.post(
        "/api/eval-runs",
        json={
            "work_item_id": work["human_id"],
            "name": "Generic agent eval",
            "adapter": "custom-agent",
            "mode": "canonical",
            "candidate_id": "agent-r2",
            "candidate_digest": "sha256:agent-r2",
            "batch_size": 32,
            "config_json": {"protocol": "custom-v1"},
        },
    )
    assert created.status_code == 200, created.text
    run = created.json()
    imported = client.post(
        f"/api/eval-runs/{run['id']}/units/import",
        json={
            "units": [
                {
                    "case_key": "case-1",
                    "rubric_key": "correctness",
                    "material_key": "case-1-artifacts",
                    "input_json": {"prompt": "first"},
                },
                {
                    "case_key": "case-1",
                    "rubric_key": "style",
                    "material_key": "case-1-artifacts",
                    "input_json": {"prompt": "second"},
                },
            ]
        },
    )
    assert imported.status_code == 200, imported.text
    claim = client.post(
        f"/api/eval-runs/{run['id']}/claim",
        json={"worker_id": "canonical-worker", "limit": 32, "lease_seconds": 60},
    )
    assert claim.status_code == 200, claim.text
    assert len(claim.json()["units"]) == 1


def test_presentbench_import_expands_common_and_slide_level_rubrics(
    client, tmp_path: Path
) -> None:
    work, _branch = create_work(client)
    task_file, result_root = _presentbench_fixture(tmp_path)
    common_path = tmp_path / "cases" / "fixture" / "common_judge_prompt.json"
    common_path.write_text(
        json.dumps(
            {
                "material_independent_prefix": "Independent prefix",
                "material_independent_checklist_1": [
                    {
                        "__type__": "partial",
                        "func": "check_slide_count",
                        "keywords": {"min_count": 16, "max_count": 20},
                    },
                    "Independent rubric 1",
                ],
                "material_independent_checklist_2": ["Independent rubric 2"],
                "material_dependent_prefix": "Dependent prefix",
                "material_dependent_checklist_3": [
                    "Slide rubric 1",
                    "Slide rubric 2",
                    "Slide rubric 3",
                ],
            }
        ),
        encoding="utf-8",
    )
    for case_name in ("case-a", "case-b"):
        candidate = result_root / case_name / "generation_task" / "results" / "slides.pptx"
        with zipfile.ZipFile(candidate, "w") as archive:
            archive.writestr("ppt/slides/slide1.xml", "<slide />")
            archive.writestr("ppt/slides/slide2.xml", "<slide />")

    run = _create_run(client, work["id"])
    imported = _import_run(client, run["id"], task_file, result_root)
    assert imported["imported_units"] == 13
    units = client.get(f"/api/eval-runs/{run['id']}/units?limit=20").json()
    code_unit = next(unit for unit in units if unit["input_json"]["rubric_kind"] == "code")
    assert code_unit["status"] == "succeeded"
    assert code_unit["score"] == 0.0
    assert code_unit["result_json"]["evaluator"] == "presentbench.check_slide_count"
    assert code_unit["input_json"]["rubric_scope"] == "material_independent"
    assert code_unit["input_json"]["prompt_prefix"] == "Independent prefix"

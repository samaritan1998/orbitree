from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from research_ops import models, schemas
from research_ops.api import create_app
from research_ops.cli import app
from research_ops.db import Database
from research_ops.ltp import (
    BundleInspector,
    BundleValidationError,
    LtpSubmissionUncertain,
    LtpSubmitAdapter,
    SubmitReceipt,
)
from research_ops.services.managed_jobs import ManagedJobService, select_hidden_cluster
from research_ops.settings import PROJECT_ROOT, Settings
from typer.testing import CliRunner


def capacity_overview(available: dict[str, int]) -> dict[str, Any]:
    scheduling = {
        "cluster-a": ("B", "regular"),
        "cluster-b": ("B", "regular"),
        "cluster-c": ("B", "regular"),
        "cluster-reserved-a": ("B", "book"),
        "cluster-reserved-b": ("B", "book"),
        "cluster-reserved-c": ("B", "book"),
        "cluster-d": ("H100", "regular"),
    }
    return {
        "observed_at": "2026-08-26T03:00:00+00:00",
        "resources_observed_at": "2026-08-26T03:00:00+00:00",
        "source": {"stale": False},
        "clusters": [
            {
                "name": name,
                "resource": {
                    "hardware": scheduling[name][0],
                    "scheduling_class": scheduling[name][1],
                    "available_cards": cards,
                },
                "running_jobs": [],
                "queued_jobs": [],
            }
            for name, cards in available.items()
        ],
    }


def write_bundle(root: Path, *, name: str = "managed-smoke") -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    config = root / "config.yaml"
    run = root / "run.sh"
    config.write_text(
        "\n".join(
            [
                "jobargs:",
                f"  job_name: {name}",
                "  image: registry.example/image:tag",
                "  num_workers: 1",
                "  gpus_per_node: 8",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    run.write_text("#!/usr/bin/env bash\nset -euo pipefail\ntrue\n", encoding="utf-8")
    return config, run


def successful_validator(*_args, **_kwargs) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def build_inspector(tmp_path: Path) -> BundleInspector:
    check = tmp_path / "check.py"
    training = tmp_path / "training.py"
    check.write_text("", encoding="utf-8")
    training.write_text("", encoding="utf-8")
    return BundleInspector(
        allowed_roots=[tmp_path],
        check_script=check,
        training_preflight_script=training,
        runner=successful_validator,
    )


def test_hidden_cluster_selection_uses_regular_then_book_then_shortfall() -> None:
    selected, _receipt = select_hidden_cluster(
        capacity_overview(
            {
                "cluster-a": 32,
                "cluster-b": 64,
                "cluster-reserved-b": 128,
            }
        ),
        hardware="b",
        required_cards=32,
    )
    assert selected == "cluster-a"

    selected, _receipt = select_hidden_cluster(
        capacity_overview(
            {
                "cluster-a": 8,
                "cluster-b": 16,
                "cluster-reserved-a": 40,
                "cluster-reserved-b": 64,
            }
        ),
        hardware="b",
        required_cards=32,
    )
    assert selected == "cluster-reserved-b"

    selected, receipt = select_hidden_cluster(
        capacity_overview({"cluster-a": 8, "cluster-b": 16, "cluster-reserved-c": 24}),
        hardware="b",
        required_cards=32,
    )
    assert selected == "cluster-reserved-c"
    assert receipt["shortfall_cards"] == 8


def test_h100_selection_is_hardware_pinned() -> None:
    selected, receipt = select_hidden_cluster(
        capacity_overview({"cluster-a": 128, "cluster-d": 0}),
        hardware="h100",
        required_cards=64,
    )
    assert selected == "cluster-d"
    assert receipt["shortfall_cards"] == 64


class FakeOverview:
    def __init__(self) -> None:
        self.payload = capacity_overview({"cluster-a": 32})

    def overview(self, *, refresh: bool = False) -> dict[str, Any]:
        del refresh
        return self.payload


class FakeSubmitAdapter:
    def __init__(self) -> None:
        self.clusters: list[str] = []

    def submit(self, *, bundle, submitter: str, cluster: str) -> SubmitReceipt:
        assert submitter == "alice"
        self.clusters.append(cluster)
        return SubmitReceipt(
            reported_job_name=bundle.job_name,
            submitted_job_base=bundle.job_name,
            bundle_fingerprint=bundle.fingerprint,
        )


class UncertainSubmitAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def submit(self, *, bundle, submitter: str, cluster: str) -> SubmitReceipt:
        del bundle, submitter, cluster
        self.calls += 1
        raise LtpSubmissionUncertain("结果不确定。", "submit_timeout")


def make_service(
    tmp_path: Path,
    *,
    submit_adapter: object | None = None,
) -> tuple[ManagedJobService, Database, FakeOverview]:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        database_url=f"sqlite:///{tmp_path / 'managed.db'}",
        programs_file=PROJECT_ROOT / "config" / "programs.yaml",
        frontend_dist=tmp_path / "dist",
        managed_job_allowed_roots=[tmp_path],
        managed_job_submit_enabled=True,
        codex_enabled=False,
        codex_result_sync_enabled=False,
        codex_local_live_enabled=False,
        codex_registry_enabled=False,
        cluster_dashboard_enabled=False,
    )
    database = Database(settings)
    database.init()
    with database.session_factory() as session:
        program = models.Program(
            code="COMMON",
            slug="common",
            name="Common",
            objective="Managed job tests",
            color="#000000",
        )
        session.add(program)
        session.flush()
        session.add(
            models.WorkItem(
                human_id="COMMON-0001",
                program_id=program.id,
                title="Managed job",
                objective="Test managed submission",
            )
        )
        session.commit()
    overview = FakeOverview()
    service = ManagedJobService(
        database.session_factory,
        settings,
        overview,  # type: ignore[arg-type]
        inspector=build_inspector(tmp_path),
        submit_adapter=submit_adapter or FakeSubmitAdapter(),  # type: ignore[arg-type]
    )
    return service, database, overview


def test_training_requires_preflight_receipt(tmp_path: Path) -> None:
    config, run = write_bundle(tmp_path / "training-bundle")

    with pytest.raises(BundleValidationError) as captured:
        build_inspector(tmp_path).inspect(
            config_path=str(config),
            run_path=str(run),
            train_path=None,
            preflight_receipt_path=None,
            job_kind="training",
        )

    assert captured.value.code == "training_preflight_missing"


def test_ltp_adapter_uses_internal_cluster_and_requires_actual_identity(tmp_path: Path) -> None:
    config, run = write_bundle(tmp_path / "adapter-bundle")
    bundle = build_inspector(tmp_path).inspect(
        config_path=str(config),
        run_path=str(run),
        train_path=None,
        preflight_receipt_path=None,
        job_kind="inference",
    )
    calls: list[list[str]] = []

    def accepted(argv: list[str], **_kwargs) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout="任务提交成功: managed-smoke\n",
            stderr="",
        )

    adapter = LtpSubmitAdapter(command="ltp-cli", timeout_seconds=30, runner=accepted)
    receipt = adapter.submit(bundle=bundle, submitter="alice", cluster="cluster-a")

    assert receipt.reported_job_name == "managed-smoke"
    assert calls[0][calls[0].index("--cluster") + 1] == "cluster-a"


def test_uncertain_submit_is_not_retried(tmp_path: Path) -> None:
    adapter = UncertainSubmitAdapter()
    service, _database, _overview = make_service(tmp_path, submit_adapter=adapter)
    config, run = write_bundle(tmp_path / "uncertain-bundle")
    created = service.submit(
        schemas.ManagedJobSubmit(
            work_item_id="COMMON-0001",
            submitter="alice",
            display_name=None,
            job_kind="inference",
            hardware="b",
            config_path=str(config),
            run_path=str(run),
            idempotency_key="uncertain-request",
        )
    )

    assert service.dispatch_one() == 1
    assert service.get(created["id"], submitter="alice")["state"] == "needs_review"
    assert service.dispatch_one() == 0
    assert adapter.calls == 1


def test_service_is_idempotent_and_public_state_hides_placement(tmp_path: Path) -> None:
    service, database, overview = make_service(tmp_path)
    config, run = write_bundle(tmp_path / "bundle")
    payload = schemas.ManagedJobSubmit(
        work_item_id="COMMON-0001",
        submitter="alice",
        display_name="experiment-one",
        job_kind="inference",
        hardware="b",
        config_path=str(config),
        run_path=str(run),
        idempotency_key="same-request",
    )

    created = service.submit(payload)
    duplicate = service.submit(payload)
    assert duplicate["id"] == created["id"]
    assert service.dispatch_one() == 1

    overview.payload["clusters"][0]["queued_jobs"] = [
        {
            "name": "managed-smoke-abc123",
            "user": "alice",
            "state": "queued",
            "provider_status": "Created",
        }
    ]
    assert service.reconcile() == 1
    public = service.get(created["id"], submitter="alice")
    assert public["state"] == "queued"
    assert "cluster" not in public
    assert "remote_job_id" not in public

    with database.session_factory() as session:
        managed = session.get(models.ManagedJob, created["id"])
        external = session.get(models.ExternalJob, managed.external_job_id)
        assert external.remote_job_id == "managed-smoke-abc123"
        assert external.metadata_json["cluster"] == "cluster-a"


def test_api_response_omits_internal_provider_identity(tmp_path: Path, fake_codex) -> None:
    config, run = write_bundle(tmp_path / "bundle")
    check = tmp_path / "check.py"
    check.write_text("raise SystemExit(0)\n", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        programs_file=PROJECT_ROOT / "config" / "programs.yaml",
        frontend_dist=tmp_path / "dist",
        managed_job_allowed_roots=[tmp_path],
        managed_job_bundle_check_script=check,
        managed_job_training_preflight_script=check,
        managed_job_submit_enabled=False,
        codex_result_sync_enabled=False,
        codex_local_live_enabled=False,
        codex_registry_enabled=False,
        cluster_dashboard_enabled=False,
    )
    application = create_app(settings, fake_codex)
    with TestClient(application) as client:
        work_item, _branch = application.state.rops.service.create_work_item(
            schemas.WorkItemCreate(
                program_code="COMMON",
                title="Managed API",
                objective="Verify public response",
            )
        )
        response = client.post(
            "/api/managed-jobs",
            json={
                "work_item_id": work_item.human_id,
                "submitter": "alice",
                "display_name": "api-job",
                "job_kind": "inference",
                "hardware": "b",
                "config_path": str(config),
                "run_path": str(run),
                "idempotency_key": "api-request",
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "accepted"
    assert "cluster" not in response.json()
    assert "remote_job_id" not in response.json()


def test_cli_submit_has_no_cluster_parameter_or_output(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    write_bundle(bundle)
    captured: dict[str, Any] = {}

    def fake_request(method: str, path: str, **kwargs) -> dict[str, Any]:
        captured.update({"method": method, "path": path, **kwargs})
        return {
            "id": "logical-job-id",
            "work_id": "COMMON-0001",
            "name": "managed-smoke",
            "submitter": "alice",
            "job_kind": "inference",
            "hardware": "b",
            "state": "accepted",
            "requested_nodes": 2,
            "requested_cards": 16,
            "message": "系统已接收任务，等待统一调度。",
            "created_at": "2026-08-26T03:00:00Z",
            "updated_at": "2026-08-26T03:00:00Z",
        }

    monkeypatch.setattr("research_ops.cli._job_api_request", fake_request)
    monkeypatch.setattr("research_ops.cli.getpass.getuser", lambda: "alice")
    result = CliRunner().invoke(
        app,
        [
            "job",
            "submit",
            str(bundle),
            "--work",
            "COMMON-0001",
            "--hardware",
            "b",
            "--kind",
            "inference",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "cluster" not in captured["payload"]
    assert "cluster" not in result.output.casefold()
    help_result = CliRunner().invoke(app, ["job", "submit", "--help"])
    assert "--cluster" not in help_result.output

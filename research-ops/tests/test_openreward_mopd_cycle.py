from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_openreward_mopd_cycle.py"
SPEC = importlib.util.spec_from_file_location("run_openreward_mopd_cycle", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
cycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cycle)


def test_parse_ltp_submission_and_status() -> None:
    output = """任务提交成功: config.yaml
任务目录: /root/jobs/alice/ors-cycle-z-r1-abc123
"""
    assert cycle._parse_job_id(output) == "ors-cycle-z-r1-abc123"
    assert cycle._parse_status("status: Running\n") == "Running"


def test_json_readiness_requires_expected_fields(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"status": "completed", "version": 1}))
    spec = {
        "path": str(receipt),
        "kind": "json",
        "equals": {"status": "completed", "version": 1},
    }
    assert cycle._is_ready(spec, tmp_path)
    spec["equals"]["version"] = 2
    assert not cycle._is_ready(spec, tmp_path)


def test_materialize_config_from_gateway_receipt(tmp_path: Path) -> None:
    template = tmp_path / "postupdate-template.yaml"
    output = tmp_path / "generated/postupdate.yaml"
    receipt = tmp_path / "gateway.json"
    template.write_text(
        yaml.safe_dump(
            {
                "jobargs": {"job_name": "postupdate"},
                "envargs": {"policy_url": "placeholder", "policy_model": "model"},
            },
            sort_keys=False,
        )
    )
    receipt.write_text(
        json.dumps(
            {
                "status": "verified",
                "endpoint": "http://198.51.100.23:14255",
            }
        )
    )
    phase = {
        "name": "postupdate",
        "config_template": str(template),
        "config_output": str(output),
        "env_from_json": {
            "policy_url": {"path": str(receipt), "key": "endpoint"},
        },
    }
    assert cycle._materialize_config(phase, tmp_path) == output
    document = yaml.safe_load(output.read_text())
    assert document["envargs"]["policy_url"] == "http://198.51.100.23:14255"


def test_materialize_rejects_loopback_gateway(tmp_path: Path) -> None:
    template = tmp_path / "template.yaml"
    output = tmp_path / "output.yaml"
    receipt = tmp_path / "gateway.json"
    template.write_text(
        yaml.safe_dump({"jobargs": {}, "envargs": {"policy_url": "placeholder"}})
    )
    receipt.write_text(json.dumps({"endpoint": "http://localhost:14255"}))
    with pytest.raises(ValueError, match="not externally reachable"):
        cycle._materialize_config(
            {
                "name": "postupdate",
                "config_template": str(template),
                "config_output": str(output),
                "env_from_json": {
                    "policy_url": {"path": str(receipt), "key": "endpoint"}
                },
            },
            tmp_path,
        )


def test_materialize_config_injects_four_domain_teacher_routes(tmp_path: Path) -> None:
    config = tmp_path / "swe.yaml"
    output = tmp_path / "generated/swe.yaml"
    route_file = tmp_path / "runtime-teachers.json"
    config.write_text(
        yaml.safe_dump(
            {
                "jobargs": {"job_name": "swe"},
                "envargs": {
                    "teacher_endpoint_file": "/stale/endpoint.json",
                    "other": "value",
                },
            },
            sort_keys=False,
        )
    )

    result = cycle._materialize_config(
        {
            "name": "swe-rollout",
            "domain": "swe",
            "config": str(config),
            "config_output": str(output),
        },
        tmp_path,
        teacher_routes_file=route_file,
        teacher_domains={"miaoda", "swe", "ppt", "tooluse"},
    )

    assert result == output
    document = yaml.safe_load(output.read_text())
    assert document["envargs"]["teacher_routes_file"] == str(route_file)
    assert "teacher_endpoint_file" not in document["envargs"]


def test_materialize_config_rejects_unconfigured_teacher_domain(tmp_path: Path) -> None:
    config = tmp_path / "rollout.yaml"
    config.write_text(yaml.safe_dump({"jobargs": {}, "envargs": {}}))

    with pytest.raises(ValueError, match="no configured teacher route"):
        cycle._materialize_config(
            {
                "name": "unknown-rollout",
                "domain": "unknown",
                "config": str(config),
                "config_output": str(tmp_path / "generated.yaml"),
            },
            tmp_path,
            teacher_routes_file=tmp_path / "runtime-teachers.json",
            teacher_domains={"miaoda", "swe", "ppt", "tooluse"},
        )


def test_cycle_materializes_teacher_routes_from_runtime_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_checkpoint = tmp_path / "base" / "iter_0000023_hf"
    ppt_checkpoint = tmp_path / "ppt" / "iter_0000002_hf"
    routing = tmp_path / "routing.yaml"
    routing.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "routing_key": "domain",
                "student": {
                    "init_checkpoint": str(base_checkpoint),
                    "update_in_place": False,
                },
                "teachers": {
                    domain: {
                        "checkpoint": str(base_checkpoint),
                        "serving_checkpoint": str(base_checkpoint),
                        "endpoint_receipt_env": "OPENREWARD_XMLSFT_I23_TEACHER_ENDPOINT_FILE",
                        "frozen": True,
                    }
                    for domain in ("miaoda", "swe", "tooluse")
                }
                | {
                    "ppt": {
                        "checkpoint": str(ppt_checkpoint),
                        "serving_checkpoint": str(ppt_checkpoint),
                        "endpoint_receipt_env": "OPENREWARD_PPT2383_TEACHER_ENDPOINT_FILE",
                        "frozen": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    base_receipt = tmp_path / "base-endpoint.json"
    base_receipt.write_text(
        json.dumps(
            {
                "status": "ready",
                "protocol_validated": True,
                "checkpoint": str(base_checkpoint),
                "url": "http://203.0.113.3:14234/generate",
            }
        ),
        encoding="utf-8",
    )
    ppt_receipt = tmp_path / "ppt-endpoint.json"
    ppt_receipt.write_text(
        json.dumps(
            {
                "status": "ready",
                "protocol_validated": True,
                "checkpoint": str(ppt_checkpoint),
                "url": "http://203.0.113.4:14235/generate",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENREWARD_XMLSFT_I23_TEACHER_ENDPOINT_FILE", str(base_receipt))
    monkeypatch.setenv("OPENREWARD_PPT2383_TEACHER_ENDPOINT_FILE", str(ppt_receipt))
    output = tmp_path / "runtime-routes.json"

    route_file, domains = cycle._materialize_teacher_routes(
        {
            "teacher_routing": {
                "config": str(routing),
                "output": str(output),
            }
        },
        tmp_path,
    )

    assert route_file == output
    assert domains == {"miaoda", "swe", "ppt", "tooluse"}
    routes = json.loads(output.read_text())
    assert routes["routes"]["ppt"]["endpoint"] == "http://203.0.113.4:14235/generate"
    assert routes["routes"]["tooluse"]["endpoint"] == "http://203.0.113.3:14234/generate"


def test_cycle_dry_run_injects_runtime_teacher_routes_into_domain_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_checkpoint = tmp_path / "base" / "iter_0000023_hf"
    ppt_checkpoint = tmp_path / "ppt" / "iter_0000002_hf"
    routing = tmp_path / "routing.yaml"
    routing.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "routing_key": "domain",
                "student": {
                    "init_checkpoint": str(base_checkpoint),
                    "update_in_place": False,
                },
                "teachers": {
                    domain: {
                        "checkpoint": str(base_checkpoint),
                        "serving_checkpoint": str(base_checkpoint),
                        "endpoint_receipt_env": "OPENREWARD_XMLSFT_I23_TEACHER_ENDPOINT_FILE",
                        "frozen": True,
                    }
                    for domain in ("miaoda", "swe", "tooluse")
                }
                | {
                    "ppt": {
                        "checkpoint": str(ppt_checkpoint),
                        "serving_checkpoint": str(ppt_checkpoint),
                        "endpoint_receipt_env": "OPENREWARD_PPT2383_TEACHER_ENDPOINT_FILE",
                        "frozen": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    base_receipt = tmp_path / "base-endpoint.json"
    base_receipt.write_text(
        json.dumps(
            {
                "status": "ready",
                "protocol_validated": True,
                "checkpoint": str(base_checkpoint),
                "url": "http://203.0.113.3:14234/generate",
            }
        ),
        encoding="utf-8",
    )
    ppt_receipt = tmp_path / "ppt-endpoint.json"
    ppt_receipt.write_text(
        json.dumps(
            {
                "status": "ready",
                "protocol_validated": True,
                "checkpoint": str(ppt_checkpoint),
                "url": "http://203.0.113.4:14235/generate",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENREWARD_XMLSFT_I23_TEACHER_ENDPOINT_FILE", str(base_receipt))
    monkeypatch.setenv("OPENREWARD_PPT2383_TEACHER_ENDPOINT_FILE", str(ppt_receipt))

    source_config = tmp_path / "swe-source.yaml"
    source_config.write_text(
        yaml.safe_dump(
            {
                "jobargs": {"job_name": "swe"},
                "envargs": {"teacher_endpoint_file": "/old/endpoint.json"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    run = tmp_path / "run.sh"
    run.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    preflight = tmp_path / "preflight.py"
    preflight.write_text("raise SystemExit(0)\n", encoding="utf-8")
    generated_config = tmp_path / "generated/swe.yaml"
    route_output = tmp_path / "generated/teacher-routes.json"
    manifest = tmp_path / "cycle.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "cluster": "cluster-d",
                "user": "tester",
                "teacher_routing": {
                    "config": str(routing),
                    "output": str(route_output),
                },
                "phases": [
                    {
                        "name": "swe-rollout",
                        "domain": "swe",
                        "config": str(source_config),
                        "config_output": str(generated_config),
                        "run": str(run),
                        "ready": {"path": str(tmp_path / "not-ready.json")},
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_openreward_mopd_cycle.py",
            "--manifest",
            str(manifest),
            "--preflight",
            str(preflight),
            "--dry-run",
        ],
    )

    assert cycle.main() == 0
    generated = yaml.safe_load(generated_config.read_text(encoding="utf-8"))
    assert generated["envargs"]["teacher_routes_file"] == str(route_output)
    assert "teacher_endpoint_file" not in generated["envargs"]


def test_manifest_is_pinned_to_train_z() -> None:
    base = {
        "version": 1,
        "cluster": "cluster-d",
        "phases": [
            {
                "name": "rollout",
                "config": "config.yaml",
                "run": "run.sh",
                "ready": {"path": "rollout.json"},
            }
        ],
    }
    cycle._validate_manifest(base)
    base["cluster"] = "cluster-c"
    with pytest.raises(ValueError, match="pinned to cluster-d"):
        cycle._validate_manifest(base)


def test_failed_phase_can_accept_explicit_durable_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"status": "completed"}\n', encoding="utf-8")
    state_path = tmp_path / "state.json"
    state = {"phases": {"rollout": {"completed": False}}}
    phase = {
        "name": "rollout",
        "allow_ready_after_failure": True,
        "ready": {
            "path": str(receipt),
            "kind": "json",
            "equals": {"status": "completed"},
        },
    }
    monkeypatch.setattr(cycle, "_show", lambda _job_id, user: "Failed")
    cycle._wait_phase(
        phase,
        job_id="job-1",
        user="tester",
        base=tmp_path,
        state=state,
        state_path=state_path,
        default_poll_seconds=0,
    )
    assert state["phases"]["rollout"]["completed"] is True
    assert state["phases"]["rollout"]["ready_after_failure"] is True

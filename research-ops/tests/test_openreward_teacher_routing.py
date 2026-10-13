from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from research_ops.openreward.teacher_routing import (
    TeacherRoutingError,
    load_runtime_teacher_route,
    load_teacher_routing,
    materialize_runtime_routes,
    write_runtime_routes,
)


def _routing_document(base_checkpoint: Path, ppt_checkpoint: Path) -> dict:
    return {
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


def _receipt(path: Path, checkpoint: Path, endpoint: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "status": "ready",
                "protocol_validated": True,
                "checkpoint": str(checkpoint),
                "url": endpoint,
                "url_candidates": [endpoint],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_materializes_shared_and_specialist_teacher_routes(tmp_path: Path) -> None:
    base = tmp_path / "base" / "iter_0000023_hf"
    ppt = tmp_path / "ppt" / "iter_0000002_hf_official_visual"
    routing_path = tmp_path / "routing.yaml"
    routing_path.write_text(yaml.safe_dump(_routing_document(base, ppt)), encoding="utf-8")
    base_receipt = _receipt(
        tmp_path / "base-endpoint.json", base, "http://203.0.113.3:14234/generate"
    )
    ppt_receipt = _receipt(
        tmp_path / "ppt-endpoint.json", ppt, "http://203.0.113.4:14235/generate"
    )

    routes = materialize_runtime_routes(
        load_teacher_routing(routing_path),
        environ={
            "OPENREWARD_XMLSFT_I23_TEACHER_ENDPOINT_FILE": str(base_receipt),
            "OPENREWARD_PPT2383_TEACHER_ENDPOINT_FILE": str(ppt_receipt),
        },
    )

    assert routes["routes"]["miaoda"]["endpoint"] == "http://203.0.113.3:14234/generate"
    assert routes["routes"]["swe"]["endpoint"] == routes["routes"]["miaoda"]["endpoint"]
    assert routes["routes"]["tooluse"]["endpoint"] == routes["routes"]["miaoda"]["endpoint"]
    assert routes["routes"]["ppt"]["endpoint"] == "http://203.0.113.4:14235/generate"

    target = write_runtime_routes(tmp_path / "runtime-routes.json", routes)
    ppt_route = load_runtime_teacher_route(target, domain="ppt")
    assert ppt_route.checkpoint == str(ppt)
    assert ppt_route.endpoint_fingerprint


def test_rejects_receipt_for_a_different_checkpoint(tmp_path: Path) -> None:
    base = tmp_path / "base"
    ppt = tmp_path / "ppt"
    routing_path = tmp_path / "routing.yaml"
    routing_path.write_text(yaml.safe_dump(_routing_document(base, ppt)), encoding="utf-8")
    wrong_receipt = _receipt(
        tmp_path / "base-endpoint.json", tmp_path / "other", "http://203.0.113.3:14234/generate"
    )
    ppt_receipt = _receipt(
        tmp_path / "ppt-endpoint.json", ppt, "http://203.0.113.4:14235/generate"
    )

    with pytest.raises(TeacherRoutingError, match="does not match miaoda lineage"):
        materialize_runtime_routes(
            load_teacher_routing(routing_path),
            environ={
                "OPENREWARD_XMLSFT_I23_TEACHER_ENDPOINT_FILE": str(wrong_receipt),
                "OPENREWARD_PPT2383_TEACHER_ENDPOINT_FILE": str(ppt_receipt),
            },
        )


def test_rejects_loopback_runtime_endpoint(tmp_path: Path) -> None:
    base = tmp_path / "base"
    ppt = tmp_path / "ppt"
    routing_path = tmp_path / "routing.yaml"
    routing_path.write_text(yaml.safe_dump(_routing_document(base, ppt)), encoding="utf-8")
    base_receipt = _receipt(
        tmp_path / "base-endpoint.json", base, "http://127.0.0.1:14234/generate"
    )
    ppt_receipt = _receipt(
        tmp_path / "ppt-endpoint.json", ppt, "http://203.0.113.4:14235/generate"
    )

    with pytest.raises(TeacherRoutingError, match="no usable endpoint"):
        materialize_runtime_routes(
            load_teacher_routing(routing_path),
            environ={
                "OPENREWARD_XMLSFT_I23_TEACHER_ENDPOINT_FILE": str(base_receipt),
                "OPENREWARD_PPT2383_TEACHER_ENDPOINT_FILE": str(ppt_receipt),
            },
        )


def test_accepts_openai_gateway_runtime_endpoint(tmp_path: Path) -> None:
    base = tmp_path / "base"
    ppt = tmp_path / "ppt"
    routing_path = tmp_path / "routing.yaml"
    routing_path.write_text(yaml.safe_dump(_routing_document(base, ppt)), encoding="utf-8")
    base_receipt = _receipt(
        tmp_path / "base-endpoint.json",
        base,
        "http://203.0.113.3:18080/v1/chat/completions",
    )
    ppt_receipt = _receipt(
        tmp_path / "ppt-endpoint.json", ppt, "http://203.0.113.4:18081/generate"
    )
    routes = materialize_runtime_routes(
        load_teacher_routing(routing_path),
        environ={
            "OPENREWARD_XMLSFT_I23_TEACHER_ENDPOINT_FILE": str(base_receipt),
            "OPENREWARD_PPT2383_TEACHER_ENDPOINT_FILE": str(ppt_receipt),
        },
    )
    assert routes["routes"]["miaoda"]["endpoint"] == (
        "http://203.0.113.3:18080/v1/chat/completions"
    )


def test_routing_configuration_rejects_inline_endpoint(tmp_path: Path) -> None:
    base = tmp_path / "base"
    ppt = tmp_path / "ppt"
    document = _routing_document(base, ppt)
    document["teachers"]["miaoda"]["endpoint"] = "http://should-not-be-versioned/generate"
    routing_path = tmp_path / "routing.yaml"
    routing_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(TeacherRoutingError, match="unexpected fields"):
        load_teacher_routing(routing_path)

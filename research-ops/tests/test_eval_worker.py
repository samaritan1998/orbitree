from __future__ import annotations

import json

import httpx
import pytest
from research_ops.eval_worker import EvalWorkerConfig, _evaluate_claim


@pytest.mark.asyncio
async def test_eval_worker_forwards_parser_config_and_routes_individual_results() -> None:
    control_requests: list[tuple[str, dict]] = []
    evaluator_payload: dict = {}

    def control_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        control_requests.append((request.url.path, payload))
        return httpx.Response(200, json={"status": "ok"})

    def evaluator_handler(request: httpx.Request) -> httpx.Response:
        evaluator_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "unit_id": "unit-1",
                        "score": 0.9,
                        "result_json": {"answer": "yes"},
                        "duration_ms": 20,
                    },
                    {
                        "unit_id": "unit-2",
                        "error": {
                            "category": "judge_timeout",
                            "message": "retry",
                            "retryable": True,
                        },
                    },
                ]
            },
        )

    claim = {
        "run": {
            "id": "run-1",
            "adapter": "presentbench",
            "mode": "fast",
            "candidate_id": "candidate",
            "config_hash": "abc",
            "config_json": {},
            "tool_parser": True,
        }
    }
    units = [
        {"id": "unit-1", "lease_token": "lease-1", "input_json": {}},
        {"id": "unit-2", "lease_token": "lease-2", "input_json": {}},
    ]
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(control_handler), base_url="http://control"
        ) as control,
        httpx.AsyncClient(transport=httpx.MockTransport(evaluator_handler)) as evaluator,
    ):
        succeeded, failed = await _evaluate_claim(
            EvalWorkerConfig(run_id="run-1", evaluator_url="http://evaluator/evaluate"),
            claim=claim,
            units=units,
            control=control,
            evaluator=evaluator,
        )

    assert (succeeded, failed) == (1, 1)
    assert evaluator_payload["run"]["tool_parser"] is True
    assert [path for path, _payload in control_requests] == [
        "/api/eval-units/unit-1/complete",
        "/api/eval-units/unit-2/fail",
    ]
    assert control_requests[1][1]["category"] == "judge_timeout"

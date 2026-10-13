#!/usr/bin/env python3
"""Run all four domain mock episodes through the real ORS HTTP/SSE routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from research_ops.openreward import build_default_environments, create_openreward_app


def _end(response_text: str) -> dict[str, Any]:
    for line in response_text.splitlines():
        if line.startswith("data: ") and '"ok"' in line:
            return json.loads(line[6:])
    raise RuntimeError("ORS response did not contain an end event")


def _call(client: TestClient, env: str, sid: str, name: str, input_: dict[str, Any]) -> dict:
    response = client.post(
        f"/{env}/call",
        headers={"X-Session-ID": sid, "Accept": "text/event-stream"},
        json={"name": name, "input": input_},
    )
    response.raise_for_status()
    body = _end(response.text)
    if not body.get("ok"):
        raise RuntimeError(body.get("error", "mock ORS call failed"))
    return dict(body["output"])


def main() -> int:
    environments = build_default_environments(
        task_catalogs={
            "miaoda": {"train": [{"query": "mock Miaoda", "mock_reward": 0.8}]},
            "swe": {
                "train": [
                    {
                        "query": "mock SWE",
                        "expected_files": {"solution.txt": "done"},
                    }
                ]
            },
            "ppt": {
                "train": [
                    {
                        "query": "mock PPT",
                        "expected_output": "deck.pptx",
                        "required_text": ["Title"],
                    }
                ]
            },
            "tooluse": {
                "train": [
                    {
                        "query": "mock ToolUse",
                        "initial_state": {"count": 0},
                        "target_state": {"count": 1},
                    }
                ]
            },
        }
    )
    app = create_openreward_app(environments)
    summary: dict[str, Any] = {}
    with TestClient(app) as client:
        flows = {
            "miaoda": [
                ("start_rollout", {}),
                ("poll_rollout", {}),
                ("poll_rollout", {}),
            ],
            "swe": [
                ("write_file", {"path": "solution.txt", "content": "done"}),
                ("submit", {}),
            ],
            "ppt": [
                ("ppt_write", {"path": "deck.pptx", "content": "Title"}),
                ("render", {"path": "deck.pptx"}),
                ("submit_deliverable", {"path": "deck.pptx"}),
            ],
            "tooluse": [
                (
                    "invoke_tool",
                    {
                        "tool_name": "increment",
                        "arguments": {"key": "count"},
                    },
                ),
                ("submit", {}),
            ],
        }
        for env, actions in flows.items():
            sid = client.post("/create_session").json()["sid"]
            created = client.post(
                "/create",
                headers={"X-Session-ID": sid},
                json={"env_name": env, "split": "train", "index": 0},
            )
            created.raise_for_status()
            outputs = [_call(client, env, sid, name, payload) for name, payload in actions]
            final = outputs[-1]
            if not final.get("finished") or final.get("reward") is None:
                raise RuntimeError(f"{env} did not finish with a reward")
            summary[env] = {
                "turns": len(outputs),
                "reward": final["reward"],
                "block_types": [block.get("type") for block in final.get("blocks", [])],
            }
            client.post("/delete", headers={"X-Session-ID": sid}).raise_for_status()
    print(json.dumps({"ok": True, "environments": summary}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

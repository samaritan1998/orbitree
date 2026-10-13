from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / (
        "convert_coding_messages_to_mopd_episodes.py"
    )
    spec = importlib.util.spec_from_file_location("coding_messages_episode_converter", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_convert_canonical_messages_to_compact_turn_episode(tmp_path: Path) -> None:
    converter = _module()
    path = tmp_path / "app-one.json"
    trajectory = [
        {
            "model": "model-r1",
            "ext": {"app_type": "MINIPROGRAM"},
            "llm_request_body": {
                "api_url": "http://model/v1/chat/completions",
                "call_llm_params": {
                    "model": "model-r1",
                    "messages": [
                        {"role": "system", "content": "system"},
                        {"role": "user", "content": "build an app"},
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": "{}"},
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "call-1",
                            "content": "Bearer secret-value",
                            "api_key": "secret-value",
                        },
                        {"role": "assistant", "content": "working"},
                        {
                            "role": "user",
                            "content": "historical duplicate",
                            "metadata": {"isHistoryMessage": True},
                        },
                    ],
                    "max_tokens": 32000,
                    "temperature": 0.6,
                },
            },
            "llm_response_body": {
                "responses": [
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "finished"},
                            }
                        ],
                        "usage": {"total_tokens": 42},
                    }
                ]
            },
        }
    ]
    path.write_text(json.dumps(trajectory), encoding="utf-8")

    episode, report = converter.convert_one(
        path,
        batch_task_id="batch-1",
        result={
            "case_id": 1,
            "query": "build an app",
            "status": "completed",
            "app_type": "MINIPROGRAM",
            "duration_seconds": 12.3,
        },
    )

    assert episode["episode_id"] == "batch-1:app-one"
    assert len(episode["initial_messages"]) == 2
    assert len(episode["turns"]) == 3
    assert episode["turns"][0]["observations"][0]["content"] == "Bearer <redacted>"
    assert episode["turns"][0]["observations"][0]["api_key"] == "<redacted>"
    assert episode["turns"][-1]["terminal"] is True
    assert episode["turns"][-1]["prompt_source"] == "captured_request_response"
    assert episode["training"]["history_messages_removed"] == 1
    assert episode["training"]["training_ready"] is False
    assert report["turns"] == 3

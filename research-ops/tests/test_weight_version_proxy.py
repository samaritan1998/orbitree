from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load_script(name: str, path: str) -> ModuleType:
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(name, root / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Tokenizer:
    def apply_chat_template(self, messages, **kwargs):
        assert messages == [{"role": "user", "content": "private prompt"}]
        assert kwargs["tokenize"] is True
        assert kwargs["add_generation_prompt"] is True
        return [10, 11, 12]

    def encode(self, value, *, add_special_tokens):
        assert add_special_tokens is False
        return {"O": [20], "K": [21]}[value]


def test_gateway_policy_capture_uses_explicit_serving_chat_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy = _load_script("weight_version_proxy_template_test", "scripts/weight_version_proxy.py")
    tokenizer = _Tokenizer()
    tokenizer.chat_template = "checkpoint-default"

    class _AutoTokenizer:
        @staticmethod
        def from_pretrained(path, *, trust_remote_code):
            assert path == "/model"
            assert trust_remote_code is True
            return tokenizer

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(AutoTokenizer=_AutoTokenizer),
    )
    template = tmp_path / "force-nothink.jinja"
    template.write_text("forced-serving-template", encoding="utf-8")

    capture = proxy.PolicyTokenCapture.from_tokenizer_path(
        tmp_path / "capture",
        "/model",
        template,
    )

    assert capture.tokenizer.chat_template == "forced-serving-template"


def test_gateway_policy_capture_writes_only_exact_token_ids(tmp_path: Path) -> None:
    proxy = _load_script("weight_version_proxy_test", "scripts/weight_version_proxy.py")
    capture = proxy.PolicyTokenCapture(tmp_path, _Tokenizer())
    request = json.dumps(
        {
            "model": "policy-r1",
            "messages": [{"role": "user", "content": "private prompt"}],
        }
    ).encode()
    response = json.dumps(
        {
            "id": "response-1",
            "weight_version": "iter-1",
            "choices": [
                {
                    "finish_reason": "stop",
                    "logprobs": {
                        "content": [
                            {"token": "O", "logprob": -0.1},
                            {"token": "K", "logprob": -0.2},
                        ]
                    },
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    ).encode()
    capture.capture(
        request_body=request,
        response_body=response,
        request_path="/v1/chat/completions?mopd_episode_id=episode-1",
        request_headers={},
    )

    output = tmp_path / "capture-episode-1.jsonl"
    text = output.read_text(encoding="utf-8")
    record = json.loads(text)
    assert record["schema_version"] == "mopd-policy-capture.v2"
    assert record["prompt_token_ids"] == [10, 11, 12]
    assert record["response_token_ids"] == [20, 21]
    assert record["behavior_logprobs"] == [-0.1, -0.2]
    assert record["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
    }
    assert record["weight_version"] == "iter-1"
    assert "private prompt" not in text
    assert '"token":"O"' not in text


def test_gateway_policy_capture_prefers_native_sglang_token_ids(tmp_path: Path) -> None:
    proxy = _load_script(
        "weight_version_proxy_native_ids_test", "scripts/weight_version_proxy.py"
    )
    capture = proxy.PolicyTokenCapture(tmp_path, _Tokenizer())
    request = json.dumps(
        {
            "model": "policy-r1",
            "messages": [{"role": "user", "content": "private prompt"}],
        }
    ).encode()
    response = json.dumps(
        {
            "id": "response-native",
            "weight_version": "iter-native",
            "choices": [{"finish_reason": "stop"}],
            "meta_info": {
                "output_token_logprobs": [[-0.5, 2345, None], [-0.6, 6789, None]]
            },
        }
    ).encode()

    assert capture.capture(
        request_body=request,
        response_body=response,
        request_path="/v1/chat/completions?mopd_episode_id=episode-native",
        request_headers={},
    )
    record = json.loads((tmp_path / "capture-episode-native.jsonl").read_text())
    assert record["response_token_ids"] == [2345, 6789]
    assert record["behavior_logprobs"] == [-0.5, -0.6]


def test_miaoda_runner_accepts_direct_v2_capture() -> None:
    runner = _load_script("run_miaoda_online_mopd_test", "scripts/run_miaoda_online_mopd.py")
    prompt, response, behavior = runner._direct_capture_tokens(
        {
            "schema_version": "mopd-policy-capture.v2",
            "prompt_token_ids": [1, 2],
            "response_token_ids": [3, 4],
            "behavior_logprobs": [-0.3, -0.4],
        }
    )
    assert prompt == [1, 2]
    assert response == [3, 4]
    assert behavior == [-0.3, -0.4]


def test_miaoda_runner_skips_artifact_download_for_direct_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_script(
        "run_miaoda_skip_artifact_download_test",
        "scripts/run_miaoda_online_mopd.py",
    )

    def unexpected_download(*args, **kwargs):
        raise AssertionError("direct v2 captures must not download an artifact")

    monkeypatch.setattr(runner.AgentOSClient, "download_result", unexpected_download)
    args = SimpleNamespace(
        capture_dir=tmp_path,
        datawheel_url="http://datawheel.invalid",
        datawheel_token_env="DATAWHEEL_TOKEN",
        ors_timeout=180.0,
        artifact_grace_seconds=180.0,
    )

    resolved = asyncio.run(
        runner._resolve_artifact(
            args,
            Path(),
            {"miaoda_batch_task_id": "batch-direct"},
            skip_remote_fetch=True,
        )
    )

    assert resolved == tmp_path / "artifacts" / "datawheel-batch-direct.xlsx"


def test_miaoda_runner_merges_shared_capture_roots_and_deduplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_script(
        "run_miaoda_shared_capture_test",
        "scripts/run_miaoda_online_mopd.py",
    )
    shared_a = tmp_path / "shared-a"
    shared_b = tmp_path / "shared-b"
    shared_a.mkdir()
    shared_b.mkdir()

    def row(capture_id: str, timestamp: str) -> dict[str, object]:
        return {
            "schema_version": "mopd-policy-capture.v2",
            "capture_id": capture_id,
            "captured_at": timestamp,
            "prompt_token_ids": [1],
            "response_token_ids": [2],
            "behavior_logprobs": [-0.1],
        }

    (shared_a / "capture-unattributed.jsonl").write_text(
        "\n".join(
            json.dumps(item)
            for item in (
                row("before", "1970-01-01T00:01:00+00:00"),
                row("a", "1970-01-01T00:02:00+00:00"),
                row("b", "1970-01-01T00:03:00+00:00"),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (shared_b / "capture-unattributed.jsonl").write_text(
        "\n".join(
            json.dumps(item)
            for item in (
                row("b", "1970-01-01T00:03:00+00:00"),
                row("c", "1970-01-01T00:04:00+00:00"),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MOPD_POLICY_CAPTURE_DIRS", str(shared_b))

    paths = runner._capture_paths(
        tmp_path / "episode",
        "episode-1",
        capture_hint=str(shared_a / "capture-episode-1.jsonl"),
    )
    captures = runner._load_captures(paths, started_at=100.0)

    assert {path.parent for path in paths} == {shared_a, shared_b}
    assert [item["capture_id"] for item in captures] == ["a", "b", "c"]


def test_miaoda_runner_labels_pure_opd_metadata_without_goblin() -> None:
    runner = _load_script(
        "run_miaoda_online_mopd_metadata_test",
        "scripts/run_miaoda_online_mopd.py",
    )
    metadata = runner._safe_metadata(
        {
            "episode_id": "episode-opd",
            "training_objective": "pure_opd",
            "task_reward_required": False,
        },
        {
            "miaoda_batch_task_id": "batch-opd",
            "scalar_reward_available": True,
            "task_reward_used": False,
        },
    )
    assert metadata["openreward_mode"] == "agentos-datawheel-pure-opd"
    assert metadata["reward_source"] == "pure_opd_neutral"
    assert metadata["task_reward_used"] is False


def test_miaoda_runner_keeps_goblin_metadata_for_reward_rollouts() -> None:
    runner = _load_script(
        "run_miaoda_online_mopd_reward_metadata_test",
        "scripts/run_miaoda_online_mopd.py",
    )
    metadata = runner._safe_metadata(
        {"episode_id": "episode-reward"},
        {"goblin_job_id": "job-1", "task_reward_used": True},
    )
    assert metadata["openreward_mode"] == "agentos-datawheel-goblin"
    assert metadata["reward_source"] == "goblin"
    assert metadata["goblin_job_id"] == "job-1"


def test_miaoda_runners_reject_ors_tool_errors_instead_of_empty_polling() -> None:
    rollout = _load_script("run_miaoda_online_mopd_error_test", "scripts/run_miaoda_online_mopd.py")
    controller = _load_script(
        "run_miaoda_api_controller_error_test",
        "scripts/run_miaoda_api_controller.py",
    )
    failed = {"ok": False, "error": "tool execution failed: MiaodaOnlineError"}
    with pytest.raises(RuntimeError, match="MiaodaOnlineError"):
        rollout._tool_output(failed)
    with pytest.raises(RuntimeError, match="MiaodaOnlineError"):
        controller._tool_output(failed)
    assert rollout._tool_output({"ok": True, "output": {"finished": False}}) == {
        "finished": False
    }


def test_miaoda_teacher_reads_sglang_input_logprobs() -> None:
    runner = _load_script("run_miaoda_teacher_values_test", "scripts/run_miaoda_online_mopd.py")
    values = runner._teacher_values(
        {
            "meta_info": {
                "input_token_logprobs": [
                    (None, 1, None),
                    (-0.1, 2, None),
                    {"logprob": -0.2},
                    (-0.3, 4, None),
                ]
            }
        },
        response_length=2,
    )
    assert values == [-0.2, -0.3]

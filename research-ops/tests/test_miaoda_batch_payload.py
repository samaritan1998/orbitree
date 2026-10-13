from __future__ import annotations

import pytest
from research_ops.agentos import (
    MiaodaBatchPayloadError,
    build_miaoda_dataset_batch_payload,
    build_miaoda_e2e_batch_payload,
    miaoda_batch_identity,
)


def _rows() -> list[dict[str, object]]:
    return [
        {
            "id": "case-1",
            "query": "做一个跑腿小程序",
            "metadata": {
                "source_batch_id": "daily-1",
                "source_line_no": 7,
                "episode_id": "episode-1",
                "app_id": "source-app",
                "conv_id": "source-conv",
                "version_id": "source-version",
                "app_type": "MINIPROGRAM",
                "split": "canary",
            },
        }
    ]


def test_build_miaoda_payload_is_raw_query_e2e_only() -> None:
    payload = build_miaoda_e2e_batch_payload(
        _rows(),
        task_name="mopd-canary",
        generated_user_id="generated-user",
        model_name="model-r1",
        model_api_url="http://model/v1/chat/completions",
        model_api_key="runtime-key",
    )

    assert payload["selectedPipeline"] == "AgenticEndToEndPipeline"
    assert payload["queryMode"] == "deep_mode"
    assert payload["messageIntervalTimeoutSeconds"] == 1500
    record = payload["records"][0]
    assert record["id"] == 1
    assert record["query"] == "做一个跑腿小程序"
    assert record["default_agent"] == "AdaProAgent"
    assert record["state_input"]["query"] == "做一个跑腿小程序"
    assert "prd" not in record["state_input"]
    assert "replay_url" not in record["state_input"]
    assert record["meta"]["query_sha256"]
    assert record["meta"]["source_record_id"] == "case-1"
    assert record["meta"]["source_episode_id"] == "episode-1"
    assert record["meta"]["source_conv_id"] == "source-conv"
    assert payload["metadata"]["codingConfig"]["codingPromptiModelConfig"]["api_key"] == (
        "runtime-key"
    )


def test_payload_identity_does_not_change_with_model_api_key() -> None:
    kwargs = {
        "task_name": "mopd-canary",
        "generated_user_id": "generated-user",
        "model_name": "model-r1",
        "model_api_url": "http://model/v1/chat/completions",
    }
    first = build_miaoda_e2e_batch_payload(_rows(), model_api_key="first", **kwargs)
    second = build_miaoda_e2e_batch_payload(_rows(), model_api_key="second", **kwargs)
    assert miaoda_batch_identity(first) == miaoda_batch_identity(second)


def test_payload_rejects_empty_query_and_duplicate_id() -> None:
    with pytest.raises(MiaodaBatchPayloadError, match="empty query"):
        build_miaoda_e2e_batch_payload(
            [{"id": "case-1", "query": ""}],
            task_name="mopd-canary",
            generated_user_id="generated-user",
            model_name="model-r1",
            model_api_url="http://model",
            model_api_key="runtime-key",
        )

    duplicate = [{"id": "same", "query": "a"}, {"id": "same", "query": "b"}]
    with pytest.raises(MiaodaBatchPayloadError, match="duplicate record id"):
        build_miaoda_e2e_batch_payload(
            duplicate,
            task_name="mopd-canary",
            generated_user_id="generated-user",
            model_name="model-r1",
            model_api_url="http://model",
            model_api_key="runtime-key",
        )


def test_payload_rejects_message_watchdog_shorter_than_model_timeout() -> None:
    with pytest.raises(
        MiaodaBatchPayloadError,
        match="message interval timeout must exceed the model timeout",
    ):
        build_miaoda_e2e_batch_payload(
            _rows(),
            task_name="mopd-canary",
            generated_user_id="generated-user",
            model_name="model-r1",
            model_api_url="http://model",
            model_api_key="runtime-key",
            timeout_seconds=1200,
            message_interval_timeout_seconds=600,
        )


def test_payload_rejects_concurrency_above_agentos_single_instance_limit() -> None:
    with pytest.raises(
        MiaodaBatchPayloadError,
        match="concurrency must be between 1 and 80",
    ):
        build_miaoda_e2e_batch_payload(
            _rows(),
            task_name="mopd-canary",
            generated_user_id="generated-user",
            model_name="model-r1",
            model_api_url="http://model",
            model_api_key="runtime-key",
            concurrency=81,
        )


def test_dataset_payload_preserves_records_and_exact_qwen38_config() -> None:
    records = [{"id": 17, "state_input": {"prd": "build it"}}]
    payload = build_miaoda_dataset_batch_payload(
        records,
        task_name="eval-qw38-inhouse-300",
        dataset_name="example-app-300",
        generated_user_id="generated-user",
        model_name="demo-base-model",
        model_api_url="http://198.51.100.51:8080/v1/chat/completions",
        model_api_key="runtime-only-key",
        concurrency=8,
    )

    assert payload["records"] == records
    assert payload["records"] is not records
    assert payload["selectedDataset"] == {"value": "example-app-300", "hash": "**"}
    assert payload["selectedPipeline"] == "AgenticFirstCodingPipeline"
    assert payload["concurrency"] == 8
    config = payload["metadata"]["codingConfig"]
    assert config["codingPromptiVariant"] == "qianfan-coder"
    assert config["codingPromptiModelConfig"] == {
        "provider": "openai",
        "api_url": "http://198.51.100.51:8080/v1/chat/completions",
        "model_value": "demo-base-model",
        "model": "demo-base-model",
        "max_tokens": 32000,
        "api_key": "runtime-only-key",
        "timeout": 600,
        "temperature": 0.6,
        "top_p": 0.95,
        "thinking_config": {
            "chat_template_kwargs": {
                "enable_thinking": True,
                "preserve_thinking": True,
            }
        },
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
    }


def test_dataset_payload_identity_redacts_runtime_model_key() -> None:
    kwargs = {
        "records": [{"id": 1}],
        "task_name": "eval-qw38-inhouse-300",
        "dataset_name": "example-app-300",
        "generated_user_id": "generated-user",
        "model_name": "demo-base-model",
        "model_api_url": "http://gateway/v1/chat/completions",
    }
    first = build_miaoda_dataset_batch_payload(model_api_key="first", **kwargs)
    second = build_miaoda_dataset_batch_payload(model_api_key="second", **kwargs)

    assert miaoda_batch_identity(first) == miaoda_batch_identity(second)

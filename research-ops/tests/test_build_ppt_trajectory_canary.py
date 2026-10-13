from __future__ import annotations

import base64
import json
from pathlib import Path

from scripts.build_ppt_trajectory_canary import (
    AssetStore,
    backfill_nested_thinking,
    build_decode_occurrences,
    externalize_images,
    load_rewards,
    reward_fields,
    sanitize,
    sha256_text,
)


def test_sanitize_removes_credentials_and_credential_hashes() -> None:
    value = {
        "authorization": "Bearer secret-value-123456",
        "api_key_sha256": "should-not-be-retained",
        "nested": "token sk-TEST_FIXTURE_NOT_A_REAL_KEY_123456 and https://user:pass@example.test/api",
        "signature": "signature-is-not-a-credential",
    }

    sanitized = sanitize(value)

    assert "authorization" not in sanitized
    assert "api_key_sha256" not in sanitized
    assert "sk-TEST_FIXTURE_NOT_A_REAL_KEY_123456" not in sanitized["nested"]
    assert "user:pass@" not in sanitized["nested"]
    assert sanitized["signature"] == "signature-is-not-a-credential"


def test_externalize_images_preserves_position_and_deduplicates(tmp_path: Path) -> None:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    encoded = base64.b64encode(png).decode("ascii")
    source = {
        "type": "tool_result",
        "tool_use_id": "tool-1",
        "content": [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": encoded},
            },
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": encoded},
            },
        ],
    }
    store = AssetStore(tmp_path)
    observations: list[dict] = []

    transformed = externalize_images(
        source,
        store,
        observations=observations,
        pointer="/0/message/content/0",
        event_index=0,
    )

    assert len(observations) == 2
    assert len(store.unique) == 1
    assert observations[0]["tool_call_id"] == "tool-1"
    assert observations[0]["json_pointer"].endswith("/source")
    image_source = transformed["content"][0]["source"]
    assert image_source["type"] == "file"
    assert "data" not in image_source
    assert (tmp_path / image_source["path"]).is_file()


def test_backfill_nested_thinking_updates_repeated_trace_history() -> None:
    signature = "test-signature"
    value = {
        "request": {
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "", "signature": signature}],
                }
            ]
        },
        "response": {"content": [{"type": "thinking", "thinking": "", "signature": signature}]},
    }

    count = backfill_nested_thinking(value, {sha256_text(signature): "decoded text"})

    assert count == 2
    assert value["request"]["messages"][0]["content"][0]["thinking"] == "decoded text"
    assert value["response"]["content"][0]["thinking"] == "decoded text"


def test_failed_decode_keeps_only_structured_error_and_sidecar(tmp_path: Path) -> None:
    signature = "failed-signature"
    digest = sha256_text(signature)
    payload, counts = build_decode_occurrences(
        [
            {
                "occurrence_index": 7,
                "source_kind": "raw_ccrelay",
                "request_index": 2,
                "request_id": "request-2",
                "content_index": 0,
                "signature": signature,
                "signature_sha256": digest,
                "signature_length": len(signature),
                "base64_valid": False,
            }
        ],
        {
            digest: {
                "decode_status": "failed",
                "decoded_text": None,
                "decoded_by": None,
                "cache_hit": None,
                "decoder_usage": None,
                "decode_attempt_count": 2,
                "decoded_at": "2026-08-03T00:00:00Z",
                "http_status": 502,
                "error_code": "decode_failed",
            }
        },
        {digest},
        {},
        tmp_path / "controlled_decode_sidecar.jsonl",
    )

    occurrence = payload["occurrences"][0]
    assert counts == {"total": 1, "failed": 1}
    assert occurrence["decode_status"] == "failed"
    assert occurrence["error_code"] == "decode_failed"
    assert occurrence["decoded_text"] is None
    assert occurrence["decoder_error_sidecar_path"].endswith("controlled_decode_sidecar.jsonl")
    assert "response" not in occurrence
    assert "error_message" not in occurrence


def test_reward_detail_is_loaded_sanitized_and_inlined(tmp_path: Path) -> None:
    reward_root = tmp_path / "postprocess" / "agentic_reward_legacy_scale_20260803"
    final_dir = reward_root / "final_agentic_reward_vlm_opus47"
    evaluation_dir = reward_root / "reward_c150_full497_vlm_opus47" / "evaluations"
    final_dir.mkdir(parents=True)
    evaluation_dir.mkdir(parents=True)
    score = {
        "task_id": "task-1",
        "reward_score": 0.75,
        "quality_band": "usable",
        "rubric_source": "generated_this_run",
        "evaluation_valid": True,
        "training_usable": True,
        "system_error_items": 0,
        "cannot_determine_items": 0,
        "not_evaluated_items": 0,
        "total_items": 2,
    }
    detail = {
        "reward_score": 0.75,
        "quality_band": "usable",
        "domain_scores": {
            "Requirement Compliance": 0.8,
            "Substance Quality": 0.7,
            "Presentation Design Quality": 0.75,
        },
        "evaluation_confidence": {"evaluation_valid": True},
        "item_results": [
            {"item_id": "REQ_001", "evaluation_method": "llm_text", "score": 0.5},
            {"item_id": "DES_001", "evaluation_method": "vlm_visual", "score": 1.0},
        ],
        "ordinal_quality": {"label": "usable"},
        "summary": {"pass": 1, "fail": 1},
        "evaluation_notes": [],
        "authorization": "Bearer should-be-removed",
    }
    (final_dir / "agentic_reward_scores.jsonl").write_text(
        json.dumps(score) + "\n", encoding="utf-8"
    )
    (final_dir / "agentic_reward_summary.json").write_text(
        json.dumps(
            {
                "judge_models": {
                    "llm_text": "claude-haiku-4-5-20251001",
                    "vlm_visual": "claude-opus-4-7",
                }
            }
        ),
        encoding="utf-8",
    )
    (evaluation_dir / "task-1.json").write_text(json.dumps(detail), encoding="utf-8")

    reward = load_rewards(tmp_path)["task-1"]
    fields = reward_fields("run-1", "task-1", "rollout", {"selected_source": "rollout"}, reward)

    assert fields["reward_status"] == "complete"
    assert fields["reward_record_id"].startswith("reward_")
    assert fields["reward_detail_status"] == "complete"
    assert fields["reward_detail_integrity_status"] == "pass"
    assert fields["reward_detail_item_count"] == 2
    assert fields["reward_detail"]["item_results"] == detail["item_results"]
    assert "authorization" not in fields["reward_detail"]
    assert fields["reward_judge_models"]["vlm_visual"] == "claude-opus-4-7"
    assert fields["reward_requirement_compliance"] == 0.8
    assert len(fields["reward_detail_source_sha256"]) == 64
    assert len(fields["reward_detail_sha256"]) == 64


def test_unselected_attempt_keeps_reward_fields_but_no_detail() -> None:
    fields = reward_fields(
        "run-1",
        "task-1",
        "rollout",
        {"selected_source": "retry_missing_8"},
        {"reward_score": 1.0},
    )

    assert fields["reward_status"] == "not_selected_for_evaluation"
    assert fields["reward_detail_status"] == "not_applicable"
    assert fields["reward_detail"] is None
    assert fields["reward_detail_integrity_status"] == "not_applicable"

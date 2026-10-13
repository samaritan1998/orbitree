from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/cap_slime_openreward_prompt_history.py"
SPEC = importlib.util.spec_from_file_location("cap_slime_openreward_prompt_history", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _sample(*, prompt_tokens: int = 5, response_tokens: int = 3, multimodal: bool = False) -> dict:
    total = prompt_tokens + response_tokens
    sample = {
        "index": 7,
        "tokens": list(range(total)),
        "response_length": response_tokens,
        "loss_mask": [1] * response_tokens,
        "rollout_log_probs": [-0.1] * response_tokens,
        "teacher_log_probs": [-0.2] * response_tokens,
        "prompt": "stale prompt",
        "metadata": {},
    }
    if multimodal:
        sample["multimodal_inputs"] = {"images": ["opaque"]}
    return sample


def test_caps_only_prompt_and_preserves_response_aligned_fields() -> None:
    capped, row = MODULE.cap_sample(_sample(), max_tokens=6, index=0)

    assert capped["tokens"] == [2, 3, 4, 5, 6, 7]
    assert capped["response_length"] == 3
    assert capped["loss_mask"] == [1, 1, 1]
    assert capped["rollout_log_probs"] == [-0.1, -0.1, -0.1]
    assert capped["teacher_log_probs"] == [-0.2, -0.2, -0.2]
    assert capped["prompt"] == ""
    assert capped["metadata"]["openreward_prompt_tokens_dropped"] == 2
    assert row["dropped_prompt_tokens"] == 2


def test_refuses_to_truncate_response() -> None:
    with pytest.raises(ValueError, match="refusing to truncate response tokens"):
        MODULE.cap_sample(_sample(prompt_tokens=1, response_tokens=4), max_tokens=3, index=0)


def test_refuses_left_trim_of_multimodal_sample() -> None:
    with pytest.raises(ValueError, match="multimodal inputs"):
        MODULE.cap_sample(_sample(prompt_tokens=5, multimodal=True), max_tokens=6, index=0)


def test_payload_summary_counts_capped_samples() -> None:
    output, summary = MODULE.cap_payload(
        {"format": "slime.debug_rollout.v1", "samples": [_sample(), _sample(prompt_tokens=1)]},
        max_tokens=6,
    )

    assert len(output["samples"]) == 2
    assert summary["sample_count"] == 2
    assert summary["capped_sample_count"] == 1
    assert summary["max_token_count"] == 6

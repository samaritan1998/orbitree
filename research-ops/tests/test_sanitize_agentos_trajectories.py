from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "sanitize_agentos_trajectories.py"
    spec = importlib.util.spec_from_file_location("agentos_trajectory_sanitizer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_sanitize_redacts_credentials_but_preserves_token_metrics() -> None:
    sanitizer = _module()
    counts = sanitizer.Counts()
    cleaned = sanitizer.sanitize(
        {
            "api_key": "secret",
            "max_tokens": 32000,
            "usage": {"total_tokens": 42},
            "text": "curl -H 'Authorization: Bearer abc.def'",
        },
        counts=counts,
    )

    assert cleaned["api_key"] == "<redacted>"
    assert cleaned["max_tokens"] == 32000
    assert cleaned["usage"]["total_tokens"] == 42
    assert "Bearer <redacted>" in cleaned["text"]
    assert counts.sensitive_fields == 1
    assert counts.bearer_strings == 1

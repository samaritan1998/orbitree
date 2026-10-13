from __future__ import annotations

import json
from types import SimpleNamespace

from scripts.run_ccrelay_claude_compat import (
    _replace_request_body,
    normalize_claude_metadata,
)


def test_normalize_claude_json_user_id_to_legacy_six_segment_format() -> None:
    body = json.dumps(
        {
            "model": "judge",
            "metadata": {
                "user_id": json.dumps(
                    {
                        "device_id": "device_with_underscores",
                        "account_uuid": "",
                        "session_id": "session_with_underscores",
                    }
                )
            },
        }
    ).encode()

    normalized = json.loads(normalize_claude_metadata(body).decode())

    assert normalized["metadata"]["user_id"] == (
        "user_device-with-underscores_account_anonymous_session_session-with-underscores"
    )
    assert normalized["model"] == "judge"


def test_normalize_claude_metadata_preserves_legacy_and_non_json_payloads() -> None:
    legacy = b'{"metadata":{"user_id":"user_u_account_a_session_s"}}'
    assert normalize_claude_metadata(legacy) == legacy
    assert normalize_claude_metadata(b"not-json") == b"not-json"
    assert normalize_claude_metadata(b'{"model":"judge"}') == b'{"model":"judge"}'


def test_normalize_claude_metadata_uses_session_header_fallback() -> None:
    body = b'{"metadata":{"user_id":"{}"}}'

    normalized = json.loads(
        normalize_claude_metadata(body, session_hint="control_session").decode()
    )

    assert normalized["metadata"]["user_id"].endswith("_session_control-session")


def test_replace_request_body_updates_asgi_content_length() -> None:
    request = SimpleNamespace(
        scope={
            "headers": [
                (b"content-type", b"application/json"),
                (b"Content-Length", b"999"),
            ]
        }
    )

    _replace_request_body(request, b"updated")

    assert request._body == b"updated"
    assert request.scope["headers"] == [
        (b"content-type", b"application/json"),
        (b"content-length", b"7"),
    ]

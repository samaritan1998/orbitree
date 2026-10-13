from research_ops.services.codex_events import redact


def test_redact_covers_common_token_and_chinese_separator_forms() -> None:
    source = " ".join(
        (
            "sk-examplecredential123456",
            "AppKey：exampleappkey123456",
            "AppSecret：exampleappsecret123456",
            "Authorization: Bearer-examplecredential123456",
        )
    )

    redacted = redact(source)

    assert "examplecredential" not in redacted
    assert "exampleappkey" not in redacted
    assert "exampleappsecret" not in redacted
    assert redacted.count("[REDACTED]") == 4

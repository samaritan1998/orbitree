import fcntl
import json
from pathlib import Path

import pytest
from research_ops.codex.history_cleanup import (
    clean_prompt,
    clean_rollout,
    restore_rollout,
    rewrite_line,
)
from research_ops.services.instruction_cleanup import GENERATED_AGENTS

WRAPPER = (
    "<research_ops_web_turn_limits>\n"
    "This is a managed Research Ops Web turn. Keep the investigation bounded.\n"
    "</research_ops_web_turn_limits>\n\n"
)
QUERY = "  keep my query\n\n  "


def record(kind, payload, ordinal=1):
    return (json.dumps({"type": kind, "payload": payload, "ordinal": ordinal}) + "\n").encode()


def message(text, role="user"):
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


def test_exact_query_and_quoted_wrappers_are_preserved():
    assert clean_prompt(WRAPPER + QUERY) == QUERY
    quote = "Why was this added?\n" + WRAPPER
    assert clean_prompt(quote) == quote
    literal = WRAPPER + "Why was this added?"
    assert clean_prompt(literal, frozenset({literal})) == literal
    assert clean_prompt("<research_ops_brief>my own XML</research_ops_brief>\nhi") != "hi"


def test_assistant_tools_native_context_and_compaction_are_preserved():
    for role in ("assistant", "tool"):
        source = record("response_item", message(WRAPPER, role))
        result, changes, _ = rewrite_line(source)
        assert result == source
        assert not changes
    native = message("<skills_instructions>My skills</skills_instructions>", "developer")
    source = record("response_item", native)
    assert rewrite_line(source)[0] == source
    source = record(
        "compacted",
        {
            "replacement_history": [message(WRAPPER + QUERY), native],
            "message": "Human-approved summary",
        },
    )
    result, changes, _ = rewrite_line(source)
    parsed = json.loads(result)["payload"]
    assert parsed["replacement_history"][0]["content"][0]["text"] == QUERY
    assert parsed["replacement_history"][1] == native
    assert parsed["message"] == "Human-approved summary"
    assert len(result) == len(source)
    assert changes


def test_developer_and_generated_agents_remove_only_platform_parts():
    developer = message("You are executing one Research Ops branch. Work ID: TEST", "developer")
    developer["content"].append({"type": "input_text", "text": "CUSTOM_NATIVE_INSTRUCTION"})
    result, changes, _ = rewrite_line(record("response_item", developer))
    assert json.loads(result)["payload"]["content"] == [
        {"type": "input_text", "text": ""},
        {"type": "input_text", "text": "CUSTOM_NATIVE_INSTRUCTION"},
    ]
    assert changes["developer_instructions"] == 1
    generated = next(iter(GENERATED_AGENTS))
    source = record("world_state", {"state": {"agents_md": {"text": generated}}})
    assert json.loads(rewrite_line(source)[0])["payload"]["state"]["agents_md"]["text"] == ""
    source = record("world_state", {"state": {"agents_md": {"text": generated + "My rule"}}})
    assert rewrite_line(source)[0] == source


def test_migration_backups_offsets_identity_dry_run_and_idempotence(tmp_path):
    path = tmp_path / "rollout.jsonl"
    original = record("session_meta", {"id": "abc"}, 0) + record(
        "response_item", message(WRAPPER + QUERY)
    )
    path.write_bytes(original)
    before_stat = path.stat()
    args = dict(thread_id="abc", sqlite_home=tmp_path)
    assert clean_rollout(path, **args)["status"] == "needs_cleanup"
    assert path.read_bytes() == original
    report = clean_rollout(path, backup_dir=tmp_path / "backup", **args)
    assert report["status"] == "cleaned"
    assert (tmp_path / "backup" / "abc" / path.name).read_bytes() == original
    assert [len(line) for line in original.splitlines()] == [
        len(line) for line in path.read_bytes().splitlines()
    ]
    assert path.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert clean_rollout(path, backup_dir=tmp_path / "second", **args)["status"] == "unchanged"


def test_active_child_writer_blocks_parent_cleanup(tmp_path):
    path = tmp_path / "rollout.jsonl"
    original = record("session_meta", {"id": "parent"}, 0) + record(
        "response_item", message(WRAPPER + QUERY)
    )
    path.write_bytes(original)
    locks = tmp_path / "thread-writer-locks"
    locks.mkdir()
    with (locks / "child.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = clean_rollout(
            path,
            thread_id="parent",
            sqlite_home=tmp_path,
            backup_dir=tmp_path / "backup",
            dependent_threads=["child"],
        )
    assert result["status"] == "busy"
    assert path.read_bytes() == original
    assert not (tmp_path / "backup").exists()


def test_invalid_history_fails_without_partial_mutation(tmp_path):
    path = tmp_path / "rollout.jsonl"
    original = (
        record("session_meta", {"id": "abc"}, 0)
        + record("response_item", message(WRAPPER + QUERY))
        + b"broken\n"
    )
    path.write_bytes(original)
    result = clean_rollout(
        path, thread_id="abc", sqlite_home=tmp_path, backup_dir=tmp_path / "backup"
    )
    assert result["status"] == "error"
    assert path.read_bytes() == original


def test_restore_checks_hash_and_never_discards_later_turns(tmp_path):
    path = tmp_path / "rollout.jsonl"
    original = record("session_meta", {"id": "abc"}, 0) + record(
        "response_item", message(WRAPPER + QUERY)
    )
    path.write_bytes(original)
    report = clean_rollout(
        path, thread_id="abc", sqlite_home=tmp_path, backup_dir=tmp_path / "backup"
    )
    receipt = Path(report["backup"]).parent / "receipt.json"
    cleaned = path.read_bytes()
    path.write_bytes(cleaned + record("response_item", message("New turn"), 2))
    with pytest.raises(ValueError, match="discard new work"):
        restore_rollout(receipt, sqlite_home=tmp_path)
    path.write_bytes(cleaned)
    restore_rollout(receipt, sqlite_home=tmp_path)
    assert path.read_bytes() == original


def test_nested_literal_query_and_generated_prefix_separator():
    literal = WRAPPER + "Explain this wrapper"
    assert clean_prompt(WRAPPER + literal, frozenset({literal})) == literal


def test_opaque_compaction_and_user_customized_rules_are_not_discarded():
    source = record("compacted", {
        "replacement_history": [{"type": "compaction", "encrypted_content": "opaque"}],
        "message": "Research Ops was discussed in an earlier task",
    })
    assert rewrite_line(source)[0] == source
    custom = next(iter(GENERATED_AGENTS)) + "Do not touch my project policy"
    source = record("response_item", message(custom))
    assert rewrite_line(source)[0] == source

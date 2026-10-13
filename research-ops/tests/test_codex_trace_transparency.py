"""Inspect real Codex wire requests and JSONL using a local synthetic model endpoint."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from types import SimpleNamespace

import pytest
from research_ops.codex.cli import CliCodexAdapter
from research_ops.codex.history_cleanup import clean_rollout
from research_ops.codex.rollout import find_rollout
from research_ops.settings import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("ROPS_RUN_NATIVE_GOAL_PROTOCOL") != "1" or not shutil.which("codex"),
    reason="Set ROPS_RUN_NATIVE_GOAL_PROTOCOL=1 for isolated real Codex trace tests",
)

PLATFORM_MARKERS = (
    "research_ops_policy", "research_ops_web_turn_limits", "research_ops_brief",
    "research_ops_handoff_policy", "research_ops_thread_recovery",
    "You are executing one Research Ops branch", "Research Ops Program Desk coordinator",
    "BRIEF_MUST_STAY_IN_UI", "DEVELOPER_MUST_STAY_IN_UI",
)
NATIVE_RULE = "USER_AUTHORED_RULE_FROM_AGENTS_51c8"
QUERY = "  \u7ee7\u7eed\u5b9e\u9a8c TRACE_QUERY_51c8\n\nKeep **my** formatting.  "
FOLLOW_UP = "  TRACE_FOLLOW_UP_51c8\nKeep this exact message.  "


def _message_text(item):
    content = item.get("content") or []
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content if isinstance(part, dict))


def _messages(packet, role):
    return [
        _message_text(item) for item in packet.get("input", [])
        if isinstance(item, dict) and item.get("role") == role
    ]


def _marker_hits(value):
    encoded = json.dumps(value, ensure_ascii=False)
    return {marker: encoded.count(marker) for marker in PLATFORM_MARKERS if marker in encoded}


async def _wait_until(predicate):
    async with asyncio.timeout(20):
        while not predicate():
            await asyncio.sleep(0.05)


@pytest.fixture
async def trace_probe(tmp_path):
    requests = []
    errors = []

    async def serve(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            fields = dict(
                line.lower().split(":", 1)
                for line in header.decode().split("\r\n")[1:] if ":" in line
            )
            packet = json.loads(await reader.readexactly(int(fields["content-length"])))
            requests.append(packet)
            number = len(requests)
            item = {
                "id": f"msg_{number}", "type": "message", "status": "completed",
                "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": f"Synthetic result {number}",
                             "annotations": []}],
            }
            response = {
                "id": f"resp_{number}", "object": "response", "status": "completed",
                "created_at": 1, "model": "gpt-6-astra", "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
            }
            events = [
                {"type": "response.created",
                 "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, "status": "in_progress", "content": []}},
                {"type": "response.output_text.delta", "item_id": item["id"],
                 "output_index": 0, "content_index": 0, "delta": item["content"][0]["text"]},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            body = "".join(
                f"event: {entry['type']}\ndata: {json.dumps(entry)}\n\n" for entry in events
            ).encode()
            await asyncio.sleep(0.1)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            )
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            errors.append(str(exc))
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    home, codex_home, workspace = (tmp_path / name for name in ("home", "codex", "work"))
    for path in (home, codex_home, workspace):
        path.mkdir()
    (workspace / "AGENTS.md").write_text(f"# User rules\n\n{NATIVE_RULE}\n")
    overrides = {
        "model_provider": "probe", "model": "gpt-6-astra", "model_reasoning_effort": "low",
        "approval_policy": "never", "sandbox_mode": "read-only",
        "sqlite_home": str(codex_home), "features.goals": True,
        "model_providers.probe.name": "Local trace test",
        "model_providers.probe.base_url": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1",
        "model_providers.probe.wire_api": "responses",
        "model_providers.probe.requires_openai_auth": False,
        "model_providers.probe.supports_websockets": False,
        "model_providers.probe.request_max_retries": 0,
        "model_providers.probe.stream_max_retries": 0,
    }
    args = [
        part for key, value in overrides.items() for part in ("-c", f"{key}={json.dumps(value)}")
    ]
    env = {
        "PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(codex_home),
        "NO_PROXY": "*", "no_proxy": "*",
    }
    launch = SimpleNamespace(
        args=lambda: args, environment=lambda: env,
        execution={"model": "gpt-6-astra", "provider": "probe", "reasoning_effort": "low"},
    )
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "data", workspace_root=workspace,
        codex_home=codex_home, codex_enabled=True, codex_command=shutil.which("codex"),
        codex_model="gpt-6-astra", codex_model_provider="probe",
    )
    adapters = []
    events = []

    async def event(method, params):
        events.append((method, params))

    async def request(request_id, method, params):
        if method == "currentTime/read":
            import time
            await adapters[-1].lifecycle.respond(request_id, {"currentTimeAt": int(time.time())})

    def new_adapter():
        adapter = CliCodexAdapter(
            settings, event_handler=event, request_handler=request, launch=launch
        )
        adapters.append(adapter)
        return adapter

    probe = SimpleNamespace(
        settings=settings, launch=launch, requests=requests, errors=errors, new_adapter=new_adapter,
        events=events, workspace=workspace, codex_home=codex_home, output=tmp_path,
    )
    try:
        yield probe
    finally:
        for adapter in adapters:
            await adapter.stop()
        server.close()
        await server.wait_closed()
        (tmp_path / "wire-requests.json").write_text(
            json.dumps(requests, ensure_ascii=False, indent=2)
        )


async def _send_loaded(probe, adapter, thread_id, text):
    result = await adapter.lifecycle.send_message(
        thread_id=thread_id, content=text, cwd=str(probe.workspace), metadata={},
        additional_context={"brief": {"objective": "BRIEF_MUST_STAY_IN_UI"}},
    )
    turn_id = result["turn"]["id"]
    await _wait_until(lambda: any(
        method == "turn/completed" and params.get("turn", {}).get("id") == turn_id
        for method, params in probe.events
    ))
    assert any(
        method == "turn/completed" and params.get("turn", {}).get("id") == turn_id
        and params["turn"]["status"] == "completed"
        for method, params in probe.events
    )


def _save_report(probe, report):
    rollouts = []
    for path in sorted((probe.codex_home / "sessions").rglob("*.jsonl")):
        records = [json.loads(line) for line in path.read_text().splitlines()]
        users = [
            _message_text(record["payload"])
            for record in records
            if record.get("type") == "response_item"
            and record.get("payload", {}).get("role") == "user"
        ]
        rollouts.append({
            "path": str(path), "records": len(records), "platform_markers": _marker_hits(records),
            "exact_probe_queries": [value for value in users if "TRACE_" in value],
        })
    report["rollouts"] = rollouts
    report["wire_request_count"] = len(probe.requests)
    (probe.output / "audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return rollouts


async def test_new_resumed_and_forked_threads_match_native_context(trace_probe):
    probe = trace_probe
    process = await asyncio.create_subprocess_exec(
        probe.settings.codex_command, *probe.launch.args(), "-C", str(probe.workspace),
        "exec", "--json", "--skip-git-repo-check", "-", env=probe.launch.environment(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(process.communicate(QUERY.encode()), timeout=30)
        assert process.returncode == 0, stderr.decode()[-1500:]
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    native = probe.requests[-1]
    adapter = probe.new_adapter()
    first = await adapter.send_message(
        thread_id="deferred:probe", content=QUERY, cwd=str(probe.workspace), metadata={},
        additional_context={"brief": {"objective": "BRIEF_MUST_STAY_IN_UI"}},
    )
    managed = probe.requests[-1]
    assert QUERY in _messages(native, "user")
    assert QUERY in _messages(managed, "user")
    assert managed.get("instructions") == native.get("instructions")
    assert _messages(managed, "developer") == _messages(native, "developer")
    thread_id = first["thread"]["id"]
    resumed = await adapter.send_message(
        thread_id=thread_id, content=FOLLOW_UP, cwd=str(probe.workspace), metadata={},
        additional_context={"brief": {"objective": "BRIEF_MUST_STAY_IN_UI"}},
    )
    assert resumed["thread"]["id"] == thread_id
    assert QUERY in _messages(probe.requests[-1], "user")
    assert FOLLOW_UP in _messages(probe.requests[-1], "user")
    child = await adapter.fork_thread(
        thread_id=thread_id, cwd=str(probe.workspace), instructions="DEVELOPER_MUST_STAY_IN_UI",
    )
    child_id = child["thread"]["id"]
    assert child_id != thread_id
    child_query = "TRACE_CHILD_QUERY_51c8"
    await adapter.send_message(
        thread_id=child_id, content=child_query, cwd=str(probe.workspace), metadata={},
    )
    assert QUERY in _messages(probe.requests[-1], "user")
    assert child_query in _messages(probe.requests[-1], "user")
    app_thread = await adapter.lifecycle.start_thread(
        cwd=str(probe.workspace), instructions="DEVELOPER_MUST_STAY_IN_UI",
    )
    await _send_loaded(probe, adapter, app_thread["thread"]["id"], "TRACE_APP_SERVER_QUERY_51c8")
    await adapter.stop()
    assert all(not _marker_hits(packet) for packet in probe.requests)
    assert all(NATIVE_RULE in json.dumps(packet) for packet in probe.requests)
    assert not probe.errors
    rollouts = _save_report(probe, {
        "scenario": "clean_new_resume_fork_app_server",
        "verdict": "no_new_platform_injection", "native_developer_messages_equal": True,
        "native_base_instructions_sha256": hashlib.sha256(
            str(native.get("instructions", "")).encode()
        ).hexdigest(),
        "query_is_exact": True, "resume_thread_preserved": True, "fork_history_preserved": True,
        "user_agents_rule_preserved": True, "wire_platform_markers": _marker_hits(probe.requests),
    })
    assert rollouts
    assert all(not row["platform_markers"] for row in rollouts)
    path = find_rollout(probe.codex_home / "sessions", thread_id)
    parent = next(row for row in rollouts if row["path"] == str(path))
    assert QUERY in parent["exact_probe_queries"]
    assert FOLLOW_UP in parent["exact_probe_queries"]


async def test_legacy_history_remains_visible_to_resume_fork_and_goal(trace_probe):
    probe = trace_probe
    adapter = probe.new_adapter()
    legacy = await adapter.lifecycle._request("thread/start", {
        "cwd": str(probe.workspace), "model": "gpt-6-astra", "modelProvider": "probe",
        "developerInstructions": "You are executing one Research Ops branch. LEGACY_SCOPE_51c8",
    })
    thread_id = legacy["thread"]["id"]
    legacy_query = "<research_ops_policy>LEGACY_POLICY_51c8</research_ops_policy>\nOld user query"
    await _send_loaded(probe, adapter, thread_id, legacy_query)
    await adapter.stop()
    adapter = probe.new_adapter()
    await adapter.send_message(
        thread_id=thread_id, content=FOLLOW_UP, cwd=str(probe.workspace), metadata={},
        additional_context={"brief": {"objective": "BRIEF_MUST_STAY_IN_UI"}},
    )
    resumed = probe.requests[-1]
    assert FOLLOW_UP in _messages(resumed, "user")
    assert legacy_query in _messages(resumed, "user")
    child = await adapter.fork_thread(
        thread_id=thread_id, cwd=str(probe.workspace), instructions="DEVELOPER_MUST_STAY_IN_UI",
    )
    await adapter.send_message(
        thread_id=child["thread"]["id"], content="TRACE_LEGACY_CHILD_51c8",
        cwd=str(probe.workspace), metadata={},
    )
    forked = probe.requests[-1]
    assert legacy_query in _messages(forked, "user")
    await adapter.prepare_goal_thread(thread_id=thread_id, cwd=str(probe.workspace))
    objective = "TRACE_GOAL_OBJECTIVE_51c8"
    previous = len(probe.requests)
    await adapter.set_goal(thread_id=thread_id, objective=objective, status="active")
    await _wait_until(lambda: len(probe.requests) > previous)
    goal_packet = probe.requests[-1]
    await adapter.set_goal(thread_id=thread_id, status="paused")
    assert (await adapter.get_goal(thread_id=thread_id))["goal"]["objective"] == objective
    await adapter.stop()
    assert legacy_query in _messages(goal_packet, "user")
    assert not any("BRIEF_MUST_STAY_IN_UI" in json.dumps(packet) for packet in probe.requests)
    assert not any("DEVELOPER_MUST_STAY_IN_UI" in json.dumps(packet) for packet in probe.requests)
    assert not probe.errors
    _save_report(probe, {
        "scenario": "legacy_resume_fork_goal", "verdict": "legacy_contamination_replayed",
        "new_query_is_exact": True, "new_platform_context_injected": False,
        "resume_history_markers": _marker_hits(resumed),
        "fork_history_markers": _marker_hits(forked),
        "goal_history_markers": _marker_hits(goal_packet),
        "legacy_developer_instruction_replayed": "LEGACY_SCOPE_51c8" in json.dumps(resumed),
    })


async def test_legacy_cleanup_reaches_native_resume_existing_fork_and_goal(trace_probe):
    probe = trace_probe
    adapter = probe.new_adapter()
    legacy = await adapter.lifecycle._request("thread/start", {
        "cwd": str(probe.workspace), "model": "gpt-6-astra", "modelProvider": "probe",
        "developerInstructions": "You are executing one Research Ops branch. LEGACY_SCOPE_51c8",
    })
    thread_id = legacy["thread"]["id"]
    wrapper = (
        "<research_ops_web_turn_limits>\n"
        "This is a managed Research Ops Web turn. Keep the investigation bounded.\n"
        "</research_ops_web_turn_limits>\n\n"
    )
    await _send_loaded(probe, adapter, thread_id, wrapper + QUERY)
    path = find_rollout(probe.codex_home / "sessions", thread_id)
    assert path
    # A live native writer, even between turns, must prevent migration.
    audit = clean_rollout(path, thread_id=thread_id, sqlite_home=probe.codex_home)
    assert audit["status"] == "busy"
    await adapter.stop()
    adapter = probe.new_adapter()
    child = await adapter.fork_thread(
        thread_id=thread_id, cwd=str(probe.workspace), instructions="",
    )
    child_id = child["thread"]["id"]
    await adapter.stop()
    original = path.read_bytes()
    report = clean_rollout(
        path, thread_id=thread_id, sqlite_home=probe.codex_home,
        backup_dir=probe.output / "backup", dependent_threads=[child_id],
    )
    assert report["status"] == "cleaned", report
    assert path.stat().st_size == len(original)
    adapter = probe.new_adapter()
    await adapter.send_message(thread_id=thread_id, content=FOLLOW_UP,
                               cwd=str(probe.workspace), metadata={})
    resumed = probe.requests[-1]
    assert QUERY in _messages(resumed, "user")
    assert FOLLOW_UP in _messages(resumed, "user")
    assert "Synthetic result 1" in _messages(resumed, "assistant")
    assert not _marker_hits(resumed)
    await adapter.send_message(thread_id=child_id, content="TRACE_CLEAN_CHILD_51c8",
                               cwd=str(probe.workspace), metadata={})
    forked = probe.requests[-1]
    assert QUERY in _messages(forked, "user")
    assert "Synthetic result 1" in _messages(forked, "assistant")
    assert not _marker_hits(forked)
    await adapter.prepare_goal_thread(thread_id=thread_id, cwd=str(probe.workspace))
    previous = len(probe.requests)
    await adapter.set_goal(thread_id=thread_id, objective="TRACE_CLEAN_GOAL_51c8", status="active")
    await _wait_until(lambda: len(probe.requests) > previous)
    goal_packet = probe.requests[-1]
    await adapter.set_goal(thread_id=thread_id, status="paused")
    assert QUERY in _messages(goal_packet, "user")
    assert not _marker_hits(goal_packet)
    assert all(NATIVE_RULE in json.dumps(packet) for packet in (resumed, forked, goal_packet))
    await adapter.stop()
    _save_report(probe, {
        "scenario": "legacy_cleanup_resume_existing_fork_goal", "verdict": "cleaned",
        "query_and_answer_preserved": True, "native_writer_lock_verified": True,
        "parent_and_child_ids_preserved": True, "byte_offsets_preserved": True,
        "resume_history_markers": _marker_hits(resumed),
        "fork_history_markers": _marker_hits(forked),
        "goal_history_markers": _marker_hits(goal_packet), "migration": report,
    })

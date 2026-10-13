"""Opt-in real Codex binary test against a local, credential-free Responses server."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from research_ops import models, schemas
from research_ops.codex.cli import CliCodexAdapter
from research_ops.services.native_goals import NativeGoalService
from research_ops.services.turn_notifications import TurnNotificationService
from test_native_goals import _materialized_branch
from test_turn_notifications import FakeSender

pytestmark = pytest.mark.skipif(
    os.environ.get("ROPS_RUN_NATIVE_GOAL_PROTOCOL") != "1" or not shutil.which("codex"),
    reason="Set ROPS_RUN_NATIVE_GOAL_PROTOCOL=1 to run the isolated Codex binary test",
)


async def test_native_continuation_restart_pause_and_error(client, fake_codex, settings, tmp_path):
    branch = _materialized_branch(client, fake_codex)
    factory = client.app.state.rops.database.session_factory
    home, codex_home, workspace = (tmp_path / name for name in ("home", "codex", "work"))
    for directory in (home, codex_home, workspace):
        directory.mkdir()
    payloads = []
    event_methods = []
    terminal_events = []
    failures = []
    fail_provider = False

    async def http(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            fields = [
                line.split(":", 1) for line in header.decode().split("\r\n")[1:] if ":" in line
            ]
            length = int(next((v for k, v in fields if k.lower() == "content-length"), "0"))
            payloads.append(json.loads(await reader.readexactly(length)))
            number = len(payloads)
            await asyncio.sleep(0.3)
            if fail_provider:
                data = json.dumps(
                    {"error": {"message": "Synthetic provider failure", "type": "server_error"}}
                ).encode()
                status, content_type = b"500 Internal Server Error", b"application/json"
            else:
                item = {
                    "id": f"msg_{number}",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "output_text",
                            "text": f"Synthetic checkpoint {number}",
                            "annotations": [],
                        }
                    ],
                }
                response = {
                    "id": f"resp_{number}",
                    "object": "response",
                    "status": "completed",
                    "created_at": 1,
                    "model": "gpt-6-astra",
                    "output": [item],
                    "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
                }
                stream = [
                    {
                        "type": "response.created",
                        "response": {**response, "status": "in_progress", "output": []},
                    },
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**item, "status": "in_progress", "content": []},
                    },
                    {
                        "type": "response.output_text.delta",
                        "item_id": item["id"],
                        "output_index": 0,
                        "content_index": 0,
                        "delta": item["content"][0]["text"],
                    },
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": response},
                ]
                data = "".join(
                    f"event: {entry['type']}\ndata: {json.dumps(entry)}\n\n" for entry in stream
                ).encode()
                status, content_type = b"200 OK", b"text/event-stream"
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Type: "
                + content_type
                + b"\r\nConnection: close\r\nContent-Length: "
                + str(len(data)).encode()
                + b"\r\n\r\n"
                + data
            )
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:
            failures.append(str(exc))
        finally:
            writer.close()

    server = await asyncio.start_server(http, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    overrides = {
        "model_provider": "probe",
        "model": "gpt-6-astra",
        "model_reasoning_effort": "low",
        "sqlite_home": str(codex_home),
        "features.goals": True,
        "model_providers.probe.name": "Local protocol test",
        "model_providers.probe.base_url": f"http://127.0.0.1:{port}/v1",
        "model_providers.probe.wire_api": "responses",
        "model_providers.probe.requires_openai_auth": False,
        "model_providers.probe.supports_websockets": False,
        "model_providers.probe.request_max_retries": 0,
        "model_providers.probe.stream_max_retries": 0,
    }
    args = [
        part for key, value in overrides.items() for part in ("-c", f"{key}={json.dumps(value)}")
    ]
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    execution = {
        "identity": "api",
        "model": "gpt-6-astra",
        "provider": "probe",
        "reasoning_effort": "low",
    }
    launch = SimpleNamespace(
        args=lambda: args, environment=lambda: environment, execution=execution
    )
    configured = settings.model_copy(
        update={
            "codex_command": shutil.which("codex"),
            "codex_home": codex_home,
            "codex_enabled": True,
            "codex_model": "gpt-6-astra",
            "codex_sandbox": "read-only",
            "codex_approval_policy": "never",
            "codex_request_timeout_seconds": 30,
            "infoflow_notifications_enabled": True,
        }
    )
    service = None
    adapter = None

    async def event(method, params):
        event_methods.append(method)
        if method in {"error", "turn/completed", "thread/goal/updated"}:
            terminal_events.append((method, params))
        if service:
            await service.observe_event(branch["id"], method, params)

    async def request(request_id, method, params):
        if method == "currentTime/read":
            import time

            await adapter.respond(request_id, {"currentTimeAt": int(time.time())})

    def new_runtime():
        runtime = CliCodexAdapter(
            configured, event_handler=event, request_handler=request, launch=launch
        )
        return runtime, NativeGoalService(factory, configured, runtime)

    async def wait_until(predicate):
        async with asyncio.timeout(25):
            while not predicate():
                await asyncio.sleep(0.1)

    try:
        adapter, service = new_runtime()
        thread = await adapter.lifecycle.start_thread(cwd=str(workspace), instructions="")
        thread_id = thread["thread"]["id"]
        with factory() as session:
            stored = session.get(models.ExecutionBranch, branch["id"])
            stored.codex_thread_id = thread_id
            stored.cwd = str(workspace)
            stored.native_goal_execution = execution
            session.commit()
        objective = "Synthetic native Goal protocol verification."
        state = await service.set(branch["id"], schemas.NativeGoalSet(objective=objective))
        assert state.goal.objective == objective
        await wait_until(lambda: len(payloads) >= 3)
        await wait_until(
            lambda: "turn/started" in event_methods and "item/completed" in event_methods
        )
        await adapter.stop()
        previous = len(payloads)
        adapter, service = new_runtime()
        result = await service.recover_active()
        assert result["recovered"] == 1
        await wait_until(lambda: len(payloads) > previous)
        await service.control(branch["id"], schemas.NativeGoalControl(action="pause"))
        await asyncio.sleep(0.7)
        paused_requests = len(payloads)
        assert (await service.monitor_once())["recovered"] == 0
        await asyncio.sleep(0.7)
        assert len(payloads) == paused_requests
        fail_provider = True
        await service.control(branch["id"], schemas.NativeGoalControl(action="resume"))
        async with asyncio.timeout(25):
            while (await adapter.get_goal(thread_id=thread_id))["goal"]["status"] != "blocked":
                await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)
        runtime = (await service.get(branch["id"])).runtime
        assert runtime.last_error
        sender = FakeSender()
        await asyncio.to_thread(
            TurnNotificationService(factory, configured, sender).dispatch_pending
        )
        assert any("目标等待重试" in text for _, text in sender.sent)
        recorded = await adapter.read_goal_turn(thread_id=thread_id)
        assert recorded["status"] == "failed"
        assert recorded["itemsView"] == "full"
        assert all(set(item) == {"id", "type", "status"} for item in recorded["items"])
        fail_provider = False
        previous = len(payloads)
        with factory() as session:
            row = session.get(models.NativeGoalMonitor, branch["id"])
            assert row.next_retry_at is not None
            row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
        assert (await service.monitor_once())["recovered"] == 1
        await wait_until(lambda: len(payloads) > previous)
        assert (await adapter.get_goal(thread_id=thread_id))["goal"]["objective"] == objective
        await service.control(branch["id"], schemas.NativeGoalControl(action="pause"))
        await asyncio.sleep(0.7)
        native_payloads = json.dumps(payloads, ensure_ascii=False)
        assert "research_ops_web_turn_limits" not in native_payloads
        assert "scheduled checkpoint from the persistent Research Ops" not in native_payloads
        for status in ("paused", "blocked", "complete"):
            await adapter.set_goal(thread_id=thread_id, status=status)
            await asyncio.gather(*adapter._goal_release_tasks)
            assert await adapter.lifecycle.owns_thread(thread_id=thread_id)
            previous = len(payloads)
            result = await adapter.send_message(
                thread_id=thread_id,
                content=f"New user direction after Goal {status}",
                cwd=str(workspace),
                metadata={"research_ops_branch_id": branch["id"]},
            )
            assert result["thread"]["id"] == thread_id
            assert len(payloads) > previous
            assert any(
                method == "turn/completed"
                and params.get("turn", {}).get("id") == result["turn"]["id"]
                and params["turn"]["status"] == "completed"
                for method, params in terminal_events
            )
            assert (await adapter.get_goal(thread_id=thread_id))["goal"]["objective"] == objective
        native_payloads = json.dumps(payloads, ensure_ascii=False)
        markers = (
            "research_ops_web_turn_limits", "research_ops_policy", "research_ops_handoff_policy",
            "research_ops_brief", "Research Ops Program Desk coordinator",
            "You are executing one Research Ops branch",
        )
        for marker in markers:
            assert marker not in native_payloads
        assert not failures
        await adapter.stop()
        rollout_paths = sorted((codex_home / "sessions").rglob("*.jsonl"))
        assert rollout_paths
        records = [
            json.loads(line) for path in rollout_paths for line in path.read_text().splitlines()
        ]
        for marker in markers:
            assert marker not in json.dumps(records, ensure_ascii=False)
        (tmp_path / "wire-requests.json").write_text(
            json.dumps(payloads, ensure_ascii=False, indent=2)
        )
        (tmp_path / "audit.json").write_text(json.dumps({
            "scenario": "native_goal_continue_restart_pause_error_recover_and_query",
            "verdict": "no_new_platform_injection", "wire_request_count": len(payloads),
            "jsonl_record_count": len(records), "goal_objective_is_exact": True,
            "platform_marker_hits": [], "rollout_paths": [str(path) for path in rollout_paths],
        }, indent=2) + "\n")
    except Exception:
        if adapter:
            print("Isolated Codex stderr:", adapter.lifecycle._stderr_tail[-8:])
        print("Isolated event methods:", event_methods)
        print("Isolated terminal events:", terminal_events[-5:])
        raise
    finally:
        if adapter:
            await adapter.stop()
        server.close()
        await server.wait_closed()

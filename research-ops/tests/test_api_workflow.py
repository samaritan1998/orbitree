from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from research_ops import models
from research_ops.services.briefs import developer_instructions
from research_ops.settings import Settings
from sqlalchemy import select


def create_work(client: TestClient, program: str = "HARN") -> tuple[dict, dict]:
    response = client.post(
        "/api/work-items",
        json={
            "program_code": program,
            "title": "Verifier experiment",
            "objective": "Build and validate one deterministic verifier.",
            "priority": 80,
            "branch_mode": "write",
            "acceptance_criteria": [{"description": "All fixture cases pass", "kind": "manual"}],
        },
    )
    assert response.status_code == 200, response.text
    work = response.json()
    tree = client.get(f"/api/programs/{program}/tree").json()
    branch = next(item for item in tree["branches"] if item["work_item_id"] == work["id"])
    return work, branch


def test_pi_continuation_objective_falls_back_for_scratch_work(client: TestClient) -> None:
    service = client.app.state.rops.service
    parent = SimpleNamespace(
        name="Main",
        work_item=SimpleNamespace(objective="", title=""),
    )
    handoff = {
        "completed_turns": [
            {"prompt": "验证在线 MOPD 的 rollout 轨迹并记录失败原因"},
        ],
        "latest_handoff": {},
    }

    objective = service._pi_continuation_objective(
        parent=parent,
        handoff=handoff,
        requested=None,
    )

    assert objective.startswith(
        "Continue the current work described by the latest completed request:"
    )
    assert "在线 MOPD" in objective


def test_catalog_and_today_are_available(client: TestClient) -> None:
    programs = client.get("/api/programs")
    assert programs.status_code == 200
    assert [item["code"] for item in programs.json()] == [
        "HARN",
        "COST",
        "ADAPT",
        "COMMON",
        "FREE",
    ]
    today = client.get("/api/today")
    assert today.status_code == 200
    assert today.json()["active_sessions"] == 0


def test_new_work_can_select_pi_runtime_without_changing_the_global_default(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/work-items",
        json={
            "program_code": "HARN",
            "title": "Pi-native experiment",
            "objective": "Run this WorkItem with a Web-managed Pi session.",
            "priority": 60,
            "branch_mode": "write",
            "runtime": "pi",
        },
    )

    assert response.status_code == 200, response.text
    work = response.json()
    tree = client.get("/api/programs/HARN/tree").json()
    branch = next(item for item in tree["branches"] if item["work_item_id"] == work["id"])
    assert branch["runtime"] == "pi"
    assert branch["codex_thread_id"] is None

    detail = client.get(f"/api/branches/{branch['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["runtime"] == "pi"
    assert client.get(f"/api/branches/{branch['id']}/terminal-command").status_code == 409

    _default_work, default_branch = create_work(client)
    assert default_branch["runtime"] == "codex"


def test_new_work_rejects_an_unknown_runtime(client: TestClient) -> None:
    response = client.post(
        "/api/work-items",
        json={
            "program_code": "HARN",
            "title": "Unknown runtime",
            "objective": "This request should be rejected.",
            "branch_mode": "write",
            "runtime": "other",
        },
    )

    assert response.status_code == 422


def test_recover_codex_thread_preserves_branch_and_historical_thread(client: TestClient) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    now = datetime.now(UTC)
    old_thread_id = "019f-recovery-old-thread"
    with state.database.session_factory() as session:
        stored_branch = session.get(models.ExecutionBranch, branch["id"])
        assert stored_branch is not None
        stored_branch.codex_thread_id = old_thread_id
        attempt = models.SessionAttempt(
            branch_id=stored_branch.id,
            codex_thread_id=old_thread_id,
            status="error",
            lease_owner="web",
            lease_expires_at=now - timedelta(minutes=1),
            heartbeat_at=now - timedelta(minutes=1),
            work_item_revision=stored_branch.work_item.revision,
            brief={},
            started_at=now - timedelta(minutes=5),
            completed_at=now - timedelta(minutes=1),
        )
        session.add(attempt)
        session.flush()
        binding = models.CodexThreadBinding(
            branch_id=stored_branch.id,
            initial_session_id=attempt.id,
            thread_id=old_thread_id,
            source="test",
            bound_at=now - timedelta(minutes=5),
        )
        session.add(binding)
        session.flush()
        session.add(
            models.CodexTurn(
                branch_id=stored_branch.id,
                session_id=attempt.id,
                thread_binding_id=binding.id,
                thread_id=old_thread_id,
                external_turn_id="old-completed-turn",
                status="completed",
                prompt="Inspect the previous deployment.",
                final_answer="The prior deployment was healthy; verify before resubmitting.",
                completed_at=now - timedelta(minutes=2),
            )
        )
        session.add(
            models.ConversationMessage(
                branch_id=stored_branch.id,
                session_id=attempt.id,
                turn_id="failed-turn",
                role="user",
                content="Continue the evaluation.",
                metadata_json={
                    "origin": "web",
                    "send_status": "failed",
                    "error": "Request blocked: stream disconnected",
                },
            )
        )
        session.commit()

    response = client.post(f"/api/branches/{branch['id']}/recover-codex-thread")
    assert response.status_code == 200, response.text
    recovered = response.json()
    assert recovered["id"] == branch["id"]
    assert recovered["cwd"] == branch["cwd"]
    assert recovered["codex_thread_id"].startswith("deferred:")
    handoff = recovered["context_snapshot"]["thread_recovery"]
    assert handoff["prior_thread_id"] == old_thread_id
    assert handoff["latest_completed_turns"][0]["turn_id"] == "old-completed-turn"

    with state.database.session_factory() as session:
        old_binding = session.get(models.CodexThreadBinding, binding.id)
        assert old_binding is not None
        assert old_binding.status == "historical"
        assert old_binding.retired_at is not None


def test_recover_codex_thread_rejects_an_active_session(client: TestClient) -> None:
    _work, branch = create_work(client)
    started = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"})
    assert started.status_code == 200, started.text
    response = client.post(f"/api/branches/{branch['id']}/recover-codex-thread")
    assert response.status_code == 409
    assert "active session" in response.json()["detail"].casefold()


def test_new_cli_thread_event_binds_a_deferred_web_session(client: TestClient) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    now = datetime.now(UTC)
    placeholder = "deferred:recovery-placeholder"
    with state.database.session_factory() as session:
        stored_branch = session.get(models.ExecutionBranch, branch["id"])
        assert stored_branch is not None
        stored_branch.codex_thread_id = placeholder
        attempt = models.SessionAttempt(
            branch_id=stored_branch.id,
            codex_thread_id=placeholder,
            status="active",
            lease_owner="web",
            lease_expires_at=now + timedelta(minutes=30),
            heartbeat_at=now,
            work_item_revision=stored_branch.work_item.revision,
            brief={},
            started_at=now,
        )
        session.add(attempt)
        session.flush()
        session.add(
            models.ConversationMessage(
                branch_id=stored_branch.id,
                session_id=attempt.id,
                turn_id="fresh-turn",
                role="user",
                content="Continue from the recovery record.",
                metadata_json={"origin": "web", "send_status": "pending"},
            )
        )
        session.commit()
        attempt_id = attempt.id

    asyncio.run(
        state.codex_events.handle_event(
            "thread/started",
            {
                "_researchOpsBranchId": branch["id"],
                "threadId": "019f-fresh-thread",
                "thread": {"id": "019f-fresh-thread"},
            },
        )
    )

    with state.database.session_factory() as session:
        stored_branch = session.get(models.ExecutionBranch, branch["id"])
        stored_attempt = session.get(models.SessionAttempt, attempt_id)
        binding = session.scalar(
            select(models.CodexThreadBinding).where(
                models.CodexThreadBinding.thread_id == "019f-fresh-thread"
            )
        )
        assert stored_branch is not None and stored_branch.codex_thread_id == "019f-fresh-thread"
        assert stored_attempt is not None and stored_attempt.codex_thread_id == "019f-fresh-thread"
        assert binding is not None and binding.status == "current"


def test_pi_runtime_control_commands_are_explicit_and_traceable(client: TestClient) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    state.settings.codex_backend = "pi"
    attempt = client.post(
        f"/api/branches/{branch['id']}/sessions", json={"owner": "test:pi"}
    ).json()
    state.codex_results.bind_thread(
        branch_id=branch["id"],
        session_id=attempt["id"],
        thread_id="pi-runtime-thread",
        bound_at=datetime.now(UTC),
        source="pi",
    )
    calls: list[tuple[str, str | None]] = []

    async def steer_message(*, thread_id: str, content: str, cwd: str) -> dict:
        calls.append((f"steer:{thread_id}:{cwd}", content))
        return {"success": True}

    async def follow_up(*, thread_id: str, content: str, cwd: str) -> dict:
        calls.append((f"follow_up:{thread_id}:{cwd}", content))
        return {"success": True}

    async def abort(*, thread_id: str, cwd: str) -> dict:
        calls.append((f"abort:{thread_id}:{cwd}", None))
        return {"success": True}

    state.codex.steer_message = steer_message
    state.codex.follow_up = follow_up
    state.codex.abort = abort

    runtime = client.get(f"/api/branches/{branch['id']}/runtime-state")
    assert runtime.status_code == 200, runtime.text
    assert runtime.json()["runtime"] == "pi"
    assert runtime.json()["capabilities"] == ["prompt", "steer", "follow_up", "abort"]

    steered = client.post(
        f"/api/branches/{branch['id']}/runtime/steer",
        json={"content": "先停止当前检查，改看失败日志"},
    )
    assert steered.status_code == 200, steered.text
    assert steered.json()["runtime"] == "pi"
    assert steered.json()["command"] == "steer"

    queued = client.post(
        f"/api/branches/{branch['id']}/runtime/follow-up",
        json={"content": "完成后补一份结论"},
    )
    assert queued.status_code == 200, queued.text
    aborted = client.post(f"/api/branches/{branch['id']}/runtime/abort")
    assert aborted.status_code == 200, aborted.text
    assert [item[0].split(":", 1)[0] for item in calls] == [
        "steer",
        "follow_up",
        "abort",
    ]

    detail = client.get(f"/api/branches/{branch['id']}").json()
    controls = [
        message
        for message in detail["messages"]
        if message["kind"] == "runtime_control"
    ]
    assert [message["metadata_json"]["delivery"] for message in controls] == [
        "steer",
        "follow_up",
    ]


def test_codex_branch_continues_in_a_distinct_pi_child_with_frozen_handoff(
    client: TestClient, fake_codex
) -> None:
    _work, parent = create_work(client)
    parent_session = client.post(
        f"/api/branches/{parent['id']}/sessions", json={"owner": "test"}
    ).json()
    state = client.app.state.rops
    captured: dict[str, str] = {}

    async def start_pi_thread(*, cwd: str, instructions: str) -> dict:
        captured["cwd"] = cwd
        captured["instructions"] = instructions
        return {"thread": {"id": "pi-child-session"}}

    state.service.pi = fake_codex
    fake_codex.start_thread = start_pi_thread
    now = datetime.now(UTC)
    with state.database.session_factory() as session:
        binding = models.CodexThreadBinding(
            branch_id=parent["id"],
            initial_session_id=parent_session["id"],
            thread_id=parent_session["codex_thread_id"],
            source="terminal",
            bound_at=now,
        )
        session.add(binding)
        session.flush()
        session.add(
            models.CodexTurn(
                branch_id=parent["id"],
                session_id=parent_session["id"],
                thread_binding_id=binding.id,
                thread_id=binding.thread_id,
                external_turn_id="codex-turn-1",
                prompt="Inspect the current evaluation failure.",
                final_answer="The checkpoint is complete; continue with the rollout diagnosis.",
                completed_at=now,
            )
        )
        attempt = session.get(models.SessionAttempt, parent_session["id"])
        assert attempt is not None
        attempt.status = "completed"
        attempt.completed_at = now
        attempt.handoff = {
            "outcome": "continue",
            "conclusion": "Use the completed checkpoint.",
            "next_action": "Inspect rollout logs.",
        }
        session.commit()

    response = client.post(
        f"/api/branches/{parent['id']}/continue-with-pi",
        json={"manual_approval": True},
    )

    assert response.status_code == 200, response.text
    child = response.json()
    assert child["runtime"] == "pi"
    assert child["parent_branch_id"] == parent["id"]
    assert child["cwd"] != parent["cwd"]
    assert child["codex_thread_id"] == "pi-child-session"
    handoff = child["context_snapshot"]["runtime_handoff"]
    assert handoff["kind"] == "codex_to_pi"
    assert handoff["source"]["thread_id"] == parent_session["codex_thread_id"]
    assert handoff["source"]["workspace_path"] == parent["cwd"]
    assert handoff["source"]["rollout_path"] is None
    assert handoff["completed_turns"][0]["turn_id"] == "codex-turn-1"
    assert handoff["handoff_hash"]
    assert captured["instructions"] == ""

    parent_detail = client.get(f"/api/branches/{parent['id']}").json()
    child_detail = client.get(f"/api/branches/{child['id']}").json()
    assert child["name"] == "Pi · Verifier experiment"
    assert parent_detail["branch"]["status"] == "active"
    assert parent_detail["branch"]["continued_by_branch_id"] is None
    assert parent_detail["conversation_state"]["can_send"] is True
    assert child_detail["runtime"] == "pi"
    local_command = client.get(f"/api/branches/{child['id']}/terminal-command")
    assert local_command.status_code == 409


def test_codex_branch_cannot_continue_with_pi_while_its_session_is_active(
    client: TestClient,
) -> None:
    _work, parent = create_work(client)
    client.post(f"/api/branches/{parent['id']}/sessions", json={"owner": "test"})

    response = client.post(
        f"/api/branches/{parent['id']}/continue-with-pi",
        json={"manual_approval": True},
    )

    assert response.status_code == 409
    assert "Finish the active Codex session" in response.json()["detail"]


def test_codex_branch_continues_in_parallel_claude_child(
    client: TestClient, fake_codex
) -> None:
    _work, parent = create_work(client)
    state = client.app.state.rops
    state.service.claude = fake_codex
    state.claude = fake_codex
    now = datetime.now(UTC)
    with state.database.session_factory() as session:
        stored_parent = session.get(models.ExecutionBranch, parent["id"])
        assert stored_parent is not None
        stored_parent.codex_thread_id = "codex-parent-session"
        attempt = models.SessionAttempt(
            branch_id=stored_parent.id,
            codex_thread_id=stored_parent.codex_thread_id,
            runtime="codex",
            status="completed",
            lease_owner="web",
            lease_expires_at=now - timedelta(minutes=1),
            heartbeat_at=now - timedelta(minutes=1),
            work_item_revision=stored_parent.work_item.revision,
            brief={},
            started_at=now - timedelta(minutes=5),
            completed_at=now - timedelta(minutes=1),
        )
        session.add(attempt)
        session.flush()
        binding = models.CodexThreadBinding(
            branch_id=stored_parent.id,
            initial_session_id=attempt.id,
            thread_id=stored_parent.codex_thread_id,
            source="terminal",
            bound_at=now - timedelta(minutes=5),
        )
        session.add(binding)
        session.flush()
        session.add(
            models.CodexTurn(
                branch_id=stored_parent.id,
                session_id=attempt.id,
                thread_binding_id=binding.id,
                thread_id=binding.thread_id,
                external_turn_id="codex-turn-for-claude",
                status="completed",
                prompt="Continue this implementation with Claude.",
                final_answer="The Codex portion is complete.",
                completed_at=now - timedelta(minutes=2),
            )
        )
        session.commit()

    response = client.post(
        f"/api/branches/{parent['id']}/continue-with-claude",
        json={"manual_approval": True},
    )

    assert response.status_code == 200, response.text
    child = response.json()
    assert child["runtime"] == "claude"
    assert child["parent_branch_id"] == parent["id"]
    assert child["name"] == "Claude · Verifier experiment"
    assert child["codex_thread_id"] in fake_codex.threads
    handoff = child["context_snapshot"]["runtime_handoff"]
    assert handoff["kind"] == "codex_to_claude"
    assert handoff["source"]["runtime"] == "codex"
    assert handoff["completed_turns"][0]["turn_id"] == "codex-turn-for-claude"


def test_new_work_accepts_claude_runtime(client: TestClient, fake_codex) -> None:
    state = client.app.state.rops
    state.service.claude = fake_codex
    response = client.post(
        "/api/work-items",
        json={
            "program_code": "FREE",
            "title": "Claude task",
            "objective": "Exercise the Claude runtime",
            "branch_mode": "write",
            "runtime": "claude",
            "priority": 50,
        },
    )
    assert response.status_code == 200, response.text
    work_id = response.json()["human_id"]
    tree = client.get("/api/programs/FREE/tree").json()
    branch = next(
        item
        for item in tree["branches"]
        if item["work_item_id"] == response.json()["id"]
    )
    assert work_id.startswith("FREE-")
    assert branch["runtime"] == "claude"


def test_pi_branch_continues_in_a_parallel_codex_child_with_frozen_handoff(
    client: TestClient, fake_codex
) -> None:
    _work, parent = create_work(client)
    state = client.app.state.rops
    captured: dict[str, str] = {}

    async def start_codex_thread(*, cwd: str, instructions: str) -> dict:
        captured["cwd"] = cwd
        captured["instructions"] = instructions
        return {"thread": {"id": "codex-child-session"}}

    fake_codex.start_thread = start_codex_thread
    now = datetime.now(UTC)
    with state.database.session_factory() as session:
        stored_parent = session.get(models.ExecutionBranch, parent["id"])
        assert stored_parent is not None
        stored_parent.runtime = "pi"
        stored_parent.codex_thread_id = "pi-parent-session"
        attempt = models.SessionAttempt(
            branch_id=stored_parent.id,
            codex_thread_id="pi-parent-session",
            runtime="pi",
            status="completed",
            lease_owner="web",
            lease_expires_at=now - timedelta(minutes=1),
            heartbeat_at=now - timedelta(minutes=1),
            work_item_revision=stored_parent.work_item.revision,
            brief={},
            handoff={"next_action": "Inspect the current rollout before resubmitting."},
            started_at=now - timedelta(minutes=5),
            completed_at=now - timedelta(minutes=1),
        )
        session.add(attempt)
        session.flush()
        binding = models.CodexThreadBinding(
            branch_id=stored_parent.id,
            initial_session_id=attempt.id,
            thread_id="pi-parent-session",
            source="pi",
            bound_at=now - timedelta(minutes=5),
        )
        session.add(binding)
        session.flush()
        session.add(
            models.CodexTurn(
                branch_id=stored_parent.id,
                session_id=attempt.id,
                thread_binding_id=binding.id,
                thread_id=binding.thread_id,
                external_turn_id="pi-turn-1",
                status="completed",
                prompt="Review the Pi rollout evidence and identify the next action.",
                final_answer="The Pi check is complete; use Codex to inspect the deployment state.",
                completed_at=now - timedelta(minutes=2),
                metadata_json={"runtime": "pi"},
            )
        )
        session.commit()

    response = client.post(
        f"/api/branches/{parent['id']}/continue-with-codex",
        json={"manual_approval": True},
    )

    assert response.status_code == 200, response.text
    child = response.json()
    assert child["runtime"] == "codex"
    assert child["parent_branch_id"] == parent["id"]
    assert child["cwd"] != parent["cwd"]
    assert child["name"] == "Codex · Verifier experiment"
    assert child["codex_thread_id"] == "codex-child-session"
    handoff = child["context_snapshot"]["runtime_handoff"]
    assert handoff["kind"] == "pi_to_codex"
    assert handoff["source"]["runtime"] == "pi"
    assert handoff["source"]["thread_id"] == "pi-parent-session"
    assert handoff["source"]["workspace_path"] == parent["cwd"]
    assert handoff["completed_turns"][0]["turn_id"] == "pi-turn-1"
    assert handoff["upstream_handoff"] == {}
    assert handoff["handoff_hash"]
    assert captured["instructions"] == ""

    parent_detail = client.get(f"/api/branches/{parent['id']}").json()
    child_detail = client.get(f"/api/branches/{child['id']}").json()
    assert parent_detail["branch"]["status"] == "active"
    assert parent_detail["branch"]["continued_by_branch_id"] is None
    assert parent_detail["conversation_state"]["can_send"] is True
    assert child_detail["runtime"] == "codex"
    assert client.get(f"/api/branches/{child['id']}/terminal-command").status_code == 200


def test_program_desk_is_hidden_but_managed(
    client: TestClient, settings: Settings
) -> None:
    desk_response = client.get("/api/programs/HARN/desk")
    assert desk_response.status_code == 200, desk_response.text
    desk = desk_response.json()
    assert desk["work_item"]["kind"] == "program_desk"
    assert desk["work_item"]["human_id"] == "HARN-DESK"
    assert Path(desk["branch"]["cwd"]) == settings.workspace_root / "HARN" / "_desk"

    work, _branch = create_work(client)
    tree = client.get("/api/programs/HARN/tree").json()
    assert [item["human_id"] for item in tree["work_items"]] == [work["human_id"]]
    assert all(item["work_item_id"] != desk["work_item"]["id"] for item in tree["branches"])
    suggested = client.get("/api/today").json()["suggested_next"]
    assert all(item["human_id"] != "HARN-DESK" for item in suggested)

    terminal = client.get(
        f"/api/branches/{desk['branch']['id']}/terminal-command"
    ).json()
    assert "rops' desk 'HARN'" in terminal["command"]

    forked = client.post(
        f"/api/branches/{desk['branch']['id']}/fork",
        json={
            "name": "invalid",
            "objective": "Desk must not fork",
            "mode": "write",
            "manual_approval": True,
        },
    )
    assert forked.status_code == 409
    assert "create a WorkItem" in forked.json()["detail"]

    started = client.post(
        f"/api/branches/{desk['branch']['id']}/sessions", json={"owner": "test"}
    )
    assert started.status_code == 200, started.text
    brief = started.json()["brief"]
    assert brief["program_desk"] is True
    assert brief["program_context"]["work_items"][0]["work_id"] == work["human_id"]
    instructions = developer_instructions(brief)
    assert instructions == ""

    context = client.get("/api/programs/HARN/desk-context").json()
    assert context["work_items"][0]["title"] == work["title"]


def test_work_can_start_undefined_and_codex_can_define_it_later(
    client: TestClient,
) -> None:
    accidental = client.post("/api/work-items", json={"program_code": "FREE"})
    assert accidental.status_code == 422

    created = client.post(
        "/api/work-items",
        json={"program_code": "FREE", "definition_mode": "emerging"},
    )
    assert created.status_code == 200, created.text
    work = created.json()
    assert work["title"] == f"未命名工作 {work['human_id']}"
    assert work["objective"] == ""
    assert "needs_definition" in work["labels"]

    tree = client.get("/api/programs/FREE/tree").json()
    branch = next(item for item in tree["branches"] if item["work_item_id"] == work["id"])
    started = client.post(
        f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}
    )
    assert started.status_code == 200, started.text
    brief = started.json()["brief"]
    assert brief["definition_pending"] is True
    instructions = developer_instructions(brief)
    assert instructions == ""

    defined = client.post(
        f"/api/work-items/{work['human_id']}/definition",
        json={
            "title": "验证一个自由探索想法",
            "objective": "先验证核心假设，再决定是否转入正式方向。",
        },
    )
    assert defined.status_code == 200, defined.text
    updated = defined.json()
    assert updated["title"] == "验证一个自由探索想法"
    assert updated["objective"] == "先验证核心假设，再决定是否转入正式方向。"
    assert "needs_definition" not in updated["labels"]
    assert updated["revision"] == work["revision"] + 1

    repeated = client.post(
        f"/api/work-items/{work['human_id']}/definition",
        json={"title": "不应覆盖", "objective": "不应覆盖已有定义。"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["title"] == updated["title"]
    assert repeated.json()["revision"] == updated["revision"]


def test_fork_is_optional_and_current_branch_remains_the_default(
    client: TestClient,
) -> None:
    work, branch = create_work(client)
    started = client.post(
        f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}
    )
    assert started.status_code == 200, started.text

    instructions = developer_instructions(started.json()["brief"])

    assert instructions == ""


def test_scratch_work_can_be_soft_hidden_without_removing_its_workspace(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/work-items",
        json={"program_code": "FREE", "definition_mode": "emerging"},
    )
    assert created.status_code == 200, created.text
    work = created.json()
    tree = client.get("/api/programs/FREE/tree").json()
    branch = next(item for item in tree["branches"] if item["work_item_id"] == work["id"])
    workspace = Path(branch["cwd"])
    assert workspace.is_dir()

    hidden = client.delete(f"/api/work-items/{work['id']}")
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["hidden"] is True
    assert hidden.json()["lifecycle"] == "cancelled"
    assert workspace.is_dir()
    assert work["human_id"] not in {
        item["human_id"] for item in client.get("/api/programs/FREE/tree").json()["work_items"]
    }
    today = client.get("/api/today").json()
    visible_today_ids = {
        item["human_id"]
        for key in ("continue_items", "needs_decision", "suggested_next")
        for item in today[key]
    }
    assert work["human_id"] not in visible_today_ids

    repeated = client.delete(f"/api/work-items/{work['id']}")
    assert repeated.status_code == 200
    assert repeated.json()["revision"] == hidden.json()["revision"]


def test_active_work_cannot_be_hidden(client: TestClient) -> None:
    work, branch = create_work(client, "FREE")
    started = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"})
    assert started.status_code == 200, started.text

    hidden = client.delete(f"/api/work-items/{work['id']}")

    assert hidden.status_code == 409
    assert "Exit active local Sessions" in hidden.json()["detail"]


def test_web_turn_is_submitted_and_terminal_entry_is_managed(client: TestClient) -> None:
    work, branch = create_work(client)
    started = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"})
    assert started.status_code == 200, started.text
    session = started.json()
    assert session["status"] == "active"
    assert session["codex_thread_id"]
    assert session["lease_expires_at"].endswith(("Z", "+00:00"))
    assert "skill_refs" not in session["brief"]

    message = client.post(
        f"/api/branches/{branch['id']}/messages",
        json={"content": "Inspect the verifier fixtures."},
    )
    assert message.status_code == 200, message.text
    assert message.json()["metadata_json"]["origin"] == "web"
    assert message.json()["metadata_json"]["send_status"] in {"pending", "submitted"}

    detail = client.get(f"/api/branches/{branch['id']}").json()
    assert detail["work_item"]["human_id"] == work["human_id"]
    assert len(detail["messages"]) == 1
    assert detail["messages"][0]["content"] == "Inspect the verifier fixtures."
    assert detail["turns"] == []
    assert detail["conversation_state"]["web_turn_status"] in {"running", "completed"}
    terminal = client.get(f"/api/branches/{branch['id']}/terminal-command").json()
    assert "rops' open" in terminal["command"]
    assert branch["id"] in terminal["command"]
    assert terminal["codex_command"] == terminal["command"]
    assert terminal["thread_id"] == session["codex_thread_id"]


def test_diagnostic_codex_turns_do_not_pollute_results_or_program_context(
    client: TestClient,
) -> None:
    work, branch = create_work(client)
    state = client.app.state.rops
    now = datetime.now(UTC)
    with state.database.session_factory() as session:
        binding = models.CodexThreadBinding(
            branch_id=branch["id"],
            thread_id="thread-with-diagnostic-turn",
            source="test",
            bound_at=now,
        )
        session.add(binding)
        session.flush()
        session.add_all(
            [
                models.CodexTurn(
                    branch_id=branch["id"],
                    thread_binding_id=binding.id,
                    thread_id=binding.thread_id,
                    external_turn_id="visible-turn",
                    final_answer="Useful experiment result",
                    completed_at=now,
                ),
                models.CodexTurn(
                    branch_id=branch["id"],
                    thread_binding_id=binding.id,
                    thread_id=binding.thread_id,
                    external_turn_id="diagnostic-turn",
                    final_answer="COMPACT_OK",
                    completed_at=now + timedelta(seconds=1),
                    metadata_json={"hidden": True, "reason": "diagnostic"},
                ),
            ]
        )
        session.commit()

    detail = client.get(f"/api/branches/{branch['id']}").json()
    assert [turn["external_turn_id"] for turn in detail["turns"]] == ["visible-turn"]

    context = client.get("/api/programs/HARN/desk-context").json()
    item = next(entry for entry in context["work_items"] if entry["work_id"] == work["human_id"])
    branch_context = next(
        entry for entry in item["branches"] if entry["branch_id"] == branch["id"]
    )
    assert branch_context["latest_result"] == "Useful experiment result"


def test_terminal_command_ignores_a_deferred_thread_placeholder(client: TestClient) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    with state.database.session_factory() as database_session:
        stored = database_session.get(models.ExecutionBranch, branch["id"])
        assert stored
        stored.codex_thread_id = "deferred:test"
        database_session.commit()

    terminal = client.get(f"/api/branches/{branch['id']}/terminal-command").json()

    assert "rops' open" in terminal["command"]
    assert terminal["codex_command"] == terminal["command"]
    assert branch["id"] in terminal["codex_command"]
    assert "deferred:" not in terminal["codex_command"]
    assert "resume" not in terminal["codex_command"]
    assert terminal["thread_id"] == ""


def test_pi_runtime_does_not_offer_a_codex_local_command(client: TestClient) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    state.settings.codex_backend = "pi"

    terminal = client.get(f"/api/branches/{branch['id']}/terminal-command")

    assert terminal.status_code == 409
    assert "Managed local Pi takeover is not available" in terminal.json()["detail"]


def test_codex_item_ids_are_idempotent_per_turn(client: TestClient) -> None:
    _work, branch = create_work(client)
    client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"})
    event_service = client.app.state.rops.codex_events

    async def emit(turn_id: str, text: str) -> None:
        await event_service.handle_event(
            "item/completed",
            {
                "_researchOpsBranchId": branch["id"],
                "turnId": turn_id,
                "item": {
                    "id": "item_0",
                    "type": "agentMessage",
                    "text": text,
                },
            },
        )

    asyncio.run(emit("turn-1", "First turn"))
    asyncio.run(emit("turn-1", "Duplicate replay"))
    asyncio.run(emit("turn-2", "Second turn"))

    messages = client.get(f"/api/branches/{branch['id']}").json()["messages"]
    assistant_messages = [item for item in messages if item["role"] == "assistant"]
    assert [item["content"] for item in assistant_messages] == ["First turn", "Second turn"]


def test_codex_turn_start_links_the_pending_web_query(client: TestClient) -> None:
    _work, branch = create_work(client)
    attempt = client.post(
        f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}
    ).json()
    state = client.app.state.rops
    with state.database.session_factory() as session:
        message = models.ConversationMessage(
            branch_id=branch["id"],
            session_id=attempt["id"],
            role="user",
            content="Summarize the current experiment",
            metadata_json={"send_status": "pending", "origin": "web"},
        )
        session.add(message)
        session.commit()
        message_id = message.id

    asyncio.run(
        state.codex_events.handle_event(
            "turn/started",
            {
                "_researchOpsBranchId": branch["id"],
                "turn": {"id": "turn-web-pending", "status": "inProgress"},
            },
        )
    )

    with state.database.session_factory() as session:
        linked = session.get(models.ConversationMessage, message_id)
        assert linked
        assert linked.turn_id == "turn-web-pending"


def test_branch_result_file_serves_only_referenced_images(client: TestClient) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    branch_root = Path(branch["cwd"])
    branch_root.mkdir(parents=True, exist_ok=True)
    image_path = branch_root / "loss.png"
    image_bytes = b"\x89PNG\r\n\x1a\nfixture"
    image_path.write_bytes(image_bytes)
    hidden_path = branch_root / "hidden.png"
    hidden_path.write_bytes(image_bytes)
    with state.database.session_factory() as session:
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                role="assistant",
                content=f"![Loss curve]({image_path})",
            )
        )
        session.commit()

    response = client.get(
        f"/api/branches/{branch['id']}/result-file",
        params={"path": str(image_path)},
    )
    assert response.status_code == 200, response.text
    assert response.content == image_bytes
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["x-content-type-options"] == "nosniff"

    unreferenced = client.get(
        f"/api/branches/{branch['id']}/result-file",
        params={"path": str(hidden_path)},
    )
    assert unreferenced.status_code == 404


def test_branch_result_file_serves_referenced_svg_in_a_sandbox(client: TestClient) -> None:
    _work, branch = create_work(client)
    state = client.app.state.rops
    branch_root = Path(branch["cwd"])
    branch_root.mkdir(parents=True, exist_ok=True)
    image_path = branch_root / "loss.svg"
    image_bytes = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" />'
    image_path.write_bytes(image_bytes)
    with state.database.session_factory() as session:
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                role="assistant",
                content=f"![Loss curve]({image_path})",
            )
        )
        session.commit()

    response = client.get(
        f"/api/branches/{branch['id']}/result-file",
        params={"path": str(image_path)},
    )

    assert response.status_code == 200, response.text
    assert response.content == image_bytes
    assert response.headers["content-type"] == "image/svg+xml"
    assert "sandbox" in response.headers["content-security-policy"]


def test_handoff_rejects_an_active_terminal_session(client: TestClient) -> None:
    _work, branch = create_work(client)
    session = client.post(
        f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}
    ).json()
    response = client.post(
        f"/api/branches/{branch['id']}/sessions/{session['id']}/handoff",
        json={
            "lease_id": session["lease_id"],
            "outcome": "continue",
            "conclusion": "Pause here",
        },
    )

    assert response.status_code == 409
    assert "Exit the local Codex terminal" in response.json()["detail"]


def test_fork_gets_independent_workspace_and_merge_review(client: TestClient) -> None:
    _work, parent = create_work(client)
    client.post(f"/api/branches/{parent['id']}/sessions", json={"owner": "test"})
    child_response = client.post(
        f"/api/branches/{parent['id']}/fork",
        json={
            "name": "Alternative verifier",
            "objective": "Test a layout-aware verification path.",
            "mode": "write",
            "start_session": False,
            "manual_approval": True,
        },
    )
    assert child_response.status_code == 200, child_response.text
    child = child_response.json()
    assert child["parent_branch_id"] == parent["id"]
    assert child["cwd"] != parent["cwd"]
    assert child["codex_thread_id"] != parent["codex_thread_id"]

    result = client.post(
        f"/api/branches/{child['id']}/result",
        json={
            "outcome": "merge",
            "conclusion": "The alternative handles the fixture set.",
            "confidence": 0.8,
        },
    )
    assert result.status_code == 200, result.text
    queue = client.get("/api/merge-queue").json()
    assert len(queue) == 1
    assert queue[0]["source_branch_id"] == child["id"]

    resolved = client.post(
        f"/api/merge-queue/{queue[0]['id']}/resolve",
        json={"resolution": "merge", "selected_changes": {}},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resolution"] == "merge"


def test_fork_requires_explicit_manual_approval(client: TestClient) -> None:
    _work, parent = create_work(client)
    response = client.post(
        f"/api/branches/{parent['id']}/fork",
        json={
            "name": "Unapproved child",
            "objective": "This must not be created by an agent automatically.",
            "mode": "write",
        },
    )
    assert response.status_code == 409, response.text
    assert "Manual approval is required" in response.json()["detail"]


def test_fork_cannot_automatically_start_child_session(client: TestClient) -> None:
    _work, parent = create_work(client)
    response = client.post(
        f"/api/branches/{parent['id']}/fork",
        json={
            "name": "Auto-start child",
            "objective": "The child must remain idle until another user action.",
            "mode": "write",
            "manual_approval": True,
            "start_session": True,
        },
    )
    assert response.status_code == 409, response.text
    assert "cannot start the child automatically" in response.json()["detail"]


def test_branch_soft_hide_preserves_workspace_and_collapses_empty_lineage(
    client: TestClient,
) -> None:
    _work, parent = create_work(client)
    middle = client.post(
        f"/api/branches/{parent['id']}/fork",
        json={
            "name": "Empty intermediary",
            "objective": "Create an intermediary that can later be hidden.",
            "mode": "write",
            "continuation": "parallel",
            "manual_approval": True,
        },
    ).json()
    child = client.post(
        f"/api/branches/{middle['id']}/fork",
        json={
            "name": "Visible descendant",
            "objective": "Remain visible when the empty intermediary is hidden.",
            "mode": "write",
            "continuation": "parallel",
            "manual_approval": True,
        },
    ).json()
    workspace = Path(middle["cwd"])
    assert workspace.is_dir()

    hidden = client.post(f"/api/branches/{middle['id']}/hide")
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["hidden"] is True
    assert workspace.is_dir()

    tree = client.get("/api/programs/HARN/tree").json()
    assert middle["id"] not in {branch["id"] for branch in tree["branches"]}
    assert child["id"] in {branch["id"] for branch in tree["branches"]}
    assert middle["id"] in {branch["id"] for branch in tree["hidden_branches"]}
    detail = client.get(f"/api/branches/{middle['id']}")
    assert detail.status_code == 200
    assert detail.json()["branch"]["hidden"] is True
    assert detail.json()["branch"]["parent_branch_id"] == parent["id"]

    restored = client.post(f"/api/branches/{middle['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["hidden"] is False
    visible = client.get("/api/programs/HARN/tree").json()
    assert middle["id"] in {branch["id"] for branch in visible["branches"]}

    primary = client.post(f"/api/branches/{parent['id']}/hide")
    assert primary.status_code == 409
    assert "primary Branch" in primary.json()["detail"]


def test_failed_native_fork_does_not_silently_create_an_empty_thread(
    client: TestClient,
    fake_codex,
) -> None:
    _work, parent = create_work(client)
    parent_session = client.post(
        f"/api/branches/{parent['id']}/sessions", json={"owner": "test"}
    )
    assert parent_session.status_code == 200, parent_session.text

    async def fail_fork(*, thread_id: str, cwd: str, instructions: str):
        del thread_id, cwd, instructions
        raise RuntimeError("simulated thread/fork timeout")

    fake_codex.fork_thread = fail_fork
    child_response = client.post(
        f"/api/branches/{parent['id']}/fork",
        json={
            "name": "Focused child",
            "objective": "Continue only the child objective.",
            "mode": "write",
            "continuation": "parallel",
            "manual_approval": True,
        },
    )
    assert child_response.status_code == 200, child_response.text
    child = child_response.json()
    assert child["status"] == "blocked"
    assert child["codex_thread_id"] is None
    assert child["context_snapshot"]["provisioning"] == {
        "state": "failed",
        "strategy": None,
        "error": "simulated thread/fork timeout",
    }

    child_session = client.post(
        f"/api/branches/{child['id']}/sessions", json={"owner": "test"}
    )
    assert child_session.status_code == 409, child_session.text
    assert child["context_snapshot"]["fork_objective"] == "Continue only the child objective."


def test_continue_fork_pauses_parent_and_parent_can_be_reopened(
    client: TestClient,
) -> None:
    _work, parent = create_work(client)
    child_response = client.post(
        f"/api/branches/{parent['id']}/fork",
        json={
            "name": "Foreground child",
            "objective": "Move the foreground conversation here.",
            "mode": "write",
            "continuation": "continue",
            "manual_approval": True,
        },
    )
    assert child_response.status_code == 200, child_response.text
    child = child_response.json()

    parent_detail = client.get(f"/api/branches/{parent['id']}").json()
    assert parent_detail["branch"]["status"] == "continued"
    assert parent_detail["branch"]["continued_by_branch_id"] == child["id"]
    assert parent_detail["conversation_state"]["can_send"] is False
    assert "continues" in parent_detail["conversation_state"]["reason"]

    reopen = client.post(f"/api/branches/{parent['id']}/reopen")
    assert reopen.status_code == 200, reopen.text
    assert reopen.json()["status"] == "active"
    assert reopen.json()["continued_by_branch_id"] is None
    assert client.get(
        f"/api/branches/{parent['id']}/conversation-state"
    ).json()["can_send"] is True


def test_continue_fork_dispatches_the_first_child_turn(
    client: TestClient,
) -> None:
    _work, parent = create_work(client)
    child_response = client.post(
        f"/api/branches/{parent['id']}/fork",
        json={
            "name": "Auto child",
            "objective": "Execute the child objective immediately.",
            "mode": "write",
            "continuation": "continue",
            "manual_approval": True,
        },
    )
    assert child_response.status_code == 200, child_response.text
    child = child_response.json()
    kicked = client.post(
        f"/api/branches/{child['id']}/continue",
        json={},
    )
    assert kicked.status_code == 200, kicked.text

    for _ in range(60):
        detail = client.get(f"/api/branches/{child['id']}").json()
        continuation = detail["branch"]["context_snapshot"].get("auto_continuation")
        if continuation and continuation["status"] in {"completed", "failed"}:
            break
        time.sleep(0.05)

    assert continuation["status"] == "completed"
    user_messages = [message for message in detail["messages"] if message["role"] == "user"]
    assert len(user_messages) == 1
    assert user_messages[0]["metadata_json"]["origin"] == "continuation"
    assert detail["session"]["status"] == "completed"


def test_fork_defaults_to_parallel_and_keeps_parent_active(
    client: TestClient,
) -> None:
    _work, parent = create_work(client)
    child_response = client.post(
        f"/api/branches/{parent['id']}/fork",
        json={
            "name": "Parallel child",
            "objective": "Explore the child while retaining the parent conversation.",
            "mode": "write",
            "manual_approval": True,
        },
    )
    assert child_response.status_code == 200, child_response.text
    child = child_response.json()
    assert child["context_snapshot"]["continuation"] == "parallel"

    parent_detail = client.get(f"/api/branches/{parent['id']}").json()
    assert parent_detail["branch"]["status"] == "active"
    assert parent_detail["branch"]["continued_by_branch_id"] is None
    assert parent_detail["conversation_state"]["can_send"] is True


def test_failed_fork_can_be_repaired_without_recreating_the_branch(
    client: TestClient,
    fake_codex,
) -> None:
    _work, parent = create_work(client)
    started = client.post(
        f"/api/branches/{parent['id']}/sessions", json={"owner": "test"}
    )
    assert started.status_code == 200, started.text
    original_start_thread = fake_codex.start_thread
    original_fork_thread = fake_codex.fork_thread

    async def fail_fork(*, thread_id: str, cwd: str, instructions: str):
        del thread_id, cwd, instructions
        raise RuntimeError("fork unavailable")

    async def fail_start(*, cwd: str, instructions: str):
        del cwd, instructions
        raise RuntimeError("start unavailable")

    fake_codex.fork_thread = fail_fork
    fake_codex.start_thread = fail_start
    created = client.post(
        f"/api/branches/{parent['id']}/fork",
        json={
            "name": "Repairable child",
            "objective": "Recover this child without replacing its identity.",
            "mode": "write",
            "continuation": "parallel",
            "manual_approval": True,
        },
    )
    assert created.status_code == 200, created.text
    child = created.json()
    assert child["status"] == "blocked"
    assert child["codex_thread_id"] is None

    fake_codex.start_thread = original_start_thread
    fake_codex.fork_thread = original_fork_thread
    repaired = client.post(
        f"/api/branches/{child['id']}/repair-fork",
        json={"continuation": "continue", "manual_approval": True},
    )
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["id"] == child["id"]
    assert repaired.json()["status"] == "active"
    assert repaired.json()["codex_thread_id"]
    assert repaired.json()["context_snapshot"]["provisioning"]["state"] == "ready"


def test_mixed_unicode_fork_name_does_not_collide_with_ascii_suffix(
    client: TestClient,
) -> None:
    _work, parent = create_work(client)
    first = client.post(
        f"/api/branches/{parent['id']}/fork",
        json={
            "name": "MOPD",
            "objective": "Keep the existing offline MOPD path.",
            "mode": "write",
            "manual_approval": True,
        },
    )
    assert first.status_code == 200, first.text

    online = client.post(
        f"/api/branches/{parent['id']}/fork",
        json={
            "name": "在线MOPD",
            "objective": "Implement true online MOPD.",
            "mode": "write",
            "manual_approval": True,
        },
    )
    assert online.status_code == 200, online.text
    assert online.json()["name"] == "在线MOPD"
    assert online.json()["slug"].startswith("mopd-")
    assert online.json()["slug"] != first.json()["slug"]

    repeated = client.post(
        f"/api/branches/{parent['id']}/fork",
        json={
            "name": "在线mopd",
            "objective": "This is the same display name.",
            "mode": "write",
            "manual_approval": True,
        },
    )
    assert repeated.status_code == 409
    assert 'Branch already exists: "在线MOPD"' in repeated.json()["detail"]


def test_optimistic_revision_rejects_stale_update(client: TestClient) -> None:
    work, _branch = create_work(client, "COST")
    first = client.patch(
        f"/api/work-items/{work['id']}",
        json={"expected_revision": 1, "priority": 91},
    )
    assert first.status_code == 200
    stale = client.patch(
        f"/api/work-items/{work['id']}",
        json={"expected_revision": 1, "priority": 92},
    )
    assert stale.status_code == 409


def test_memory_requires_explicit_review(client: TestClient) -> None:
    work, branch = create_work(client)
    created = client.post(
        "/api/memory",
        json={
            "program_code": "HARN",
            "scope_type": "branch",
            "scope_id": branch["id"],
            "kind": "hypothesis",
            "title": "Layout evidence",
            "content": "Layout consistency may predict edit success.",
            "status": "proposed",
            "source_branch_id": branch["id"],
            "evidence_refs": [work["human_id"]],
        },
    )
    assert created.status_code == 200, created.text
    memory = created.json()
    reviewed = client.post(f"/api/memory/{memory['id']}/review", json={"status": "accepted"})
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "accepted"
    assert reviewed.json()["version"] == 2


def test_repository_registration_creates_real_worktree(client: TestClient, tmp_path: Path) -> None:
    source = tmp_path / "registered-source"
    source.mkdir()
    subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "research-ops@example.invalid"],
        cwd=source,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Research Ops Test"], cwd=source, check=True)
    (source / "README.md").write_text("registered\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=source, check=True)

    invalid = client.post(
        "/api/repositories",
        json={"name": "invalid", "canonical_path": str(tmp_path / "missing")},
    )
    assert invalid.status_code == 409

    registered = client.post(
        "/api/repositories",
        json={
            "name": "registered-source",
            "canonical_path": str(source),
            "default_ref": "HEAD",
            "program_codes": ["HARN"],
        },
    )
    assert registered.status_code == 200, registered.text
    response = client.post(
        "/api/work-items",
        json={
            "program_code": "HARN",
            "title": "Repository-backed task",
            "objective": "Run in an isolated real worktree.",
            "repository_id": registered.json()["id"],
        },
    )
    assert response.status_code == 200, response.text
    tree = client.get("/api/programs/HARN/tree").json()
    branch = next(
        item for item in tree["branches"] if item["work_item_id"] == response.json()["id"]
    )
    assert Path(branch["cwd"], "README.md").read_text(encoding="utf-8") == "registered\n"
    assert branch["git_branch"].startswith("rops/harn-")


def test_external_job_can_detach_and_complete(client: TestClient) -> None:
    work, branch = create_work(client, "ADAPT")
    created = client.post(
        "/api/external-jobs",
        json={
            "work_item_id": work["id"],
            "provider": "ltp",
            "remote_job_id": "cluster-c-123",
            "status": "running",
        },
    )
    assert created.status_code == 200, created.text
    assert client.get("/api/today").json()["waiting_external"][0]["remote_job_id"] == "cluster-c-123"
    running_detail = client.get(f"/api/branches/{branch['id']}").json()
    assert running_detail["work_item"]["flow"] == "running"
    updated = client.patch(
        f"/api/external-jobs/{created.json()['id']}", json={"status": "completed"}
    )
    assert updated.status_code == 200
    assert updated.json()["last_observed_at"]
    assert (
        client.get(f"/api/branches/{branch['id']}").json()["external_jobs"][0]["status"]
        == "completed"
    )
    completed_detail = client.get(f"/api/branches/{branch['id']}").json()
    assert completed_detail["work_item"]["flow"] == "actionable"


def test_running_external_job_survives_a_failed_codex_session(client: TestClient) -> None:
    work, branch = create_work(client, "COST")
    state = client.app.state.rops
    attempt = state.service.claim_terminal_session(branch["id"], "terminal:test:external")
    created = client.post(
        "/api/external-jobs",
        json={
            "work_item_id": work["id"],
            "provider": "ltp/cluster-c",
            "remote_job_id": "fp8-retry-1",
            "status": "created",
        },
    )
    assert created.status_code == 200, created.text

    failed = state.service.finish_terminal_session(
        branch["id"], attempt.id, attempt.lease_id, 1
    )
    assert failed.status == "error"
    detail = client.get(f"/api/branches/{branch['id']}").json()
    assert detail["session"]["status"] == "error"
    assert detail["work_item"]["flow"] == "running"

    completed = client.patch(
        f"/api/external-jobs/{created.json()['id']}", json={"status": "completed"}
    )
    assert completed.status_code == 200, completed.text
    assert client.get(f"/api/branches/{branch['id']}").json()["work_item"]["flow"] == "actionable"


def test_reviewed_memory_enters_brief_and_fork_snapshot_is_frozen(
    client: TestClient,
) -> None:
    program = next(item for item in client.get("/api/programs").json() if item["code"] == "HARN")
    _work, parent = create_work(client)

    first = client.post(
        "/api/memory",
        json={
            "program_code": "HARN",
            "scope_type": "program",
            "scope_id": program["id"],
            "kind": "procedure",
            "title": "Freeze evaluator seed",
            "content": "Use evaluator seed 17 for paired comparisons.",
            "status": "proposed",
            "evidence_refs": ["protocol-v1"],
        },
    ).json()
    client.post(f"/api/memory/{first['id']}/review", json={"status": "accepted"})
    parent_session = client.post(
        f"/api/branches/{parent['id']}/sessions", json={"owner": "test"}
    ).json()
    assert [item["id"] for item in parent_session["brief"]["memory_context"]] == [first["id"]]

    child = client.post(
        f"/api/branches/{parent['id']}/fork",
        json={
            "name": "Frozen memory fork",
            "objective": "Keep the reviewed protocol fixed.",
            "mode": "write",
            "manual_approval": True,
            "start_session": False,
        },
    ).json()
    child_session = client.post(
        f"/api/branches/{child['id']}/sessions", json={"owner": "test"}
    )
    assert child_session.status_code == 200, child_session.text
    second = client.post(
        "/api/memory",
        json={
            "program_code": "HARN",
            "scope_type": "program",
            "scope_id": program["id"],
            "kind": "procedure",
            "title": "New evaluator seed",
            "content": "Future work should use evaluator seed 29.",
            "status": "proposed",
            "evidence_refs": ["protocol-v2"],
        },
    ).json()
    client.post(f"/api/memory/{second['id']}/review", json={"status": "accepted"})

    child_detail = client.get(f"/api/branches/{child['id']}").json()
    child_memory_ids = [item["id"] for item in child_detail["session"]["brief"]["memory_context"]]
    assert child_memory_ids == [first["id"]]
    assert child_detail["branch"]["context_snapshot"]["inherited_memory"][0]["evidence_refs"] == [
        "protocol-v1"
    ]


def test_session_heartbeat_rejects_expired_lease_and_allows_restart(
    client: TestClient,
) -> None:
    _work, branch = create_work(client, "COMMON")
    started = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}).json()
    renewed = client.post(
        f"/api/branches/{branch['id']}/sessions/{started['id']}/heartbeat",
        json={"lease_id": started["lease_id"]},
    )
    assert renewed.status_code == 200, renewed.text

    state = client.app.state.rops
    with state.database.session_factory() as session:
        attempt = session.get(models.SessionAttempt, started["id"])
        assert attempt
        attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    expired = client.post(
        f"/api/branches/{branch['id']}/sessions/{started['id']}/heartbeat",
        json={"lease_id": started["lease_id"]},
    )
    assert expired.status_code == 409
    assert "expired" in expired.json()["detail"].lower()
    detail = client.get(f"/api/branches/{branch['id']}").json()
    assert detail["session"]["status"] == "expired"
    web_message = client.post(
        f"/api/branches/{branch['id']}/messages", json={"content": "Do not run"}
    )
    assert web_message.status_code == 200, web_message.text

    restarted = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"})
    assert restarted.status_code == 200
    assert restarted.json()["id"] != started["id"]


def test_parent_work_must_belong_to_same_program(client: TestClient) -> None:
    parent, _branch = create_work(client, "HARN")
    child = client.post(
        "/api/work-items",
        json={
            "program_code": "HARN",
            "title": "Child verifier task",
            "objective": "Implement the next verifier stage.",
            "primary_parent_id": parent["id"],
        },
    )
    assert child.status_code == 200, child.text
    assert child.json()["primary_parent_id"] == parent["id"]

    cross_program = client.post(
        "/api/work-items",
        json={
            "program_code": "COST",
            "title": "Invalid child",
            "objective": "This dependency crosses Program boundaries.",
            "primary_parent_id": parent["id"],
        },
    )
    assert cross_program.status_code == 409


def test_today_reconciles_stale_running_session(client: TestClient) -> None:
    work, branch = create_work(client, "COST")
    started = client.post(f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}).json()
    state = client.app.state.rops
    with state.database.session_factory() as session:
        attempt = session.get(models.SessionAttempt, started["id"])
        assert attempt
        attempt.status = "expired"
        attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    today = client.get("/api/today")
    assert today.status_code == 200
    assert today.json()["active_sessions"] == 0
    reconciled = next(item for item in today.json()["continue_items"] if item["id"] == work["id"])
    assert reconciled["flow"] == "actionable"
    detail = client.get(f"/api/branches/{branch['id']}").json()
    assert detail["session"]["status"] == "expired"


def test_memory_acceptance_requires_provenance_and_valid_scope(client: TestClient) -> None:
    programs = client.get("/api/programs").json()
    harn = next(item for item in programs if item["code"] == "HARN")
    cost = next(item for item in programs if item["code"] == "COST")
    no_evidence = client.post(
        "/api/memory",
        json={
            "program_code": "HARN",
            "scope_type": "program",
            "scope_id": harn["id"],
            "kind": "observation",
            "title": "Unverified claim",
            "content": "This has no provenance.",
            "status": "proposed",
        },
    )
    assert no_evidence.status_code == 200
    rejected_review = client.post(
        f"/api/memory/{no_evidence.json()['id']}/review", json={"status": "accepted"}
    )
    assert rejected_review.status_code == 409

    invalid_scope = client.post(
        "/api/memory",
        json={
            "program_code": "HARN",
            "scope_type": "program",
            "scope_id": cost["id"],
            "kind": "fact",
            "title": "Cross-program memory",
            "content": "This should be rejected.",
            "status": "proposed",
            "evidence_refs": ["evidence-1"],
        },
    )
    assert invalid_scope.status_code == 409

    direct_accept = client.post(
        "/api/memory",
        json={
            "program_code": "HARN",
            "scope_type": "program",
            "scope_id": harn["id"],
            "kind": "fact",
            "title": "Bypass review",
            "content": "Direct acceptance is invalid.",
            "status": "accepted",
            "evidence_refs": ["evidence-2"],
        },
    )
    assert direct_accept.status_code == 422

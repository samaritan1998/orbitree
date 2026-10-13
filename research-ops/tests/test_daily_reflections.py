from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from research_ops import models, schemas
from research_ops.codex.reflection import parse_reflection_events, reflection_failure_detail
from research_ops.infoflow import InfoflowSendResult
from research_ops.services.daily_reflections import DailyReflectionService, reflection_window
from sqlalchemy import select


class FakeReflectionAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def analyze(self, evidence: dict[str, Any]) -> schemas.DailyReflectionReport:
        self.calls.append(evidence)
        reference = evidence["evidence_refs"][0]
        return schemas.DailyReflectionReport(
            executive_summary="完成了一次有证据的日终复盘。",
            workstreams=[
                schemas.ReflectionWorkstream(
                    work_id="ADAPT-0001",
                    task_title="Adapt model",
                    program="ADAPT",
                    state="completed",
                    summary="完成架构验证。",
                    next_action="整理验证结果。",
                    evidence_refs=[reference],
                )
            ],
            accomplishments=[
                schemas.ReflectionAccomplishment(
                    title="完成验证",
                    outcome="实验轮次已完成。",
                    significance="形成了可复查结果。",
                    evidence_refs=[reference],
                )
            ],
            lessons=[
                schemas.ReflectionLesson(
                    title="保留证据",
                    insight="最终结论必须关联原始轮次。",
                    reusable_as_skill=True,
                    evidence_refs=[reference, "turn:not-real"],
                )
            ],
            shortcomings=[],
            optimizations=[],
            tomorrow_focus=[],
            coverage_notes=["未挂载窗口只能统计活跃信息。"],
        )


class FakeSender:
    configured = True

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send_text(self, recipient: str, content: str) -> InfoflowSendResult:
        self.messages.append((recipient, content))
        return InfoflowSendResult(message_id="reflection-message-1")


@pytest.mark.asyncio
async def test_daily_reflection_uses_shanghai_day_and_runs_once(
    client,
    settings,
) -> None:
    created = client.post(
        "/api/work-items",
        json={
            "program_code": "ADAPT",
            "title": "Adapt model",
            "objective": "Validate one architecture",
            "branch_mode": "write",
        },
    ).json()
    tree = client.get("/api/programs/ADAPT/tree").json()
    branch = next(item for item in tree["branches"] if item["work_item_id"] == created["id"])
    state = client.app.state.rops
    included = datetime(2026, 7, 27, 15, 30, tzinfo=UTC)
    excluded = datetime(2026, 7, 27, 16, 0, tzinfo=UTC)
    with state.database.session_factory() as session:
        binding = models.CodexThreadBinding(
            branch_id=branch["id"],
            thread_id="thread-daily-reflection",
            status="current",
            source="terminal",
            bound_at=datetime(2026, 7, 26, 16, 0, tzinfo=UTC),
        )
        session.add(binding)
        session.flush()
        first_turn = models.CodexTurn(
            branch_id=branch["id"],
            thread_binding_id=binding.id,
            thread_id=binding.thread_id,
            external_turn_id="turn-included",
            prompt="Run one controlled experiment",
            final_answer="The controlled experiment passed.",
            status="completed",
            completed_at=included,
        )
        second_turn = models.CodexTurn(
            branch_id=branch["id"],
            thread_binding_id=binding.id,
            thread_id=binding.thread_id,
            external_turn_id="turn-next-day",
            prompt="This belongs to tomorrow",
            final_answer="Tomorrow result.",
            status="completed",
            completed_at=excluded,
        )
        session.add_all((first_turn, second_turn))
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                role="user",
                content="Record the experiment result",
                metadata_json={"origin": "web", "send_status": "submitted"},
                created_at=included,
            )
        )
        session.add(
            models.CodexWindow(
                thread_id="unassigned-reflection-window",
                cwd=str(Path(settings.workspace_root) / "outside"),
                rollout_path="/tmp/unassigned-reflection.jsonl",
                display_name="Unassigned investigation",
                live=False,
                branch_id=None,
                ignored=False,
                last_activity_at=included,
                last_seen_at=included,
            )
        )
        session.commit()
        first_turn_id = first_turn.id

    configured = settings.model_copy(
        update={
            "daily_reflection_enabled": True,
            "daily_reflection_infoflow_enabled": True,
            "infoflow_notifications_enabled": True,
        }
    )
    agent = FakeReflectionAgent()
    sender = FakeSender()
    service = DailyReflectionService(
        state.database.session_factory,
        configured,
        agent,
        sender,
    )
    reflection = service.queue(
        date(2026, 7, 27),
        now=datetime(2026, 7, 28, 1, 0, tzinfo=UTC),
    )
    completed = await service.run(reflection.id)

    assert completed.status == "completed"
    assert len(agent.calls) == 1
    evidence = agent.calls[0]
    assert evidence["window_utc"] == {
        "start": "2026-07-26T16:00:00+00:00",
        "end": "2026-07-27T16:00:00+00:00",
    }
    assert f"turn:{first_turn_id}" in evidence["evidence_refs"]
    assert all("turn-next-day" not in str(item) for item in evidence["records"])
    assert evidence["coverage"]["unassigned_windows"] == 1
    assert evidence["coverage"]["work_item_titles"] == [
        {"id": "ADAPT-0001", "title": "Adapt model", "program": "ADAPT"}
    ]
    assert "turn:not-real" not in completed.report_markdown
    assert "ADAPT-0001 · Adapt model" in completed.report_markdown
    assert "任务总览" in completed.report_markdown
    assert "经验沉淀候选" in completed.report_markdown

    repeated = service.queue(
        date(2026, 7, 27),
        now=datetime(2026, 7, 28, 2, 0, tzinfo=UTC),
    )
    assert repeated.id == reflection.id
    assert repeated.status == "completed"
    assert (await service.run(repeated.id)).id == reflection.id
    assert len(agent.calls) == 1

    assert service.dispatch_notifications() == [reflection.id]
    assert service.dispatch_notifications() == []
    assert sender.messages[0][0] == "alice"
    assert "每日总结与反思" in sender.messages[0][1]
    assert "ADAPT-0001 Adapt model" in sender.messages[0][1]

    refreshed = service.queue(
        date(2026, 7, 27),
        force=True,
        now=datetime(2026, 7, 28, 3, 0, tzinfo=UTC),
    )
    assert refreshed.notification_status == "sent"
    refreshed = await service.run(refreshed.id)
    assert refreshed.status == "completed"
    assert refreshed.notification_status == "sent"
    assert len(agent.calls) == 2
    assert service.dispatch_notifications() == []
    assert len(sender.messages) == 1

    response = client.get("/api/daily-reflections")
    assert response.status_code == 200
    assert response.json()[0]["reflection_date"] == "2026-07-27"
    with state.database.session_factory() as session:
        stored = session.scalar(select(models.DailyReflection))
        assert stored is not None
        assert stored.notification_status == "sent"


def test_reflection_window_handles_shanghai_midnight() -> None:
    start, end = reflection_window(
        date(2026, 7, 27),
        ZoneInfo("Asia/Shanghai"),
    )
    assert start == datetime(2026, 7, 26, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 27, 16, 0, tzinfo=UTC)


def test_reflection_event_parser_validates_structured_final_message() -> None:
    report = {
        "executive_summary": "summary",
        "workstreams": [],
        "accomplishments": [],
        "lessons": [],
        "shortcomings": [],
        "optimizations": [],
        "tomorrow_focus": [],
        "coverage_notes": [],
    }
    output = "\n".join(
        (
            '{"type":"thread.started","thread_id":"ephemeral"}',
            json_event(report),
            '{"type":"turn.completed","usage":{}}',
        )
    )
    assert parse_reflection_events(output).executive_summary == "summary"


def test_reflection_failure_detail_reads_jsonl_stdout() -> None:
    output = json.dumps(
        {
            "type": "error",
            "message": "Selected model is at capacity",
        }
    )

    assert reflection_failure_detail(output) == "Selected model is at capacity"


def json_event(report: dict[str, Any]) -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(report)},
        }
    )

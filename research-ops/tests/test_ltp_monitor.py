from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any

import pytest
from research_ops import models
from research_ops.infoflow import InfoflowDeliveryBlockedError, InfoflowSendResult
from research_ops.services.ltp_monitor import (
    LtpCliJob,
    LtpCliTaskSource,
    LtpMonitorService,
)
from sqlalchemy import select


@dataclass
class FakeSource:
    listed: dict[str, list[LtpCliJob]] = field(default_factory=dict)
    shown: dict[tuple[str, str], LtpCliJob | None] = field(default_factory=dict)

    def list_jobs(self, user: str) -> list[LtpCliJob]:
        return list(self.listed.get(user, []))

    def show_job(self, user: str, job_name: str) -> LtpCliJob | None:
        return self.shown.get((user, job_name))


class FakeGroupSender:
    configured = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_group_text(self, group_id: str, content: str) -> InfoflowSendResult:
        self.sent.append((group_id, content))
        return InfoflowSendResult(message_id=f"group-{len(self.sent)}")


class BlockedGroupSender(FakeGroupSender):
    def send_group_text(self, group_id: str, content: str) -> InfoflowSendResult:
        self.sent.append((group_id, content))
        raise InfoflowDeliveryBlockedError("group not follow")


def job(*, state: str, source: str = "ltp_cli_list") -> LtpCliJob:
    return LtpCliJob(
        user="alice",
        job_name="glm52-r15-a1b2c3",
        cluster="cluster-d",
        state=state,
        provider_status=state.capitalize(),
        state_group="active" if state == "running" else "queued",
        category="training",
        metadata_json={"nodes": 16},
        source=source,
    )


def test_ltp_monitor_tracks_lifecycle_and_notifies_only_new_transitions(client, settings) -> None:
    configured = settings.model_copy(
        update={
            "ltp_monitor_enabled": True,
            "ltp_monitor_notifications_enabled": True,
            "ltp_monitor_group_id": "12345678",
            "infoflow_public_base_url": "http://research-ops.example",
        }
    )
    source = FakeSource()
    sender = FakeGroupSender()
    service = LtpMonitorService(
        client.app.state.rops.database.session_factory,
        configured,
        source,  # type: ignore[arg-type]
        sender,
    )

    # The first successful scan establishes the baseline and cannot flood the group.
    assert service.sync() == {"observed": 0, "transitions": 0, "errors": 0}
    source.listed["alice"] = [job(state="created")]
    assert service.sync() == {"observed": 1, "transitions": 1, "errors": 0}
    assert [item.status for item in service.dispatch_notifications()] == ["sent"]

    source.listed["alice"] = [job(state="pending")]
    assert service.sync()["transitions"] == 1
    assert [item.status for item in service.dispatch_notifications()] == ["sent"]

    source.listed["alice"] = [job(state="running")]
    assert service.sync()["transitions"] == 1
    assert [item.status for item in service.dispatch_notifications()] == ["sent"]

    # A disappeared active job is not assumed complete: `show` must report the terminal state.
    source.listed["alice"] = []
    source.shown[("alice", "glm52-r15-a1b2c3")] = job(
        state="failed", source="ltp_cli_show"
    )
    assert service.sync()["transitions"] == 1
    assert [item.status for item in service.dispatch_notifications()] == ["sent"]
    assert service.sync()["transitions"] == 0
    assert service.dispatch_notifications() == []

    assert [target for target, _message in sender.sent] == ["12345678"] * 4
    assert "状态：Created" in sender.sent[0][1]
    assert "状态：Created → Pending" in sender.sent[1][1]
    assert "状态：Pending → Running" in sender.sent[2][1]
    assert "状态：Running → Failed" in sender.sent[3][1]
    assert "research-ops.example/ltp-monitor" in sender.sent[3][1]

    with client.app.state.rops.database.session_factory() as session:
        tracked = session.scalar(select(models.LtpMonitoredJob))
        assert tracked is not None
        assert tracked.state == "failed"
        events = list(
            session.scalars(
                select(models.LtpMonitorEvent).order_by(models.LtpMonitorEvent.observed_at)
            )
        )
        assert [event.state for event in events] == ["created", "pending", "running", "failed"]
        assert [event.source for event in events][-1] == "ltp_cli_show"


def test_ltp_monitor_preserves_last_known_state_when_cli_list_fails(client, settings) -> None:
    configured = settings.model_copy(update={"ltp_monitor_enabled": True})

    class ErrorSource(FakeSource):
        def list_jobs(self, user: str) -> list[LtpCliJob]:
            del user
            from research_ops.services.ltp_monitor import LtpMonitorSourceError

            raise LtpMonitorSourceError("LTP CLI list failed")

    service = LtpMonitorService(
        client.app.state.rops.database.session_factory,
        configured,
        ErrorSource(),  # type: ignore[arg-type]
        FakeGroupSender(),
    )

    assert service.sync() == {"observed": 0, "transitions": 0, "errors": 1}
    overview = service.overview()
    assert overview["state"] is not None
    assert overview["state"].last_error == "alice: LTP CLI list failed"
    assert overview["jobs"] == []


def test_ltp_monitor_can_track_a_known_job_when_list_discovery_is_empty(client, settings) -> None:
    configured = settings.model_copy(update={"ltp_monitor_enabled": True})
    source = FakeSource()
    service = LtpMonitorService(
        client.app.state.rops.database.session_factory,
        configured,
        source,  # type: ignore[arg-type]
        FakeGroupSender(),
    )
    tracked = service.track_job(job_name="glm52-r15-a1b2c3")
    source.shown[("alice", tracked.job_name)] = job(
        state="running", source="ltp_cli_show"
    )

    assert service.sync() == {"observed": 0, "transitions": 1, "errors": 0}
    overview = service.overview()
    assert overview["jobs"][0].state == "running"
    assert overview["events"][0].previous_state == "unknown"


def test_ltp_monitor_bootstrap_probes_do_not_notify_historical_jobs(client, settings) -> None:
    configured = settings.model_copy(
        update={
            "ltp_monitor_enabled": True,
            "ltp_monitor_notifications_enabled": True,
            "ltp_monitor_group_id": "12345678",
        }
    )
    listed = job(state="unknown")
    source = FakeSource(
        listed={"alice": [listed]},
        shown={
            ("alice", listed.job_name): job(
                state="running", source="ltp_cli_show"
            )
        },
    )
    sender = FakeGroupSender()
    service = LtpMonitorService(
        client.app.state.rops.database.session_factory,
        configured,
        source,  # type: ignore[arg-type]
        sender,
    )

    assert service.sync()["transitions"] == 1
    assert service.dispatch_notifications() == []
    assert sender.sent == []


def test_ltp_monitor_does_not_retry_group_membership_failures(client, settings) -> None:
    configured = settings.model_copy(
        update={
            "ltp_monitor_enabled": True,
            "ltp_monitor_notifications_enabled": True,
            "ltp_monitor_group_id": "12345678",
        }
    )
    source = FakeSource()
    sender = BlockedGroupSender()
    service = LtpMonitorService(
        client.app.state.rops.database.session_factory,
        configured,
        source,  # type: ignore[arg-type]
        sender,
    )

    assert service.sync()["transitions"] == 0
    source.listed["alice"] = [job(state="created")]
    assert service.sync()["transitions"] == 1
    assert [delivery.status for delivery in service.dispatch_notifications()] == ["blocked"]
    assert service.dispatch_notifications() == []
    assert len(sender.sent) == 1
    assert service.overview()["notifications"][0].status == "blocked"


def test_ltp_monitor_api_exposes_the_independent_task_ledger(client) -> None:
    created = client.post(
        "/api/ltp-monitor/tasks",
        json={"job_name": "glm52-r15-a1b2c3", "cluster": "cluster-d"},
    )
    assert created.status_code == 200
    assert created.json()["state"] == "unknown"
    assert created.json()["cluster"] == "cluster-d"

    overview = client.get("/api/ltp-monitor")
    assert overview.status_code == 200
    payload = overview.json()
    assert payload["jobs"][0]["job_name"] == "glm52-r15-a1b2c3"
    assert payload["events"] == []


def test_ltp_cli_source_uses_list_and_show_with_user_identity() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "list" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                '[{"job_name":"job-a","status":"Created","cluster":"cluster-d"}]',
                "",
            )
        return subprocess.CompletedProcess(
            argv,
            0,
            "任务名称：job-a\n状态：Failed\n集群：cluster-d\n",
            "",
        )

    source = LtpCliTaskSource(
        command="ltp-cli",
        api_url="https://ltp.example",
        timeout_seconds=20,
        runner=runner,
    )

    listed = source.list_jobs("alice")
    shown = source.show_job("alice", "job-a")

    assert listed[0].state == "unknown"
    assert listed[0].metadata_json["list_status"] == "Created"
    assert shown is not None and shown.state == "failed"
    assert calls[0] == [
        "ltp-cli",
        "--api-url",
        "https://ltp.example",
        "list",
        "--user",
        "alice",
        "--refresh",
    ]
    assert calls[1][-4:] == ["show", "job-a", "--user", "alice"]


def test_ltp_cli_source_treats_zero_exit_error_text_as_a_source_failure() -> None:
    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "获取任务列表失败: 'data'", "")

    source = LtpCliTaskSource(
        command="ltp-cli",
        api_url=None,
        timeout_seconds=20,
        runner=runner,
    )

    from research_ops.services.ltp_monitor import LtpMonitorSourceError

    with pytest.raises(LtpMonitorSourceError, match="获取任务列表失败"):
        source.list_jobs("alice")

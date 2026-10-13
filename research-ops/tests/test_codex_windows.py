from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from research_ops import models
from sqlalchemy import select
from test_api_workflow import create_work


def test_discovers_live_codex_window_and_adopts_its_existing_directory(
    client, settings, tmp_path: Path
) -> None:
    existing = tmp_path / "existing-research"
    existing.mkdir()
    marker = existing / "keep-me.txt"
    marker.write_text("untouched\n")
    thread_id = "019f-registry-live-thread"
    started_at = datetime.now(UTC) - timedelta(minutes=2)
    rollout = _write_rollout(
        settings.codex_home,
        thread_id,
        existing,
        started_at,
        final_answer="Imported final result",
    )
    proc_root = _fake_codex_process(tmp_path, 4242, existing)
    client.app.state.rops.codex_windows.proc_root = proc_root

    discovered = client.get("/api/codex-windows?include_ignored=true")

    assert discovered.status_code == 200, discovered.text
    window = next(item for item in discovered.json() if item["thread_id"] == thread_id)
    assert window["state"] == "unassigned"
    assert window["live"] is True
    assert window["pid"] == 4242
    assert window["tty"] == "/dev/pts/12"
    assert window["cwd"] == str(existing.resolve())
    assert thread_id in window["resume_command"]
    assert str(existing.resolve()) in window["resume_command"]
    assert client.get(f"/api/codex-windows/{window['id']}/attach-targets").json() == []

    adopted = client.post(
        f"/api/codex-windows/{window['id']}/create-work",
        json={"program_code": "FREE"},
    )

    assert adopted.status_code == 200, adopted.text
    payload = adopted.json()
    assert payload["window"]["state"] == "managed"
    assert payload["window"]["program_code"] == "FREE"
    assert payload["work_item"]["title"] == "existing-research · 019f-reg"
    assert "imported_codex_session" in payload["work_item"]["labels"]
    assert payload["branch"]["cwd"] == str(existing.resolve())
    assert marker.read_text() == "untouched\n"
    assert sorted(item.name for item in existing.iterdir()) == ["keep-me.txt"]
    detail = client.get(f"/api/branches/{payload['branch']['id']}").json()
    assert detail["session"]["status"] == "active"
    assert detail["session"]["lease_owner"].startswith("registry:")
    assert [item["final_answer"] for item in detail["turns"]] == [
        "Imported final result"
    ]
    assert rollout.exists()

    child_directory = tmp_path / "forked-research"
    child_directory.mkdir()
    child_marker = child_directory / "child.txt"
    child_marker.write_text("child untouched\n")
    child_thread_id = "019f-registry-child-thread"
    _write_rollout(
        settings.codex_home,
        child_thread_id,
        child_directory,
        started_at + timedelta(minutes=1),
        final_answer="Forked final result",
        parent_thread_id=thread_id,
    )
    lineage = client.get("/api/codex-windows?include_ignored=true").json()
    root_window = next(item for item in lineage if item["thread_id"] == thread_id)
    child_window = next(item for item in lineage if item["thread_id"] == child_thread_id)
    assert root_window["child_count"] == 1
    assert child_window["parent_thread_id"] == thread_id
    assert child_window["parent_window_id"] == root_window["id"]
    assert child_window["parent_work_id"] == payload["work_item"]["human_id"]
    assert child_window["can_attach_to_parent"] is True

    child_adoption = client.post(
        f"/api/codex-windows/{child_window['id']}/attach-parent"
    )
    assert child_adoption.status_code == 200, child_adoption.text
    child_payload = child_adoption.json()
    assert child_payload["work_item"]["id"] == payload["work_item"]["id"]
    assert child_payload["branch"]["parent_branch_id"] == payload["branch"]["id"]
    assert child_payload["branch"]["cwd"] == str(child_directory.resolve())
    assert child_payload["branch"]["context_snapshot"]["fork_reason"] == (
        "external_codex_fork"
    )
    assert child_marker.read_text() == "child untouched\n"
    child_detail = client.get(f"/api/branches/{child_payload['branch']['id']}").json()
    assert [item["final_answer"] for item in child_detail["turns"]] == [
        "Forked final result"
    ]

    ignored = client.post(f"/api/codex-windows/{window['id']}/ignore")
    assert ignored.status_code == 200
    assert ignored.json()["state"] == "ignored"
    assert all(
        item["thread_id"] != thread_id for item in client.get("/api/codex-windows").json()
    )
    restored = client.post(f"/api/codex-windows/{window['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["state"] == "managed"


def test_attach_only_offers_branches_with_the_same_directory(
    client, settings, tmp_path: Path
) -> None:
    _work, branch = create_work(client, "HARN")
    thread_id = "019f-registry-saved-thread"
    _write_rollout(
        settings.codex_home,
        thread_id,
        Path(branch["cwd"]),
        datetime.now(UTC) - timedelta(minutes=3),
        final_answer="Saved result",
        originator="codex_exec",
    )
    client.app.state.rops.codex_windows.proc_root = tmp_path / "empty-proc"
    windows = client.get("/api/codex-windows?include_ignored=true").json()
    window = next(item for item in windows if item["thread_id"] == thread_id)

    targets = client.get(
        f"/api/codex-windows/{window['id']}/attach-targets"
    ).json()
    assert [item["branch_id"] for item in targets] == [branch["id"]]

    attached = client.post(
        f"/api/codex-windows/{window['id']}/attach",
        json={"branch_id": branch["id"]},
    )
    assert attached.status_code == 200, attached.text
    assert attached.json()["state"] == "managed"
    assert attached.json()["live"] is False
    detail = client.get(f"/api/branches/{branch['id']}").json()
    assert detail["session"]["status"] == "completed"
    assert [item["final_answer"] for item in detail["turns"]] == ["Saved result"]


def test_active_web_attempt_is_not_reported_as_a_local_codex_window(
    client, settings, tmp_path: Path
) -> None:
    _work, branch = create_work(client, "COST")
    attempt = client.post(
        f"/api/branches/{branch['id']}/sessions", json={"owner": "test"}
    ).json()
    thread_id = attempt["codex_thread_id"]
    _write_rollout(
        settings.codex_home,
        thread_id,
        Path(branch["cwd"]),
        datetime.now(UTC) - timedelta(minutes=1),
        final_answer="A previous result",
    )
    with client.app.state.rops.database.session_factory() as session:
        stored_attempt = session.get(models.SessionAttempt, attempt["id"])
        assert stored_attempt is not None
        stored_attempt.lease_owner = "web"
        session.commit()

    empty_proc = tmp_path / "empty-proc"
    empty_proc.mkdir()
    client.app.state.rops.codex_windows.proc_root = empty_proc
    windows = client.get("/api/codex-windows?include_ignored=true").json()

    window = next(item for item in windows if item["thread_id"] == thread_id)
    assert window["live"] is False
    assert window["pid"] is None
    assert window["match_confidence"] == "rollout_only"


def test_registry_releases_a_managed_session_after_its_tui_is_killed(
    client, settings, tmp_path: Path
) -> None:
    """A registry lease must not keep a disappeared terminal alive forever."""
    existing = tmp_path / "killed-terminal-workspace"
    existing.mkdir()
    thread_id = "019f-registry-killed-thread"
    _write_rollout(
        settings.codex_home,
        thread_id,
        existing,
        datetime.now(UTC) - timedelta(minutes=1),
        final_answer="Result saved before the terminal was killed",
    )
    registry = client.app.state.rops.codex_windows
    registry.proc_root = _fake_codex_process(tmp_path, 4243, existing)

    window = next(
        item
        for item in client.get("/api/codex-windows?include_ignored=true").json()
        if item["thread_id"] == thread_id
    )
    adopted = client.post(
        f"/api/codex-windows/{window['id']}/create-work",
        json={"program_code": "FREE"},
    )
    assert adopted.status_code == 200, adopted.text
    branch_id = adopted.json()["branch"]["id"]
    assert client.get(f"/api/branches/{branch_id}").json()["session"]["status"] == "active"

    # The registry can no longer find the TUI after its process exits.
    empty_proc = tmp_path / "empty-proc"
    empty_proc.mkdir()
    registry.proc_root = empty_proc
    rescanned = client.post("/api/codex-windows/scan")
    assert rescanned.status_code == 200, rescanned.text
    stored = next(item for item in rescanned.json() if item["thread_id"] == thread_id)
    assert stored["live"] is False

    detail = client.get(f"/api/branches/{branch_id}").json()
    assert detail["session"]["status"] == "completed"
    assert detail["conversation_state"]["local_turn_active"] is False
    assert detail["conversation_state"]["can_send"] is True


def test_subagents_are_registered_for_traceability_but_hidden_from_sessions(
    client, settings, tmp_path: Path
) -> None:
    cwd = tmp_path / "shared-workspace"
    cwd.mkdir()
    started_at = datetime.now(UTC) - timedelta(minutes=2)
    root_thread = "019f-user-window"
    subagent_thread = "019f-internal-subagent"
    _write_rollout(
        settings.codex_home,
        root_thread,
        cwd,
        started_at,
        final_answer="Root result",
    )
    _write_rollout(
        settings.codex_home,
        subagent_thread,
        cwd,
        started_at + timedelta(seconds=10),
        final_answer="Internal result",
        parent_thread_id=root_thread,
        source={
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": root_thread,
                    "depth": 1,
                    "agent_nickname": "Tesla",
                    "agent_role": "explorer",
                }
            }
        },
    )
    client.app.state.rops.codex_windows.proc_root = tmp_path / "empty-proc"

    windows = client.get("/api/codex-windows?include_ignored=true").json()
    assert [item["thread_id"] for item in windows] == [root_thread]
    assert windows[0]["child_count"] == 0
    with client.app.state.rops.database.session_factory() as session:
        stored = session.scalar(
            select(models.CodexWindow).where(
                models.CodexWindow.thread_id == subagent_thread
            )
        )
        assert stored is not None
        assert stored.kind == "subagent"
        assert stored.metadata_json["subagent_nickname"] == "Tesla"


def test_ai_and_manual_window_titles(client, settings, tmp_path: Path) -> None:
    cwd = tmp_path / "wide"
    cwd.mkdir()
    thread_id = "019f-title-window"
    _write_rollout(
        settings.codex_home,
        thread_id,
        cwd,
        datetime.now(UTC) - timedelta(minutes=1),
        final_answer="Built the service",
        user_message="建设一个每天推送论文和 GitHub 进展的 AI 技术情报系统",
    )
    registry = client.app.state.rops.codex_windows
    registry.proc_root = _fake_codex_process(tmp_path, 4343, cwd)
    settings.codex_title_generation_enabled = True
    registry._run_title_model = lambda candidates: {  # type: ignore[method-assign]
        candidates[0]["thread_id"]: "建设 AI 技术情报系统"
    }

    generated = client.post("/api/codex-windows/generate-titles")
    assert generated.status_code == 200, generated.text
    window = next(item for item in generated.json() if item["thread_id"] == thread_id)
    assert window["display_name"] == "建设 AI 技术情报系统"
    assert window["title_source"] == "ai"

    renamed = client.patch(
        f"/api/codex-windows/{window['id']}/title",
        json={"title": "我的技术情报窗口"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["display_name"] == "我的技术情报窗口"
    assert renamed.json()["title_source"] == "manual"
    registry._run_title_model = lambda _candidates: {  # type: ignore[method-assign]
        thread_id: "不应覆盖手工标题"
    }
    regenerated = client.post("/api/codex-windows/generate-titles").json()
    current = next(item for item in regenerated if item["thread_id"] == thread_id)
    assert current["display_name"] == "我的技术情报窗口"
    assert current["title_source"] == "manual"


def _write_rollout(
    codex_home: Path,
    thread_id: str,
    cwd: Path,
    started_at: datetime,
    *,
    final_answer: str,
    originator: str = "codex-tui",
    parent_thread_id: str | None = None,
    source: str | dict = "cli",
    user_message: str = "Import this work",
) -> Path:
    directory = codex_home / "sessions" / "2026" / "07" / "14"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-07-14-{thread_id}.jsonl"
    turn_started = started_at + timedelta(seconds=2)
    completed = started_at + timedelta(seconds=4)
    turn_id = f"turn-{thread_id}"
    metadata = {
        "id": thread_id,
        "session_id": thread_id,
        "cwd": str(cwd),
        "timestamp": started_at.isoformat(),
        "cli_version": "0.144.3",
        "originator": originator,
        "source": source,
    }
    if parent_thread_id:
        metadata["forked_from_id"] = parent_thread_id
    records = [
        {
            "timestamp": started_at.isoformat(),
            "type": "session_meta",
            "payload": metadata,
        },
        {
            "timestamp": turn_started.isoformat(),
            "type": "event_msg",
            "payload": {
                "type": "task_started",
                "turn_id": turn_id,
                "started_at": turn_started.timestamp(),
            },
        },
        {
            "timestamp": turn_started.isoformat(),
            "type": "event_msg",
            "payload": {"type": "user_message", "message": user_message},
        },
        {
            "timestamp": completed.isoformat(),
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": turn_id,
                "last_agent_message": final_answer,
                "completed_at": completed.timestamp(),
            },
        },
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in records))
    return path


def _fake_codex_process(
    tmp_path: Path, pid: int, cwd: Path
) -> Path:
    proc_root = tmp_path / "proc"
    process = proc_root / str(pid)
    (process / "fd").mkdir(parents=True)
    (process / "cmdline").write_bytes(b"/usr/local/bin/codex\0--yolo\0")
    (process / "cwd").symlink_to(cwd, target_is_directory=True)
    (process / "fd" / "0").symlink_to("/dev/pts/12")
    return proc_root

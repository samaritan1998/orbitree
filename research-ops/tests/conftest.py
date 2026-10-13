from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from research_ops.api import create_app
from research_ops.settings import PROJECT_ROOT, Settings


class FakeCodexAdapter:
    def __init__(self) -> None:
        self.started = False
        self.threads: set[str] = set()
        self.goals: dict[str, dict[str, Any]] = {}
        self.loaded_goal_threads: set[str] = set()
        self.goal_resumes: list[str] = []
        self.goal_turns: dict[str, dict[str, Any]] = {}
        self.interrupted_turns: list[tuple[str, str]] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def health(self) -> dict[str, Any]:
        return {"available": True, "protocol": "fake", "threads_visible": len(self.threads)}

    async def start_thread(self, *, cwd: str, instructions: str) -> dict[str, Any]:
        del cwd, instructions
        thread_id = str(uuid.uuid4())
        self.threads.add(thread_id)
        return {"thread": {"id": thread_id}}

    async def resume_thread(self, *, thread_id: str, cwd: str) -> dict[str, Any]:
        del cwd
        self.threads.add(thread_id)
        return {"thread": {"id": thread_id}}

    async def fork_thread(self, *, thread_id: str, cwd: str, instructions: str) -> dict[str, Any]:
        del thread_id, cwd, instructions
        child_id = str(uuid.uuid4())
        self.threads.add(child_id)
        return {"thread": {"id": child_id}}

    async def get_goal(self, *, thread_id: str) -> dict[str, Any]:
        return {"goal": self.goals.get(thread_id)}

    async def read_goal_thread(self, *, thread_id: str) -> dict[str, Any]:
        status = "active" if thread_id in self.loaded_goal_threads else "notLoaded"
        return {"thread": {"id": thread_id, "status": {"type": status}}}

    async def read_goal_turn(self, *, thread_id: str) -> dict[str, Any]:
        return self.goal_turns.get(thread_id, {})

    async def interrupt_turn(self, *, thread_id: str, turn_id: str) -> dict[str, Any]:
        self.interrupted_turns.append((thread_id, turn_id))
        self.goal_turns[thread_id] = {"id": turn_id, "status": "interrupted"}
        self.loaded_goal_threads.discard(thread_id)
        return {}

    async def prepare_goal_thread(self, *, thread_id: str, cwd: str) -> dict[str, Any]:
        del cwd
        self.loaded_goal_threads.add(thread_id)
        self.goal_resumes.append(thread_id)
        return await self.read_goal_thread(thread_id=thread_id)

    async def set_goal(
        self,
        *,
        thread_id: str,
        objective: str | None = None,
        status: str | None = None,
        token_budget: int | None = None,
        update_token_budget: bool = False,
    ) -> dict[str, Any]:
        current = dict(self.goals.get(thread_id) or {})
        if not current and objective is None:
            raise RuntimeError("No goal exists")
        if objective is not None:
            current = {
                "threadId": thread_id,
                "objective": objective,
                "status": status or "active",
                "tokenBudget": token_budget if update_token_budget else None,
                "tokensUsed": 0,
                "timeUsedSeconds": 0,
                "createdAt": 1,
                "updatedAt": 1,
            }
        else:
            if status is not None:
                current["status"] = status
            if update_token_budget:
                current["tokenBudget"] = token_budget
        self.goals[thread_id] = current
        return {"goal": current}

    async def clear_goal(self, *, thread_id: str) -> dict[str, Any]:
        return {"cleared": self.goals.pop(thread_id, None) is not None}

    async def send_message(
        self,
        *,
        thread_id: str,
        content: str,
        cwd: str,
        metadata: dict[str, str],
        additional_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del content, cwd, metadata, additional_context
        assert thread_id in self.threads
        return {"turn": {"id": str(uuid.uuid4())}}

    async def respond(self, request_id: str, result: dict[str, Any]) -> None:
        del request_id, result


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
        web_terminal_enabled=False,
        workspace_root=tmp_path / "workspaces",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        programs_file=PROJECT_ROOT / "config" / "programs.yaml",
        frontend_dist=tmp_path / "dist",
        codex_home=tmp_path / "codex",
        codex_model_provider=None,
        oneapi_api_key=None,
        codex_enabled=True,
        codex_result_sync_enabled=False,
        codex_local_live_enabled=False,
        daily_reflection_enabled=False,
        session_workflows_enabled=False,
        session_workflow_analysis_enabled=False,
        octo_api_key=None,
        infoflow_notifications_enabled=False,
        ltp_monitor_enabled=False,
        native_goal_supervision_enabled=False,
        scheduled_checks_enabled=False,
    )


@pytest.fixture
def fake_codex() -> FakeCodexAdapter:
    return FakeCodexAdapter()


@pytest.fixture
def client(settings: Settings, fake_codex: FakeCodexAdapter) -> TestClient:
    app = create_app(settings, fake_codex)
    with TestClient(app) as test_client:
        yield test_client

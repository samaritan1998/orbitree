import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from research_ops import models, schemas
from research_ops.services.scheduled_check_prompts import SYSTEM_PROMPT, ScheduledCheckPromptService
from research_ops.services.scheduled_checks import DEFAULT_CHECK_PROMPT
from sqlalchemy import func, select
from test_api_workflow import create_work

DRAFT = (
    "看看这轮训练怎么样了，报错或跑完了就告诉我。\n\n"
    "最后一行写「检查结果：无变化/有进展/失败/完成/需要决定」，选一项。"
)


def history(client, branch_id, *, age=1, prompt="检查 train-test", answer="Running"):
    with client.app.state.rops.database.session_factory() as session:
        binding = session.scalar(
            select(models.CodexThreadBinding).where(
                models.CodexThreadBinding.branch_id == branch_id,
            )
        )
        if binding is None:
            binding = models.CodexThreadBinding(
                branch_id=branch_id,
                thread_id=f"thread-{branch_id}",
                source="test",
                bound_at=datetime.now(UTC) - timedelta(days=1),
            )
            session.add(binding)
            session.flush()
        turn = models.CodexTurn(
            branch_id=branch_id,
            thread_binding_id=binding.id,
            thread_id=f"thread-{branch_id}",
            external_turn_id=models.new_id(),
            prompt=prompt,
            final_answer=answer,
            completed_at=datetime.now(UTC) - timedelta(minutes=age),
        )
        session.add(turn)
        session.commit()
        return turn.id


def mock_api(monkeypatch, handler):
    original = httpx.AsyncClient
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return options


def completion(prompt=DRAFT):
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps({"prompt": prompt}),
                    "reasoning_content": "private thoughts",
                },
            }
        ]
    }


def test_drafting_reads_only_current_branch_and_has_no_side_effects(client, settings, monkeypatch):
    _, branch = create_work(client)
    _, sibling = create_work(client)
    history(client, branch["id"], answer="train-test has reached step 200")
    history(client, sibling["id"], answer="DO_NOT_READ_OTHER_BRANCH")
    settings.oneapi_api_key = "unit-test-secret"
    state = client.app.state.rops
    with state.database.session_factory() as session:
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                role="user",
                content=(
                    "有问题就处理，但不要重提。sk-TEST_FIXTURE_NOT_A_REAL_KEY_123456\n"
                    "Authorization: Bearer fake-sensitive-bearer\n"
                    '{"api_key": "fake-sensitive-json-key", '
                    '"refresh_token": "fake-sensitive-refresh"}'
                ),
            )
        )
        session.add(
            models.ConversationMessage(
                branch_id=branch["id"],
                role="tool",
                kind="command",
                content="DO_NOT_READ_TOOL_LOG",
            )
        )
        session.commit()
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer unit-test-secret"
        assert str(request.url).endswith("/chat/completions")
        return httpx.Response(200, json=completion())

    options = mock_api(monkeypatch, handler)
    response = client.post(
        f"/api/branches/{branch['id']}/scheduled-check/prompt",
        json={
            "current_prompt": "有问题处理一下，但不重提。",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["prompt"] == DRAFT
    assert result["model"] == "DeepSeek-V4.1-Flash"
    assert result["provider"] == "oneapi"
    assert result["context_turns"] == 1
    assert result["context_messages"] == 1
    assert "private thoughts" not in response.text
    assert "unit-test-secret" not in response.text
    assert seen[0]["model"] == settings.oneapi_model
    assert seen[0]["thinking"] == {"type": "disabled"}
    assert seen[0]["max_tokens"] == 2048
    assert "reasoning_effort" not in seen[0]
    assert seen[0]["messages"][0]["content"] == SYSTEM_PROMPT
    assert "tools" not in seen[0]
    assert not options[0]["trust_env"] and not options[0]["follow_redirects"]
    content = seen[0]["messages"][1]["content"]
    assert "step 200" in content and "不要重提" in content
    for excluded in (
        "DO_NOT_READ_OTHER_BRANCH",
        "DO_NOT_READ_TOOL_LOG",
        "sk-TEST_FIXTURE_NOT_A_REAL_KEY_123456",
        "fake-sensitive-bearer",
        "fake-sensitive-json-key",
        "fake-sensitive-refresh",
    ):
        assert excluded not in content
    with state.database.session_factory() as session:
        for model in (
            models.ScheduledCheck,
            models.ScheduledCheckRun,
            models.SessionAttempt,
            models.GoalRun,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert session.scalar(select(func.count()).select_from(models.ConversationMessage)) == 2


def test_draft_style_is_short_without_removing_permission_or_notification_contracts():
    assert "1–2 句自然中文" in SYSTEM_PROMPT
    assert "30–80 字" in SYSTEM_PROMPT
    assert "不罗列路径" in SYSTEM_PROMPT
    assert "精简措辞而非删掉约束" in SYSTEM_PROMPT
    assert "不要新增重提、删除、扩容等权限" in SYSTEM_PROMPT
    assert "150–500" not in SYSTEM_PROMPT
    for verdict in ("无变化", "有进展", "失败", "完成", "需要决定"):
        assert verdict in SYSTEM_PROMPT and verdict in DEFAULT_CHECK_PROMPT
    assert len(DEFAULT_CHECK_PROMPT) < 180
    assert "不改任务" in DEFAULT_CHECK_PROMPT and "不用一直等" in DEFAULT_CHECK_PROMPT


def test_context_is_bounded_and_current_prompt_permissions_are_preserved(client, settings):
    _, branch = create_work(client)
    for age in range(1, 20):
        history(client, branch["id"], age=age, prompt=f"task-{age}", answer="long context " * 2000)
    context = ScheduledCheckPromptService(
        client.app.state.rops.database.session_factory, settings
    ).context(
        branch["id"],
        "可以修复，但不要扩容。",
    )
    assert len(json.dumps(context, ensure_ascii=False)) < 28000
    assert len(context["records"]) <= 8
    assert context["records"][-1]["query"] == "task-1"
    assert context["current_prompt"] == "可以修复，但不要扩容。"


def test_missing_configuration_and_empty_context_do_not_call_provider(
    client, settings, monkeypatch
):
    _, branch = create_work(client)
    calls = []
    mock_api(monkeypatch, lambda request: calls.append(request))
    url = f"/api/branches/{branch['id']}/scheduled-check/prompt"
    response = client.post(url, json={})
    assert response.status_code == 503 and "not configured" in response.text
    settings.oneapi_api_key = "test"
    response = client.post(url, json={})
    assert response.status_code == 503 and "No recent conversation" in response.text
    assert not calls


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_provider_errors_never_leak_response_body(client, settings, monkeypatch, status):
    _, branch = create_work(client)
    history(client, branch["id"])
    settings.oneapi_api_key = "test"
    mock_api(monkeypatch, lambda request: httpx.Response(status, text="SECRET_PROVIDER_BODY"))
    response = client.post(f"/api/branches/{branch['id']}/scheduled-check/prompt", json={})
    assert response.status_code == 503
    assert f"HTTP {status}" in response.text
    assert "SECRET_PROVIDER_BODY" not in response.text


@pytest.mark.parametrize(
    "body, message",
    [
        ({"choices": []}, "unexpected response format"),
        (
            {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
            "exhausted its output budget",
        ),
        ({"choices": [{"message": {"content": "not json"}}]}, "unexpected response format"),
        ({"choices": [{"message": {"content": None}}]}, "no draft text"),
        ({"choices": [{"message": {"content": " "}}]}, "no draft text"),
        (completion("No classification marker"), "omitted the check-result instruction"),
        (completion("sk-TEST_FIXTURE_NOT_A_REAL_KEY_123456 检查结果"), "credential-like text"),
        (
            completion("Authorization: Bearer fake-sensitive-bearer 检查结果"),
            "credential-like text",
        ),
        (completion('{"api_key": "fake-sensitive-json-key"} 检查结果'), "credential-like text"),
        (completion("检查结果" * 2500), "empty or oversized prompt"),
    ],
)
def test_invalid_drafts_are_rejected(client, settings, monkeypatch, body, message):
    _, branch = create_work(client)
    history(client, branch["id"])
    settings.oneapi_api_key = "test"
    mock_api(monkeypatch, lambda request: httpx.Response(200, json=body))
    response = client.post(f"/api/branches/{branch['id']}/scheduled-check/prompt", json={})
    assert response.status_code == 503
    assert message in response.text


def test_reasoning_only_truncation_is_safe_not_cached_and_never_falls_back(
    client, settings, monkeypatch, caplog
):
    _, branch = create_work(client)
    history(client, branch["id"])
    settings.oneapi_api_key = "test-oneapi-secret"
    settings.oneapi_base_url = "https://oneapi.invalid/v1"
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url) == "https://oneapi.invalid/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-oneapi-secret"
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": "", "reasoning_content": "PRIVATE_REASONING"},
                        }
                    ],
                    "usage": {"completion_tokens": 2048},
                },
            )
        return httpx.Response(200, json=completion())

    mock_api(monkeypatch, handler)
    url = f"/api/branches/{branch['id']}/scheduled-check/prompt"
    failed = client.post(url, json={})
    assert failed.status_code == 503 and "exhausted its output budget" in failed.text
    assert "exhausted its output budget" in caplog.text
    assert len(seen) == 1
    for secret in ("PRIVATE_REASONING", "test-oneapi-secret"):
        assert secret not in failed.text and secret not in caplog.text
    recovered = client.post(url, json={})
    assert recovered.status_code == 200 and recovered.json()["prompt"] == DRAFT
    assert not recovered.json()["cached"] and len(seen) == 2
    assert all(json.loads(request.content)["thinking"] == {"type": "disabled"} for request in seen)


def test_deepseek_drafting_option_does_not_leak_to_other_models(client, settings, monkeypatch):
    _, branch = create_work(client)
    history(client, branch["id"])
    settings.oneapi_api_key = "test"
    settings.oneapi_model = "another-draft-model"
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    mock_api(monkeypatch, handler)
    response = client.post(f"/api/branches/{branch['id']}/scheduled-check/prompt", json={})
    assert response.status_code == 200
    assert "thinking" not in seen[0]


def test_timeout_has_actionable_error(client, settings, monkeypatch):
    _, branch = create_work(client)
    history(client, branch["id"])
    settings.oneapi_api_key = "test"

    def handler(request):
        raise httpx.ReadTimeout("secret-sensitive-details", request=request)

    mock_api(monkeypatch, handler)
    response = client.post(f"/api/branches/{branch['id']}/scheduled-check/prompt", json={})
    assert response.status_code == 503
    assert "timed out" in response.text and "secret-sensitive-details" not in response.text


async def test_parallel_reopens_coalesce_and_new_context_invalidates_cache(
    client, settings, monkeypatch
):
    _, branch = create_work(client)
    history(client, branch["id"])
    settings.oneapi_api_key = "test"
    service = client.app.state.rops.scheduled_check_prompts
    entered, finish = asyncio.Event(), asyncio.Event()
    seen = []

    async def complete(context):
        seen.append(context)
        entered.set()
        await finish.wait()
        return DRAFT

    monkeypatch.setattr(service, "_complete", complete)
    payload = schemas.ScheduledCheckPromptRequest()
    first = asyncio.create_task(service.generate(branch["id"], payload))
    second = asyncio.create_task(service.generate(branch["id"], payload))
    await asyncio.wait_for(entered.wait(), 2)
    finish.set()
    assert (await first)["prompt"] == (await second)["prompt"]
    assert (await service.generate(branch["id"], payload))["cached"]
    assert len(seen) == 1
    history(client, branch["id"], age=0, answer="New result")
    assert not (await service.generate(branch["id"], payload))["cached"]
    assert len(seen) == 2
    await service.generate(
        branch["id"], schemas.ScheduledCheckPromptRequest(current_prompt="changed")
    )
    assert len(seen) == 3


async def test_cancelled_caller_does_not_poison_next_draft(client, settings, monkeypatch):
    _, branch = create_work(client)
    history(client, branch["id"])
    settings.oneapi_api_key = "test"
    service = client.app.state.rops.scheduled_check_prompts
    entered, finish = asyncio.Event(), asyncio.Event()

    async def complete(_context):
        entered.set()
        await finish.wait()
        return DRAFT

    monkeypatch.setattr(service, "_complete", complete)
    task = asyncio.create_task(
        service.generate(branch["id"], schemas.ScheduledCheckPromptRequest())
    )
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    finish.set()
    assert (await service.generate(branch["id"], schemas.ScheduledCheckPromptRequest()))[
        "prompt"
    ] == DRAFT

import asyncio
import json
import os
import stat

import httpx
import pytest
from research_ops import models, schemas
from research_ops.codex.base import CodexUnavailable
from research_ops.codex.identity import (
    API_ENV_KEY,
    ONEAPI_ENV_KEY,
    CodexIdentityService,
    ExecutionPreferences,
)
from research_ops.codex.identity_pool import IdentityCodexPool
from research_ops.services import model_catalog
from research_ops.services.model_catalog import CodexModelCatalog
from sqlalchemy import select
from test_api_workflow import create_work
from test_native_goals import _materialized_branch


@pytest.fixture
def dual_auth(settings, monkeypatch):
    monkeypatch.delenv("CODEX_SQLITE_HOME", raising=False)
    settings.codex_model_provider = None
    settings.codex_home.mkdir(parents=True, exist_ok=True)
    (settings.codex_home / "config.toml").write_text(
        'model_provider="gateway"\n[model_providers.gateway]\n'
        'name="Company"\nbase_url="https://company.invalid/v1"\n'
        'wire_api="responses"\nrequires_openai_auth=true\n'
    )
    auth = settings.codex_home / "auth.json"
    auth.write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "test-api-secret"}))
    for name in (API_ENV_KEY, "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    identities = CodexIdentityService(settings)
    identities.preserve_api_key()
    identities.prepare_personal_home()
    (identities.personal_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": "test-oauth-secret"},
            }
        )
    )
    return identities


def test_credentials_coexist_without_appearing_in_state(dual_auth, monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "inherited-wrong-key")
    monkeypatch.setenv("OPENAI_API_KEY", "another-wrong-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong.invalid")
    status = dual_auth.status()
    assert all(row["ready"] for row in status["identities"] if row["id"] != "oneapi")
    assert not next(row["ready"] for row in status["identities"] if row["id"] == "oneapi")
    assert stat.S_IMODE(dual_auth.api_auth_path.stat().st_mode) == 0o600
    api = dual_auth.snapshot()
    personal = dual_auth.snapshot({"identity": "chatgpt", "model": "personal-model"})
    api_launch = dual_auth.launch(api)
    personal_launch = dual_auth.launch(personal)
    assert api_launch.environment()[API_ENV_KEY] == "test-api-secret"
    assert API_ENV_KEY not in personal_launch.environment()
    for name in ("CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL"):
        assert name not in personal_launch.environment()
        assert name not in api_launch.environment()
    args = api_launch.args()
    assert "model_providers.gateway.requires_openai_auth=false" in args
    assert 'cli_auth_credentials_store="ephemeral"' in args
    assert 'model_provider="openai"' in personal_launch.args()
    assert 'forced_login_method="chatgpt"' in personal_launch.args()
    public = json.dumps([status, api, personal, args, personal_launch.args()])
    assert "test-api-secret" not in public
    assert "test-oauth-secret" not in public
    assert os.environ["CODEX_API_KEY"] == "inherited-wrong-key"
    assert api_launch.environment()["CODEX_HOME"] != personal_launch.environment()["CODEX_HOME"]
    assert (
        api_launch.environment()["CODEX_SQLITE_HOME"]
        == (personal_launch.environment()["CODEX_SQLITE_HOME"])
    )
    original = json.loads((dual_auth.settings.codex_home / "auth.json").read_text())
    assert original == {"auth_mode": "apikey", "OPENAI_API_KEY": "test-api-secret"}
    for name in ("sessions", "thread-writer-locks"):
        assert (dual_auth.personal_home / name).resolve() == (
            dual_auth.settings.codex_home / name
        ).resolve()


def test_missing_login_never_uses_api_as_personal(dual_auth, settings):
    (dual_auth.personal_home / "auth.json").unlink()
    launch = dual_auth.launch(dual_auth.snapshot({"identity": "chatgpt", "model": "account-model"}))
    with pytest.raises(CodexUnavailable, match="not signed in"):
        launch.environment()
    assert dual_auth.launch(dual_auth.snapshot()).environment()[API_ENV_KEY] == "test-api-secret"


def test_personal_proxy_applies_to_login_and_execution_only(dual_auth, settings, monkeypatch):
    settings.codex_personal_http_proxy = "http://personal-proxy.invalid:8891"
    settings.codex_personal_https_proxy = "http://personal-proxy.invalid:8891"
    settings.codex_personal_no_proxy = "localhost,127.0.0.1"
    inherited = "http://original-proxy.invalid:8080"
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.setenv(name, inherited)
    monkeypatch.setenv("no_proxy", "original.invalid")
    monkeypatch.setenv("NO_PROXY", "original.invalid")
    for login in (False, True):
        personal = dual_auth.environment("chatgpt", login=login)
        for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            assert personal[name] == settings.codex_personal_https_proxy
        assert personal["no_proxy"] == personal["NO_PROXY"] == "localhost,127.0.0.1"
    api = dual_auth.environment("api")
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        assert api[name] == inherited
        assert os.environ[name] == inherited
    assert api["NO_PROXY"] == os.environ["NO_PROXY"] == "original.invalid"


def test_unconfigured_personal_proxy_preserves_shell_environment(dual_auth, settings, monkeypatch):
    settings.codex_personal_http_proxy = None
    settings.codex_personal_https_proxy = None
    settings.codex_personal_no_proxy = None
    monkeypatch.setenv("https_proxy", "http://shell-proxy.invalid:8080")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    env = dual_auth.environment("chatgpt", login=True)
    assert env["https_proxy"] == "http://shell-proxy.invalid:8080"
    assert "HTTPS_PROXY" not in env


def test_unknown_api_provider_does_not_fall_through_to_openai(dual_auth, settings):
    settings.codex_model_provider = "missing"
    with pytest.raises(CodexUnavailable, match="missing from"):
        dual_auth.snapshot()


def test_personal_model_must_be_explicit(dual_auth):
    with pytest.raises(CodexUnavailable, match="Choose a model"):
        dual_auth.snapshot({"identity": "chatgpt"})


def test_advertised_ultra_effort_is_preserved(dual_auth):
    execution = dual_auth.snapshot(
        {"identity": "chatgpt", "model": "account-model", "reasoning_effort": "ultra"}
    )
    assert execution["reasoning_effort"] == "ultra"
    assert 'model_reasoning_effort="ultra"' in dual_auth.launch(execution).args()


def test_provider_change_does_not_reuse_credentials_at_a_stale_endpoint(dual_auth, settings):
    launch = dual_auth.launch(dual_auth.snapshot())
    path = settings.codex_home / "config.toml"
    path.write_text(path.read_text().replace("company.invalid", "replacement.invalid"))
    with pytest.raises(CodexUnavailable, match="provider changed"):
        launch.environment()


def test_branch_selection_persists_and_does_not_start_a_turn(client, dual_auth):
    _, branch = create_work(client)
    endpoint = f"/api/branches/{branch['id']}/execution"
    response = client.put(
        endpoint,
        json={
            "identity": "chatgpt",
            "model": "my-account-model",
            "reasoning_effort": "high",
        },
    )
    assert response.status_code == 200, response.text
    assert client.get(endpoint).json()["effective"]["identity"] == "chatgpt"
    with client.app.state.rops.database.session_factory() as session:
        stored = session.get(models.ExecutionBranch, branch["id"])
        assert stored.execution_preferences["identity"] == "chatgpt"
        assert not list(session.scalars(select(models.ConversationMessage)))
        assert not list(session.scalars(select(models.SessionAttempt)))
        event = session.scalar(
            select(models.ActivityEvent).where(
                models.ActivityEvent.event_type == "execution.identity_selected"
            )
        )
        assert event.payload["identity"] == "chatgpt"
        assert "secret" not in json.dumps(event.payload)
    invalid = client.put(endpoint, json={"identity": "auto", "model": "anything"})
    assert invalid.status_code == 422
    invalid = client.put(endpoint, json={"identity": "api", "api_key": "must-not-be-accepted"})
    assert invalid.status_code == 422


@pytest.mark.parametrize("next_identity", ["chatgpt", "oneapi"])
async def test_running_turn_and_retry_keep_frozen_identity(
    client, fake_codex, settings, oneapi_auth, monkeypatch, next_identity
):
    _, branch = create_work(client)
    service = client.app.state.rops.service
    started = asyncio.Event()
    release = asyncio.Event()
    executions = []
    calls = 0
    settings.codex_capacity_retry_initial_seconds = 0.01
    settings.codex_capacity_retry_max_seconds = 0.01

    def select_execution(execution):
        executions.append(dict(execution))
        return fake_codex

    async def run(**_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        if calls == 1:
            raise CodexUnavailable("Selected model is at capacity. Please try a different model.")
        return {"turn": {"id": f"turn-{calls}"}}

    monkeypatch.setattr(fake_codex, "for_execution", select_execution, raising=False)
    monkeypatch.setattr(fake_codex, "send_message", run)
    task = asyncio.create_task(
        service.send_message(branch["id"], schemas.ConversationSend(content="test"))
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    before = executions[-1]
    service.update_branch_execution(
        branch["id"],
        ExecutionPreferences(
            identity=next_identity,
            model="my-next-model",
            reasoning_effort="high",
        ),
    )
    settings.codex_model = "changed-default"
    release.set()
    message = await asyncio.wait_for(task, timeout=10)
    assert calls == 2
    assert message.metadata_json["execution"] == before
    assert executions[-1] == before
    assert before["identity"] == "api"
    next_message = await service.send_message(
        branch["id"], schemas.ConversationSend(content="next")
    )
    assert next_message.metadata_json["execution"]["identity"] == next_identity
    assert executions[-1]["model"] == "my-next-model"


@pytest.mark.parametrize("next_identity", ["chatgpt", "oneapi"])
def test_native_goal_keeps_its_identity_after_manual_next_turn_change(
    client, fake_codex, oneapi_auth, monkeypatch, next_identity
):
    executions = []

    def select_execution(execution):
        executions.append(dict(execution))
        return fake_codex

    monkeypatch.setattr(fake_codex, "for_execution", select_execution, raising=False)
    branch = _materialized_branch(client, fake_codex)
    endpoint = f"/api/branches/{branch['id']}"
    created = client.put(f"{endpoint}/native-goal", json={"objective": "Original goal"})
    assert created.status_code == 200, created.text
    changed = client.put(
        f"{endpoint}/execution", json={"identity": next_identity, "model": "next-model"}
    )
    assert changed.status_code == 200, changed.text
    assert client.get(f"{endpoint}/native-goal").status_code == 200
    assert executions[-1]["identity"] == "api"
    assert client.get(f"{endpoint}/execution").json()["effective"]["identity"] == next_identity


async def test_web_query_steers_goal_owner_after_next_model_changes(
    client, fake_codex, dual_auth, monkeypatch
):
    executions = []

    def select_execution(execution):
        executions.append(dict(execution))
        return fake_codex

    monkeypatch.setattr(fake_codex, "for_execution", select_execution, raising=False)
    branch = _materialized_branch(client, fake_codex)
    endpoint = f"/api/branches/{branch['id']}"
    created = client.put(endpoint + "/native-goal", json={"objective": "Keep working"})
    assert created.status_code == 200
    owner = dict(executions[-1])
    assert client.put(endpoint + "/execution", json={
        "identity": "api", "model": "different-next-model", "reasoning_effort": "low",
    }).status_code == 200
    state = client.app.state.rops
    sent = await state.service.send_message(
        branch["id"], schemas.ConversationSend(content="Change direction")
    )
    assert executions[-1] == owner
    assert sent.metadata_json["execution"] == owner
    with state.database.session_factory() as session:
        attempt = session.get(models.SessionAttempt, sent.session_id)
        assert attempt.execution_config == owner
    selected = client.get(endpoint + "/execution").json()["effective"]
    assert selected["model"] == "different-next-model"


def test_web_query_never_silently_uses_old_account_for_active_goal(
    client, fake_codex, dual_auth
):
    branch = _materialized_branch(client, fake_codex)
    endpoint = f"/api/branches/{branch['id']}"
    created = client.put(endpoint + "/native-goal", json={"objective": "Keep working"})
    assert created.status_code == 200
    assert client.put(endpoint + "/execution", json={
        "identity": "chatgpt", "model": "personal-model",
    }).status_code == 200
    response = client.post(endpoint + "/messages", json={"content": "Change direction"})
    assert response.status_code == 409
    assert "Pause the active Goal before switching" in response.text


def test_identity_pool_never_mutates_existing_adapter(dual_auth, settings):
    async def ignore(*_args):
        pass

    pool = IdentityCodexPool(settings, event_handler=ignore, request_handler=ignore)
    first = dual_auth.snapshot()
    api = pool.for_execution(first)
    personal = pool.for_execution(dual_auth.snapshot({"identity": "chatgpt", "model": "personal"}))
    assert api is not personal
    assert api.lifecycle is not personal.lifecycle
    settings.codex_model = "new-default"
    next_api = pool.for_execution(dual_auth.snapshot())
    assert next_api is not api
    assert api.settings.codex_model == first["model"]
    assert pool.for_execution(first) is api


async def test_model_catalog_is_isolated_cached_and_never_starts_a_thread(
    dual_auth, settings, monkeypatch
):
    instances = []

    class CatalogAdapter:
        def __init__(self, _settings, *, launch, **_kwargs):
            self.identity = launch.execution["identity"]
            self.pages = []
            self.stopped = False
            instances.append(self)

        async def start(self):
            pass

        async def list_models(self, *, cursor=None):
            self.pages.append(cursor)
            if cursor:
                return {"data": [{"model": "hidden", "hidden": True}], "nextCursor": None}
            return {
                "data": [
                    {
                        "model": self.identity + "-model",
                        "displayName": self.identity + " model",
                        "isDefault": True,
                        "defaultReasoningEffort": "max",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "max"},
                            {"reasoningEffort": "ultra"},
                            {"reasoningEffort": "max"},
                            {"reasoningEffort": "unknown-effort"},
                        ],
                    }
                ],
                "nextCursor": "second",
            }

        async def stop(self):
            self.stopped = True

    monkeypatch.setattr(model_catalog, "AppServerCodexAdapter", CatalogAdapter)
    catalog = CodexModelCatalog(settings)
    first, duplicate = await asyncio.gather(
        catalog.list_models("chatgpt"), catalog.list_models("chatgpt")
    )
    assert first == duplicate
    assert len(instances) == 1
    assert instances[0].pages == [None, "second"]
    assert instances[0].stopped
    assert first["models"][0]["reasoning_efforts"] == ["max", "ultra"]
    assert first["models"][0]["default_reasoning_effort"] == "max"
    assert not first["allow_custom"]
    first["models"].clear()
    assert (await catalog.list_models("chatgpt"))["models"]
    api = await catalog.list_models("api")
    assert api["models"][0]["id"] == "api-model"
    assert api["allow_custom"]
    assert len(instances) == 2
    await catalog.list_models("chatgpt", refresh=True)
    assert len(instances) == 3
    (dual_auth.personal_home / "auth.json").unlink()
    with pytest.raises(CodexUnavailable, match="not signed in"):
        await catalog.list_models("chatgpt")
    assert all(instance.stopped for instance in instances)


async def test_model_catalog_closes_failed_adapter_and_redacts_provider_errors(
    dual_auth, settings, monkeypatch
):
    stopped = []

    class FailedAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def start(self):
            pass

        async def list_models(self, **_kwargs):
            raise CodexUnavailable("Authorization: private-token")

        async def stop(self):
            stopped.append(True)

    monkeypatch.setattr(model_catalog, "AppServerCodexAdapter", FailedAdapter)
    with pytest.raises(CodexUnavailable, match="Unable to load models") as error:
        await CodexModelCatalog(settings).list_models("chatgpt")
    assert "private-token" not in str(error.value)
    assert stopped == [True]


def test_model_catalog_endpoint_reads_without_changing_branch(client, dual_auth, monkeypatch):
    _, branch = create_work(client)
    calls = []

    async def fetch(execution):
        calls.append(execution["identity"])
        return [
            {
                "id": "account-model",
                "name": "Account model",
                "reasoning_efforts": ["high"],
                "default_reasoning_effort": "high",
                "is_default": True,
            }
        ]

    monkeypatch.setattr(client.app.state.rops.model_catalog, "_fetch", fetch)
    url = "/api/settings/identities/chatgpt/models"
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.json()["models"][0]["id"] == "account-model"
    assert client.get(url).status_code == 200
    assert calls == ["chatgpt"]
    assert client.get(url + "?refresh=true").status_code == 200
    assert calls == ["chatgpt", "chatgpt"]
    assert client.get("/api/settings/identities/automatic/models").status_code == 422
    selection = client.get(f"/api/branches/{branch['id']}/execution").json()
    assert selection["effective"]["identity"] == "api"
    with client.app.state.rops.database.session_factory() as session:
        assert not list(session.scalars(select(models.ConversationMessage)))
        assert not list(session.scalars(select(models.SessionAttempt)))


@pytest.fixture
def oneapi_auth(dual_auth, settings):
    settings.oneapi_api_key = "test-oneapi-secret"
    settings.oneapi_base_url = "https://oneapi.invalid/v1"
    return dual_auth


def test_oneapi_credentials_are_isolated_and_defaults_are_provider_specific(
    oneapi_auth, settings, monkeypatch
):
    monkeypatch.setenv(ONEAPI_ENV_KEY, "inherited-wrong-key")
    monkeypatch.setenv(API_ENV_KEY, "inherited-company-key")
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-openai-key")
    monkeypatch.setenv("NO_PROXY", "localhost")
    settings.codex_personal_https_proxy = "http://personal-proxy.invalid:8080"
    execution = oneapi_auth.snapshot({"identity": "oneapi"})
    assert execution["model"] == "DeepSeek-V4.1-Flash"
    assert execution["reasoning_effort"] == "high"
    assert execution["provider"] == "rops-oneapi"
    assert execution["provider_config"]["env_key"] == ONEAPI_ENV_KEY
    launch = oneapi_auth.launch(execution)
    env = launch.environment()
    assert env[ONEAPI_ENV_KEY] == settings.oneapi_api_key
    assert API_ENV_KEY not in env
    assert "OPENAI_API_KEY" not in env
    assert "oneapi.invalid" in env["NO_PROXY"].split(",")
    assert "localhost" in env["NO_PROXY"].split(",")
    assert env.get("https_proxy") != settings.codex_personal_https_proxy
    assert 'cli_auth_credentials_store="ephemeral"' in launch.args()
    assert "model_providers.rops-oneapi.requires_openai_auth=false" in launch.args()
    for identity in ("api", "chatgpt"):
        assert ONEAPI_ENV_KEY not in oneapi_auth.environment(identity)
    public = json.dumps(
        [oneapi_auth.status(), execution, launch.args(), settings.model_dump()], default=str
    )
    assert "test-oneapi-secret" not in public
    assert "test-oneapi-secret" not in repr(settings)
    assert oneapi_auth.snapshot()["identity"] == "api"
    assert os.environ[ONEAPI_ENV_KEY] == "inherited-wrong-key"


def test_oneapi_does_not_fall_back_to_existing_credentials(dual_auth):
    execution = dual_auth.snapshot({"identity": "oneapi"})
    with pytest.raises(CodexUnavailable, match="OneAPI credentials"):
        dual_auth.launch(execution).environment()


def test_oneapi_endpoint_change_rejects_frozen_launch(oneapi_auth, settings):
    launch = oneapi_auth.launch(oneapi_auth.snapshot({"identity": "oneapi"}))
    settings.oneapi_base_url = "https://replacement.invalid/v1"
    with pytest.raises(CodexUnavailable, match="provider changed"):
        launch.environment()


@pytest.mark.parametrize(
    "url",
    [
        "https://secret@oneapi.invalid/v1",
        "https://oneapi.invalid/v1?key=secret",
        "file:///etc",
    ],
)
def test_oneapi_rejects_credentials_in_endpoint(oneapi_auth, settings, url):
    settings.oneapi_base_url = url
    with pytest.raises(CodexUnavailable, match="without credentials"):
        oneapi_auth.snapshot({"identity": "oneapi"})


@pytest.mark.parametrize("effort", [
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
])
def test_oneapi_manual_effort_survives_save_and_launch(client, oneapi_auth, effort):
    _, branch = create_work(client)
    endpoint = f"/api/branches/{branch['id']}/execution"
    response = client.put(endpoint, json={
        "identity": "oneapi", "model": "DeepSeek-V4.1-Flash", "reasoning_effort": effort,
    })
    assert response.status_code == 200, response.text
    execution = client.get(endpoint).json()["effective"]
    assert execution["reasoning_effort"] == effort
    assert f'model_reasoning_effort="{effort}"' in oneapi_auth.launch(execution).args()
    with client.app.state.rops.database.session_factory() as session:
        assert not list(session.scalars(select(models.ConversationMessage)))
        assert not list(session.scalars(select(models.SessionAttempt)))


def test_oneapi_selection_changes_only_next_turn(client, oneapi_auth):
    _, branch = create_work(client)
    endpoint = f"/api/branches/{branch['id']}/execution"
    response = client.put(endpoint, json={"identity": "oneapi"})
    assert response.status_code == 200, response.text
    assert response.json()["effective"]["model"] == "DeepSeek-V4.1-Flash"
    assert response.json()["effective"]["identity"] == "oneapi"
    assert "test-oneapi-secret" not in response.text
    with client.app.state.rops.database.session_factory() as session:
        stored = session.get(models.ExecutionBranch, branch["id"])
        assert stored.execution_preferences["identity"] == "oneapi"
        assert not list(session.scalars(select(models.ConversationMessage)))
        assert not list(session.scalars(select(models.SessionAttempt)))


def test_oneapi_pool_does_not_reuse_api_or_personal_adapter(oneapi_auth, settings):
    async def ignore(*_args):
        pass

    pool = IdentityCodexPool(settings, event_handler=ignore, request_handler=ignore)
    execution = oneapi_auth.snapshot({"identity": "oneapi"})
    oneapi = pool.for_execution(execution)
    assert oneapi.settings.codex_model == "DeepSeek-V4.1-Flash"
    assert oneapi is pool.for_execution(execution)
    for identity in ("api", "chatgpt"):
        other = pool.for_execution(oneapi_auth.snapshot({"identity": identity, "model": "other"}))
        assert other is not oneapi
        assert other.lifecycle is not oneapi.lifecycle


async def test_oneapi_catalog_uses_own_endpoint_and_key_without_codex(
    oneapi_auth, settings, monkeypatch
):
    requests = []
    client_class = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "Another-model"},
                    {"id": "DeepSeek-V4.1-Flash"},
                    {"id": "DeepSeek-V4.1-Flash"},
                    {"id": ""},
                    {"bad": "row"},
                ]
            },
        )

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return client_class(transport=httpx.MockTransport(respond), **kwargs)

    def forbidden(*_args, **_kwargs):
        pytest.fail("OneAPI catalog must not start a Codex process")

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", client)
    monkeypatch.setattr(model_catalog, "AppServerCodexAdapter", forbidden)
    catalog = CodexModelCatalog(settings)
    result = await catalog.list_models("oneapi")
    assert result["identity"] == "oneapi" and result["allow_custom"]
    assert [row["id"] for row in result["models"]] == ["DeepSeek-V4.1-Flash", "Another-model"]
    assert result["models"][0]["is_default"]
    assert result["models"][0]["reasoning_efforts"] == []
    assert str(requests[0].url) == "https://oneapi.invalid/v1/models"
    assert requests[0].headers["authorization"] == "Bearer test-oneapi-secret"
    assert await catalog.list_models("oneapi") == result
    assert len(requests) == 1
    settings.oneapi_api_key = "rotated-oneapi-secret"
    assert await catalog.list_models("oneapi") == result
    assert len(requests) == 2
    assert requests[-1].headers["authorization"] == "Bearer rotated-oneapi-secret"
    settings.oneapi_api_key = None
    with pytest.raises(CodexUnavailable, match="OneAPI credentials"):
        await catalog.list_models("oneapi")


@pytest.mark.parametrize(
    "status,body",
    [
        (401, b"test-oneapi-secret"),
        (302, b"test-oneapi-secret"),
        (200, b"not json"),
        (200, b"[]"),
        (200, b'{"data":[]}'),
        (200, b"x" * (2 * 1024 * 1024 + 1)),
    ],
)
async def test_oneapi_catalog_errors_are_safe(oneapi_auth, settings, monkeypatch, status, body):
    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _request: httpx.Response(status, content=body))
    monkeypatch.setattr(
        model_catalog.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(transport=transport, **kwargs),
    )
    with pytest.raises(CodexUnavailable, match="Unable to load OneAPI models") as error:
        await CodexModelCatalog(settings).list_models("oneapi")
    assert "test-oneapi-secret" not in str(error.value)


def test_oneapi_catalog_api_route_is_read_only(client, oneapi_auth, monkeypatch):
    calls = []

    async def fetch(execution):
        calls.append(execution["identity"])
        return [{"id": "DeepSeek-V4.1-Flash", "reasoning_efforts": []}]

    monkeypatch.setattr(client.app.state.rops.model_catalog, "_fetch", fetch)
    response = client.get("/api/settings/identities/oneapi/models")
    assert response.status_code == 200, response.text
    assert calls == ["oneapi"]
    assert "test-oneapi-secret" not in response.text
    with client.app.state.rops.database.session_factory() as session:
        assert not list(session.scalars(select(models.ConversationMessage)))
        assert not list(session.scalars(select(models.SessionAttempt)))

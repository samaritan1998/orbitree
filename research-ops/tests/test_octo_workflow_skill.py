from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/octo-workflow/scripts/octo_workflow.py"
spec = importlib.util.spec_from_file_location("octo_workflow_skill", SCRIPT)
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)


@pytest.fixture
def document():
    return {
        "job_id": "fixture-workflow",
        "meta": {"title": "Fixture evaluation"},
        "nodes": [{"name": "prepare", "status": "succeeded"}, {"name": "evaluate"}],
        "edges": [{"source": "prepare", "target": "evaluate"}],
    }


@pytest.fixture
def server():
    state = {"document": None, "calls": [], "conflict": False, "redirect": False, "revision": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, payload):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_GET(self):
            state["calls"].append(("GET", self.path, None))
            assert self.headers["Authorization"] == "Bearer fixture-secret"
            if state["redirect"]:
                self.send_response(302)
                self.send_header("Location", "/unexpected-credential-target")
                self.end_headers()
            elif state["document"] is None:
                self.reply(404, {})
            else:
                self.reply(200, state["document"])

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append(("POST", self.path, body))
            state["document"] = {**body, "status": "pending", "updated_at": "2026-09-16T00:00:00Z"}
            for node in state["document"]["nodes"]:
                node.update(started_at=None, finished_at=None)
            self.reply(200, state["document"])

        def do_PUT(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append(("PUT", self.path, body))
            assert self.headers["Authorization"] == "Bearer fixture-secret"
            if state["conflict"] or body["expected_updated_at"] != state["document"]["updated_at"]:
                self.reply(409, {})
                return
            if self.path.endswith("/definition"):
                assert set(body) <= {
                    "nodes",
                    "edges",
                    "expected_updated_at",
                    "current_stage",
                    "message",
                }
                old_nodes = {node["name"]: node for node in state["document"]["nodes"]}
                nodes = []
                for node in body["nodes"]:
                    assert set(node) == {"name", "meta"}
                    previous = old_nodes.get(
                        node["name"], {"status": "pending", "started_at": None, "finished_at": None}
                    )
                    nodes.append({**previous, **node})
                state["document"].update(nodes=nodes, edges=body["edges"])
            else:
                assert self.path.endswith("/advance")
                for update in body["node_updates"]:
                    node = next(
                        n for n in state["document"]["nodes"] if n["name"] == update["name"]
                    )
                    node.update(update)
            if "current_stage" in body:
                state["document"]["current_stage"] = body["current_stage"]
            state["revision"] += 1
            state["document"]["updated_at"] = f"2026-09-16T00:00:{state['revision']:02d}Z"
            self.reply(200, state["document"])

    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=instance.serve_forever, daemon=True)
    worker.start()
    try:
        yield skill.Client(f"http://127.0.0.1:{instance.server_port}", "fixture-secret"), state
    finally:
        instance.shutdown()
        instance.server_close()
        worker.join(timeout=2)


def test_create_is_idempotent_and_does_not_overwrite(server, document):
    client, state = server
    _, action = client.create(document)
    assert action == "created"
    _, action = client.create(document)
    assert action == "unchanged"
    changed = copy.deepcopy(document)
    changed["nodes"][0]["status"] = "failed"
    with pytest.raises(ValueError, match="already exists"):
        client.create(changed)
    assert [method for method, _, _ in state["calls"]].count("POST") == 1
    assert all(path.startswith("/api/v1/workflows") for _, path, _ in state["calls"])


def test_update_uses_concurrency_guard_and_does_not_retry_conflicts(server, document):
    client, state = server
    client.create(document)
    payload = {
        "current_stage": "evaluate",
        "node_updates": [{"name": "evaluate", "status": "running"}],
    }
    result, _ = client.update(document["job_id"], payload)
    assert result["nodes"][1]["status"] == "running"
    put = next(body for method, _, body in state["calls"] if method == "PUT")
    assert put["expected_updated_at"] == "2026-09-16T00:00:00Z"
    state["conflict"] = True
    with pytest.raises(skill.ApiError) as error:
        client.update(document["job_id"], payload)
    assert error.value.status == 409
    assert [method for method, _, _ in state["calls"]].count("PUT") == 2


def test_no_credential_forwarding_on_redirect(server):
    client, state = server
    state["redirect"] = True
    with pytest.raises(skill.ApiError):
        client.show("fixture")
    assert len(state["calls"]) == 1


def test_extend_preserves_existing_state_and_is_idempotent(server, document):
    client, state = server
    client.create(document)
    state["document"]["nodes"][0].update(
        started_at="2026-09-15T00:00:00Z",
        finished_at="2026-09-15T00:01:00Z",
        meta={"evidence": {"file": "baseline.json", "samples": 24}},
    )
    state["document"]["current_stage"] = "evaluate"
    state["document"]["nodes"][1]["status"] = "running"
    before = copy.deepcopy(state["document"])
    additions = {
        "nodes": [
            {"name": "prepare"},
            {"name": "baseline-a", "meta": {"description": "Formal baseline"}},
            {"name": "memory-095"},
        ],
        "edges": [
            {"source": "prepare", "target": "baseline-a"},
            {"source": "prepare", "target": "memory-095"},
            {"source": "prepare", "target": "memory-095"},
        ],
        "message": "Add experiments",
    }
    result, action = client.extend(document["job_id"], additions)
    assert action == "extended"
    assert result["nodes"][:2] == before["nodes"]
    assert result["meta"] == before["meta"]
    assert result["current_stage"] == "evaluate"
    assert result["edges"] == before["edges"] + additions["edges"][:2]
    assert [node["status"] for node in result["nodes"][2:]] == ["pending", "pending"]
    definition = next(body for method, _, body in state["calls"] if method == "PUT")
    assert definition["expected_updated_at"] == before["updated_at"]
    assert all(set(node) == {"name", "meta"} for node in definition["nodes"])
    client.update(
        document["job_id"],
        {
            "current_stage": "baseline-a",
            "node_updates": [{"name": "baseline-a", "status": "running"}],
        },
    )
    result, action = client.extend(document["job_id"], additions)
    assert action == "unchanged"
    assert result["nodes"][2]["status"] == "running"
    assert result["current_stage"] == "baseline-a"
    assert (
        sum(method == "PUT" and path.endswith("/definition") for method, path, _ in state["calls"])
        == 1
    )


def test_extend_can_add_only_edges_and_preserves_inputs(server, document):
    client, state = server
    document["nodes"].append({"name": "report"})
    client.create(document)
    before = copy.deepcopy(state["document"])
    addition = {"edges": [{"source": "evaluate", "target": "report"}]}
    definition = skill.extension_definition(before, addition)
    assert before == state["document"]
    assert definition["edges"][-1] == addition["edges"][0]
    result, action = client.extend(document["job_id"], addition)
    assert action == "extended"
    assert result["nodes"] == before["nodes"]
    assert result["edges"] == before["edges"] + addition["edges"]


def test_extend_dry_run_validates_without_writing(server, document):
    client, state = server
    client.create(document)
    before = copy.deepcopy(state["document"])
    state["calls"].clear()
    result, action = client.extend(
        document["job_id"],
        {
            "nodes": [{"name": "extra"}],
            "edges": [{"source": "evaluate", "target": "extra"}],
        },
        dry_run=True,
    )
    assert action == "planned"
    assert result["nodes"][-1] == {"name": "extra", "meta": {}, "status": "pending"}
    assert state["document"] == before
    assert [method for method, _, _ in state["calls"]] == ["GET"]


@pytest.mark.parametrize(
    "payload",
    [
        {"nodes": [{"name": "extra", "status": "succeeded"}]},
        {"nodes": [{"name": "extra", "meta": []}]},
        {"nodes": [{"name": "extra"}, {"name": "extra"}]},
        {"nodes": [{"name": "prepare", "meta": {"changed": True}}]},
        {"nodes": [{"name": "extra", "meta": {"api_key": "private"}}]},
        {"edges": [{"source": "evaluate", "target": "prepare"}]},
        {"edges": [{"source": "prepare", "target": "missing"}]},
        {"edges": [{"source": "prepare", "target": "prepare"}]},
        {"edges": [{"source": "prepare", "target": "evaluate", "label": "extra"}]},
        {"nodes": "extra"},
        {"edges": "extra"},
        {"message": ["not a string"]},
        {"message": "x" * 10001},
        {"remove_nodes": ["prepare"]},
        {"current_stage": "evaluate"},
    ],
)
def test_invalid_extension_never_writes(server, document, payload):
    client, state = server
    client.create(document)
    before = copy.deepcopy(state["document"])
    state["calls"].clear()
    with pytest.raises(ValueError):
        client.extend(document["job_id"], payload)
    assert state["document"] == before
    assert all(method == "GET" for method, _, _ in state["calls"])


def test_extension_limit_applies_to_merged_graph():
    current = {
        "job_id": "large",
        "nodes": [{"name": f"node-{index}", "meta": {}} for index in range(200)],
        "edges": [],
        "updated_at": "2026-09-16T00:00:00Z",
    }
    with pytest.raises(ValueError, match="1 to 200"):
        skill.extension_definition(current, {"nodes": [{"name": "one-too-many"}]})
    current["nodes"] = current["nodes"][:70]
    current["edges"] = [
        {"source": f"node-{source}", "target": f"node-{target}"}
        for target in range(70)
        for source in range(target)
    ][:2000]
    with pytest.raises(ValueError, match="2000 edges"):
        skill.extension_definition(current, {"edges": [{"source": "node-68", "target": "node-69"}]})


def test_extend_rejects_conflict_without_retry(server, document):
    client, state = server
    client.create(document)
    before = copy.deepcopy(state["document"])
    state["calls"].clear()
    state["conflict"] = True
    with pytest.raises(skill.ApiError) as exc:
        client.extend(document["job_id"], {"nodes": [{"name": "extra"}]})
    assert exc.value.status == 409
    assert [method for method, _, _ in state["calls"]] == ["GET", "PUT"]
    assert state["document"] == before


def test_extend_missing_workflow_does_not_create_one(server):
    client, state = server
    with pytest.raises(skill.ApiError) as exc:
        client.extend("missing", {"nodes": [{"name": "extra"}]})
    assert exc.value.status == 404
    assert [method for method, _, _ in state["calls"]] == ["GET"]


def test_extend_requires_server_version(server, document):
    client, state = server
    client.create(document)
    del state["document"]["updated_at"]
    with pytest.raises(ValueError, match="no updated_at"):
        client.extend(document["job_id"], {"nodes": [{"name": "extra"}]})
    assert not any(method == "PUT" for method, _, _ in state["calls"])


def test_extension_reconciles_a_lost_response_without_rewriting(server, document, monkeypatch):
    client, state = server
    client.create(document)
    original_request = client.request

    def lose_response(method, path, payload=None):
        result = original_request(method, path, payload)
        if method == "PUT":
            raise RuntimeError("Response lost; inspect the same ID before retrying")
        return result

    monkeypatch.setattr(client, "request", lose_response)
    additions = {"nodes": [{"name": "extra"}]}
    with pytest.raises(RuntimeError, match="Response lost"):
        client.extend(document["job_id"], additions)
    assert client.show(document["job_id"])["nodes"][-1]["name"] == "extra"
    assert client.extend(document["job_id"], additions)[1] == "unchanged"
    assert sum(method == "PUT" for method, _, _ in state["calls"]) == 1


def test_extend_cli_preview_publish_and_noop(server, document, tmp_path):
    client, state = server
    client.create(document)
    additions = tmp_path / "additions.json"
    additions.write_text(
        json.dumps(
            {
                "nodes": [{"name": "extra"}],
                "edges": [{"source": "evaluate", "target": "extra"}],
            }
        )
    )
    command = [
        sys.executable,
        str(SCRIPT),
        "--env-file",
        str(tmp_path / "missing.env"),
        "--base-url",
        client.base,
        "extend",
        "--job-id",
        document["job_id"],
        "--file",
        str(additions),
    ]
    for flags, expected in [(["--dry-run"], "planned"), ([], "extended"), ([], "unchanged")]:
        result = subprocess.run(
            [*command, *flags],
            env={**os.environ, "OCTO_API_KEY": "fixture-secret"},
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        output = json.loads(result.stdout.splitlines()[0])
        assert output["action"] == expected
        assert output["edges"][-1] == {"source": "evaluate", "target": "extra"}
        assert "fixture-secret" not in result.stdout + result.stderr
        assert f"/#workflow/{document['job_id']}" in result.stdout
        if expected == "planned":
            assert len(state["document"]["nodes"]) == 2
    assert sum(method == "PUT" for method, _, _ in state["calls"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"nodes": []},
        {"nodes": [{"name": "duplicate"}, {"name": "duplicate"}]},
        {"edges": [{"source": "missing", "target": "evaluate"}]},
        {"edges": [{"source": "prepare", "target": "prepare"}]},
        {"meta": {"api_key": "private"}},
        {"meta": {"link": "https://example.test/?authorization=private"}},
        {"meta": {"value": "wfk_TEST_FIXTURE_NOT_A_REAL_KEY_123456"}},
    ],
)
def test_invalid_or_secret_bearing_documents_are_rejected(document, change):
    with pytest.raises(ValueError):
        skill.validate({**document, **change})


def test_script_runs_and_outputs_a_safe_link(server, document, tmp_path):
    client, state = server
    artifact = tmp_path / "workflow.json"
    artifact.write_text(json.dumps(document))
    env = {**os.environ, "CODEX_THREAD_ID": "fixture-session", "OCTO_API_KEY": "fixture-secret"}
    command = [
        sys.executable,
        str(SCRIPT),
        "--env-file",
        str(tmp_path / "missing.env"),
        "--base-url",
        client.base,
    ]
    checked = subprocess.run(
        [*command, "validate", "--file", str(artifact)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(checked.stdout)["valid"]
    assert not state["calls"]
    for _ in range(2):
        created = subprocess.run(
            [*command, "create", "--file", str(artifact)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert (
            f"[Workflow: Fixture evaluation]({client.base}/#workflow/fixture-workflow)"
            in created.stdout
        )
        assert "fixture-secret" not in created.stdout + created.stderr
    assert state["document"]["meta"]["codex_session_id"] == "fixture-session"
    assert [method for method, _, _ in state["calls"]].count("POST") == 1


@pytest.mark.parametrize("use_xdg", [False, True])
def test_user_credentials_do_not_read_platform_secrets(tmp_path, monkeypatch, use_xdg):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    config_home = tmp_path / "home" / ".config"
    if use_xdg:
        config_home = tmp_path / "xdg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    user_env = config_home / "research-ops" / "octo.env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("OCTO_BASE_URL=http://octo.test\nOCTO_API_KEY=fixture-key\n")
    monkeypatch.delenv("OCTO_API_KEY", raising=False)
    monkeypatch.delenv("OCTO_BASE_URL", raising=False)
    platform_env = tmp_path / "platform.env"
    monkeypatch.setattr(skill, "DEFAULT_ENV", platform_env)
    from dotenv import dotenv_values

    def read_user_only(path):
        assert path == user_env
        return dotenv_values(path)

    monkeypatch.setattr("dotenv.dotenv_values", read_user_only)
    config = skill.load_config()
    assert config["OCTO_API_KEY"] == "fixture-key"
    assert config["OCTO_BASE_URL"] == "http://octo.test"


def test_explicit_env_file_and_environment_take_precedence(tmp_path, monkeypatch):
    selected = tmp_path / "selected.env"
    selected.write_text("OCTO_API_KEY=file-key\nOCTO_BASE_URL=http://selected.test\n")
    monkeypatch.setenv("OCTO_API_KEY", "environment-key")
    monkeypatch.delenv("OCTO_BASE_URL", raising=False)
    config = skill.load_config(selected)
    assert config["OCTO_API_KEY"] == "environment-key"
    assert config["OCTO_BASE_URL"] == "http://selected.test"


def test_platform_env_remains_the_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("ROPS_OCTO_API_KEY", raising=False)
    platform_env = tmp_path / "platform.env"
    platform_env.write_text("ROPS_OCTO_API_KEY=platform-key\n")
    monkeypatch.setattr(skill, "DEFAULT_ENV", platform_env)
    assert skill.load_config()["ROPS_OCTO_API_KEY"] == "platform-key"


def test_unreadable_config_reports_scoped_remedy(tmp_path, monkeypatch):
    env_file = tmp_path / "denied.env"
    env_file.touch()

    def denied(path):
        raise PermissionError("not permitted")

    monkeypatch.setattr("dotenv.dotenv_values", denied)
    with pytest.raises(ValueError, match="Octo-only credential file"):
        skill.load_config(env_file)

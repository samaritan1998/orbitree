from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from research_ops.services.runtime_settings import (
    RuntimeModelSettingsService,
    apply_persisted_runtime_model_settings,
)
from research_ops.settings import Settings


def test_runtime_model_settings_are_platform_persistent(
    client: TestClient, settings: Settings
) -> None:
    response = client.get("/api/settings/runtime-model")
    assert response.status_code == 200
    assert response.json()["codex_model"] == settings.codex_model

    response = client.patch(
        "/api/settings/runtime-model",
        json={
            "codex_model": "  model-from-platform  ",
            "codex_reasoning_effort": "xhigh",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["codex_model"] == "model-from-platform"
    assert payload["codex_reasoning_effort"] == "xhigh"
    assert payload["pi_model"] == settings.pi_model
    assert payload["source"] == "platform"

    path = settings.data_dir / "runtime-model-settings.json"
    assert path.exists()
    assert (path.stat().st_mode & 0o077) == 0
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert "api_key" not in stored
    assert stored["codex_model"] == "model-from-platform"

    fresh = Settings(
        data_dir=settings.data_dir,
        workspace_root=settings.workspace_root,
        database_url=settings.database_url,
        programs_file=settings.programs_file,
        frontend_dist=settings.frontend_dist,
        codex_home=settings.codex_home,
    )
    assert apply_persisted_runtime_model_settings(fresh)
    assert fresh.codex_model == "model-from-platform"
    assert fresh.codex_reasoning_effort == "xhigh"


def test_runtime_model_settings_update_is_validated(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspace")
    service = RuntimeModelSettingsService(settings)

    try:
        service.update({"codex_model": "   "})
    except ValueError:
        pass
    else:
        raise AssertionError("blank model should be rejected")

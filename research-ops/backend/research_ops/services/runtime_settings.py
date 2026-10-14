from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from research_ops.settings import Settings

logger = logging.getLogger(__name__)

# These are suggestions only. The input remains free-form so a newly deployed
# gateway model does not require a Orbitree release.
MODEL_PRESETS = (
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.5",
)
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
RUNTIME_SETTINGS_VERSION = 1


class RuntimeModelValues(BaseModel):
    codex_model: str = Field(min_length=1, max_length=160)
    codex_reasoning_effort: str = Field(min_length=1, max_length=24)
    pi_model: str = Field(min_length=1, max_length=160)
    pi_thinking: str = Field(min_length=1, max_length=24)

    @field_validator("codex_model", "codex_reasoning_effort", "pi_model", "pi_thinking")
    @classmethod
    def strip_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("runtime model settings must not be blank")
        return normalized


def _settings_path(settings: Settings) -> Path:
    return settings.data_dir / "runtime-model-settings.json"


def _read_payload(path: Path) -> tuple[RuntimeModelValues | None, datetime | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = RuntimeModelValues.model_validate(payload)
        raw_updated_at = payload.get("updated_at")
        updated_at = None
        if raw_updated_at:
            updated_at = datetime.fromisoformat(str(raw_updated_at).replace("Z", "+00:00"))
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            else:
                updated_at = updated_at.astimezone(UTC)
        return values, updated_at
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Ignoring invalid persisted runtime model settings %s: %s", path, exc)
        return None, None


def _apply_values(settings: Settings, values: RuntimeModelValues) -> None:
    # Settings is intentionally mutable for this small, process-wide control
    # plane. All subprocesses read these fields when their next turn starts.
    settings.codex_model = values.codex_model
    settings.codex_reasoning_effort = values.codex_reasoning_effort
    settings.pi_model = values.pi_model
    settings.pi_thinking = values.pi_thinking


def apply_persisted_runtime_model_settings(settings: Settings) -> bool:
    """Apply the last platform selection to a freshly loaded Settings object."""
    values, _ = _read_payload(_settings_path(settings))
    if values is None:
        return False
    _apply_values(settings, values)
    return True


class RuntimeModelSettingsService:
    """Durable, non-secret model preferences shared by all agent adapters."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = _settings_path(settings)
        self._lock = RLock()
        self._updated_at: datetime | None = None
        self._source = "environment"
        values, updated_at = _read_payload(self.path)
        if values is not None:
            _apply_values(settings, values)
            self._updated_at = updated_at
            self._source = "platform"

    def _current(self) -> RuntimeModelValues:
        return RuntimeModelValues(
            codex_model=self.settings.codex_model or "gpt-6-astra",
            codex_reasoning_effort=self.settings.codex_reasoning_effort or "max",
            pi_model=self.settings.pi_model or self.settings.codex_model or "gpt-6-astra",
            pi_thinking=self.settings.pi_thinking or self.settings.codex_reasoning_effort or "max",
        )

    def read(self) -> dict[str, Any]:
        with self._lock:
            values = self._current().model_dump()
            presets = list(
                dict.fromkeys([*MODEL_PRESETS, values["codex_model"], values["pi_model"]])
            )
            return {
                **values,
                "model_presets": presets,
                "reasoning_efforts": list(REASONING_EFFORTS),
                "source": self._source,
                "updated_at": self._updated_at,
            }

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self._current()
            allowed = set(RuntimeModelValues.model_fields)
            merged = current.model_copy(
                update={
                    key: value
                    for key, value in changes.items()
                    if key in allowed and value is not None
                }
            )
            # Revalidate after applying a partial patch, including length and
            # whitespace constraints.
            merged = RuntimeModelValues.model_validate(merged.model_dump())
            now = datetime.now(UTC)
            payload = {
                "version": RUNTIME_SETTINGS_VERSION,
                **merged.model_dump(),
                "updated_at": now.isoformat().replace("+00:00", "Z"),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=".runtime-model-settings.", suffix=".tmp", dir=self.path.parent
            )
            try:
                os.chmod(temporary, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            _apply_values(self.settings, merged)
            self._updated_at = now
            self._source = "platform"
            return self.read()

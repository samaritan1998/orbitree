from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from research_ops.settings import PROJECT_ROOT, Settings

from .base import CodexUnavailable

IDENTITY_LABELS = {"api": "Company API", "chatgpt": "Personal Codex", "oneapi": "Company OneAPI"}
API_ENV_KEY = "ROPS_CODEX_API_KEY"
ONEAPI_ENV_KEY = "ROPS_ONEAPI_API_KEY"
_AUTH_ENV = (
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "CHATGPT_BASE_URL",
    "OPENAI_FEDERATION_RULE_ID",
    "OPENAI_IDENTITY_TOKEN_FILE",
    "OPENAI_WORKLOAD_IDENTITY_CONTEXT",
    API_ENV_KEY,
    ONEAPI_ENV_KEY,
)
_PROVIDER_FIELDS = {
    "name",
    "base_url",
    "wire_api",
    "supports_websockets",
    "request_max_retries",
    "stream_max_retries",
    "stream_idle_timeout_ms",
    "websocket_connect_timeout_ms",
}


class ExecutionPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: Literal["api", "chatgpt", "oneapi"] = "api"
    model: str | None = Field(default=None, min_length=1, max_length=160)
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] | None
    ) = None

    @field_validator("model", mode="before")
    @classmethod
    def strip_model(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class CodexIdentityService:
    """Non-secret execution selections; credentials never enter a Branch or trace."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api_auth_path = settings.codex_home / "research-ops" / "api-auth.json"
        self.personal_home = settings.codex_home / "research-ops" / "personal"

    @property
    def sqlite_home(self) -> Path:
        value = self._config().get("sqlite_home") or os.environ.get("CODEX_SQLITE_HOME")
        path = Path(value).expanduser() if value else self.settings.codex_home
        return path if path.is_absolute() else self.settings.codex_home / path

    def prepare_personal_home(self) -> None:
        """Isolate login/config while keeping one transcript store and writer lock namespace."""
        self.personal_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        links = {
            "sessions": self.settings.codex_home / "sessions",
            "archived_sessions": self.settings.codex_home / "archived_sessions",
            "thread-writer-locks": self.sqlite_home / "thread-writer-locks",
        }
        for target in links.values():
            target.mkdir(parents=True, exist_ok=True)
        for name in ("skills", "AGENTS.md", "AGENTS.override.md", "rules"):
            target = self.settings.codex_home / name
            if target.exists():
                links[name] = target
        for name, target in links.items():
            link = self.personal_home / name
            if link.is_symlink() and link.resolve() == target.resolve():
                continue
            if link.exists() or link.is_symlink():
                raise CodexUnavailable(f"Personal Codex path conflicts with shared state: {name}")
            link.symlink_to(target.resolve(), target_is_directory=target.is_dir())
        path = self.personal_home / "config.toml"
        if not path.exists():
            config = self._config()
            values = {
                "model_provider": "openai",
                "forced_login_method": "chatgpt",
                "cli_auth_credentials_store": "file",
                "sqlite_home": str(self.sqlite_home),
            }
            for key in ("approval_policy", "sandbox_mode", "web_search", "file_opener"):
                if key in config:
                    values[key] = config[key]
            for key, value in config.get("features", {}).items():
                if re.fullmatch(r"[A-Za-z0-9_-]+", key) and isinstance(value, bool):
                    values[f"features.{key}"] = value
            with path.open("x") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(
                    "\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items())
                )
                handle.write("\n")

    def _config(self) -> dict[str, Any]:
        try:
            return tomllib.loads((self.settings.codex_home / "config.toml").read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise CodexUnavailable("Unable to read the Codex provider configuration") from exc

    @staticmethod
    def _read_auth(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _api_key(self) -> str | None:
        config = self._config()
        name = self.settings.codex_model_provider or config.get("model_provider", "openai")
        provider = config.get("model_providers", {}).get(name, {})
        for name in (API_ENV_KEY, provider.get("env_key")):
            if name and os.environ.get(name):
                return os.environ[name]
        cached = self._read_auth(self.api_auth_path).get("OPENAI_API_KEY")
        if isinstance(cached, str) and cached:
            return cached
        # Compatibility for an existing API-only installation. Personal login
        # never writes this file; managed API execution still uses env_key.
        auth = self._read_auth(self.settings.codex_home / "auth.json")
        value = auth.get("OPENAI_API_KEY")
        if isinstance(value, str) and value and not auth.get("tokens"):
            return value
        return None

    def preserve_api_key(self) -> None:
        key = self._api_key()
        if not key:
            raise CodexUnavailable(
                f"Set {API_ENV_KEY} in the server environment before importing API credentials"
            )
        self.api_auth_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".api-auth-", dir=self.api_auth_path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump({"OPENAI_API_KEY": key}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.api_auth_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def status(self) -> dict[str, Any]:
        api_ready = bool(self._api_key())
        auth = self._read_auth(self.personal_home / "auth.json")
        tokens = auth.get("tokens") or {}
        personal_ready = (
            auth.get("auth_mode") in {None, "chatgpt"}
            and isinstance(tokens, dict)
            and bool(tokens.get("access_token"))
            and not auth.get("OPENAI_API_KEY")
        )
        return {
            "identities": [
                {
                    "id": "api",
                    "label": IDENTITY_LABELS["api"],
                    "ready": api_ready,
                    "reason": None if api_ready else "API credentials are not configured",
                },
                {
                    "id": "chatgpt",
                    "label": IDENTITY_LABELS["chatgpt"],
                    "ready": personal_ready,
                    "reason": None if personal_ready else "Personal Codex is not signed in",
                },
                {
                    "id": "oneapi",
                    "label": IDENTITY_LABELS["oneapi"],
                    "ready": bool(self.settings.oneapi_api_key),
                    "reason": None
                    if self.settings.oneapi_api_key
                    else "OneAPI credentials are not configured",
                    "default_model": self.settings.oneapi_model,
                    "default_reasoning_effort": self.settings.oneapi_reasoning_effort,
                },
            ],
            "api_isolated": self.api_auth_path.exists() or bool(os.environ.get(API_ENV_KEY)),
            "login_command": f"{shlex.quote(str(PROJECT_ROOT / '.venv/bin/rops'))} auth login",
        }

    def snapshot(self, preferences: dict[str, Any] | None = None) -> dict[str, Any]:
        choice = ExecutionPreferences.model_validate(preferences or {})
        if choice.identity == "oneapi":
            endpoint = self.settings.oneapi_base_url.rstrip("/")
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise CodexUnavailable("OneAPI endpoint must be an HTTP URL without credentials")
            return {
                "identity": "oneapi",
                "label": IDENTITY_LABELS["oneapi"],
                "model": choice.model or self.settings.oneapi_model,
                "reasoning_effort": (
                    choice.reasoning_effort or self.settings.oneapi_reasoning_effort
                ),
                "provider": "rops-oneapi",
                "provider_config": {
                    "name": IDENTITY_LABELS["oneapi"],
                    "base_url": endpoint,
                    "wire_api": "responses",
                    "supports_websockets": False,
                    "requires_openai_auth": False,
                    "env_key": ONEAPI_ENV_KEY,
                },
                "base_url": endpoint,
            }
        if choice.identity == "chatgpt":
            # Gateway aliases are not assumed to exist in a personal account.
            model = choice.model
            if not model:
                raise CodexUnavailable("Choose a model available in your personal Codex account")
            return {
                "identity": "chatgpt",
                "label": IDENTITY_LABELS["chatgpt"],
                "model": model,
                "reasoning_effort": choice.reasoning_effort or "high",
                "provider": "openai",
                "provider_config": {},
                "base_url": "https://chatgpt.com/backend-api",
            }
        config = self._config()
        source = self.settings.codex_model_provider or config.get("model_provider", "openai")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", source):
            raise CodexUnavailable("The configured API provider ID is invalid")
        provider = config.get("model_providers", {}).get(source, {})
        if source != "openai" and not provider:
            raise CodexUnavailable(f"API provider {source!r} is missing from Codex config.toml")
        if any(
            provider.get(key)
            for key in ("http_headers", "env_http_headers", "auth", "experimental_bearer_token")
        ):
            raise CodexUnavailable(
                "Move API provider credentials to env_key before enabling identities"
            )
        public = {key: value for key, value in provider.items() if key in _PROVIDER_FIELDS}
        endpoint = str(public.get("base_url") or "https://api.openai.com/v1")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or (parsed.username or parsed.password or parsed.query or parsed.fragment)
        ):
            raise CodexUnavailable(
                "The API endpoint must be an HTTP URL without embedded credentials"
            )
        public.update(
            {
                "name": public.get("name", "Research Ops API"),
                "base_url": endpoint,
                "wire_api": public.get("wire_api", "responses"),
                "requires_openai_auth": False,
                "env_key": API_ENV_KEY,
            }
        )
        return {
            "identity": "api",
            "label": IDENTITY_LABELS["api"],
            "model": choice.model or self.settings.codex_model,
            "reasoning_effort": choice.reasoning_effort or self.settings.codex_reasoning_effort,
            "provider": source if source != "openai" else "rops-api",
            "provider_config": public,
            "base_url": endpoint,
        }

    def ensure_ready(self, identity: str) -> None:
        entry = next((row for row in self.status()["identities"] if row["id"] == identity), None)
        if not entry or not entry["ready"]:
            raise CodexUnavailable(entry["reason"] if entry else "Unknown execution identity")

    def environment(self, identity: str, *, login: bool = False) -> dict[str, str]:
        if not login:
            self.ensure_ready(identity)
        env = os.environ.copy()
        config = self._config()
        for provider in config.get("model_providers", {}).values():
            if provider.get("env_key"):
                env.pop(provider["env_key"], None)
        for key in _AUTH_ENV:
            env.pop(key, None)
        env["CODEX_HOME"] = str(
            self.personal_home if identity == "chatgpt" else self.settings.codex_home
        )
        env["CODEX_SQLITE_HOME"] = str(self.sqlite_home)
        if identity == "chatgpt":
            for name, value in (
                ("http_proxy", self.settings.codex_personal_http_proxy),
                ("https_proxy", self.settings.codex_personal_https_proxy),
                ("no_proxy", self.settings.codex_personal_no_proxy),
            ):
                if value is not None:
                    env[name] = value
                    env[name.upper()] = value
        if identity == "api":
            env[API_ENV_KEY] = self._api_key() or ""
        elif identity == "oneapi":
            env[ONEAPI_ENV_KEY] = self.settings.oneapi_api_key or ""
            # Internal OneAPI requests must not use the personal account's egress proxy.
            hostname = urlsplit(self.settings.oneapi_base_url).hostname or ""
            exclusions = ",".join(
                filter(None, [env.get("NO_PROXY"), env.get("no_proxy"), hostname])
            )
            env["NO_PROXY"] = env["no_proxy"] = exclusions
        return env

    def launch(self, execution: dict[str, Any]) -> CodexLaunch:
        return CodexLaunch(self, execution)


@dataclass(frozen=True)
class CodexLaunch:
    identities: CodexIdentityService
    execution: dict[str, Any]

    def args(self) -> list[str]:
        values: dict[str, Any] = {
            "model_provider": self.execution["provider"],
            "model_reasoning_effort": self.execution["reasoning_effort"],
        }
        if self.execution["identity"] == "chatgpt":
            values.update(
                {
                    "forced_login_method": "chatgpt",
                    "cli_auth_credentials_store": "file",
                    "chatgpt_base_url": self.execution["base_url"],
                    "sqlite_home": str(self.identities.sqlite_home),
                }
            )
        else:
            # Do not load or refresh the personal OAuth credentials for API runs.
            values["cli_auth_credentials_store"] = "ephemeral"
            for key, value in self.execution["provider_config"].items():
                values[f"model_providers.{self.execution['provider']}.{key}"] = value
        args: list[str] = ["-m", self.execution["model"]] if self.execution["model"] else []
        for key, value in values.items():
            if value is not None:
                args.extend(["-c", f"{key}={json.dumps(value)}"])
        return args

    def environment(self) -> dict[str, str]:
        if self.execution["identity"] in {"api", "oneapi"}:
            current = self.identities.snapshot({"identity": self.execution["identity"]})
            if any(current[key] != self.execution[key] for key in ("provider", "base_url")):
                raise CodexUnavailable(
                    "The API provider changed after this turn was accepted; "
                    "submit a new turn with an explicit identity selection"
                )
        return self.identities.environment(self.execution["identity"])

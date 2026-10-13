"""Credential-safe client for the infoflow-message-group single-chat protocol."""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class InfoflowClientError(RuntimeError):
    pass


class InfoflowConfigurationError(InfoflowClientError):
    pass


class InfoflowDeliveryBlockedError(InfoflowClientError):
    """A group delivery that requires a human configuration change.

    Retrying errors such as a robot not being in the target group cannot make
    progress and would only create noisy API traffic.
    """


@dataclass(frozen=True)
class InfoflowSendResult:
    message_id: str | None


class InfoflowMessageClient:
    """Send Infoflow messages without persisting credentials or access tokens."""

    def __init__(
        self,
        *,
        base_url: str = "https://messages.example.com",
        app_key_env: str = "INFOFLOW_APP_KEY",
        app_secret_env: str = "INFOFLOW_APP_SECRET",
        credentials_file: Path | str | None = None,
        timeout_seconds: float = 15.0,
        verify: bool = True,
        trust_env: bool = True,
        environ: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = str(base_url).strip().rstrip("/")
        if not self.base_url:
            raise ValueError("Infoflow base_url must not be empty")
        self.app_key_env = app_key_env
        self.app_secret_env = app_secret_env
        self.credentials_file = Path(credentials_file).expanduser() if credentials_file else None
        self.timeout_seconds = timeout_seconds
        self.verify = verify
        self.trust_env = trust_env
        self.environ = environ if environ is not None else os.environ
        self.transport = transport
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()
        self._file_credentials: dict[str, str] | None = None

    @property
    def configured(self) -> bool:
        return bool(self._credential(self.app_key_env) and self._credential(self.app_secret_env))

    def send_text(self, recipient: str, content: str) -> InfoflowSendResult:
        target = str(recipient).strip()
        if not target or "@" in target:
            raise ValueError("Infoflow recipient must be an email prefix")
        message = str(content).strip()
        if not message:
            raise ValueError("Infoflow message must not be empty")
        payload = self._request(
            "/api/v1/app/message/send",
            headers={
                "Authorization": f"Bearer-{self._access_token()}",
                "X-OP-ACCESS-CHANNEL": "skill",
            },
            json_body={
                "touser": target,
                "msgtype": "text",
                "text": {"content": message},
            },
        )
        self._require_success(payload, "Infoflow message send failed")
        data = _mapping(payload.get("data"))
        nested = _mapping(data.get("data"))
        message_id = _first_text(
            data.get("msgkey"),
            data.get("messageId"),
            data.get("message_id"),
            nested.get("msgkey"),
            nested.get("messageId"),
        )
        return InfoflowSendResult(message_id=message_id)

    def send_group_text(self, group_id: str, content: str) -> InfoflowSendResult:
        """Send a plain-text message as the configured robot to one numeric group."""
        target = str(group_id).strip()
        if not target.isdigit():
            raise ValueError("Infoflow group ID must be numeric")
        message = str(content).strip()
        if not message:
            raise ValueError("Infoflow message must not be empty")
        payload = self._request(
            "/api/v1/robot/msg/groupmsgsend",
            headers={
                "Authorization": f"Bearer-{self._access_token()}",
                "X-OP-ACCESS-CHANNEL": "skill",
            },
            json_body={
                "message": {
                    "header": {
                        "toid": int(target),
                        "totype": "GROUP",
                        "msgtype": "TEXT",
                        "clientmsgid": int(time.time() * 1000),
                        "role": "robot",
                    },
                    "body": [{"type": "TEXT", "content": message}],
                }
            },
        )
        try:
            self._require_success(payload, "Infoflow group message send failed")
        except InfoflowClientError as exc:
            if _is_blocked_group_delivery(payload):
                raise InfoflowDeliveryBlockedError(str(exc)) from exc
            raise
        data = _mapping(payload.get("data"))
        nested = _mapping(data.get("data"))
        message_id = _first_text(
            data.get("msgkey"),
            data.get("messageId"),
            data.get("message_id"),
            data.get("msgId"),
            nested.get("msgkey"),
            nested.get("messageId"),
            nested.get("msgId"),
        )
        return InfoflowSendResult(message_id=message_id)

    def _access_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token
        with self._token_lock:
            now = time.monotonic()
            if self._token and now < self._token_expires_at:
                return self._token
            app_key = self._required_credential(self.app_key_env)
            app_secret = self._required_credential(self.app_secret_env)
            secret_digest = hashlib.md5(
                app_secret.encode("utf-8"), usedforsecurity=False
            ).hexdigest()
            payload = self._request(
                "/api/v1/auth/app_access_token",
                headers={"X-OP-ACCESS-CHANNEL": "skill"},
                json_body={"app_key": app_key, "app_secret": secret_digest},
            )
            self._require_success(payload, "Infoflow authentication failed")
            data = _mapping(payload.get("data"))
            nested = _mapping(data.get("data"))
            token = _first_text(
                payload.get("app_access_token"),
                data.get("app_access_token"),
                nested.get("app_access_token"),
            )
            if not token:
                raise InfoflowClientError("Infoflow authentication response omitted access token")
            expires = _first_int(
                payload.get("expire"),
                data.get("expire"),
                nested.get("expire"),
            ) or 7_200
            self._token = token.removeprefix("Bearer-")
            self._token_expires_at = time.monotonic() + max(30, expires - 60)
            return self._token

    def _request(
        self,
        path: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                verify=self.verify,
                trust_env=self.trust_env,
                transport=self.transport,
            ) as client:
                response = client.post(
                    path,
                    headers={"Content-Type": "application/json; charset=utf-8", **headers},
                    json=json_body,
                )
        except httpx.HTTPError as exc:
            raise InfoflowClientError(
                f"Infoflow request failed: {type(exc).__name__}"
            ) from exc
        if not 200 <= response.status_code < 300:
            raise InfoflowClientError(f"Infoflow returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise InfoflowClientError("Infoflow returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise InfoflowClientError("Infoflow returned an invalid response")
        return payload

    @staticmethod
    def _require_success(payload: dict[str, Any], fallback: str) -> None:
        code = payload.get("code")
        if code not in {None, "ok", 0, 200, "200"}:
            message = _first_text(payload.get("message"), payload.get("errmsg")) or fallback
            raise InfoflowClientError(message[:500])
        data = _mapping(payload.get("data"))
        business_code = data.get("errcode", data.get("code"))
        if business_code not in {None, 0, "0", 200, "200"}:
            message = _first_text(data.get("errmsg"), data.get("message")) or fallback
            raise InfoflowClientError(message[:500])

    def _credential(self, name: str) -> str:
        value = str(self.environ.get(name) or "").strip()
        if value:
            return value
        credentials = self._credentials_from_file()
        aliases = [name]
        if name == "INFOFLOW_APP_KEY":
            aliases.append("INFOFLOW_AK")
        elif name == "INFOFLOW_APP_SECRET":
            aliases.append("INFOFLOW_SK")
        for alias in aliases:
            value = str(credentials.get(alias) or "").strip()
            if value:
                return value
        return ""

    def _credentials_from_file(self) -> dict[str, str]:
        if self._file_credentials is not None:
            return self._file_credentials
        path = self.credentials_file
        if path is None:
            self._file_credentials = {}
            return self._file_credentials
        try:
            file_stat = path.stat()
            if stat.S_IMODE(file_stat.st_mode) & 0o077:
                raise InfoflowConfigurationError(
                    "Infoflow credentials file must not be accessible by group or other users"
                )
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            self._file_credentials = {}
            return self._file_credentials
        except OSError as exc:
            raise InfoflowConfigurationError("Infoflow credentials file is unavailable") from exc

        parsed: dict[str, str] = {}
        allowed = {
            self.app_key_env,
            self.app_secret_env,
            "INFOFLOW_AK",
            "INFOFLOW_SK",
        }
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or key not in allowed:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if value:
                parsed[key] = value
        self._file_credentials = parsed
        return self._file_credentials

    def _required_credential(self, name: str) -> str:
        value = self._credential(name)
        if not value:
            raise InfoflowConfigurationError(
                f"Infoflow credential is unavailable; configure environment variable {name}"
            )
        return value


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


def _first_int(*values: Any) -> int | None:
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _is_blocked_group_delivery(payload: dict[str, Any]) -> bool:
    """Recognize configuration failures that cannot be repaired by retrying."""
    data = _mapping(payload.get("data"))
    message = " ".join(
        str(value)
        for value in (
            payload.get("message"),
            payload.get("errmsg"),
            data.get("message"),
            data.get("errmsg"),
        )
        if value
    ).lower()
    return any(
        marker in message
        for marker in (
            "group not follow",
            "not in group",
            "not a group member",
            "permission",
            "forbidden",
            "not allowed",
            "机器人未在群成员列表中",
            "非法的企业机器人",
        )
    )

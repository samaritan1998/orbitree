"""Resolve four-domain frozen-teacher lineage to runtime serving receipts.

The versioned routing contract contains immutable checkpoint lineage and the
*names* of environment variables that point to serving receipts.  Runtime
endpoint URLs remain in those receipts and in a short-lived materialized route
file; they never belong in a versioned YAML configuration.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_DOMAINS = ("miaoda", "swe", "ppt", "tooluse")
_TEACHER_ENDPOINT_PATHS = frozenset({"/generate", "/v1/chat/completions"})
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class TeacherRoutingError(ValueError):
    """Raised when frozen-teacher lineage cannot safely reach a runtime endpoint."""


@dataclass(frozen=True)
class TeacherRoute:
    """One domain's immutable lineage and runtime receipt reference."""

    domain: str
    checkpoint: str
    serving_checkpoint: str
    endpoint_receipt_env: str


@dataclass(frozen=True)
class TeacherRouting:
    """The complete four-domain teacher routing contract."""

    routing_key: str
    student_init_checkpoint: str
    routes: dict[str, TeacherRoute]


@dataclass(frozen=True)
class RuntimeTeacherRoute:
    """A non-secret, resolved route ready for one rollout process."""

    domain: str
    checkpoint: str
    serving_checkpoint: str
    endpoint: str
    endpoint_receipt: str
    model: str | None = None

    @property
    def endpoint_fingerprint(self) -> str:
        return hashlib.sha256(self.endpoint.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, str]:
        return {
            "checkpoint": self.checkpoint,
            "endpoint": self.endpoint,
            "endpoint_fingerprint": self.endpoint_fingerprint,
            "endpoint_receipt": self.endpoint_receipt,
            "serving_checkpoint": self.serving_checkpoint,
        }


def _require_mapping(value: Any, *, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TeacherRoutingError(message)
    return value


def _absolute_path(value: Any, *, field: str) -> str:
    path = Path(str(value or "").strip()).expanduser()
    if not path.is_absolute():
        raise TeacherRoutingError(f"{field} must be an absolute checkpoint path")
    return str(path)


def _normalized_path(value: str) -> str:
    return str(Path(value).expanduser().resolve(strict=False))


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise TeacherRoutingError("teacher routing YAML support is unavailable") from exc
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TeacherRoutingError("teacher routing configuration is unreadable") from exc
    return _require_mapping(payload, message="teacher routing configuration must be an object")


def load_teacher_routing(path: str | Path) -> TeacherRouting:
    """Load a strict, endpoint-free four-domain routing configuration."""

    payload = _load_yaml(Path(path))
    if set(payload) != {"version", "routing_key", "student", "teachers"}:
        raise TeacherRoutingError("teacher routing configuration has unexpected fields")
    if payload.get("version") != 1:
        raise TeacherRoutingError("teacher routing version must be 1")
    if payload.get("routing_key") != "domain":
        raise TeacherRoutingError("teacher routing key must be domain")

    student = _require_mapping(payload.get("student"), message="student routing must be an object")
    if set(student) != {"init_checkpoint", "update_in_place"}:
        raise TeacherRoutingError("student routing has unexpected fields")
    if student.get("update_in_place") is not False:
        raise TeacherRoutingError("student must not update the frozen teacher checkpoint in place")
    student_init = _absolute_path(student.get("init_checkpoint"), field="student.init_checkpoint")

    raw_teachers = _require_mapping(payload.get("teachers"), message="teachers must be an object")
    if set(raw_teachers) != set(_DOMAINS):
        raise TeacherRoutingError(
            "teacher routing must cover exactly miaoda, swe, ppt, and tooluse"
        )

    routes: dict[str, TeacherRoute] = {}
    for domain in _DOMAINS:
        raw = _require_mapping(
            raw_teachers.get(domain),
            message=f"teacher route {domain} must be an object",
        )
        if set(raw) != {
            "checkpoint",
            "serving_checkpoint",
            "endpoint_receipt_env",
            "frozen",
        }:
            raise TeacherRoutingError(f"teacher route {domain} has unexpected fields")
        if raw.get("frozen") is not True:
            raise TeacherRoutingError(f"teacher route {domain} must be frozen")
        env_name = str(raw.get("endpoint_receipt_env") or "").strip()
        if not _ENV_NAME.fullmatch(env_name):
            raise TeacherRoutingError(
                f"teacher route {domain} must use an environment-variable reference"
            )
        routes[domain] = TeacherRoute(
            domain=domain,
            checkpoint=_absolute_path(raw.get("checkpoint"), field=f"teachers.{domain}.checkpoint"),
            serving_checkpoint=_absolute_path(
                raw.get("serving_checkpoint"),
                field=f"teachers.{domain}.serving_checkpoint",
            ),
            endpoint_receipt_env=env_name,
        )

    return TeacherRouting(
        routing_key="domain",
        student_init_checkpoint=student_init,
        routes=routes,
    )


def _endpoint_candidates(receipt: Mapping[str, Any]) -> list[str]:
    raw_candidates = [receipt.get("url"), *(receipt.get("url_candidates") or [])]
    candidates: list[str] = []
    for raw in raw_candidates:
        value = str(raw or "").strip()
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def _validate_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TeacherRoutingError("teacher receipt has no valid HTTP endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise TeacherRoutingError(
            "teacher endpoint must not include credentials or query parameters"
        )
    endpoint_path = parsed.path.rstrip("/") or "/"
    if endpoint_path not in _TEACHER_ENDPOINT_PATHS:
        raise TeacherRoutingError(
            "teacher endpoint must use /generate or /v1/chat/completions"
        )
    host = parsed.hostname.casefold()
    if host in {"localhost", "localhost.localdomain"}:
        raise TeacherRoutingError("teacher endpoint must be externally reachable")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_loopback or address.is_unspecified):
        raise TeacherRoutingError("teacher endpoint must be externally reachable")
    return urlunsplit((parsed.scheme, parsed.netloc, endpoint_path, "", ""))


def _endpoint_receipt(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TeacherRoutingError("teacher endpoint receipt is unreadable") from exc
    return _require_mapping(value, message="teacher endpoint receipt must be an object")


def _receipt_matches_route(receipt: Mapping[str, Any], route: TeacherRoute) -> None:
    if receipt.get("status") != "ready" or receipt.get("protocol_validated") is not True:
        raise TeacherRoutingError(
            f"teacher endpoint receipt for {route.domain} is not protocol-ready"
        )
    received = _absolute_path(
        receipt.get("checkpoint"),
        field="teacher endpoint receipt checkpoint",
    )
    expected = {
        _normalized_path(route.checkpoint),
        _normalized_path(route.serving_checkpoint),
    }
    if _normalized_path(received) not in expected:
        raise TeacherRoutingError(
            f"teacher endpoint receipt checkpoint does not match {route.domain} lineage"
        )


def _health_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


def _healthy(endpoint: str, *, model: str | None = None, timeout_seconds: float) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    parsed = urlsplit(endpoint)
    if parsed.path.rstrip("/") == "/v1/chat/completions":
        if not model:
            return False
        payload = json.dumps(
            {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": "__openreward_input_ids__:[1]",
                    }
                ],
                "max_tokens": 1,
                "temperature": 0,
                "logprobs": True,
                "top_logprobs": 0,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            with opener.open(
                urllib.request.Request(
                    endpoint,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=timeout_seconds,
            ) as response:
                if int(response.status) != 200:
                    return False
                body = json.load(response)
                meta = body.get("meta_info") if isinstance(body, dict) else None
                choices = body.get("choices") if isinstance(body, dict) else None
                choice = choices[0] if isinstance(choices, list) and choices else {}
                logprobs = choice.get("logprobs") if isinstance(choice, dict) else None
                return bool(
                    isinstance(meta, dict) and meta.get("input_token_logprobs")
                    or isinstance(logprobs, dict) and logprobs.get("token_logprobs")
                )
        except (
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            urllib.error.HTTPError,
            urllib.error.URLError,
        ):
            return False
    try:
        with opener.open(
            urllib.request.Request(_health_url(endpoint), method="GET"),
            timeout=timeout_seconds,
        ) as response:
            return int(response.status) == 200
    except (
        OSError,
        ValueError,
        urllib.error.HTTPError,
        urllib.error.URLError,
    ):
        return False


def resolve_teacher_route(
    routing: TeacherRouting,
    *,
    domain: str,
    environ: Mapping[str, str] | None = None,
    check_health: bool = False,
    timeout_seconds: float = 8.0,
) -> RuntimeTeacherRoute:
    """Resolve one configured domain through its matching serving receipt."""

    normalized_domain = str(domain).strip().casefold()
    route = routing.routes.get(normalized_domain)
    if route is None:
        raise TeacherRoutingError("teacher routing has no route for the requested domain")
    runtime = os.environ if environ is None else environ
    receipt_reference = str(runtime.get(route.endpoint_receipt_env, "")).strip()
    if not receipt_reference:
        raise TeacherRoutingError(f"missing runtime reference: {route.endpoint_receipt_env}")
    receipt_path = Path(receipt_reference).expanduser()
    if not receipt_path.is_file():
        raise TeacherRoutingError(f"teacher endpoint receipt is missing for {route.domain}")
    receipt = _endpoint_receipt(receipt_path)
    _receipt_matches_route(receipt, route)

    candidates: list[str] = []
    for candidate in _endpoint_candidates(receipt):
        try:
            endpoint = _validate_endpoint(candidate)
        except TeacherRoutingError:
            continue
        if endpoint not in candidates:
            candidates.append(endpoint)
    if not candidates:
        raise TeacherRoutingError(
            f"teacher endpoint receipt has no usable endpoint for {route.domain}"
        )

    if check_health:
        endpoint = next(
            (
                candidate
                for candidate in candidates
                if _healthy(candidate, timeout_seconds=timeout_seconds)
            ),
            None,
        )
        if endpoint is None:
            raise TeacherRoutingError(f"no reachable teacher endpoint for {route.domain}")
    else:
        endpoint = candidates[0]
    return RuntimeTeacherRoute(
        domain=route.domain,
        checkpoint=route.checkpoint,
        serving_checkpoint=route.serving_checkpoint,
        endpoint=endpoint,
        endpoint_receipt=str(receipt_path),
    )


def materialize_runtime_routes(
    routing: TeacherRouting,
    *,
    environ: Mapping[str, str] | None = None,
    check_health: bool = False,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    """Resolve all four domains to a runtime-only route document."""

    routes = {
        domain: resolve_teacher_route(
            routing,
            domain=domain,
            environ=environ,
            check_health=check_health,
            timeout_seconds=timeout_seconds,
        ).to_dict()
        for domain in _DOMAINS
    }
    return {
        "version": 1,
        "routing_key": routing.routing_key,
        "student_init_checkpoint": routing.student_init_checkpoint,
        "routes": routes,
    }


def write_runtime_routes(path: str | Path, routes: Mapping[str, Any]) -> Path:
    """Atomically write a derived runtime route document without logging endpoints."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(dict(routes), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(target)
    return target


def load_runtime_teacher_route(path: str | Path, *, domain: str) -> RuntimeTeacherRoute:
    """Read one domain from a materialized route document."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TeacherRoutingError("runtime teacher routes are unreadable") from exc
    root = _require_mapping(payload, message="runtime teacher routes must be an object")
    if root.get("version") != 1 or root.get("routing_key") != "domain":
        raise TeacherRoutingError("runtime teacher routes have an unsupported schema")
    routes = _require_mapping(root.get("routes"), message="runtime teacher routes omit routes")
    normalized_domain = str(domain).strip().casefold()
    raw = _require_mapping(
        routes.get(normalized_domain),
        message="runtime teacher routes omit the requested domain",
    )
    required = {"checkpoint", "serving_checkpoint", "endpoint", "endpoint_receipt"}
    if not required.issubset(raw):
        raise TeacherRoutingError("runtime teacher route is incomplete")
    endpoint = _validate_endpoint(str(raw["endpoint"]))
    model = str(raw.get("model") or "").strip() or None
    if endpoint.endswith("/v1/chat/completions") and not model:
        raise TeacherRoutingError("OpenReward gateway teacher route has no model")
    return RuntimeTeacherRoute(
        domain=normalized_domain,
        checkpoint=_absolute_path(raw["checkpoint"], field="runtime teacher checkpoint"),
        serving_checkpoint=_absolute_path(
            raw["serving_checkpoint"], field="runtime serving checkpoint"
        ),
        endpoint=endpoint,
        endpoint_receipt=str(raw["endpoint_receipt"]),
        model=model,
    )


def validate_runtime_teacher_route(
    path: str | Path,
    *,
    domain: str,
    check_health: bool = False,
    timeout_seconds: float = 8.0,
) -> RuntimeTeacherRoute:
    """Load one route and optionally probe its serving health endpoint."""

    route = load_runtime_teacher_route(path, domain=domain)
    if check_health and not _healthy(
        route.endpoint, model=route.model, timeout_seconds=timeout_seconds
    ):
        raise TeacherRoutingError(f"no reachable teacher endpoint for {route.domain}")
    return route

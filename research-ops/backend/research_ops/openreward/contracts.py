"""Machine-readable configuration contract for real ORS domain backends.

The contract stores environment-variable *names* and non-secret operation
descriptions only.  Runtime values remain outside Research Ops state and are
never included in readiness reports.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_OPERATION = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)\s+/\S*$")


class BackendContractError(ValueError):
    """Raised when a backend contract file is invalid or unsafe."""


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def validate_environment_references(cls, value: Any, info: Any) -> Any:
        name = str(info.field_name or "")
        candidates: list[str] = []
        if name.endswith("_env") and isinstance(value, str):
            candidates = [value]
        elif name.endswith("_envs") and isinstance(value, list):
            candidates = [str(item) for item in value]
        for candidate in candidates:
            if not _ENV_NAME.fullmatch(candidate):
                raise ValueError(f"{name} must contain environment variable names only")
        if (
            name.endswith("_operation")
            and value is not None
            and not _OPERATION.fullmatch(str(value).strip())
        ):
            raise ValueError(f"{name} must look like 'POST /v1/resource'")
        return value


class MiaodaBackendContract(_StrictContract):
    adapter: Literal["datawheel-agentos-goblin"] = "datawheel-agentos-goblin"
    environment: str = "evaluation"
    datawheel_base_url_env: str
    datawheel_token_env: str
    generated_user_id_env: str
    policy_model_name_env: str
    policy_model_api_url_env: str
    policy_model_api_key_env: str
    goblin_base_url_env: str
    goblin_token_env: str
    goblin_result_access: Literal["shared_path", "download_url"]
    goblin_result_location_env: str
    join_fields: list[str] = Field(
        default_factory=lambda: [
            "episode_id",
            "case_id",
            "app_id",
            "跑批单条记录id",
        ],
        min_length=1,
    )
    success_agent_states: list[str] = Field(
        default_factory=lambda: ["finished"], min_length=1
    )
    boundary_agent_states: list[str] = Field(
        default_factory=lambda: ["awaiting_user_input"], min_length=1
    )


class SweBackendContract(_StrictContract):
    adapter: Literal["sandbox"] = "sandbox"
    runtime_mode: Literal["image", "service"]
    runtime_ref_env: str
    auth_envs: list[str] = Field(default_factory=list, max_length=1)
    workspace_mount: str
    create_operation: str
    exec_operation: str
    read_operation: str
    write_operation: str
    destroy_operation: str
    verifier_operation: str
    patch_output: str
    timeout_seconds: float = Field(default=120.0, gt=0)
    verify_tls: bool = True
    trust_env: bool = False


class PptBackendContract(_StrictContract):
    adapter: Literal["office-runtime"] = "office-runtime"
    runtime_mode: Literal["image", "service"]
    runtime_ref_env: str
    auth_envs: list[str] = Field(default_factory=list, max_length=1)
    workspace_mount: str
    create_operation: str
    exec_operation: str
    file_read_operation: str
    file_write_operation: str
    file_edit_operation: str
    read_operation: str
    write_operation: str
    render_operation: str
    submit_operation: str
    destroy_operation: str
    artifact_extensions: list[str] = Field(default_factory=lambda: [".pptx"], min_length=1)
    render_extensions: list[str] = Field(
        default_factory=lambda: [".png"], min_length=1
    )
    timeout_seconds: float = Field(default=120.0, gt=0)
    verify_tls: bool = True
    trust_env: bool = False


class ToolUseBackendContract(_StrictContract):
    adapter: Literal["tool-registry", "openreward-proxy"] = "tool-registry"
    service_base_url_env: str
    remote_environment: str = "toolathlongym"
    auth_envs: list[str] = Field(default_factory=list, max_length=1)
    registry_operation: str
    reset_operation: str
    invoke_operation: str
    state_operation: str
    verifier_operation: str
    destroy_operation: str
    timeout_seconds: float = Field(default=120.0, gt=0)
    verify_tls: bool = True
    trust_env: bool = False


class OpenRewardBackendContract(_StrictContract):
    version: Literal[1] = 1
    miaoda: MiaodaBackendContract
    swe: SweBackendContract
    ppt: PptBackendContract
    tooluse: ToolUseBackendContract


def load_backend_contract(path: str | Path) -> OpenRewardBackendContract:
    """Load a strict YAML contract without resolving any runtime secret."""

    source = Path(path)
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BackendContractError(f"cannot read backend contract: {type(exc).__name__}") from exc
    if not isinstance(payload, Mapping):
        raise BackendContractError("backend contract root must be an object")
    try:
        return OpenRewardBackendContract.model_validate(payload)
    except ValueError as exc:
        raise BackendContractError("backend contract validation failed") from exc


def environment_references(contract: OpenRewardBackendContract) -> dict[str, list[str]]:
    """Return sorted runtime env names for each domain, never their values."""

    result: dict[str, list[str]] = {}
    for domain in ("miaoda", "swe", "ppt", "tooluse"):
        model = getattr(contract, domain)
        names: set[str] = set()
        for field_name, value in model.model_dump().items():
            if field_name.endswith("_env") and isinstance(value, str):
                names.add(value)
            elif field_name.endswith("_envs") and isinstance(value, list):
                names.update(str(item) for item in value)
        result[domain] = sorted(names)
    return result


def backend_readiness(
    contract: OpenRewardBackendContract,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Report presence of required runtime references without exposing values."""

    runtime = os.environ if environ is None else environ
    report: dict[str, dict[str, Any]] = {}
    for domain, names in environment_references(contract).items():
        missing = [name for name in names if not str(runtime.get(name, "")).strip()]
        report[domain] = {
            "ready": not missing,
            "required_env_count": len(names),
            "missing_env": missing,
        }
    return report

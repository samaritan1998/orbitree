"""Production assembly for the four standard OpenReward environments.

The ORS protocol server is deliberately independent of deployment details.
This module is the one place that translates a versioned backend contract and
runtime environment references into concrete backend objects.  Missing values
are reported by variable name only; credential values never enter task specs,
session state or readiness output.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import yaml

from research_ops.agentos import AgentOSClient
from research_ops.reward import GoblinRewardClient

from .adapters import EnvironmentBackend, build_default_environments
from .contracts import (
    OpenRewardBackendContract,
    backend_readiness,
    load_backend_contract,
)
from .environment import ORSEnvironment, sensitive_task_key
from .miaoda_online import MiaodaDatawheelGoblinBackend, MiaodaOnlineConfig
from .service_backends import (
    PptOfficeBackend,
    RemoteORSBackend,
    RemoteORSConfig,
    RemoteServiceConfig,
    SweSandboxBackend,
    ToolRegistryBackend,
)

_DOMAINS = ("miaoda", "swe", "ppt", "tooluse")
_SPLITS = {"train", "validation", "test"}


class OpenRewardRuntimeError(RuntimeError):
    """Raised when a production OpenReward deployment cannot be assembled."""


def _runtime_value(environ: Mapping[str, str], name: str) -> str:
    value = str(environ.get(name, "")).strip()
    if not value:
        raise OpenRewardRuntimeError(f"missing runtime environment reference: {name}")
    return value


def _auth_headers(auth_envs: Sequence[str]) -> dict[str, str]:
    if not auth_envs:
        return {}
    if len(auth_envs) != 1:
        raise OpenRewardRuntimeError(
            "service adapters currently require at most one bearer credential reference"
        )
    return {"Authorization": str(auth_envs[0])}


def _require_service_mode(domain: str, mode: str) -> None:
    if mode != "service":
        raise OpenRewardRuntimeError(
            f"{domain} runtime_mode=image requires a deployment-owned container launcher"
        )


def build_backends_from_contract(
    contract: OpenRewardBackendContract,
    *,
    domains: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    transports: Mapping[str, httpx.BaseTransport | httpx.AsyncBaseTransport] | None = None,
) -> dict[str, EnvironmentBackend]:
    """Build selected real backend adapters from safe runtime references.

    ``transports`` is only for tests or deployment-specific networking.  Keys
    are ``miaoda_datawheel``, ``miaoda_goblin``, ``swe``, ``ppt`` and
    ``tooluse``.  Normal deployments omit it.  When ``domains`` is omitted all
    four adapters are required; selecting only ``miaoda`` supports an
    incremental rollout while the other three environments continue using
    their safe mocks.
    """

    selected = _normalize_domains(domains)
    runtime = os.environ if environ is None else environ
    report = backend_readiness(contract, environ=runtime)
    missing = {
        domain: item["missing_env"]
        for domain, item in report.items()
        if domain in selected and item["missing_env"]
    }
    if missing:
        detail = "; ".join(
            f"{domain}: {','.join(names)}" for domain, names in sorted(missing.items())
        )
        raise OpenRewardRuntimeError(f"missing runtime references: {detail}")
    transports = transports or {}
    backends: dict[str, EnvironmentBackend] = {}
    if "miaoda" in selected:
        backends["miaoda"] = _build_miaoda_backend(contract, runtime, transports)
    if "swe" in selected:
        backends["swe"] = _build_swe_backend(contract, runtime, transports)
    if "ppt" in selected:
        backends["ppt"] = _build_ppt_backend(contract, runtime, transports)
    if "tooluse" in selected:
        backends["tooluse"] = _build_tooluse_backend(contract, runtime, transports)
    return backends


def _normalize_domains(domains: Sequence[str] | None) -> tuple[str, ...]:
    if domains is None:
        return _DOMAINS
    selected = tuple(dict.fromkeys(str(item).strip().casefold() for item in domains))
    if not selected:
        raise OpenRewardRuntimeError("at least one real OpenReward domain is required")
    if any(item not in _DOMAINS for item in selected):
        raise OpenRewardRuntimeError("unknown real OpenReward domain")
    return selected


def _build_miaoda_backend(
    contract: OpenRewardBackendContract,
    runtime: Mapping[str, str],
    transports: Mapping[str, httpx.BaseTransport | httpx.AsyncBaseTransport],
) -> MiaodaDatawheelGoblinBackend:
    miaoda = contract.miaoda
    # ``download_url`` is the service-boundary mode used by the K8s
    # deployment: the Datawheel workbook is uploaded to Goblin and the job
    # result is read through Goblin's API.  ``shared_path`` is retained for
    # installations where the ORS process can directly read Goblin's result
    # root.  Both modes are implemented by MiaodaOnlineConfig; rejecting the
    # upload mode here would make a normal pod-local deployment impossible.
    datawheel_transport = transports.get("miaoda_datawheel")
    goblin_transport = transports.get("miaoda_goblin")
    if datawheel_transport is not None and not isinstance(
        datawheel_transport, httpx.BaseTransport
    ):
        raise OpenRewardRuntimeError("miaoda_datawheel transport must be synchronous")
    if goblin_transport is not None and not isinstance(goblin_transport, httpx.BaseTransport):
        raise OpenRewardRuntimeError("miaoda_goblin transport must be synchronous")
    datawheel = AgentOSClient(
        _runtime_value(runtime, miaoda.datawheel_base_url_env),
        token_env=miaoda.datawheel_token_env,
        transport=datawheel_transport,
    )
    goblin = GoblinRewardClient(
        _runtime_value(runtime, miaoda.goblin_base_url_env),
        token_env=miaoda.goblin_token_env,
        transport=goblin_transport,
    )
    return MiaodaDatawheelGoblinBackend(
        datawheel,
        goblin,
        MiaodaOnlineConfig(
            generated_user_id=_runtime_value(runtime, miaoda.generated_user_id_env),
            model_name=_runtime_value(runtime, miaoda.policy_model_name_env),
            model_api_url=_runtime_value(runtime, miaoda.policy_model_api_url_env),
            model_api_key_env=miaoda.policy_model_api_key_env,
            benchmark_dataset_name=(
                str(runtime.get("GOBLIN_BENCHMARK_DATASET_NAME", "")).strip()
                or "example-app-300"
            ),
            artifact_dir=Path(
                str(runtime.get("MIAODA_ARTIFACT_DIR", "")).strip()
                or str(runtime.get("GOBLIN_REWARD_RESULT_ROOT", "")).strip()
                or "/tmp/miaoda-online-artifacts"
            ),
            reward_result_root=Path(
                _runtime_value(runtime, miaoda.goblin_result_location_env)
            ),
            # ``shared_path`` is the JSON-only Goblin deployment contract:
            # submit a CFS path rather than calling the multipart upload
            # endpoint.  ``download_url`` keeps the historical upload mode.
            reward_upload=miaoda.goblin_result_access != "shared_path",
            reward_workdir_prefix=Path(
                _runtime_value(runtime, miaoda.goblin_result_location_env)
            )
            if miaoda.goblin_result_access == "shared_path"
            else None,
            policy_capture_dir=(
                Path(str(runtime.get("MOPD_POLICY_CAPTURE_DIR", "")).strip())
                if str(runtime.get("MOPD_POLICY_CAPTURE_DIR", "")).strip()
                else None
            ),
            concurrency=int(
                str(runtime.get("MIAODA_DATAWHEEL_CONCURRENCY", "")).strip() or "1"
            ),
            model_max_tokens=int(
                str(runtime.get("MIAODA_MODEL_MAX_TOKENS", "")).strip() or "32000"
            ),
            model_timeout_seconds=int(
                str(runtime.get("MIAODA_MODEL_TIMEOUT_SECONDS", "")).strip() or "1200"
            ),
            message_interval_timeout_seconds=int(
                str(
                    runtime.get("MIAODA_MESSAGE_INTERVAL_TIMEOUT_SECONDS", "")
                ).strip()
                or "1500"
            ),
            submission_batch_size=int(
                str(runtime.get("MIAODA_SUBMISSION_BATCH_SIZE", "")).strip() or "1"
            ),
            submission_batch_window_seconds=float(
                str(runtime.get("MIAODA_SUBMISSION_BATCH_WINDOW_SECONDS", "")).strip()
                or "0"
            ),
            explicit_batch_wait_timeout_seconds=float(
                str(runtime.get("MIAODA_EXPLICIT_BATCH_WAIT_TIMEOUT_SECONDS", "")).strip()
                or "120"
            ),
            datawheel_poll_cache_seconds=float(
                str(runtime.get("MIAODA_DATAWHEEL_POLL_CACHE_SECONDS", "")).strip()
                or "2"
            ),
            policy_capture_join_timeout_seconds=float(
                str(runtime.get("MIAODA_POLICY_CAPTURE_JOIN_TIMEOUT_SECONDS", "")).strip()
                or "120"
            ),
            trajectory_join_concurrency=int(
                str(runtime.get("MIAODA_TRAJECTORY_JOIN_CONCURRENCY", "")).strip()
                or "16"
            ),
        ),
    )


def _build_swe_backend(
    contract: OpenRewardBackendContract,
    runtime: Mapping[str, str],
    transports: Mapping[str, httpx.BaseTransport | httpx.AsyncBaseTransport],
) -> SweSandboxBackend:
    swe = contract.swe
    _require_service_mode("swe", swe.runtime_mode)
    return SweSandboxBackend(
        RemoteServiceConfig(
            environment="swe",
            base_url_env=swe.runtime_ref_env,
            auth_header_envs=_auth_headers(swe.auth_envs),
            create_operation=swe.create_operation,
            action_operations={
                "bash": swe.exec_operation,
                "read_file": swe.read_operation,
                "write_file": swe.write_operation,
                "submit": swe.verifier_operation,
            },
            destroy_operation=swe.destroy_operation,
            terminal_actions=frozenset({"submit"}),
            timeout_seconds=swe.timeout_seconds,
            verify_tls=swe.verify_tls,
            trust_env=swe.trust_env,
        ),
        environ=runtime,
        transport=_async_transport(transports.get("swe"), "swe"),
    )


def _build_ppt_backend(
    contract: OpenRewardBackendContract,
    runtime: Mapping[str, str],
    transports: Mapping[str, httpx.BaseTransport | httpx.AsyncBaseTransport],
) -> PptOfficeBackend:
    ppt = contract.ppt
    _require_service_mode("ppt", ppt.runtime_mode)
    return PptOfficeBackend(
        RemoteServiceConfig(
            environment="ppt",
            base_url_env=ppt.runtime_ref_env,
            auth_header_envs=_auth_headers(ppt.auth_envs),
            create_operation=ppt.create_operation,
            action_operations={
                "bash": ppt.exec_operation,
                "read_file": ppt.file_read_operation,
                "write_file": ppt.file_write_operation,
                "edit_file": ppt.file_edit_operation,
                "ppt_read": ppt.read_operation,
                "ppt_write": ppt.write_operation,
                "render": ppt.render_operation,
                "submit_deliverable": ppt.submit_operation,
            },
            destroy_operation=ppt.destroy_operation,
            terminal_actions=frozenset({"submit_deliverable"}),
            timeout_seconds=ppt.timeout_seconds,
            verify_tls=ppt.verify_tls,
            trust_env=ppt.trust_env,
        ),
        environ=runtime,
        transport=_async_transport(transports.get("ppt"), "ppt"),
    )


def _build_tooluse_backend(
    contract: OpenRewardBackendContract,
    runtime: Mapping[str, str],
    transports: Mapping[str, httpx.BaseTransport | httpx.AsyncBaseTransport],
) -> EnvironmentBackend:
    tooluse = contract.tooluse
    if tooluse.adapter == "openreward-proxy":
        return RemoteORSBackend(
            RemoteORSConfig(
                environment="tooluse",
                remote_environment=tooluse.remote_environment,
                base_url_env=tooluse.service_base_url_env,
                auth_header_envs=_auth_headers(tooluse.auth_envs),
                timeout_seconds=tooluse.timeout_seconds,
                verify_tls=tooluse.verify_tls,
                trust_env=tooluse.trust_env,
            ),
            environ=runtime,
            transport=_async_transport(transports.get("tooluse"), "tooluse"),
        )
    return ToolRegistryBackend(
        RemoteServiceConfig(
            environment="tooluse",
            base_url_env=tooluse.service_base_url_env,
            auth_header_envs=_auth_headers(tooluse.auth_envs),
            create_operation=tooluse.reset_operation,
            tools_operation=tooluse.registry_operation,
            action_operations={
                "invoke_tool": tooluse.invoke_operation,
                "get_state": tooluse.state_operation,
                "submit": tooluse.verifier_operation,
            },
            dynamic_action_operation=tooluse.invoke_operation,
            destroy_operation=tooluse.destroy_operation,
            terminal_actions=frozenset({"submit"}),
            timeout_seconds=tooluse.timeout_seconds,
            verify_tls=tooluse.verify_tls,
            trust_env=tooluse.trust_env,
        ),
        environ=runtime,
        transport=_async_transport(transports.get("tooluse"), "tooluse"),
    )


def _async_transport(
    value: httpx.BaseTransport | httpx.AsyncBaseTransport | None,
    domain: str,
) -> httpx.AsyncBaseTransport | None:
    if value is None:
        return None
    if not isinstance(value, httpx.AsyncBaseTransport):
        raise OpenRewardRuntimeError(f"{domain} transport must be asynchronous")
    return value


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            sensitive_task_key(key) or _contains_sensitive_key(child)
            for key, child in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def load_task_catalogs(
    path: str | Path,
) -> dict[str, dict[str, tuple[dict[str, Any], ...]]]:
    """Load a four-domain YAML/JSON task catalog without allowing secrets."""

    source = Path(path)
    try:
        if source.suffix.casefold() == ".json":
            payload = json.loads(source.read_text(encoding="utf-8"))
        else:
            payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise OpenRewardRuntimeError(
            f"cannot read OpenReward task catalog: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise OpenRewardRuntimeError("OpenReward task catalog root must be an object")
    if "environments" in payload:
        if payload.get("version", 1) != 1:
            raise OpenRewardRuntimeError("unsupported OpenReward task catalog version")
        payload = payload.get("environments")
    if not isinstance(payload, Mapping):
        raise OpenRewardRuntimeError("OpenReward task catalog environments must be an object")
    unknown_domains = sorted(str(item) for item in payload if str(item) not in _DOMAINS)
    if unknown_domains:
        raise OpenRewardRuntimeError("OpenReward task catalog contains an unknown environment")
    catalogs: dict[str, dict[str, tuple[dict[str, Any], ...]]] = {}
    for domain, raw_splits in payload.items():
        if not isinstance(raw_splits, Mapping):
            raise OpenRewardRuntimeError("OpenReward environment catalog must be an object")
        split_catalog: dict[str, tuple[dict[str, Any], ...]] = {}
        for split, raw_tasks in raw_splits.items():
            split_name = str(split)
            if split_name not in _SPLITS:
                raise OpenRewardRuntimeError("OpenReward task catalog contains an unknown split")
            if not isinstance(raw_tasks, Sequence) or isinstance(raw_tasks, (str, bytes)):
                raise OpenRewardRuntimeError("OpenReward split tasks must be an array")
            tasks: list[dict[str, Any]] = []
            for raw_task in raw_tasks:
                if not isinstance(raw_task, Mapping):
                    raise OpenRewardRuntimeError("OpenReward task must be an object")
                if _contains_sensitive_key(raw_task):
                    raise OpenRewardRuntimeError(
                        "OpenReward task catalog contains a credential-like field"
                    )
                tasks.append(dict(raw_task))
            split_catalog[split_name] = tuple(tasks)
        catalogs[str(domain)] = split_catalog
    return catalogs


def load_task_catalogs_many(
    paths: Sequence[str | Path],
) -> dict[str, dict[str, tuple[dict[str, Any], ...]]]:
    """Load and merge multiple domain catalogs without ambiguous selectors.

    Catalog order and task order are preserved.  A repeated ``task_id`` in the
    same domain/split is rejected so a Slime ``split/index`` selector cannot
    silently resolve to a different benchmark task after deployment.
    """

    merged: dict[str, dict[str, list[dict[str, Any]]]] = {}
    seen: dict[tuple[str, str], set[str]] = {}
    for path in paths:
        for domain, splits in load_task_catalogs(path).items():
            output_splits = merged.setdefault(domain, {})
            for split, tasks in splits.items():
                output_tasks = output_splits.setdefault(split, [])
                identifiers = seen.setdefault((domain, split), set())
                for task in tasks:
                    task_id = str(task.get("task_id") or "").strip()
                    if task_id and task_id in identifiers:
                        raise OpenRewardRuntimeError(
                            "OpenReward task catalogs contain a duplicate task_id"
                        )
                    if task_id:
                        identifiers.add(task_id)
                    output_tasks.append(dict(task))
    return {
        domain: {
            split: tuple(tasks)
            for split, tasks in splits.items()
        }
        for domain, splits in merged.items()
    }


def build_environments_from_contract(
    contract: OpenRewardBackendContract | str | Path,
    *,
    real_domains: Sequence[str] | None = None,
    task_catalogs: Mapping[
        str,
        Mapping[str, Sequence[Mapping[str, Any]]],
    ]
    | None = None,
    task_catalog_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    transports: Mapping[str, httpx.BaseTransport | httpx.AsyncBaseTransport] | None = None,
) -> dict[str, type[ORSEnvironment]]:
    """Return a server-ready four-domain registry using real backend bridges."""

    if not isinstance(contract, OpenRewardBackendContract):
        contract = load_backend_contract(contract)
    if task_catalogs is not None and task_catalog_path is not None:
        raise OpenRewardRuntimeError("provide task_catalogs or task_catalog_path, not both")
    catalogs = task_catalogs
    if task_catalog_path is not None:
        catalogs = load_task_catalogs(task_catalog_path)
    backends = build_backends_from_contract(
        contract,
        domains=real_domains,
        environ=environ,
        transports=transports,
    )
    return build_default_environments(
        backends=backends,
        task_catalogs=catalogs,
    )

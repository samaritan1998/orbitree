#!/usr/bin/env python3
"""Run one resumable OpenReward MOPD cycle through cluster-d LTP jobs.

The controller deliberately keeps environment rollout, optimizer, checkpoint
conversion, serving, and post-update rollout as separate jobs.  Fresh rollout
artifacts are phase handoffs, not an offline dataset: the next phase starts as
soon as its upstream receipt is durable.  Runtime credentials remain inside
the referenced LTP bundles and are never read by this process.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from research_ops.openreward.teacher_routing import (  # noqa: E402
    TeacherRoutingError,
    load_teacher_routing,
    materialize_runtime_routes,
    write_runtime_routes,
)

DEFAULT_PREFLIGHT = Path(
    "/root/.codex/skills/chaoxi-infra-shared/scripts/check_ltp_bundle.py"
)
SUCCESS_STATES = {"Succeeded", "Completed"}
FAILURE_STATES = {"Failed", "Stopped", "Deleted", "Cancelled", "Canceled"}


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(name, None)
    return env


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def _load_object(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one object")
    return value


def _resolve(base: Path, raw: str | Path) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _parse_job_id(output: str) -> str:
    match = re.search(r"^任务目录:\s*(\S+)\s*$", output, flags=re.MULTILINE)
    if match:
        return Path(match.group(1)).name
    match = re.search(
        r"^(?:job(?:_id)?|任务(?:ID|名称))\s*[:：]\s*([A-Za-z0-9._-]+)\s*$",
        output,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if match:
        return match.group(1)
    raise RuntimeError("ltp-cli submit returned no parseable job id")


def _parse_status(output: str) -> str:
    matches = re.findall(r"^status:\s*(\S+)\s*$", output, flags=re.MULTILINE)
    if not matches:
        raise RuntimeError("ltp-cli show returned no status")
    return matches[-1]


def _json_ready(path: Path, expected: dict[str, Any]) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(value, dict):
        return False
    return all(value.get(key) == item for key, item in expected.items())


def _is_ready(spec: dict[str, Any], base: Path) -> bool:
    path = _resolve(base, str(spec["path"]))
    kind = str(spec.get("kind") or "file")
    if kind == "dir":
        if not path.is_dir():
            return False
        required = [str(item) for item in spec.get("required") or []]
        return all((path / item).exists() for item in required)
    if not path.is_file():
        return False
    if kind == "json":
        expected = spec.get("equals") or {}
        if not isinstance(expected, dict):
            raise ValueError("ready.equals must be an object")
        return _json_ready(path, expected)
    return path.stat().st_size > 0


def _read_json_key(path: Path, dotted_key: str) -> Any:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    for part in dotted_key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(f"{path}: missing JSON key {dotted_key}")
        value = value[part]
    return value


def _materialize_teacher_routes(
    manifest: dict[str, Any],
    base: Path,
) -> tuple[Path | None, set[str]]:
    """Resolve static teacher lineage to a runtime-only route file.

    Endpoint URLs remain in the derived file and are never copied into an LTP
    bundle or controller state receipt.  The per-domain pod performs the final
    reachability check from its own network namespace.
    """

    raw = manifest.get("teacher_routing")
    if raw is None:
        return None, set()
    if not isinstance(raw, dict):
        raise ValueError("teacher_routing must be an object")
    allowed = {"config", "output", "check_health", "timeout_seconds"}
    if set(raw) - allowed or "config" not in raw or "output" not in raw:
        raise ValueError("teacher_routing must declare config and output only")
    check_health = raw.get("check_health", False)
    if not isinstance(check_health, bool):
        raise ValueError("teacher_routing.check_health must be a boolean")
    timeout_seconds = float(raw.get("timeout_seconds", 8.0))
    if timeout_seconds <= 0:
        raise ValueError("teacher_routing.timeout_seconds must be positive")
    try:
        routing = load_teacher_routing(_resolve(base, str(raw["config"])))
        routes = materialize_runtime_routes(
            routing,
            check_health=check_health,
            timeout_seconds=timeout_seconds,
        )
    except TeacherRoutingError as exc:
        raise ValueError(f"teacher routing is not ready: {exc}") from exc
    output = _resolve(base, str(raw["output"]))
    write_runtime_routes(output, routes)
    return output, set(routing.routes)


def _materialize_config(
    phase: dict[str, Any],
    base: Path,
    *,
    teacher_routes_file: Path | None = None,
    teacher_domains: set[str] | None = None,
) -> Path:
    raw_config = phase.get("config")
    raw_template = phase.get("config_template")
    if bool(raw_config) == bool(raw_template):
        raise ValueError(
            f"phase {phase.get('name')!r} must set exactly one of config/config_template"
        )
    raw_domain = phase.get("domain")
    domain = str(raw_domain).strip().casefold() if raw_domain is not None else ""
    inject_teacher_routes = teacher_routes_file is not None and bool(domain)
    if inject_teacher_routes and domain not in (teacher_domains or set()):
        raise ValueError(f"phase {phase.get('name')!r} has no configured teacher route")

    source = _resolve(base, str(raw_config or raw_template))
    if raw_config and not inject_teacher_routes:
        return source
    if "config_output" not in phase:
        raise ValueError(
            f"phase {phase.get('name')!r} needs config_output for generated config"
        )
    output = _resolve(base, str(phase["config_output"]))
    document = _load_object(source)
    envargs = document.get("envargs")
    if not isinstance(envargs, dict):
        raise ValueError(f"{source}: envargs must be an object")
    if inject_teacher_routes:
        route_env = str(phase.get("teacher_routes_env") or "teacher_routes_file").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", route_env):
            raise ValueError("teacher_routes_env must be a lowercase LTP envarg name")
        # Historical configs point at one stale endpoint receipt. A four-domain
        # launch must route in the rollout process, never leave both mechanisms
        # active and silently prefer the old one.
        envargs.pop("teacher_endpoint_file", None)
        envargs[route_env] = str(teacher_routes_file)
    bindings = phase.get("env_from_json") or {}
    if not isinstance(bindings, dict):
        raise ValueError("env_from_json must be an object")
    for env_name, binding in bindings.items():
        if not isinstance(binding, dict):
            raise ValueError(f"env_from_json.{env_name} must be an object")
        source = _resolve(base, str(binding["path"]))
        value = _read_json_key(source, str(binding["key"]))
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"env_from_json.{env_name} resolved to a non-scalar")
        rendered = (
            str(binding.get("prefix") or "")
            + str(value)
            + str(binding.get("suffix") or "")
        )
        if env_name.endswith("url") and (
            "localhost" in rendered or "127.0.0.1" in rendered
        ):
            raise ValueError(f"env_from_json.{env_name} is not externally reachable")
        envargs[str(env_name)] = rendered
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return output


def _run(command: list[str], *, timeout: int | None = None) -> str:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_clean_env(),
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n{output[-4000:]}"
        )
    return output


def _preflight(config: Path, run: Path, preflight: Path) -> None:
    command = [
        sys.executable,
        str(preflight),
        str(config),
        "--run",
        str(run),
        "--strict",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=_clean_env(),
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode == 0:
        return
    warnings = [line for line in output.splitlines() if line.startswith("WARN:")]
    allowed_warning = (
        "WARN: environment-bound scalar jobargs.gpus_per_node has YAML type int; "
        "quote it as a string"
    )
    if (
        completed.returncode == 1
        and warnings
        and all(line == allowed_warning for line in warnings)
        and "ERROR:" not in output
    ):
        # The current LTP contract requires gpus_per_node to remain a YAML
        # integer.  The shared checker intentionally reports that mismatch;
        # no other strict warning is accepted here.
        return
    raise RuntimeError(
        f"strict LTP preflight failed ({completed.returncode}): {config}\n{output[-4000:]}"
    )


def _submit(config: Path, run: Path, *, user: str) -> str:
    output = _run(
        [
            "ltp-cli",
            "submit",
            "--config",
            str(config),
            "--run",
            str(run),
            "--cluster",
            "cluster-d",
            "--user",
            user,
        ],
        timeout=120,
    )
    return _parse_job_id(output)


def _show(job_id: str, *, user: str) -> str:
    return _parse_status(
        _run(["ltp-cli", "show", job_id, "--user", user], timeout=60)
    )


def _wait_phase(
    phase: dict[str, Any],
    *,
    job_id: str,
    user: str,
    base: Path,
    state: dict[str, Any],
    state_path: Path,
    default_poll_seconds: int,
) -> None:
    timeout_seconds = int(phase.get("timeout_seconds") or 14400)
    poll_seconds = int(phase.get("poll_seconds") or default_poll_seconds)
    ready_while_running = bool(phase.get("ready_while_running", False))
    deadline = time.monotonic() + timeout_seconds
    last_status: str | None = None
    while time.monotonic() < deadline:
        ready = _is_ready(phase["ready"], base)
        status = _show(job_id, user=user)
        if status != last_status:
            print(
                json.dumps(
                    {"phase": phase["name"], "job_id": job_id, "status": status},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            last_status = status
        phase_state = state["phases"][phase["name"]]
        phase_state["status"] = status
        phase_state["ready"] = ready
        _atomic_json(state_path, state)
        if status in FAILURE_STATES:
            # A fused probe may intentionally run the online rollout and write
            # its durable receipt before the optional in-process optimizer
            # exits on a known runtime error. Only an explicitly opted-in
            # phase may consume that receipt; ordinary phases fail closed.
            if ready and bool(phase.get("allow_ready_after_failure", False)):
                phase_state["completed"] = True
                phase_state["ready_after_failure"] = True
                _atomic_json(state_path, state)
                print(
                    json.dumps(
                        {
                            "phase": phase["name"],
                            "job_id": job_id,
                            "status": status,
                            "accepted_durable_receipt": True,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return
            raise RuntimeError(f"phase {phase['name']} failed in LTP state {status}")
        if ready and (status in SUCCESS_STATES or ready_while_running):
            phase_state["completed"] = True
            _atomic_json(state_path, state)
            return
        if status in SUCCESS_STATES and not ready:
            raise RuntimeError(
                f"phase {phase['name']} succeeded but its durable receipt is missing"
            )
        time.sleep(poll_seconds)
    raise TimeoutError(f"phase {phase['name']} timed out after {timeout_seconds}s")


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if int(manifest.get("version") or 0) != 1:
        raise ValueError("cycle manifest version must be 1")
    if manifest.get("cluster", "cluster-d") != "cluster-d":
        raise ValueError("OpenReward MOPD cycles are pinned to cluster-d")
    phases = manifest.get("phases")
    if not isinstance(phases, list) or not phases:
        raise ValueError("cycle manifest must contain phases")
    names = [str(item.get("name") or "") for item in phases if isinstance(item, dict)]
    if len(names) != len(phases) or any(not name for name in names):
        raise ValueError("every phase must be an object with a name")
    if len(set(names)) != len(names):
        raise ValueError("phase names must be unique")
    teacher_routing = manifest.get("teacher_routing")
    if teacher_routing is not None and not isinstance(teacher_routing, dict):
        raise ValueError("teacher_routing must be an object")
    for phase in phases:
        if not isinstance(phase.get("ready"), dict) or "path" not in phase["ready"]:
            raise ValueError(f"phase {phase['name']} must declare a durable ready path")


def _verify_transition(spec: dict[str, Any], base: Path) -> None:
    script = _resolve(
        PROJECT_ROOT,
        str(spec.get("script") or "scripts/verify_online_mopd_transition.py"),
    )
    _run(
        [
            sys.executable,
            str(script),
            "--before",
            str(_resolve(base, spec["before"])),
            "--after",
            str(_resolve(base, spec["after"])),
            "--gateway-receipt",
            str(_resolve(base, spec["gateway_receipt"])),
            "--output",
            str(_resolve(base, spec["output"])),
        ],
        timeout=120,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, default=DEFAULT_PREFLIGHT)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest_path = args.manifest.expanduser().resolve(strict=True)
    base = manifest_path.parent
    manifest = _load_object(manifest_path)
    _validate_manifest(manifest)
    teacher_routes_file, teacher_domains = _materialize_teacher_routes(manifest, base)
    user = str(manifest.get("user") or os.environ.get("USER") or "").strip()
    if not user:
        raise ValueError("cycle manifest must declare user")
    state_path = _resolve(base, manifest.get("state_path") or "mopd_cycle_state.json")
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("manifest") != str(manifest_path):
            raise ValueError("existing state file belongs to a different manifest")
    else:
        state = {
            "version": 1,
            "manifest": str(manifest_path),
            "cluster": "cluster-d",
            "user": user,
            "phases": {},
        }
    if teacher_routes_file is not None:
        state["teacher_routing"] = {
            "route_file": str(teacher_routes_file),
            "domains": sorted(teacher_domains),
        }

    for phase in manifest["phases"]:
        name = phase["name"]
        run = _resolve(base, phase["run"])
        config = _materialize_config(
            phase,
            base,
            teacher_routes_file=teacher_routes_file,
            teacher_domains=teacher_domains,
        )
        if not run.is_file() or not config.is_file():
            raise FileNotFoundError(f"phase {name} bundle is incomplete")
        _preflight(config, run, args.preflight.expanduser().resolve(strict=True))
        phase_state = state["phases"].setdefault(
            name,
            {"config": str(config), "run": str(run), "completed": False},
        )
        if _is_ready(phase["ready"], base):
            phase_state.update({"ready": True, "completed": True})
            if not args.dry_run:
                _atomic_json(state_path, state)
            print(json.dumps({"phase": name, "status": "durable_ready"}), flush=True)
            continue
        if args.dry_run:
            print(json.dumps({"phase": name, "status": "preflight_ok"}), flush=True)
            continue
        job_id = str(phase_state.get("job_id") or "")
        if not job_id:
            job_id = _submit(config, run, user=user)
            phase_state.update({"job_id": job_id, "status": "Submitted"})
            _atomic_json(state_path, state)
            print(json.dumps({"phase": name, "submitted": job_id}), flush=True)
        _wait_phase(
            phase,
            job_id=job_id,
            user=user,
            base=base,
            state=state,
            state_path=state_path,
            default_poll_seconds=args.poll_seconds,
        )

    if args.dry_run:
        return 0
    verification = manifest.get("verification")
    if isinstance(verification, dict):
        _verify_transition(verification, base)
        state["verified"] = True
        state["verification_output"] = str(_resolve(base, verification["output"]))
        _atomic_json(state_path, state)
    print(json.dumps({"status": "completed", "state": str(state_path)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

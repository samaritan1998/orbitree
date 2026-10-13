#!/usr/bin/env python3
"""Run one real Miaoda AgentOS episode and materialize a pure-MOPD Sample.

AgentOS owns the multi-turn application execution.  The ORS policy proxy
captures output token bytes and behavior logprobs without retaining
prompt/response text; this runner joins those records to the downloaded coding
trajectory by prompt hash and queries the teacher for the same token suffix.
Goblin reward is optional monitoring evidence and never gates pure MOPD.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import math
import os
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from research_ops.agentos import AgentOSClient, AgentOSClientError
from research_ops.openreward import ORSClient, ORSClientError
from research_ops.openreward.teacher_routing import (
    TeacherRoutingError,
    validate_runtime_teacher_route,
)
from research_ops.reward import GoblinRewardError, load_reward_records


def _sha256(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_id(value: Any) -> str:
    text = str(value or "").strip()
    return "".join(char if (char.isalnum() or char in "._:-") else "_" for char in text)[:160]


def _tool_output(result: Any) -> dict[str, Any]:
    """Unwrap one successful ORS tool response or fail without empty polling."""

    if not isinstance(result, dict):
        raise RuntimeError("ORS tool result is not an object")
    if result.get("ok") is False:
        reason = _safe_id(result.get("reason") or result.get("error") or "remote_tool_error")
        raise RuntimeError(f"ORS tool call failed: {reason}")
    output = result.get("output")
    if not isinstance(output, dict):
        raise RuntimeError("ORS tool call omitted output")
    return output


def _read_task(path: Path, index: int) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict) and isinstance(value.get("environments"), dict):
        value = value["environments"].get("miaoda", {}).get("train", [])
    # A single-query canary materialized by materialize_miaoda_task_spec.py is
    # intentionally a task object rather than a benchmark list.  Accept it as
    # a one-element list so the same runner handles both a bundle and a
    # one-episode online probe.
    elif isinstance(value, dict) and str(value.get("query") or value.get("prompt") or "").strip():
        value = [value]
    if not isinstance(value, list) or index < 0 or index >= len(value):
        raise RuntimeError("Miaoda task spec does not contain the selected task")
    task = value[index]
    if not isinstance(task, dict):
        raise RuntimeError("selected Miaoda task is not an object")
    if not str(task.get("query") or task.get("prompt") or "").strip():
        raise RuntimeError("Miaoda task requires a query")
    task = dict(task)
    task.setdefault("episode_id", f"miaoda-online-{index:03d}")
    return task


def _capture_paths(
    capture_dir: Path,
    episode_id: str,
    *,
    capture_hint: str = "",
) -> list[Path]:
    """Discover local and ORS-shared capture files for one episode.

    AgentOS may route different turns through separate policy proxies.  The
    proxies therefore write to shared roots, and a gateway that strips query
    parameters names those files ``capture-unattributed.jsonl``.  Use the
    terminal path as the primary hint, while allowing explicitly configured
    additional roots for multi-replica serving.  Timestamp filtering and
    capture-id deduplication happen in ``_load_captures``.
    """

    safe = _safe_id(episode_id) or "unattributed"
    roots: list[Path] = [capture_dir]
    candidates: list[Path] = []

    hint = Path(str(capture_hint).strip()).expanduser() if str(capture_hint).strip() else None
    if hint is not None:
        if hint.is_file():
            candidates.append(hint)
        elif hint.parent.is_dir():
            roots.append(hint.parent)

    configured = os.environ.get("MOPD_POLICY_CAPTURE_DIRS", "")
    for raw in configured.split(os.pathsep):
        value = raw.strip()
        if value:
            roots.append(Path(value).expanduser())

    seen: set[Path] = set()
    for root in roots:
        exact = root / f"capture-{safe}.jsonl"
        paths = [exact] if exact.is_file() else sorted(root.glob("capture-*.jsonl"))
        for path in paths:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                candidates.append(path)
    return sorted(candidates, key=lambda path: (path.stat().st_mtime, str(path)))


def _capture_path(capture_dir: Path, episode_id: str) -> Path | None:
    """Backward-compatible single-path view for callers and diagnostics."""

    paths = _capture_paths(capture_dir, episode_id)
    return paths[-1] if paths else None


def _load_captures(
    path: Path | Sequence[Path],
    *,
    started_at: float,
) -> list[dict[str, Any]]:
    paths = [path] if isinstance(path, Path) else list(path)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in paths:
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not (
                isinstance(value, dict)
                and value.get("schema_version")
                in {"mopd-policy-capture.v1", "mopd-policy-capture.v2"}
                and _capture_timestamp(value) >= started_at
            ):
                continue
            capture_id = str(value.get("capture_id") or "").strip()
            key = ("id", capture_id) if capture_id else ("row", _sha256(value))
            if key in seen:
                continue
            seen.add(key)
            rows.append(value)
    rows.sort(key=_capture_timestamp)
    return rows


def _capture_timestamp(value: Mapping[str, Any]) -> float:
    raw = str(value.get("captured_at") or "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _trajectory_messages(value: Any) -> dict[str, list[dict[str, Any]]]:
    """Index exact request messages and their valid prefixes by hash."""

    result: dict[str, list[dict[str, Any]]] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            body = node.get("llm_request_body")
            if isinstance(body, dict):
                params = body.get("call_llm_params")
                params = params if isinstance(params, dict) else body
                messages = params.get("messages")
                if isinstance(messages, list) and all(isinstance(item, dict) for item in messages):
                    _index_messages(messages, result)
            for child in node.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    def _index_messages(
        messages: list[dict[str, Any]], target: dict[str, list[dict[str, Any]]]
    ) -> None:
        target.setdefault(_sha256(messages), messages)
        # AgentOS trajectory endpoints often return one final full history;
        # each prefix ending before an assistant turn is a prior policy query.
        for index, item in enumerate(messages):
            if str(item.get("role") or "").casefold() == "assistant" and index > 0:
                prefix = messages[:index]
                target.setdefault(_sha256(prefix), prefix)

    visit(value)
    return result


def _trajectory_url(record: Mapping[str, Any]) -> str:
    for key in ("coding模型轨迹下载地址", "coding_trajectory_url", "trajectory_url"):
        value = str(record.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


def _trajectory_app_ids(row: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("app_id", "generated_app_id"):
        value = _safe_id(row.get(key))
        if value and value not in values:
            values.append(value)
    raw_output = row.get("Pipeline输出结果")
    if isinstance(raw_output, str):
        with contextlib.suppress(TypeError, ValueError):
            raw_output = json.loads(raw_output)
    if isinstance(raw_output, Mapping):
        def visit(node: Any) -> None:
            if isinstance(node, Mapping):
                for key, value in node.items():
                    if str(key).casefold() in {"app_id", "generated_app_id"}:
                        candidate = _safe_id(value)
                        if candidate and candidate not in values:
                            values.append(candidate)
                    if isinstance(value, (Mapping, list)):
                        visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(raw_output)
    return values


def _load_trajectory_from_artifact(
    artifact: Path,
    *,
    datawheel: AgentOSClient | None = None,
) -> dict[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    with contextlib.suppress(GoblinRewardError, OSError, ValueError):
        rows.extend(load_reward_records(artifact))
    candidates = [artifact]
    if artifact.suffix.casefold() in {".xlsx", ".xls"}:
        candidates.extend(
            [
                artifact.with_suffix(".jsonl"),
                artifact.parent / (artifact.stem + "-normalized.jsonl"),
            ]
        )
        candidates.extend(sorted(artifact.parent.glob("datawheel-*.jsonl")))
    for candidate in candidates:
        if (
            candidate == artifact
            or not candidate.is_file()
            or candidate.suffix.casefold() not in {".jsonl", ".ndjson"}
        ):
            continue
        try:
            rows.extend(load_reward_records(candidate))
        except (GoblinRewardError, OSError, ValueError):
            continue
    for row in rows:
        url = _trajectory_url(row)
        if url:
            try:
                response = httpx.get(url, timeout=120, follow_redirects=True, trust_env=False)
                response.raise_for_status()
                messages = _trajectory_messages(response.json())
            except (httpx.HTTPError, ValueError, TypeError):
                messages = {}
            if messages:
                return messages
        if datawheel is not None:
            for app_id in _trajectory_app_ids(row):
                try:
                    messages = _trajectory_messages(datawheel.get_trajectory(app_id))
                except (AgentOSClientError, OSError, ValueError, TypeError):
                    continue
                if messages:
                    return messages
    return {}


async def _resolve_artifact(
    args: argparse.Namespace,
    artifact: Path,
    terminal: Mapping[str, Any],
    *,
    skip_remote_fetch: bool = False,
) -> Path:
    """Resolve a pure-OPD Datawheel workbook after ORS terminalization.

    ORS returns the durable batch id even when its pod cannot reach the
    short-lived signed download URL.  Prefer the shared path, then retry the
    download from the LTP pod using a runtime-only Datawheel token reference.
    No token or signed URL is written to evidence.
    """

    candidates: list[Path] = []
    if artifact and str(artifact) not in {"", "."}:
        candidates.append(artifact)
    batch_id = _safe_id(terminal.get("miaoda_batch_task_id"))
    if batch_id:
        candidates.append(args.capture_dir / "artifacts" / f"datawheel-{batch_id}.xlsx")
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate

    # v2 policy captures already carry the exact prompt/response token IDs and
    # behavior logprobs needed by pure OPD.  Do not hold an otherwise ready
    # episode behind a signed workbook download that is only needed for the
    # legacy trajectory-to-token join path.
    if skip_remote_fetch:
        return candidates[0] if candidates else artifact

    if batch_id:
        token_env = str(args.datawheel_token_env or "").strip()
        base_url = str(args.datawheel_url or "").strip()
        if base_url and token_env and os.environ.get(token_env, "").strip():
            destination = candidates[-1]
            try:
                client = AgentOSClient(
                    base_url,
                    token_env=token_env,
                    timeout_seconds=max(args.ors_timeout, 120.0),
                    trust_env=False,
                )
                await asyncio.to_thread(client.download_result, batch_id, destination)
            except (AgentOSClientError, OSError, ValueError):
                pass
            if destination.is_file() and destination.stat().st_size > 0:
                return destination

    deadline = time.monotonic() + max(float(args.artifact_grace_seconds), 0.0)
    while time.monotonic() < deadline:
        for candidate in candidates:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        await asyncio.sleep(2.0)
    return candidates[0] if candidates else artifact


def _decode_capture_tokens(
    capture: Mapping[str, Any], tokenizer: Any
) -> tuple[list[int], list[float]]:
    raw = capture.get("tokens")
    if not isinstance(raw, list):
        return [], []
    blob = bytearray()
    logprobs: list[float] = []
    for item in raw:
        if not isinstance(item, dict):
            return [], []
        try:
            blob.extend(base64.b64decode(str(item.get("bytes_b64") or ""), validate=True))
            value = float(item["logprob"])
        except (TypeError, ValueError, KeyError, OverflowError):
            return [], []
        if not math.isfinite(value):
            return [], []
        logprobs.append(value)
    try:
        text = bytes(blob).decode("utf-8")
    except UnicodeDecodeError:
        return [], []
    token_ids = tokenizer.encode(text, add_special_tokens=False)
    if len(token_ids) != len(logprobs):
        return [], []
    return [int(item) for item in token_ids], logprobs


def _direct_capture_tokens(
    capture: Mapping[str, Any],
) -> tuple[list[int], list[int], list[float]]:
    """Read the exact tokenizer-side v2 capture without replay text joining."""

    if capture.get("schema_version") != "mopd-policy-capture.v2":
        return [], [], []
    prompt = capture.get("prompt_token_ids")
    response = capture.get("response_token_ids")
    behavior = capture.get("behavior_logprobs")
    if (
        not isinstance(prompt, list)
        or not isinstance(response, list)
        or not isinstance(behavior, list)
    ):
        return [], [], []
    try:
        prompt_ids = [int(value) for value in prompt]
        response_ids = [int(value) for value in response]
        logprobs = [float(value) for value in behavior]
    except (TypeError, ValueError, OverflowError):
        return [], [], []
    if (
        not prompt_ids
        or not response_ids
        or len(response_ids) != len(logprobs)
        or any(value < 0 for value in prompt_ids + response_ids)
        or any(not math.isfinite(value) for value in logprobs)
    ):
        return [], [], []
    return prompt_ids, response_ids, logprobs


def _tokenize_prompt(tokenizer: Any, messages: list[dict[str, Any]]) -> list[int]:
    try:
        value = tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True, enable_thinking=False
        )
    except (TypeError, ValueError):
        value = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
    if hasattr(value, "tolist"):
        value = value.tolist()
    return [int(item) for item in value]


def _teacher_values(body: Any, response_length: int) -> list[float]:
    meta = body.get("meta_info") if isinstance(body, dict) else None
    meta = meta if isinstance(meta, dict) else body if isinstance(body, dict) else {}
    # With max_new_tokens=0 SGLang scores the supplied full sequence and
    # returns input_token_logprobs. Retain output_* fallbacks for compatible
    # teacher gateways that expose the same values under the older names.
    raw = meta.get("input_token_logprobs")
    if not isinstance(raw, list):
        raw = meta.get("output_token_logprobs")
    if not isinstance(raw, list):
        raw = meta.get("output_logprobs")
    if not isinstance(raw, list):
        raise RuntimeError("teacher response omitted token logprobs")
    values: list[float] = []
    for item in raw:
        if isinstance(item, Mapping):
            value = item.get("logprob", item.get("log_prob"))
        else:
            value = item[0] if isinstance(item, (list, tuple)) and item else item
        if value is None:
            continue
        number = float(value)
        if not math.isfinite(number):
            raise RuntimeError("teacher response contained non-finite logprob")
        values.append(number)
    if len(values) < response_length:
        raise RuntimeError("teacher logprobs do not cover the response")
    return values[-response_length:]


async def _teacher(
    teacher_url: str,
    tokens: list[int],
    response_length: int,
    *,
    teacher_model: str | None = None,
) -> list[float]:
    if teacher_model and urlsplit(teacher_url).path.rstrip("/") == "/v1/chat/completions":
        payload = {
            "model": teacher_model,
            "messages": [
                {
                    "role": "user",
                    "content": "__openreward_input_ids__:" + json.dumps(
                        tokens, separators=(",", ":")
                    ),
                }
            ],
            "max_tokens": 1,
            "temperature": 0,
            "logprobs": True,
            "top_logprobs": 0,
        }
    else:
        payload = {
            "input_ids": tokens,
            "sampling_params": {
                "temperature": 0,
                "max_new_tokens": 0,
                "skip_special_tokens": False,
            },
            "return_logprob": True,
            "logprob_start_len": 0,
        }
    async with httpx.AsyncClient(timeout=3_600, trust_env=False) as client:
        response = await client.post(teacher_url, json=payload)
    response.raise_for_status()
    return _teacher_values(response.json(), response_length)


def _safe_metadata(task: Mapping[str, Any], terminal: Mapping[str, Any]) -> dict[str, Any]:
    pure_opd = (
        str(task.get("training_objective") or "").strip().casefold() == "pure_opd"
        or task.get("task_reward_required") is False
    )
    metadata = {
        "openreward_env": "miaoda",
        "openreward_mode": (
            "agentos-datawheel-pure-opd"
            if pure_opd
            else "agentos-datawheel-goblin"
        ),
        "openreward_task_id": _safe_id(task.get("episode_id") or task.get("id")),
        "miaoda_batch_task_id": _safe_id(terminal.get("miaoda_batch_task_id")),
        "miaoda_app_id": _safe_id(terminal.get("miaoda_app_id")),
        "miaoda_conv_id": _safe_id(terminal.get("miaoda_conv_id")),
        "goblin_job_id": _safe_id(terminal.get("goblin_job_id")),
        "reward_source": "pure_opd_neutral" if pure_opd else "goblin",
    }
    # Keep terminal diagnosis machine-readable without copying free-form
    # AgentOS/Goblin payloads (which may contain prompts or app content).
    for key in ("terminal_reason", "reward_label"):
        value = _safe_id(terminal.get(key))
        if value:
            metadata[key] = value
    if "scalar_reward_available" in terminal:
        metadata["scalar_reward_available"] = bool(terminal.get("scalar_reward_available"))
    if "task_reward_used" in terminal:
        metadata["task_reward_used"] = bool(terminal.get("task_reward_used"))
    return metadata


async def _recover_datawheel_terminal(
    args: argparse.Namespace,
    *,
    batch_task_id: str,
    polls: int,
) -> tuple[dict[str, Any], int]:
    """Finish polling an already-submitted batch after an ORS restart.

    ORS session state is intentionally in-memory.  A deployment restart must
    not invalidate a real Datawheel batch that is still running independently
    on AgentOS.  Once ``start_rollout`` has returned a durable batch ID, the
    runner can safely use the Datawheel control plane as a narrow recovery
    path and resume normal artifact/capture materialization afterwards.
    """

    if not args.datawheel_url or not args.datawheel_token_env:
        raise RuntimeError("cannot recover Miaoda polling without Datawheel settings")
    datawheel = AgentOSClient(
        args.datawheel_url,
        token_env=args.datawheel_token_env,
        timeout_seconds=max(args.ors_timeout, 120.0),
        trust_env=False,
    )
    terminal_statuses = {"completed", "failed", "cancelled", "canceled", "error", "stopped"}
    while polls < args.max_polls:
        snapshot = await asyncio.to_thread(datawheel.get_batch, batch_task_id)
        polls += 1
        status = str(snapshot.status or "unknown").strip().casefold()
        print(
            json.dumps(
                {
                    "poll": polls,
                    "finished": status in terminal_statuses,
                    "phase": "datawheel_recovery",
                    "status": status,
                    "miaoda_batch_task_id": _safe_id(batch_task_id),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if status in terminal_statuses:
            completed = status == "completed"
            return (
                {
                    "finished": True,
                    "reward": 0.0,
                    "metadata": {
                        "phase": "terminal",
                        "status": status,
                        "usable": completed,
                        "task_reward_used": False,
                        "miaoda_batch_task_id": _safe_id(batch_task_id),
                        "terminal_reason": "datawheel_recovery",
                        "scalar_reward_available": False,
                    },
                },
                polls,
            )
        await asyncio.sleep(max(args.poll_interval, 0.1))
    raise RuntimeError("Miaoda Datawheel recovery polling exceeded max_polls")


async def main_async(args: argparse.Namespace) -> int:
    task = _read_task(args.task_spec, args.task_index)
    if args.pure_opd:
        # Miaoda formal MOPD learns from teacher-aligned policy tokens.  The
        # source warehouse rows predate this flag, so make the objective
        # explicit at the AgentOS boundary instead of requiring a reward or a
        # successful Publish post-process.
        task = dict(task)
        task["training_objective"] = "pure_opd"
        task["task_reward_required"] = False
    episode_id = str(task.get("episode_id") or f"miaoda-online-{args.task_index:03d}")
    started = datetime.now(UTC)
    terminal: dict[str, Any] = {}
    polls = 0
    batch_task_id = _safe_id(args.resume_batch_task_id)
    try:
        if batch_task_id:
            terminal, polls = await _recover_datawheel_terminal(
                args,
                batch_task_id=batch_task_id,
                polls=polls,
            )
        else:
            client = ORSClient(
                args.ors_url,
                timeout_seconds=args.ors_timeout,
                token_env="ORS_API_KEY",
                trust_env=False,
            )
            with client.session("miaoda", task_spec=task) as session:
                started_call = session.call("start_rollout", {})
                while polls < args.max_polls:
                    try:
                        result = (
                            started_call if polls == 0 else session.call("poll_rollout", {})
                        )
                    except ORSClientError:
                        if not batch_task_id:
                            raise
                        terminal, polls = await _recover_datawheel_terminal(
                            args,
                            batch_task_id=batch_task_id,
                            polls=polls,
                        )
                        break
                    output = _tool_output(result)
                    polls += 1
                    metadata = (
                        output.get("metadata")
                        if isinstance(output.get("metadata"), dict)
                        else {}
                    )
                    observed_batch_id = str(
                        metadata.get("miaoda_batch_task_id") or ""
                    ).strip()
                    if observed_batch_id:
                        batch_task_id = observed_batch_id
                    progress = {
                        "poll": polls,
                        "finished": bool(output.get("finished")),
                        "phase": _safe_id(metadata.get("phase")),
                        "status": _safe_id(metadata.get("status")),
                    }
                    for key in ("miaoda_batch_task_id", "goblin_job_id"):
                        value = _safe_id(metadata.get(key))
                        if value:
                            progress[key] = value
                    print(json.dumps(progress, ensure_ascii=False), flush=True)
                    if bool(output.get("finished")):
                        terminal = output
                        break
                    time.sleep(max(args.poll_interval, 0.1))
                if not terminal:
                    raise RuntimeError("Miaoda ORS polling exceeded max_polls")
    except (ORSClientError, OSError, RuntimeError) as exc:
        _write_evidence(
            args.output,
            {
                "version": 1,
                "status": "failed",
                "error_type": type(exc).__name__,
                "polls": polls,
            },
        )
        return 2

    metadata = terminal.get("metadata") if isinstance(terminal.get("metadata"), dict) else {}
    observed_reward = terminal.get("reward")
    observed_reward = (
        float(observed_reward)
        if isinstance(observed_reward, (int, float)) and math.isfinite(float(observed_reward))
        else None
    )
    training_reward = 0.0
    usable = bool(metadata.get("usable"))
    artifact = Path(str(metadata.get("miaoda_artifact") or ""))
    capture_dir = args.capture_dir
    capture_paths = _capture_paths(
        capture_dir,
        episode_id,
        capture_hint=str(metadata.get("mopd_capture_file") or ""),
    )
    capture_started_at = (
        float(args.capture_started_at)
        if float(args.capture_started_at) > 0
        else started.timestamp()
    )
    captures = _load_captures(capture_paths, started_at=capture_started_at)
    direct_capture_ready = bool(captures) and all(
        bool(_direct_capture_tokens(capture_item)[0]) for capture_item in captures
    )
    artifact = await _resolve_artifact(
        args,
        artifact,
        metadata,
        skip_remote_fetch=bool(args.pure_opd) and direct_capture_ready,
    )
    evidence: dict[str, Any] = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "duration_seconds": (datetime.now(UTC) - started).total_seconds(),
        "environment": "miaoda",
        "episode_id": _safe_id(episode_id),
        "polls": polls,
        "finished": True,
        "training_objective": "pure_opd",
        "reward": training_reward,
        "task_reward_used": False,
        "observed_task_reward": observed_reward,
        "usable": usable,
        "terminal_metadata": _safe_metadata(task, metadata),
        "capture_file": str(capture_paths[-1]) if capture_paths else None,
        "capture_files": [str(path) for path in capture_paths],
        "capture_started_at": capture_started_at,
        "capture_count": len(captures),
        "artifact": str(artifact) if artifact else None,
    }
    if not usable:
        evidence["status"] = "unusable"
        _write_evidence(args.output, evidence)
        return 3
    if not captures:
        evidence.update({"status": "failed", "failure_reason": "policy_capture_missing"})
        _write_evidence(args.output, evidence)
        return 4

    try:
        import torch
        from slime.rollout.openreward_rollout import build_debug_rollout_dump
        from slime.utils.types import Sample

        datawheel = None
        if (
            args.datawheel_url
            and args.datawheel_token_env
            and os.environ.get(args.datawheel_token_env, "").strip()
        ):
            datawheel = AgentOSClient(
                args.datawheel_url,
                token_env=args.datawheel_token_env,
                timeout_seconds=max(args.ors_timeout, 120.0),
                trust_env=False,
            )
        needs_legacy_join = any(
            item.get("schema_version") != "mopd-policy-capture.v2"
            for item in captures
        )
        tokenizer = None
        if needs_legacy_join:
            from transformers import AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(
                args.checkpoint,
                trust_remote_code=True,
            )
        messages_by_hash = (
            _load_trajectory_from_artifact(artifact, datawheel=datawheel)
            if needs_legacy_join and artifact.is_file()
            else {}
        )
        samples: list[Any] = []
        unmatched = 0
        direct_capture_count = 0
        legacy_capture_count = 0
        teacher_counts: list[int] = []
        for index, capture_item in enumerate(captures):
            prompt_ids, response_ids, behavior = _direct_capture_tokens(capture_item)
            if prompt_ids:
                direct_capture_count += 1
            else:
                prompt_hash = str(capture_item.get("prompt_sha256") or "")
                messages = messages_by_hash.get(prompt_hash)
                if messages is None or tokenizer is None:
                    unmatched += 1
                    continue
                response_ids, behavior = _decode_capture_tokens(capture_item, tokenizer)
                if not response_ids or len(response_ids) != len(behavior):
                    unmatched += 1
                    continue
                prompt_ids = _tokenize_prompt(tokenizer, messages)
                legacy_capture_count += 1
            full_tokens = prompt_ids + response_ids
            teacher = await _teacher(
                args.teacher_url,
                full_tokens,
                len(response_ids),
                teacher_model=getattr(args, "teacher_model", None),
            )
            sample = Sample(
                index=index,
                prompt="",
                tokens=full_tokens,
                response_length=len(response_ids),
                loss_mask=[1] * len(response_ids),
                rollout_log_probs=behavior,
                teacher_log_probs=teacher,
                reward=training_reward,
                weight_versions=[str(capture_item.get("weight_version") or "")],
                status=Sample.Status.COMPLETED,
                metadata={
                    "openreward_env": "miaoda",
                    "openreward_finished": True,
                    "openreward_training_objective": "pure_opd",
                    "openreward_task_reward_used": False,
                    "openreward_reward_source": "pure_opd_neutral",
                    "openreward_policy_weight_version": str(
                        capture_item.get("weight_version") or ""
                    ),
                    "openreward_episode_id": _safe_id(episode_id),
                    "openreward_capture_count": len(captures),
                    "openreward_teacher_source": (
                        "sglang_routed" if args.teacher_routes is not None else "sglang"
                    ),
                },
            )
            if args.teacher_routes is not None and args.teacher_checkpoint:
                sample.metadata["openreward_teacher_domain"] = "miaoda"
                sample.metadata["openreward_teacher_checkpoint"] = args.teacher_checkpoint
            # Each AgentOS policy call is an independent OPD training group.
            # Keep this assignment compatible with Slime versions where
            # group_id is a dynamic Sample attribute.
            sample.group_id = index
            samples.append(sample)
            teacher_counts.append(len(teacher))
        evidence.update({
            "status": "completed" if samples and unmatched == 0 else "failed",
            "matched_capture_count": len(samples),
            "unmatched_capture_count": unmatched,
            "direct_token_capture_count": direct_capture_count,
            "legacy_trajectory_join_count": legacy_capture_count,
            "response_lengths": [int(s.response_length) for s in samples],
            "sample_token_counts": [len(s.tokens or []) for s in samples],
            "max_token_count": max((len(s.tokens or []) for s in samples), default=0),
            "behavior_logprob_counts": [len(s.rollout_log_probs or []) for s in samples],
            "teacher_logprob_counts": teacher_counts,
            "token_count": sum(len(s.tokens or []) for s in samples),
            "response_length": sum(int(s.response_length) for s in samples),
            "logprob_count": sum(len(s.rollout_log_probs or []) for s in samples),
            "finite_logprob_count": sum(
                sum(math.isfinite(float(value)) for value in (s.rollout_log_probs or []))
                for s in samples
            ),
            "teacher_logprob_count": sum(teacher_counts),
            "finite_teacher_logprob_count": sum(
                sum(math.isfinite(float(value)) for value in (s.teacher_log_probs or []))
                for s in samples
            ),
            "loss_mask_ones": sum(
                sum(int(bool(value)) for value in (s.loss_mask or []))
                for s in samples
            ),
            "group_ids": sorted(int(s.group_id) for s in samples),
            "unique_group_count": len({int(s.group_id) for s in samples}),
            "weight_versions": sorted({v for s in samples for v in s.weight_versions if v}),
        })
        if samples and unmatched == 0:
            payload = build_debug_rollout_dump(samples, rollout_id=0, include_text=False)
            args.sample_output.parent.mkdir(parents=True, exist_ok=True)
            torch.save(payload, args.sample_output)
            evidence["sample_artifact"] = str(args.sample_output)
            evidence["sample_artifact_sha256"] = hashlib.sha256(
                args.sample_output.read_bytes()
            ).hexdigest()
    except Exception as exc:
        evidence.update({"status": "failed", "error_type": type(exc).__name__})
    _write_evidence(args.output, evidence)
    return 0 if evidence.get("status") == "completed" else 5


def _write_evidence(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ors-url", default=os.environ.get("ORS_URL", ""))
    parser.add_argument("--task-spec", type=Path, required=True)
    parser.add_argument("--task-index", type=int, default=0)
    parser.add_argument(
        "--pure-opd",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use teacher token logprobs as the learning signal without Goblin/task reward.",
    )
    parser.add_argument(
        "--resume-batch-task-id",
        default="",
        help="Resume polling this durable Datawheel batch without submitting another rollout.",
    )
    parser.add_argument(
        "--capture-started-at",
        type=float,
        default=0.0,
        help="Original episode UNIX timestamp used when recovering shared captures.",
    )
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--teacher-url", default="")
    teacher_routes_env = os.environ.get(
        "SLIME_OPENREWARD_TEACHER_ROUTES_FILE",
        os.environ.get("TEACHER_ROUTES_FILE", ""),
    ).strip()
    parser.add_argument(
        "--teacher-routes",
        type=Path,
        default=(
            Path(teacher_routes_env)
            if teacher_routes_env
            else None
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-output", type=Path, required=True)
    parser.add_argument("--max-polls", type=int, default=720)
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--ors-timeout", type=float, default=180.0)
    parser.add_argument(
        "--datawheel-url",
        default=os.environ.get("MIAODA_DATAWHEEL_URL", ""),
    )
    parser.add_argument(
        "--datawheel-token-env",
        default=os.environ.get("MIAODA_DATAWHEEL_TOKEN_ENV", "DATAWHEEL_TOKEN"),
    )
    parser.add_argument("--artifact-grace-seconds", type=float, default=180.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.ors_url:
        raise SystemExit("--ors-url or ORS_URL is required")
    args.teacher_checkpoint = ""
    args.teacher_model = ""
    if args.teacher_routes is not None:
        try:
            route = validate_runtime_teacher_route(
                args.teacher_routes,
                domain="miaoda",
                check_health=True,
            )
        except TeacherRoutingError as exc:
            raise SystemExit(f"teacher route is not ready: {exc}") from exc
        args.teacher_url = route.endpoint
        args.teacher_checkpoint = route.checkpoint
        try:
            route_payload = json.loads(args.teacher_routes.read_text(encoding="utf-8"))
            route_entry = route_payload.get("routes", {}).get("miaoda", {})
            args.teacher_model = str(route_entry.get("model") or "").strip()
        except (OSError, TypeError, ValueError, AttributeError):
            args.teacher_model = ""
    if urlsplit(args.teacher_url).path.rstrip("/") not in {"/generate", "/v1/chat/completions"}:
        raise SystemExit(
            "--teacher-url or --teacher-routes must use /generate or /v1/chat/completions"
        )
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

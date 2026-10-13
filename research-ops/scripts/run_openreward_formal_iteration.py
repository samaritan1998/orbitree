#!/usr/bin/env python3
"""Run one four-domain online rollout collection inside a single LTP job."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERIC_RUNNER = PROJECT_ROOT / "scripts/run_slime_openreward_tooluse_live.py"
MIAODA_RUNNER = PROJECT_ROOT / "scripts/run_miaoda_online_mopd.py"
MERGE_RUNNER = PROJECT_ROOT / "scripts/merge_slime_openreward_samples.py"
VALIDATE_RUNNER = PROJECT_ROOT / "scripts/validate_slime_openreward_sample.py"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _safe_command_error(command: list[str], code: int) -> str:
    rendered = " ".join(
        "<path>" if item.startswith("/") else item for item in command
    )
    return f"command failed with exit {code}: {rendered}"


def _run(command: list[str], env: dict[str, str], log_path: Path, label: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{datetime.now(UTC).isoformat()}] {label}\n")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            print(f"[{label}] {line}", end="", flush=True)
        code = process.wait()
    if code != 0:
        raise RuntimeError(_safe_command_error(command, code))


def _require_env(env: dict[str, str], name: str) -> str:
    value = str(env.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"missing runtime reference: {name}")
    return value


def _task_count(pool: dict[str, Any], domain: str) -> int:
    environments = pool.get("environments")
    if not isinstance(environments, dict):
        raise ValueError("task pool has no environments object")
    entry = environments.get(domain)
    if not isinstance(entry, dict) or not isinstance(entry.get("train"), list):
        raise ValueError(f"task pool has no {domain}.train list")
    if not entry["train"]:
        raise ValueError(f"task pool has an empty {domain}.train list")
    return len(entry["train"])


def _evidence_status(path: Path, domain: str, index: int) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"{domain}[{index}] did not write evidence: {path}")
    evidence = load_json(path)
    if evidence.get("status") != "completed":
        raise RuntimeError(f"{domain}[{index}] evidence is not completed")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "status": evidence.get("status"),
        "sample_artifact": str(
            evidence.get("sample_artifact")
            or evidence.get("sample_artifact_path")
            or ""
        ),
        "sample_count": int(
            evidence.get("matched_capture_count")
            or evidence.get("unique_group_count")
            or 1
        ),
        "response_tokens": int(
            evidence.get("response_length") or evidence.get("response_tokens") or 0
        ),
        "weight_versions": list(evidence.get("weight_versions") or []),
    }


def _unusable_episode_status(path: Path, domain: str, index: int) -> dict[str, Any] | None:
    """Summarize an upstream-unusable Miaoda result without treating it as data."""

    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        evidence = load_json(path)
    except (OSError, ValueError, TypeError):
        return None
    if evidence.get("status") != "unusable" or evidence.get("usable") is not False:
        return None
    terminal_metadata = evidence.get("terminal_metadata")
    terminal_metadata = terminal_metadata if isinstance(terminal_metadata, dict) else {}
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "status": "unusable",
        "usable": False,
        "skipped": True,
        "sample_count": 0,
        "response_tokens": 0,
        "reason": str(terminal_metadata.get("terminal_reason") or "upstream_unusable"),
    }


def _reusable_episode(
    evidence_path: Path,
    sample_path: Path,
    domain: str,
    index: int,
) -> dict[str, Any] | None:
    """Return a completed, teacher-aligned episode that can be resumed safely."""

    if not sample_path.is_file():
        return None
    try:
        item = _evidence_status(evidence_path, domain, index)
        evidence = load_json(evidence_path)
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    response_length = int(evidence.get("response_length") or 0)
    teacher_count = int(evidence.get("teacher_logprob_count") or 0)
    if (
        response_length < 1
        or teacher_count != response_length
        or evidence.get("teacher_logprob_lengths_match") is not True
    ):
        return None
    item["sample_path"] = str(sample_path)
    item["sample_sha256"] = sha256_file(sample_path)
    return item


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-pool", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--rollout-slime-root", required=True)
    parser.add_argument("--policy-checkpoint", required=True)
    parser.add_argument("--policy-url", required=True)
    parser.add_argument("--policy-model", required=True)
    parser.add_argument("--ors-url", required=True)
    parser.add_argument("--teacher-routes", type=Path, required=True)
    parser.add_argument("--datawheel-url", required=True)
    parser.add_argument("--seq-len", type=int, default=131072)
    parser.add_argument("--miaoda-max-polls", type=int, default=720)
    parser.add_argument("--miaoda-poll-interval", type=float, default=10.0)
    parser.add_argument(
        "--miaoda-resume-batch-task-id",
        default="",
        help="Resume one existing Datawheel batch without submitting another rollout.",
    )
    parser.add_argument(
        "--miaoda-resume-task-index",
        type=int,
        default=-1,
        help="Miaoda task index associated with --miaoda-resume-batch-task-id.",
    )
    parser.add_argument(
        "--miaoda-capture-started-at",
        type=float,
        default=0.0,
        help="Original episode UNIX timestamp used for shared capture filtering.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse completed teacher-aligned episodes already present under run-root",
    )
    parser.add_argument(
        "--skip-unusable-miaoda",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "retain and skip AgentOS/Datawheel episodes marked unusable; never "
            "skip ORS, policy, or teacher failures"
        ),
    )
    args = parser.parse_args()

    task_pool = args.task_pool.expanduser().resolve(strict=True)
    routes = args.teacher_routes.expanduser().resolve(strict=True)
    run_root = args.run_root.expanduser().resolve()
    if run_root.exists() and any(run_root.iterdir()):
        receipt = run_root / "rollout_collection_receipt.json"
        if receipt.is_file():
            print(json.dumps(load_json(receipt), ensure_ascii=False, sort_keys=True))
            return 0
        if not args.resume:
            raise RuntimeError(f"refusing to reuse non-empty run root: {run_root}")
    run_root.mkdir(parents=True, exist_ok=True)
    pool = load_json(task_pool)
    for domain in ("tooluse", "swe", "ppt", "miaoda"):
        _task_count(pool, domain)
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": ":".join(
                [
                    str(Path(args.rollout_slime_root).expanduser()),
                    str(Path(args.rollout_slime_root).expanduser() / "slime"),
                    str(PROJECT_ROOT / "backend"),
                ]
            ),
            "SLIME_OPENREWARD_TEACHER_ROUTES_FILE": str(routes),
            "SLIME_OPENREWARD_REQUIRE_TEACHER_LOGPROBS": "true",
            "SLIME_OPENREWARD_PURE_OPD": "true",
        }
    )
    _require_env(env, "ORS_API_KEY")
    _require_env(env, "DATAWHEEL_TOKEN")

    common = [
        sys.executable,
        str(GENERIC_RUNNER),
        "--checkpoint",
        str(args.policy_checkpoint),
        "--policy-url",
        args.policy_url,
        "--policy-model",
        args.policy_model,
        "--task-spec",
        str(task_pool),
        "--pure-opd",
    ]
    domain_options = {
        "tooluse": ("2048", "16", ""),
        # Formal pure-OPD does not require a verifier terminal action.  A
        # separate environment canary covers submit/reward parity; here a
        # horizon close preserves teacher-aligned policy tokens even when the
        # model has not emitted a valid submit call yet.
        "swe": ("1024", "8", ""),
        "ppt": ("2048", "4", ""),
    }
    episodes: dict[str, list[dict[str, Any]]] = {}
    sample_paths: list[str] = []
    for domain in ("tooluse", "swe", "ppt"):
        episodes[domain] = []
        max_tokens, max_turns, finalize = domain_options[domain]
        for index in range(_task_count(pool, domain)):
            episode_root = run_root / "episodes" / domain / f"{index:02d}"
            evidence_path = episode_root / "evidence.json"
            sample_path = episode_root / "sample.pt"
            if args.resume:
                reused = _reusable_episode(evidence_path, sample_path, domain, index)
                if reused is not None:
                    episodes[domain].append(reused)
                    sample_paths.append(str(sample_path))
                    print(f"[{domain}[{index}]] reusing completed episode", flush=True)
                    continue
            command = common + [
                "--environment",
                domain,
                "--task-index",
                str(index),
                "--max-new-tokens",
                max_tokens,
                "--max-turns",
                max_turns,
                "--output",
                str(evidence_path),
                "--sample-output",
                str(sample_path),
            ]
            if finalize:
                command += ["--finalize-action-on-limit", finalize]
            # Pure OPD trains on the observed policy suffix, not on a task
            # verifier result.  Close a horizon episode explicitly so a
            # long-running ToolUse task still gets teacher-aligned tokens.
            if domain in {"tooluse", "swe", "ppt"}:
                command += ["--complete-on-turn-limit"]
            _run(command, env, run_root / "coordinator.log", f"{domain}[{index}]")
            item = _evidence_status(evidence_path, domain, index)
            if not sample_path.is_file():
                raise RuntimeError(f"{domain}[{index}] sample artifact is missing")
            item["sample_path"] = str(sample_path)
            item["sample_sha256"] = sha256_file(sample_path)
            episodes[domain].append(item)
            sample_paths.append(str(sample_path))

    episodes["miaoda"] = []
    for index in range(_task_count(pool, "miaoda")):
        episode_root = run_root / "episodes" / "miaoda" / f"{index:02d}"
        evidence_path = episode_root / "evidence.json"
        sample_path = episode_root / "sample.pt"
        if args.resume:
            reused = _reusable_episode(evidence_path, sample_path, "miaoda", index)
            if reused is not None:
                episodes["miaoda"].append(reused)
                sample_paths.append(str(sample_path))
                print(f"[miaoda[{index}]] reusing completed episode", flush=True)
                continue
            if args.skip_unusable_miaoda and not (
                args.miaoda_resume_batch_task_id
                and index == args.miaoda_resume_task_index
            ):
                skipped = _unusable_episode_status(evidence_path, "miaoda", index)
                if skipped is not None:
                    episodes["miaoda"].append(skipped)
                    print(
                        f"[miaoda[{index}]] skipping upstream-unusable episode: "
                        f"{skipped['reason']}",
                        flush=True,
                    )
                    continue
        command = [
            sys.executable,
            str(MIAODA_RUNNER),
            "--ors-url",
            args.ors_url,
            "--task-spec",
            str(task_pool),
            "--task-index",
            str(index),
            "--capture-dir",
            str(episode_root / "capture"),
            "--checkpoint",
            str(args.policy_checkpoint),
            "--teacher-routes",
            str(routes),
            "--output",
            str(evidence_path),
            "--sample-output",
            str(sample_path),
            "--pure-opd",
            "--datawheel-url",
            args.datawheel_url,
            "--datawheel-token-env",
            "DATAWHEEL_TOKEN",
            "--max-polls",
            str(args.miaoda_max_polls),
            "--poll-interval",
            str(args.miaoda_poll_interval),
        ]
        if (
            args.miaoda_resume_batch_task_id
            and index == args.miaoda_resume_task_index
        ):
            command.extend(
                [
                    "--resume-batch-task-id",
                    args.miaoda_resume_batch_task_id,
                    "--capture-started-at",
                    str(args.miaoda_capture_started_at),
                ]
            )
        _run(command, env, run_root / "coordinator.log", f"miaoda[{index}]")
        if args.skip_unusable_miaoda:
            skipped = _unusable_episode_status(evidence_path, "miaoda", index)
            if skipped is not None:
                episodes["miaoda"].append(skipped)
                print(
                    f"[miaoda[{index}]] skipping upstream-unusable episode: "
                    f"{skipped['reason']}",
                    flush=True,
                )
                continue
        item = _evidence_status(evidence_path, "miaoda", index)
        if not sample_path.is_file():
            raise RuntimeError(f"miaoda[{index}] sample artifact is missing")
        item["sample_path"] = str(sample_path)
        item["sample_sha256"] = sha256_file(sample_path)
        episodes["miaoda"].append(item)
        sample_paths.append(str(sample_path))

    usable_miaoda = [
        item
        for item in episodes["miaoda"]
        if item.get("status") == "completed" and item.get("sample_path")
    ]
    if not usable_miaoda:
        raise RuntimeError("Miaoda produced no usable teacher-aligned episodes")

    merged = run_root / "merged_samples.pt"
    merge_command = [sys.executable, str(MERGE_RUNNER), "--output", str(merged)]
    for path in sample_paths:
        merge_command += ["--input", path]
    _run(merge_command, env, run_root / "coordinator.log", "merge")
    validation = run_root / "sample_validation.json"
    _run(
        [
            sys.executable,
            str(VALIDATE_RUNNER),
            "--sample",
            str(merged),
            "--max-tokens",
            str(args.seq_len),
            "--summary",
            str(validation),
            "--require-pure-opd",
        ],
        env,
        run_root / "coordinator.log",
        "validate",
    )
    validation_data = load_json(validation)
    receipt = {
        "schema_version": "openreward.formal_rollout_collection.v1",
        "status": "completed",
        "created_at": datetime.now(UTC).isoformat(),
        "task_pool": str(task_pool),
        "task_pool_sha256": sha256_file(task_pool),
        "domains": {
            domain: {
                "task_count": len(items),
                "sample_count": sum(int(item.get("sample_count") or 0) for item in items),
                "response_tokens": sum(int(item.get("response_tokens") or 0) for item in items),
                "episodes": items,
            }
            for domain, items in episodes.items()
        },
        "miaoda_usable_episode_count": len(usable_miaoda),
        "miaoda_unusable_episode_count": sum(
            int(item.get("status") == "unusable") for item in episodes["miaoda"]
        ),
        "sample_paths": sample_paths,
        "merged_sample_path": str(merged),
        "merged_sample_sha256": sha256_file(merged),
        "validation_path": str(validation),
        "validated_sample_count": int(validation_data.get("sample_count") or 0),
        "validated_response_tokens": sum(
            int(row.get("response_length") or 0) for row in validation_data.get("rows") or []
        ),
        "weight_versions": sorted(
            {
                str(version)
                for row in validation_data.get("rows") or []
                for version in row.get("weight_versions") or []
            }
        ),
        "training_objective": "pure_opd",
        "task_reward_used": False,
        "resumed": bool(args.resume),
    }
    receipt_path = run_root / "rollout_collection_receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

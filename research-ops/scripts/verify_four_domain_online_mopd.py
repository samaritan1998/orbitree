#!/usr/bin/env python3
"""Produce a secret-safe four-domain online-MOPD readiness matrix."""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _valid_sample(value: dict[str, Any] | None) -> tuple[bool, list[str]]:
    if value is None:
        return False, ["evidence_missing"]
    errors: list[str] = []
    if value.get("status") != "completed":
        errors.append("status_not_completed")
    if value.get("finished") is not True:
        errors.append("not_finished")
    if value.get("training_objective") != "pure_opd":
        errors.append("training_objective_not_pure_opd")
    if value.get("task_reward_used") is not False:
        errors.append("task_reward_not_neutralized")
    reward = value.get("reward")
    if (
        not isinstance(reward, (int, float))
        or not math.isfinite(float(reward))
        or float(reward) != 0.0
    ):
        errors.append("neutral_training_reward_missing")
    behavior = int(value.get("logprob_count") or value.get("behavior_logprob_count") or 0)
    finite_behavior = int(
        value.get("finite_logprob_count") or value.get("finite_behavior_logprob_count") or 0
    )
    response_length = int(value.get("response_length") or 0)
    if behavior < 1 or behavior != finite_behavior or behavior != response_length:
        errors.append("behavior_logprobs_not_aligned")
    teacher = int(value.get("teacher_logprob_count") or 0)
    finite_teacher = int(value.get("finite_teacher_logprob_count") or 0)
    if teacher < 1 or teacher != finite_teacher or teacher != response_length:
        errors.append("teacher_logprobs_not_aligned")
    sample_count = int(value.get("sample_count") or 0)
    if sample_count > 1 and int(value.get("unique_group_count") or 0) != sample_count:
        errors.append("training_groups_not_distinct")
    versions = value.get("weight_versions") or value.get("after_versions") or []
    if not isinstance(versions, list) or not any(
        str(item).strip() not in {"", "default"} for item in versions
    ):
        errors.append("concrete_weight_version_missing")
    artifact = value.get("sample_artifact") or value.get("sample_artifact_path")
    if artifact and not Path(str(artifact)).is_file():
        errors.append("sample_artifact_missing")
    return not errors, errors


def _transition_valid(value: dict[str, Any] | None) -> tuple[bool, list[str]]:
    if value is None:
        return False, ["transition_evidence_missing"]
    errors = []
    if value.get("status") != "verified":
        errors.append("transition_not_verified")
    if value.get("version_changed") is not True:
        errors.append("weight_version_not_changed")
    return not errors, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("data/openreward_four_domain_20260726"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root
    candidates = {
        "swe": [
            Path(
                "data/openreward_four_domain_20260727/"
                "swe_r8_pure_opd_poststep1.json"
            ),
            Path("data/openreward_four_domain_20260727/swe_r7_pure_opd.json"),
            root / "swe_pptiter1_r1.json",
            Path("data/openreward_swe_canary_20260724/policy_mopd_easy_postupdate_r5.json"),
        ],
        "ppt": [
            Path(
                "data/openreward_four_domain_20260727/"
                "ppt_r21_horizon_pure_opd.json"
            ),
            root / "ppt_create_structured_r1.json",
            Path("data/openreward_ppt_canary_20260726/policy_mopd_r14.json"),
        ],
        "tooluse": [
            Path(
                "data/openreward_four_domain_20260727/"
                "tooluse_fetch_pure_opd_r1.json"
            ),
            root / "tooluse_postupdate_r14_opd_r2.json",
            Path("data/openreward_ppt_canary_20260726/tooluse_postupdate_r14_opd_r2.json"),
        ],
        "miaoda": [
            Path(
                "data/openreward_four_domain_20260728/"
                "miaoda_r13_direct_token_pure_opd_groups_v2.json"
            ),
            Path(
                "data/openreward_four_domain_20260728/"
                "miaoda_r13_direct_token_pure_opd.json"
            ),
            Path(
                "data/openreward_four_domain_20260727/"
                "miaoda_r8_direct_token_pure_opd.json"
            ),
            Path("data/openreward_four_domain_20260727/miaoda_r6_pure_opd.json"),
            root / "miaoda_r1.json",
        ],
    }
    current_canary_root = root / "canary"
    domains: dict[str, Any] = {}
    all_ready = True
    for domain, paths in candidates.items():
        # Newer four-domain runs use one durable, domain-scoped canary
        # directory.  Prefer it when the requested root provides one, but
        # retain historical candidates so this validator can still audit older
        # evidence bundles.
        current_paths = [root / "canary" / domain / "evidence.json"]
        # A failed live attempt remains durable evidence.  Follow-up attempts
        # are deliberately written beside it as ``<domain>_retry`` instead of
        # overwriting history; select the first current candidate that passes
        # the same readiness gate so an older failure cannot mask its retry.
        current_paths.append(root / "canary" / f"{domain}_retry" / "evidence.json")
        candidate_paths = (
            current_paths
            if current_canary_root.is_dir()
            else [*paths, current_paths[0]]
        )
        evidence_path = candidate_paths[0]
        evidence = _load(evidence_path)
        ready, errors = _valid_sample(evidence)
        for path in candidate_paths[1:]:
            retry_evidence = _load(path)
            retry_ready, retry_errors = _valid_sample(retry_evidence)
            if retry_ready:
                evidence_path = path
                evidence = retry_evidence
                ready, errors = retry_ready, retry_errors
                break
        if domain == "miaoda" and evidence is not None and evidence.get("status") == "completed":
            transition_ready, transition_errors = True, []
        else:
            transition_ready, transition_errors = True, []
        domains[domain] = {
            "ready": ready,
            "errors": errors,
            "evidence": str(evidence_path),
            "reward": evidence.get("reward") if evidence else None,
            "weight_versions": (
                sorted(set(evidence.get("weight_versions") or [])) if evidence else []
            ),
            "transition_ready": transition_ready,
            "transition_errors": transition_errors,
        }
        all_ready = all_ready and ready
    report = {
        "version": 1,
        "verified_at": datetime.now(UTC).isoformat(),
        "status": "ready_for_mixed_mopd" if all_ready else "waiting_for_domain_evidence",
        "domains": domains,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"status": report["status"], "domains": {k: v["ready"] for k, v in domains.items()}},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if all_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())

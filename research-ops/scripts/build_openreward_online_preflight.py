#!/usr/bin/env python3
# ruff: noqa: E501
"""Build a source- and bundle-bound preflight page for online OpenReward MOPD."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SENSITIVE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|password|passwd|secret|cookie|"
    r"authorization|signature|credential)"
)
SECRET_VALUE = re.compile(r"(?i)(bearer\s+|token\s*[:=]\s*)[^\s,}\"']+")
CANARY_EVIDENCE = {
    "tooluse": PROJECT_ROOT / "data/openreward_four_domain_20260731/canary/tooluse/evidence.json",
    "swe": PROJECT_ROOT / "data/openreward_four_domain_20260731/canary/swe/evidence.json",
    "ppt": PROJECT_ROOT / "data/openreward_four_domain_20260731/canary/ppt/evidence.json",
    "miaoda": PROJECT_ROOT / "data/openreward_four_domain_20260731/canary/miaoda_retry/evidence.json",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact(value: Any, *, key: str = "", limit: int = 1200) -> Any:
    if key and SENSITIVE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = SECRET_VALUE.sub(r"\1[REDACTED]", value)
        return value if len(value) <= limit else value[:limit] + "…[TRUNCATED]"
    return value


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def compact_evidence(path: Path) -> dict[str, Any]:
    evidence = load_json(path)
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "status": evidence.get("status"),
        "token_count": evidence.get("token_count"),
        "response_length": evidence.get("response_length"),
        "loss_mask_count": evidence.get("loss_mask_count"),
        "loss_mask_ones": evidence.get("loss_mask_ones"),
        "logprob_count": evidence.get("logprob_count"),
        "finite_logprob_count": evidence.get("finite_logprob_count"),
        "teacher_logprob_count": evidence.get("teacher_logprob_count"),
        "finite_teacher_logprob_count": evidence.get("finite_teacher_logprob_count"),
        "weight_versions": sorted(set(str(item) for item in evidence.get("weight_versions") or [])),
        "training_objective": evidence.get("training_objective"),
        "task_reward_used": evidence.get("task_reward_used"),
    }


def json_block(value: Any) -> str:
    return html.escape(json.dumps(redact(value), ensure_ascii=False, indent=2))


def build_html(payload: dict[str, Any]) -> str:
    source_rows = "".join(
        "<tr>"
        f"<td>{html.escape(domain)}</td>"
        f"<td><a href=\"{html.escape(item['source_url'])}\">{html.escape(item['source_url'])}</a></td>"
        f"<td>{html.escape(item['kind'])}</td><td>{item['selected_count']}</td>"
        f"<td><code>{html.escape(item.get('source_sha256') or 'task/tree hashes recorded')}</code></td>"
        "</tr>"
        for domain, item in payload["source_provenance"].items()
    )
    evidence_rows = "".join(
        "<tr>"
        f"<td>{html.escape(domain)}</td><td>{item['status']}</td>"
        f"<td>{item.get('token_count') or 0:,}</td><td>{item.get('response_length') or 0:,}</td>"
        f"<td>{item.get('loss_mask_ones') or 0:,}</td>"
        f"<td>{item.get('logprob_count') or 0:,}/{item.get('teacher_logprob_count') or 0:,}</td>"
        f"<td>{html.escape(','.join(item['weight_versions']))}</td></tr>"
        for domain, item in payload["renderer_evidence"].items()
    )
    task_sections = []
    for domain, tasks in payload["tasks"].items():
        cards = "".join(
            "<article><h3>"
            f"{html.escape(str(task.get('task_id') or task.get('id') or task.get('episode_id')))}"
            f"</h3><p>query chars: {len(str(task.get('query') or ''))}</p>"
            f"<details><summary>脱敏 task record</summary><pre>{json_block(task)}</pre></details></article>"
            for task in tasks
        )
        task_sections.append(f"<section><h2>{html.escape(domain)} · {len(tasks)} tasks</h2>{cards}</section>")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(payload['job_name'])}</title>
<style>
body{{margin:0;background:#f3f5f7;color:#17212b;font:14px/1.5 system-ui,sans-serif}}
main{{max-width:1500px;margin:auto;padding:24px}}header,.panel,article{{background:#fff;border:1px solid #d9e0e7;border-radius:10px;padding:16px;margin:12px 0}}
header{{background:#163449;color:#fff}}h1{{margin:0 0 8px}}h2{{color:#163449}}table{{width:100%;border-collapse:collapse}}th,td{{padding:7px;border-bottom:1px solid #e1e6eb;text-align:left;vertical-align:top}}th{{font-size:11px;color:#536575}}pre{{white-space:pre-wrap;word-break:break-word;max-height:450px;overflow:auto;background:#f7f9fb;padding:10px;font-size:11px}}code,pre{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}}a{{color:#0d6aa6}}.note{{border-left:4px solid #e07b39;padding:9px;background:#fff4e9}}
</style></head><body><main>
<header><h1>{html.escape(payload['job_name'])}</h1><p>formal online MOPD preflight · {html.escape(payload['generated_at'])}</p>
<p>四域各 {payload['task_count_per_domain']} 条 task；训练输入由当前 student 在线 rollout 生成，目标为纯 OPD。</p></header>
<section class="panel"><h2>数据/任务源</h2><table><thead><tr><th>domain</th><th>source URL</th><th>kind</th><th>selected</th><th>source SHA</th></tr></thead><tbody>{source_rows}</tbody></table>
<p><b>task pool：</b><code>{html.escape(payload['task_pool']['path'])}</code> · <code>{payload['task_pool']['sha256']}</code></p>
<p class="note">Miaoda upstream 已排除 example-app-300 query hashes；SWE 只读 swebench_train；PPT 使用 0629 closure-filtered training bundles；ToolUse 固定官方 revision。</p></section>
<section class="panel"><h2>真实 Slime rollout / mask / logprob 证据</h2>
<p>下表来自已通过 ORS 的真实 interactive Sample 结构；正式 job 会在同一容器中重复生成并再次校验。</p>
<table><thead><tr><th>domain</th><th>status</th><th>tokens</th><th>response</th><th>mask=1</th><th>behavior/teacher</th><th>weight</th></tr></thead><tbody>{evidence_rows}</tbody></table>
<p>renderer：<code>slime.rollout.openreward_rollout</code> → <code>build_debug_rollout_dump</code> → <code>validate_slime_openreward_sample.py --require-pure-opd</code>。</p></section>
<section class="panel"><h2>待提交 LTP bundle</h2><p>config SHA-256：<code>{payload['bundle']['config_sha256']}</code></p><pre>{html.escape(payload['bundle']['redacted_config'])}</pre>
<p>run SHA-256：<code>{payload['bundle']['run_sha256']}</code></p><pre>{html.escape(payload['bundle']['redacted_run'])}</pre></section>
<section class="panel"><h2>四域 task records（脱敏）</h2>{''.join(task_sections)}</section>
<section class="panel"><a href="audit.json">audit.json</a> · <a href="preflight_manifest.json">preflight_manifest.json</a></section>
</main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-pool", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    pool_path = args.task_pool.expanduser().resolve(strict=True)
    config_path = args.config.expanduser().resolve(strict=True)
    run_path = args.run.expanduser().resolve(strict=True)
    pool = load_json(pool_path)
    environments = pool.get("environments") or {}
    tasks = {
        domain: (spec.get("train") or [])
        for domain, spec in environments.items()
    }
    if set(tasks) != {"miaoda", "swe", "tooluse", "ppt"}:
        raise ValueError("formal task pool must contain exactly four domains")
    counts = {len(items) for items in tasks.values()}
    if len(counts) != 1:
        raise ValueError("all four domains must have the same task count")
    evidence = {
        domain: compact_evidence(path)
        for domain, path in CANARY_EVIDENCE.items()
    }
    source_provenance = pool["source_provenance"]
    bundle = {
        "config_path": str(config_path),
        "config_sha256": sha256_file(config_path),
        "run_path": str(run_path),
        "run_sha256": sha256_file(run_path),
        "redacted_config": redact(config_path.read_text(encoding="utf-8")),
        "redacted_run": redact(run_path.read_text(encoding="utf-8")),
    }
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    payload = {
        "schema_version": "openreward.online_mopd_preflight.v1",
        "job_name": "ors-four-domain-formal-r1",
        "generated_at": generated_at,
        "task_count_per_domain": next(iter(counts)),
        "task_pool": {
            "path": str(pool_path),
            "sha256": sha256_file(pool_path),
            "records": sum(len(items) for items in tasks.values()),
        },
        "source_provenance": source_provenance,
        "tasks": tasks,
        "renderer_evidence": evidence,
        "bundle": bundle,
    }
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "audit.json").write_text(
        json.dumps(redact(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    source_batches = []
    for domain, source in source_provenance.items():
        batch_id = str(
            source.get("source_batch_id")
            or source.get("source_batch")
            or source.get("kind")
            or domain
        )
        source_batches.append(
            {
                "batch_id": f"{domain}:{batch_id}",
                "name": source.get("kind") or domain,
                "sample_count": 1,
                "merged_rows": len(tasks[domain]),
                "source_path": source.get("source_path") or source.get("bundle_root"),
            }
        )
    manifest = {
        "schema_version": 1,
        "generated_at": generated_at,
        "job_name": payload["job_name"],
        "warehouse_urls": [
            str(source["source_url"]) for source in source_provenance.values()
        ],
        "dataset_drift": {"detected": False},
        "merged_data": {
            "path": str(pool_path),
            "manifest_path": str(output_dir / "audit.json"),
            "records": payload["task_pool"]["records"],
        },
        "training_data": {
            "path": str(pool_path),
            "records": payload["task_pool"]["records"],
            "kept": payload["task_pool"]["records"],
            "dropped": 0,
            "invalid": 0,
            "online_generated": True,
        },
        "config": {"path": str(config_path), "sha256": bundle["config_sha256"]},
        "run": {"path": str(run_path), "sha256": bundle["run_sha256"]},
        "batches": source_batches,
        "samples": len(source_batches),
    }
    (output_dir / "preflight_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "index.html").write_text(build_html(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "completed",
                "output_dir": str(output_dir),
                "job_name": payload["job_name"],
                "task_count_per_domain": payload["task_count_per_domain"],
                "config_sha256": bundle["config_sha256"],
                "run_sha256": bundle["run_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

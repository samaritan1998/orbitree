#!/usr/bin/env python3
"""Summarize real-workload saturation for a persistent OpenReward MOPD run."""

from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path

KEEPER_RE = re.compile(
    r"\[GPU\s+(?P<gpu>\d+)\]\s+"
    r"util=\s*(?P<util>[0-9.]+)%\s+"
    r"free=\s*(?P<free>[0-9.]+)GiB\s+"
    r"duty=(?P<duty>[0-9.]+)\s+"
    r"mode=(?P<mode>FILL|IDLE)"
)
ROLLOUT_RE = re.compile(r"['\"]?perf/rollout_time['\"]?\s*:\s*(?P<seconds>[0-9.]+)")
ACTOR_RE = re.compile(r"Timer actor_train end \(elapsed:\s*(?P<seconds>[0-9.]+)s\)")
DRIVER_RE = re.compile(
    r"\[train-driver\]\s+entrypoint=(?P<entrypoint>\S+)\s+"
    r"update_weights_interval=(?P<interval>\d+)"
)


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def analyze(run_root: Path) -> dict[str, object]:
    keeper_files = sorted((run_root / "gpu_keeper").glob("node_*.log"))
    samples: list[dict[str, object]] = []
    per_node: dict[str, dict[str, object]] = {}
    for path in keeper_files:
        node_samples: list[dict[str, object]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = KEEPER_RE.search(line)
            if not match:
                continue
            sample = {
                "gpu": int(match.group("gpu")),
                "util": float(match.group("util")),
                "free_gib": float(match.group("free")),
                "duty": float(match.group("duty")),
                "mode": match.group("mode"),
            }
            samples.append(sample)
            node_samples.append(sample)
        fill_count = sum(item["mode"] == "FILL" for item in node_samples)
        per_node[path.stem] = {
            "samples": len(node_samples),
            "fill_fraction": fill_count / len(node_samples) if node_samples else None,
            "keeper_duty": _summary([float(item["duty"]) for item in node_samples]),
            "idle_mode_raw_util": _summary(
                [float(item["util"]) for item in node_samples if item["mode"] == "IDLE"]
            ),
            "free_gib": _summary([float(item["free_gib"]) for item in node_samples]),
        }

    fill_count = sum(item["mode"] == "FILL" for item in samples)
    train_log = run_root / "train.log"
    text = train_log.read_text(encoding="utf-8", errors="replace") if train_log.exists() else ""
    rollout_seconds = [float(match.group("seconds")) for match in ROLLOUT_RE.finditer(text)]
    actor_seconds = [float(match.group("seconds")) for match in ACTOR_RE.finditer(text)]
    driver_matches = list(DRIVER_RE.finditer(text))
    driver = None
    if driver_matches:
        latest = driver_matches[-1]
        driver = {
            "entrypoint": latest.group("entrypoint"),
            "update_weights_interval": int(latest.group("interval")),
        }

    steady_pairs = min(len(rollout_seconds), len(actor_seconds))
    serial_seconds = [rollout_seconds[i] + actor_seconds[i] for i in range(steady_pairs)]
    ideal_async_seconds = [max(rollout_seconds[i], actor_seconds[i]) for i in range(steady_pairs)]
    theoretical_overlap_speedup = None
    if serial_seconds and sum(ideal_async_seconds) > 0:
        theoretical_overlap_speedup = sum(serial_seconds) / sum(ideal_async_seconds)

    return {
        "schema_version": "openreward.mopd_gpu_util.v1",
        "run_root": str(run_root),
        "driver": driver,
        "keeper": {
            "files": [str(path) for path in keeper_files],
            "samples": len(samples),
            "fill_fraction": fill_count / len(samples) if samples else None,
            "idle_fraction": (len(samples) - fill_count) / len(samples) if samples else None,
            "duty": _summary([float(item["duty"]) for item in samples]),
            "idle_mode_raw_util": _summary(
                [float(item["util"]) for item in samples if item["mode"] == "IDLE"]
            ),
            "free_gib": _summary([float(item["free_gib"]) for item in samples]),
            "per_node": per_node,
            "interpretation": (
                "NVML utilization is inflated whenever mode=FILL. Treat fill_fraction and duty "
                "as the idle-pressure signal; only IDLE-mode utilization is unfilled workload "
                "utilization."
            ),
        },
        "timers": {
            "rollout_seconds": _summary(rollout_seconds),
            "actor_train_seconds": _summary(actor_seconds),
            "paired_updates": steady_pairs,
            "serial_seconds": _summary(serial_seconds),
            "ideal_async_seconds": _summary(ideal_async_seconds),
            "theoretical_overlap_speedup": theoretical_overlap_speedup,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = analyze(args.run_root.expanduser().resolve())
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()

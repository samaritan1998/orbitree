#!/usr/bin/env python3
"""Run invalid and structurally valid PPT controls through Docker and ORS."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from research_ops.openreward import (
    DockerPptEngine,
    PptOfficeBackend,
    PptRuntimeConfig,
    PptRuntimeService,
    RemoteServiceConfig,
    build_default_environments,
    create_openreward_app,
    create_ppt_runtime_app,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT_ROOT / "data" / "openreward_ppt_canary_20260720"
DEFAULT_OUTPUT = DEFAULT_ROOT / "runtime_evidence.json"
TASK_ID = "research-ops-ppt-runtime-control"

_DECK_SOURCE = r'''const pptxgen = require("pptxgenjs");
const pptx = new pptxgen();
pptx.layout = "LAYOUT_WIDE";
pptx.author = "Orbitree";
pptx.subject = "OpenReward PPT runtime control";
pptx.title = "PPT Runtime Control";
pptx.company = "Orbitree";
pptx.lang = "zh-CN";
pptx.theme = {
  headFontFace: "Noto Sans CJK SC",
  bodyFontFace: "Noto Sans CJK SC",
  lang: "zh-CN"
};

let slide = pptx.addSlide();
slide.background = { color: "17252A" };
slide.addText("OpenReward PPT Runtime", {
  x: 0.8, y: 2.45, w: 8.3, h: 0.7,
  fontFace: "Noto Sans CJK SC", fontSize: 30, bold: true, color: "FFFFFF",
  margin: 0
});
slide.addText("Isolated workspace · Clean rendering · Rubric gated", {
  x: 0.82, y: 3.35, w: 7.8, h: 0.45,
  fontFace: "Noto Sans CJK SC", fontSize: 18, color: "9FE7D7", margin: 0
});
slide.addShape(pptx.ShapeType.rect, {
  x: 9.55, y: 1.3, w: 2.5, h: 4.9,
  fill: { color: "2B7A78" }, line: { color: "2B7A78" }
});
slide.addText("ORS", {
  x: 9.8, y: 3.1, w: 2, h: 0.75,
  fontSize: 32, bold: true, color: "FFFFFF", align: "center", margin: 0
});
slide.addText("ENVIRONMENT\nCONTRACT", {
  x: 9.8, y: 3.95, w: 2, h: 0.55,
  fontFace: "Noto Sans CJK SC", fontSize: 11, color: "EAFBF7",
  align: "center", breakLine: false, margin: 0
});

slide = pptx.addSlide();
slide.background = { color: "F4F7F6" };
slide.addText("Terminal acceptance gates", {
  x: 0.75, y: 0.55, w: 8.8, h: 0.6,
  fontFace: "Noto Sans CJK SC", fontSize: 32, bold: true, color: "17252A",
  margin: 0
});
const gates = [
  ["01", "Editable OOXML", "Reject corrupt or flattened artifacts"],
  ["02", "Clean render", "One PNG must be produced for every slide"],
  ["03", "Rubric score", "Four dimensions must meet the task rubric threshold"]
];
gates.forEach((gate, index) => {
  const y = 1.45 + index * 1.42;
  slide.addShape(pptx.ShapeType.roundRect, {
    x: 0.75, y, w: 11.75, h: 1.05, rectRadius: 0.04,
    fill: { color: "FFFFFF" },
    line: { color: "87B7AF", width: 1.5 }
  });
  slide.addText(gate[0], {
    x: 1.05, y: y + 0.25, w: 0.8, h: 0.45,
    fontSize: 20, bold: true, color: "2B7A78", margin: 0
  });
  slide.addText(gate[1], {
    x: 2.0, y: y + 0.18, w: 3.2, h: 0.35,
    fontSize: 18, bold: true, color: "17252A", margin: 0
  });
  slide.addText(gate[2], {
    x: 2.0, y: y + 0.55, w: 8.8, h: 0.3,
    fontSize: 14, color: "4B6464", margin: 0
  });
});
slide.addShape(pptx.ShapeType.roundRect, {
  x: 0.75, y: 5.72, w: 11.75, h: 0.72, rectRadius: 0.04,
  fill: { color: "FBE9E7" }, line: { color: "C96B61", width: 1.5 }
});
slide.addText("!", {
  x: 1.05, y: 5.9, w: 0.35, h: 0.34,
  fontSize: 18, bold: true, color: "A13F35", align: "center", margin: 0
});
slide.addText("A valid render without a rubric remains unusable for MOPD.", {
  x: 1.62, y: 5.91, w: 9.8, h: 0.3,
  fontSize: 14, bold: true, color: "8B3A3A", margin: 0
});

pptx.writeFile({ fileName: "/workspace/slides.pptx" });
'''


def _task() -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "query": "Create a two-slide editable runtime control grounded in reference.md.",
        "benchmark_family": "ppt_online",
        "output_path": "/workspace/slides.pptx",
        "assets": [{"path": "/inputs/references/reference.md"}],
        "verifier": {
            "min_slides": 2,
            "max_slides": 2,
            "editable_pptx_required": True,
            "render_required": True,
            "content_grounding_required": True,
            "visual_qa_required": True,
        },
    }


def _sse_output(response: Any) -> dict[str, Any]:
    for line in response.text.splitlines():
        if line.startswith("data: ") and '"ok"' in line:
            payload = json.loads(line[6:])
            if payload.get("ok") is not True:
                raise RuntimeError("PPT ORS action failed")
            return dict(payload["output"])
    raise RuntimeError("PPT ORS response omitted terminal SSE payload")


def _create(client: TestClient) -> tuple[str, dict[str, str]]:
    sid = client.post("/create_session").json()["sid"]
    headers = {"X-Session-ID": sid}
    response = client.post(
        "/create",
        headers=headers,
        json={"env_name": "ppt", "split": "train", "index": 0},
    )
    response.raise_for_status()
    return sid, headers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="research-ops/ppt-policy:20260720")
    parser.add_argument("--bundle-root", type=Path, default=DEFAULT_ROOT / "synthetic_bundle")
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ROOT / "artifacts")
    parser.add_argument("--skill-root", type=Path, default=Path("/root/.codex/skills/pptx"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    bundle_root = args.bundle_root.expanduser().resolve()
    reference = bundle_root / TASK_ID / "inputs" / "references" / "reference.md"
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_text(
        "The runtime must preserve editable OOXML, render in a clean container, "
        "and require a four-dimensional rubric before training.",
        encoding="utf-8",
    )
    artifact_root = args.artifact_root.expanduser().resolve()
    config = PptRuntimeConfig(
        policy_image=args.image,
        verifier_image=args.image,
        bundle_root=bundle_root,
        artifact_root=artifact_root,
        skill_root=args.skill_root.expanduser().resolve(),
    )
    service = PptRuntimeService(
        DockerPptEngine(config),
        bundle_root=bundle_root,
        artifact_root=artifact_root,
        rubric=None,
        ttl_seconds=config.session_ttl_seconds,
    )
    sidecar = create_ppt_runtime_app(service)
    backend = PptOfficeBackend(
        RemoteServiceConfig(
            environment="ppt",
            base_url_env="PPT_CANARY_URL",
            create_operation="POST /v1/presentations",
            action_operations={
                "bash": "POST /v1/presentations/{session_id}/exec",
                "read_file": "POST /v1/presentations/{session_id}/files/read",
                "write_file": "POST /v1/presentations/{session_id}/files/write",
                "edit_file": "POST /v1/presentations/{session_id}/files/edit",
                "ppt_read": "POST /v1/presentations/read",
                "ppt_write": "POST /v1/presentations/write",
                "render": "POST /v1/presentations/render",
                "submit_deliverable": "POST /v1/presentations/verify",
            },
            destroy_operation="DELETE /v1/presentations/{session_id}",
            terminal_actions=frozenset({"submit_deliverable"}),
            timeout_seconds=900,
        ),
        environ={"PPT_CANARY_URL": "http://ppt-runtime.local"},
        transport=httpx.ASGITransport(app=sidecar),
    )
    ors = create_openreward_app(
        build_default_environments(
            backends={"ppt": backend},
            task_catalogs={"ppt": {"train": [_task()]}},
        )
    )

    with TestClient(ors) as client:
        invalid_sid, invalid_headers = _create(client)
        try:
            _sse_output(
                client.post(
                    "/ppt/call",
                    headers=invalid_headers,
                    json={
                        "name": "write_file",
                        "input": {
                            "path": "/workspace/slides.pptx",
                            "content": "not a PPTX",
                        },
                    },
                )
            )
            invalid = _sse_output(
                client.post(
                    "/ppt/call",
                    headers=invalid_headers,
                    json={
                        "name": "submit_deliverable",
                        "input": {"path": "/workspace/slides.pptx"},
                    },
                )
            )
        finally:
            client.post("/delete", headers=invalid_headers)

        valid_sid, valid_headers = _create(client)
        try:
            _sse_output(
                client.post(
                    "/ppt/call",
                    headers=valid_headers,
                    json={
                        "name": "write_file",
                        "input": {
                            "path": "/workspace/create.js",
                            "content": _DECK_SOURCE,
                        },
                    },
                )
            )
            generated = _sse_output(
                client.post(
                    "/ppt/call",
                    headers=valid_headers,
                    json={"name": "bash", "input": {"command": "node create.js"}},
                )
            )
            if dict(generated.get("metadata") or {}).get("exit_code") != 0:
                raise RuntimeError("PPT canary source did not generate a deck")
            valid = _sse_output(
                client.post(
                    "/ppt/call",
                    headers=valid_headers,
                    json={
                        "name": "submit_deliverable",
                        "input": {"path": "/workspace/slides.pptx"},
                    },
                )
            )
        finally:
            client.post("/delete", headers=valid_headers)

    asyncio.run(service.shutdown())
    if invalid.get("reward") != 0.0 or invalid.get("finished") is not True:
        raise RuntimeError("invalid PPT control must terminate with reward 0")
    valid_metadata = dict(valid.get("metadata") or {})
    if valid.get("reward") is not None or valid_metadata.get("usable") is not False:
        raise RuntimeError("valid PPT without rubric must remain unusable")
    evidence = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "image": args.image,
        "image_id": service.engine.image_id("policy"),
        "invalid_control": {
            "reward": invalid.get("reward"),
            "finished": invalid.get("finished"),
            "metadata": invalid.get("metadata"),
        },
        "valid_without_rubric": {
            "reward": valid.get("reward"),
            "finished": valid.get("finished"),
            "metadata": valid.get("metadata"),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

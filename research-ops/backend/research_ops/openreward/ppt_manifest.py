"""Extract online PPT tasks from prompt-only trajectory records."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_INPUT_PATH = re.compile(r"/inputs/[^\s<>]+")
_OUTPUT_PATH = re.compile(r"Save the slide deck to:\s*(/[^\s<>]+\.pptx)", re.IGNORECASE)
_SLIDE_RANGE = re.compile(
    r"(?:页数(?:控制在|为)?|控制在)\s*[:：]?\s*(\d+)\s*(?:到|至|[-–—])\s*(\d+)\s*页"
)


def _task_prompt(messages: Any) -> str:
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise ValueError("PPT source record has no messages")
    candidates = [
        str(item.get("content") or "")
        for item in messages
        if isinstance(item, Mapping)
        and item.get("role") == "user"
        and isinstance(item.get("content"), str)
    ]
    for candidate in candidates:
        if "Save the slide deck to:" in candidate:
            return candidate
    raise ValueError("PPT source record has no task prompt")


def build_ppt_online_task(record: Mapping[str, Any]) -> dict[str, Any]:
    """Build a catalog task while keeping runtime assets server-side."""

    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("PPT source record has no provenance metadata")
    task_id = str(metadata.get("original_id") or "").strip()
    if not task_id:
        raise ValueError("PPT source record has no original_id")
    query = _task_prompt(record.get("messages"))
    output_match = _OUTPUT_PATH.search(query)
    if output_match is None:
        raise ValueError("PPT task has no .pptx output path")
    output_path = output_match.group(1).rstrip(".,;:)")

    asset_paths = tuple(dict.fromkeys(path.rstrip(".,;:)") for path in _INPUT_PATH.findall(query)))
    assets = [
        {
            "path": path,
            "role": (
                "style_reference"
                if path.startswith("/inputs/style_refs/")
                else (
                    "current_presentation"
                    if path.startswith("/inputs/current_pptx/")
                    else "content_reference"
                )
            ),
        }
        for path in asset_paths
    ]
    slide_match = _SLIDE_RANGE.search(query)
    min_slides = int(slide_match.group(1)) if slide_match else 1
    max_slides = int(slide_match.group(2)) if slide_match else 100
    if min_slides < 1 or max_slides < min_slides:
        raise ValueError("PPT task slide range is invalid")

    return {
        "task_id": task_id,
        "query": query,
        "benchmark_family": "ppt_online",
        "output_path": output_path,
        "assets": assets,
        "runtime": {
            "asset_mode": "task_bundle",
            "asset_root": "/inputs",
            "workspace_root": "/workspace",
            "pptx_skill_required": True,
        },
        "verifier": {
            "min_slides": min_slides,
            "max_slides": max_slides,
            "editable_pptx_required": True,
            "render_required": True,
            "content_grounding_required": bool(assets),
            "visual_qa_required": True,
        },
        "source": {
            "source_line": int(metadata.get("source_line") or 0),
            "prompt_sha256": str(metadata.get("prompt_sha256") or ""),
            "provenance_status": "confirmed_prompt_only",
            "asset_status": "requires_original_task_bundle",
        },
    }

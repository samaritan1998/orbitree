from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


def bound_claude_handoff(handoff: dict[str, Any]) -> None:
    # Native TUI startup accepts the attachment as one argument. Keep its UTF-8
    # representation below Linux's per-argument limit, including JSON escaping.
    while len(json.dumps(handoff, ensure_ascii=False, indent=2).encode("utf-8")) > 80_000:
        handoff["truncated"] = True
        if handoff.get("upstream_handoff"):
            handoff["upstream_handoff"] = {}
        elif len(handoff.get("completed_turns", [])) > 1:
            handoff["completed_turns"].pop(0)
        elif handoff.get("latest_handoff"):
            handoff["latest_handoff"] = {}
        elif handoff.get("completed_turns"):
            turn = handoff["completed_turns"][0]
            field = max(("prompt", "final_answer"), key=lambda key: len(turn.get(key, "")))
            value = turn.get(field, "")
            if not value:
                raise ValueError("Claude handoff metadata exceeds the size limit")
            turn[field] = value[len(value) // 2 + 1:]
        else:
            raise ValueError("Claude handoff metadata exceeds the size limit")


def render_claude_handoff(handoff: dict[str, Any]) -> str:
    """Render an explicitly approved history attachment, without a new objective."""
    return (
        "Conversation handoff (reference history, not a new task)\n\n"
        + json.dumps(handoff, ensure_ascii=False, indent=2)
    )


def handoff_path(state_dir: Path, thread_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", thread_id):
        raise ValueError("Invalid Claude session ID")
    return state_dir / f"{thread_id}.handoff.txt"


def stage_claude_handoff(state_dir: Path, thread_id: str, handoff: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = handoff_path(state_dir, thread_id)
    content = render_claude_handoff(handoff)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != content:
            raise ValueError("The Claude handoff is already frozen") from None
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)

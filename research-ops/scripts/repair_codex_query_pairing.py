"""Repair one Branch's result pairings without replaying or notifying any turn."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from research_ops import models
from research_ops.db import Database
from research_ops.services.codex_results import CodexResultSync
from research_ops.settings import Settings
from sqlalchemy import select


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("branch_id")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if args.apply and not args.backup:
        parser.error("--apply requires a new --backup path")
    settings = Settings()
    database = Database(settings)
    try:
        with database.session_factory() as session:
            branch = session.get(models.ExecutionBranch, args.branch_id)
            if branch is None:
                parser.error("Branch not found")
            turns = list(session.scalars(select(models.CodexTurn).where(
                models.CodexTurn.branch_id == args.branch_id
            )))
            snapshot = [{
                "id": turn.id, "external_turn_id": turn.external_turn_id,
                "session_id": turn.session_id, "prompt": turn.prompt,
                "started_at": turn.started_at.isoformat() if turn.started_at else None,
                "metadata_json": turn.metadata_json,
            } for turn in turns]
            answers = {turn.id: turn.final_answer for turn in turns}
            paths = dict(session.execute(
                select(models.CodexThreadBinding.thread_id, models.CodexSyncCursor.path)
                .join(models.CodexSyncCursor)
                .where(models.CodexThreadBinding.branch_id == args.branch_id)
            ).all())
        print(json.dumps({"branch_id": args.branch_id, "results": len(turns),
                          "threads": len(paths), "apply": args.apply}))
        if not args.apply:
            return
        assert args.backup is not None
        descriptor = os.open(args.backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"branch_id": args.branch_id, "turns": snapshot}, handle, ensure_ascii=False)
        sync = CodexResultSync(database.session_factory, settings.codex_home / "sessions")
        # Use registered paths only, not a recursive home-directory scan.
        sync._files.update({thread: Path(path) for thread, path in paths.items()})
        changed = sync.backfill_missing_prompts(branch_id=args.branch_id, repair_mismatched=True)
        with database.session_factory() as session:
            for turn in session.scalars(select(models.CodexTurn).where(
                models.CodexTurn.branch_id == args.branch_id
            )):
                if turn.id in answers and turn.final_answer != answers[turn.id]:
                    raise RuntimeError(f"Answer unexpectedly changed: {turn.id}")
        print(json.dumps({"repaired": changed, "answers_unchanged": True,
                          "backup": str(args.backup)}))
    finally:
        database.engine.dispose()


if __name__ == "__main__":
    main()

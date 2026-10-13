"""Clean only registered native transcripts and their referenced parent histories.

Default: audit. --backup-dir applies with native writer locks and per-file receipts.
Assistant answers, tool output, unknown text and compacted prose are never rewritten.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from research_ops.codex.history_cleanup import clean_rollout


def read_only(path: Path):
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--sqlite-home", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--thread-id", action="append", default=[])
    args = parser.parse_args()
    with read_only(args.database) as db:
        registered = {
            str(row[0])
            for row in db.execute(
                "SELECT thread_id FROM codex_thread_bindings UNION "
                "SELECT codex_thread_id FROM execution_branches WHERE codex_thread_id IS NOT NULL"
            )
        }
        literal_queries = frozenset(
            row[0]
            for row in db.execute(
                "SELECT content FROM conversation_messages WHERE role='user' "
                "AND json_extract(metadata_json, '$.goal_id') IS NULL "
                "AND json_extract(metadata_json, '$.auto_continuation') IS NULL"
            )
        )
    targets = set(args.thread_id) if args.thread_id else registered
    if targets - registered:
        parser.error("Only Research Ops registered thread IDs may be cleaned")
    paths = {}
    parents = {}
    skipped = []
    roots = [args.codex_home.resolve() / name for name in ("sessions", "archived_sessions")]
    with read_only(args.sqlite_home / "state_5.sqlite") as db:
        rows = db.execute("SELECT id, rollout_path FROM threads").fetchall()
    # Resolve native dependency edges from indexed files, never scan HOME or the workspace.
    for thread_id, filename in rows:
        path = Path(filename)
        if path.is_symlink() or not any(path.resolve().is_relative_to(root) for root in roots):
            continue
        try:
            with path.open("rb") as handle:
                metadata = json.loads(handle.readline(2 * 1024 * 1024)).get("payload", {})
            if metadata.get("id") != thread_id:
                continue
        except (OSError, ValueError):
            continue
        paths[thread_id] = path
        base = metadata.get("history_base") or {}
        if base.get("thread_id"):
            parents[thread_id] = base["thread_id"]
    for thread_id in tuple(targets):
        ancestors = set()
        while thread_id in parents and thread_id not in ancestors:
            ancestors.add(thread_id)
            thread_id = parents[thread_id]
            targets.add(thread_id)
    dependents = {thread_id: [] for thread_id in targets}
    for child in parents:
        cursor, visited = child, set()
        while cursor in parents and cursor not in visited:
            visited.add(cursor)
            cursor = parents[cursor]
            if cursor in dependents:
                dependents[cursor].append(child)
    files = []
    for number, thread_id in enumerate(sorted(targets), 1):
        path = paths.get(thread_id)
        if path is None:
            skipped.append(thread_id)
            continue
        entry = clean_rollout(
            path,
            thread_id=thread_id,
            sqlite_home=args.sqlite_home,
            backup_dir=args.backup_dir,
            dependent_threads=dependents[thread_id],
            literal_queries=literal_queries,
        )
        files.append(entry)
        # Persist progress without storing any conversation content in the report.
        report = {
            "mode": "apply" if args.backup_dir else "audit",
            "counts": dict(Counter(item["status"] for item in files)),
            "files": files,
            "unavailable_thread_ids": skipped,
            "note": "remaining_marker_records includes preserved user quotations, assistant "
            "answers, tool output and compacted prose; these are not automatically deleted.",
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if number % 50 == 0 or entry["status"] == "error":
            print(
                json.dumps({"checked": number, "total": len(targets), "counts": report["counts"]}),
                flush=True,
            )
    print(
        json.dumps(
            {
                "report": str(args.report),
                "counts": dict(Counter(item["status"] for item in files)),
                "unavailable": len(skipped),
            },
            indent=2,
        )
    )
    report = {
        "mode": "apply" if args.backup_dir else "audit",
        "counts": dict(Counter(item["status"] for item in files)),
        "files": files,
        "unavailable_thread_ids": skipped,
        "note": "Remaining markers in user quotations, assistant answers, tool output, and "
        "opaque compactions are preserved, not asserted to be clean.",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

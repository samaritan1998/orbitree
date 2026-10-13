"""Audit registered workspaces; --backup-dir opts into removing exact platform templates."""

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from research_ops.services.instruction_cleanup import cleanup_generated_instructions

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--database", type=Path, required=True)
parser.add_argument("--workspace-root", type=Path, required=True)
parser.add_argument("--backup-dir", type=Path)
args = parser.parse_args()
root = args.workspace_root.resolve()
with sqlite3.connect(f"{args.database.resolve().as_uri()}?mode=ro", uri=True) as db:
    workspaces = {Path(row[0]) for row in db.execute("SELECT DISTINCT cwd FROM execution_branches")}
    for (code,) in db.execute("SELECT code FROM programs"):
        workspaces.add(root / code / "_desk")
for workspace in tuple(workspaces):
    resolved = workspace.resolve()
    if resolved.is_relative_to(root):
        workspaces.update(parent for parent in resolved.parents if parent.is_relative_to(root))
report = cleanup_generated_instructions(workspaces, backup_dir=args.backup_dir)
result = {"counts": dict(Counter(item["status"] for item in report)), "files": report}
if args.backup_dir:
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    (args.backup_dir / "manifest.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))

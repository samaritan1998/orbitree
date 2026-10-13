#!/usr/bin/env python3
"""Make Toolathlon task scripts honor the per-session PostgreSQL database."""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from typing import Any

_DATABASE_VALUE = '__import__("os").environ.get("PGDATABASE", "toolathlon_gym")'
_PATTERNS = (
    re.compile(
        r"(?P<prefix>(?P<key_quote>['\"])(?:dbname|database)(?P=key_quote)\s*:\s*)"
        r"(?P<value_quote>['\"])toolathlon_gym(?P=value_quote)"
    ),
    re.compile(
        r"(?P<prefix>\b(?:dbname|database)\s*=\s*)"
        r"(?P<value_quote>['\"])toolathlon_gym(?P=value_quote)"
    ),
)


class TaskDatabasePatchError(RuntimeError):
    """A task script contains an unsupported hard-coded database reference."""


def _is_pgdatabase_default(node: ast.Constant, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    if not isinstance(parent, ast.Call) or len(parent.args) < 2 or parent.args[1] is not node:
        return False
    function = parent.func
    return (
        isinstance(function, ast.Attribute)
        and function.attr == "get"
        and isinstance(parent.args[0], ast.Constant)
        and parent.args[0].value == "PGDATABASE"
    )


def _unsupported_database_lines(source: str, *, path: Path) -> list[int]:
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise TaskDatabasePatchError(f"patched task script does not compile: {path}") from exc
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    return sorted(
        {
            int(node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and node.value == "toolathlon_gym"
            and not _is_pgdatabase_default(node, parents)
        }
    )


def patch_source(source: str, *, path: Path) -> tuple[str, int]:
    """Rewrite explicit database arguments and reject unhandled exact literals."""

    updated = source
    replacements = 0
    for pattern in _PATTERNS:
        updated, count = pattern.subn(
            lambda match: f"{match.group('prefix')}{_DATABASE_VALUE}",
            updated,
        )
        replacements += count
    unsupported = _unsupported_database_lines(updated, path=path)
    if unsupported:
        rendered = ",".join(str(item) for item in unsupported)
        raise TaskDatabasePatchError(
            f"unsupported hard-coded toolathlon_gym database in {path}:{rendered}"
        )
    return updated, replacements


def patch_task_databases(root: Path, *, write: bool) -> dict[str, Any]:
    """Patch preprocess/evaluation scripts below a Toolathlon finalpool root."""

    root = root.expanduser().resolve()
    if not root.is_dir():
        raise TaskDatabasePatchError(f"Toolathlon task root does not exist: {root}")
    paths = sorted(
        {
            *root.glob("*/preprocess/main.py"),
            *root.glob("*/evaluation/main.py"),
        }
    )
    changed_files: list[str] = []
    replacements = 0
    for path in paths:
        source = path.read_text(encoding="utf-8")
        updated, count = patch_source(source, path=path)
        if count:
            changed_files.append(path.relative_to(root).as_posix())
            replacements += count
            if write:
                path.write_text(updated, encoding="utf-8")
    return {
        "version": 1,
        "scanned_files": len(paths),
        "changed_files": len(changed_files),
        "replacements": replacements,
        "write": write,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_root", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and report the patch without changing task files",
    )
    args = parser.parse_args()
    report = patch_task_databases(args.task_root, write=not args.check)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

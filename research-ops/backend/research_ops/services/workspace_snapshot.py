from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .workspace import WorkspaceError

MANIFEST = ".research-ops-workspace-snapshot.json"
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    ".next",
    "dist",
    "build",
    "checkpoints",
    "weights",
    "models",
    ".claude",
    ".codex",
}
EVIDENCE_DIRS = {"profile", "profiles", "artifacts", "runs", "logs", "results", "outputs"}
SOURCE_SUFFIXES = {
    ".py",
    ".sh",
    ".bash",
    ".zsh",
    ".js",
    ".mjs",
    ".cjs",
    ".jsx",
    ".ts",
    ".tsx",
    ".css",
    ".html",
    ".md",
    ".rst",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".c",
    ".cc",
    ".cpp",
    ".cu",
    ".cuh",
    ".h",
    ".hpp",
    ".go",
    ".rs",
    ".sql",
    ".jinja",
    ".j2",
    ".ipynb",
}
TEXT_SUFFIXES = SOURCE_SUFFIXES | {".json", ".txt", ".lock", ".svg"}
SOURCE_NAMES = {"Dockerfile", "Makefile", "CMakeLists.txt", ".gitignore", ".dockerignore"}
SECRET_VALUE = re.compile(
    r'''(?ix)(?<![a-z0-9_])(?:api[_-]?key|app[_-]?key|app[_-]?secret|access[_-]?key|'''
    r'''password|token|secret|authorization)["']?\s*[:=]\s*'''
    r'''(?:["']([^"'\n]{12,})["']|([a-z0-9][a-z0-9_+/=.:-]{15,})(?=[\s,;]|$))'''
)
SECRET_FORMAT = re.compile(
    r"(?:\b(?:sk[-_]|ghp_|github_pat_)[A-Za-z0-9_-]{12,}\b|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|bce-auth-v1/[^\s\"']{40,})"
)


def contains_credentials(text: str) -> bool:
    # Logging redaction also matches token-count variables; don't drop source modules for those.
    if SECRET_FORMAT.search(text):
        return True
    return any(
        not (match.group(1) or match.group(2)).startswith(("${", "<", "YOUR_", "REPLACE_"))
        for match in SECRET_VALUE.finditer(text)
    )


def plan_workspace_snapshot(source: Path, destination: Path) -> dict[str, Any]:
    source, destination = source.resolve(), destination.resolve()
    if not source.is_dir() or source == destination or source in destination.parents:
        raise WorkspaceError("Snapshot needs separate existing source and destination directories")
    files, skipped = [], []
    total = 0
    visited = 0
    for root, directories, names in os.walk(source, followlinks=False):
        relative_root = Path(root).relative_to(source)
        for name in list(directories):
            path = Path(root) / name
            evidence_depth = (
                relative_root.parts
                and relative_root.parts[0] in {"profile", "profiles"}
                and len(relative_root.parts) >= 3
            )
            if (
                name in SKIP_DIRS
                or name in EVIDENCE_DIRS - {"profile", "profiles"}
                or name.startswith((".venv", "venv-"))
                or path.is_symlink()
                or evidence_depth
            ):
                directories.remove(name)
                skipped.append({"path": str(relative_root / name), "reason": "excluded_directory"})
        for name in sorted(names):
            visited += 1
            if visited > 50_000:
                raise WorkspaceError(f"Snapshot inspection limit exceeded at {relative_root}")
            path = Path(root) / name
            relative = path.relative_to(source)
            info = path.lstat()
            reason = None
            if not stat.S_ISREG(info.st_mode):
                reason = "symlink_or_special_file"
            elif (
                name.startswith((".research-ops", ".env"))
                or any(
                    word in name.lower() for word in ("credential", "secret", "auth.json", "id_rsa")
                )
                or path.suffix in {".pem", ".key"}
            ):
                reason = "private_or_platform_metadata"
            elif info.st_size > 2 * 1024 * 1024:
                reason = "large_file"
            elif any(part in EVIDENCE_DIRS for part in relative.parts[:-1]) and (
                path.suffix not in SOURCE_SUFFIXES and name not in SOURCE_NAMES
            ):
                reason = "experiment_output"
            elif path.suffix not in TEXT_SUFFIXES and name not in SOURCE_NAMES:
                reason = "non_source_file"
            if reason is None:
                data = path.read_bytes()
                try:
                    text = data.decode("utf-8")
                    if "\x00" in text:
                        reason = "binary_content"
                    elif contains_credentials(text):
                        reason = "credential_content"
                except UnicodeDecodeError:
                    reason = "binary_content"
            if reason:
                skipped.append({"path": str(relative), "reason": reason})
                continue
            total += len(data)
            if total > 128 * 1024 * 1024:
                raise WorkspaceError("Snapshot exceeds the 128 MiB source-copy limit")
            files.append(
                {
                    "path": str(relative),
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "executable": bool(info.st_mode & stat.S_IXUSR),
                }
            )
    return {
        "schema_version": 1,
        "source": str(source),
        "destination": str(destination),
        "created_at": datetime.now(UTC).isoformat(),
        "files": files,
        "skipped": skipped,
        "file_count": len(files),
        "bytes": total,
        "skipped_count": len(skipped),
        "skip_reasons": dict(Counter(item["reason"] for item in skipped)),
    }


def apply_workspace_snapshot(plan: dict[str, Any]) -> dict[str, Any]:
    source, destination = Path(plan["source"]), Path(plan["destination"])
    if not destination.is_dir():
        raise WorkspaceError("Snapshot destination must already exist")
    manifest = destination / MANIFEST
    if manifest.exists() or manifest.is_symlink():
        raise WorkspaceError("This Branch already has a workspace snapshot")
    # Verify the whole selection and all conflicts before creating any files.
    contents: list[tuple[dict[str, Any], bytes]] = []
    for item in plan["files"]:
        relative = Path(item["path"])
        src, dst = source / relative, destination / relative
        if relative.is_absolute() or ".." in relative.parts:
            raise WorkspaceError("Invalid snapshot path")
        if src.is_symlink() or not src.resolve().is_relative_to(source):
            raise WorkspaceError(f"Source became a symlink: {relative}")
        if dst.is_symlink() or not dst.resolve().is_relative_to(destination):
            raise WorkspaceError(f"Destination is a symlink: {relative}")
        data = src.read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise WorkspaceError(f"Source changed; rebuild the snapshot: {relative}")
        if dst.exists() and (not dst.is_file() or dst.read_bytes() != data):
            raise WorkspaceError(f"Child file would be overwritten: {relative}")
        contents.append((item, data))
    for item, data in contents:
        dst = destination / item["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            mode = 0o700 if item["executable"] else 0o600
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        if hashlib.sha256(dst.read_bytes()).hexdigest() != item["sha256"]:
            raise WorkspaceError(f"Snapshot verification failed: {item['path']}")
    fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(plan, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return {
        key: plan[key]
        for key in (
            "source",
            "destination",
            "created_at",
            "file_count",
            "bytes",
            "skipped_count",
            "skip_reasons",
        )
    } | {"manifest_path": str(manifest), "state": "ready"}

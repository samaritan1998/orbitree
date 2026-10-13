from __future__ import annotations

import hashlib
import json
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from research_ops import models
from research_ops.settings import Settings


@dataclass(slots=True)
class BranchWorkspace:
    cwd: str
    git_branch: str | None
    base_sha: str | None


class WorkspaceError(RuntimeError):
    pass


class WorkspaceManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def prepare(
        self,
        *,
        program_code: str,
        work_human_id: str,
        branch_slug: str,
        repository: models.Repository | None,
        mode: str,
    ) -> BranchWorkspace:
        branch_root = self.workspace_path(
            program_code=program_code,
            work_human_id=work_human_id,
            branch_slug=branch_slug,
            repository_name=None,
        )
        branch_root.mkdir(parents=True, exist_ok=True)
        metadata = {
            "program": program_code,
            "work_item": work_human_id,
            "branch": branch_slug,
            "mode": mode,
        }
        metadata_path = branch_root / ".research-ops.json"
        if not metadata_path.exists():
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        if repository is None:
            self._prepare_scratch(branch_root)
            return BranchWorkspace(cwd=str(branch_root), git_branch=None, base_sha=None)

        canonical = Path(repository.canonical_path).expanduser().resolve()
        if not canonical.exists():
            raise WorkspaceError(f"Repository path does not exist: {canonical}")
        self._run_git(canonical, "rev-parse", "--git-dir")
        base_sha = self._run_git(canonical, "rev-parse", repository.default_ref).strip()
        destination = branch_root / _slug(repository.name)
        git_branch = f"rops/{work_human_id.lower()}/{branch_slug}"
        if not destination.exists() or not (destination / ".git").exists():
            if self._branch_exists(canonical, git_branch):
                self._run_git(canonical, "worktree", "add", str(destination), git_branch)
            else:
                self._run_git(
                    canonical,
                    "worktree",
                    "add",
                    "-b",
                    git_branch,
                    str(destination),
                    base_sha,
                )
        return BranchWorkspace(cwd=str(destination), git_branch=git_branch, base_sha=base_sha)

    def workspace_path(
        self,
        *,
        program_code: str,
        work_human_id: str,
        branch_slug: str,
        repository_name: str | None,
    ) -> Path:
        task_root = self.settings.workspace_root / program_code.upper() / "tasks" / work_human_id
        branch_root = (
            task_root / "primary" if branch_slug == "main" else task_root / "forks" / branch_slug
        )
        return branch_root / _slug(repository_name) if repository_name else branch_root

    def ensure_workspace_entry(
        self,
        *,
        program_code: str,
        work_human_id: str,
        branch_slug: str,
        repository_name: str | None,
        existing_cwd: str,
    ) -> str:
        """Expose an existing Branch under Q3 without moving or editing its files."""
        source = Path(existing_cwd).expanduser().resolve()
        if not source.is_dir():
            raise WorkspaceError(f"Branch workspace does not exist: {source}")
        target = self.workspace_path(
            program_code=program_code,
            work_human_id=work_human_id,
            branch_slug=branch_slug,
            repository_name=repository_name,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            try:
                if target.resolve() == source:
                    return str(target)
            except OSError as exc:
                raise WorkspaceError(f"Workspace entry is not usable: {target}") from exc
            raise WorkspaceError(f"Workspace entry already contains other data: {target}")
        try:
            target.symlink_to(source, target_is_directory=True)
        except FileExistsError as exc:
            if target.resolve() != source:
                raise WorkspaceError(
                    f"Workspace entry already contains other data: {target}"
                ) from exc
        return str(target)

    def prepare_program_desk(
        self,
        *,
        program_code: str,
        program_name: str,
        objective: str,
    ) -> BranchWorkspace:
        desk_root = self.settings.workspace_root / program_code.upper() / "_desk"
        for directory in ("memory", "decisions", "playbook"):
            (desk_root / directory).mkdir(parents=True, exist_ok=True)
        metadata = desk_root / ".research-ops.json"
        if not metadata.exists():
            metadata.write_text(
                json.dumps(
                    {"program": program_code.upper(), "kind": "program_desk", "mode": "write"},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        charter = desk_root / "CHARTER.md"
        if not charter.exists():
            charter.write_text(f"# {program_name}\n\n{objective}\n", encoding="utf-8")
        self._prepare_scratch(desk_root)
        return BranchWorkspace(cwd=str(desk_root), git_branch=None, base_sha=None)

    def validate_repository(self, path: str, default_ref: str) -> str:
        canonical = Path(path).expanduser().resolve()
        if not canonical.is_dir():
            raise WorkspaceError(f"Repository path does not exist: {canonical}")
        self._run_git(canonical, "rev-parse", "--git-dir")
        self._run_git(canonical, "rev-parse", "--verify", default_ref)
        return str(canonical)

    def adopt_existing(self, path: str) -> BranchWorkspace:
        """Reference an existing directory without creating, moving, or editing files."""
        canonical = Path(path).expanduser().resolve()
        if canonical == Path(canonical.anchor) or not canonical.is_dir():
            raise WorkspaceError(f"Existing session directory is not usable: {canonical}")
        try:
            base_sha = self._run_git(canonical, "rev-parse", "HEAD").strip()
            git_branch = self._run_git(canonical, "branch", "--show-current").strip() or None
        except WorkspaceError:
            base_sha = None
            git_branch = None
        return BranchWorkspace(cwd=str(canonical), git_branch=git_branch, base_sha=base_sha)

    def current_head(self, cwd: str) -> str | None:
        path = Path(cwd)
        if not path.exists():
            return None
        try:
            return self._run_git(path, "rev-parse", "HEAD").strip()
        except WorkspaceError:
            return None

    @staticmethod
    def _prepare_scratch(path: Path) -> None:
        git_dir = path / ".git"
        if not git_dir.exists():
            result = subprocess.run(
                ["git", "init", str(path)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise WorkspaceError(result.stderr.strip() or "git init failed")

    @staticmethod
    def _branch_exists(repository: Path, branch: str) -> bool:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    @staticmethod
    def _run_git(repository: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise WorkspaceError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout


def branch_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    slug = _slug(normalized)
    if slug:
        if any(character.isalnum() and not character.isascii() for character in normalized):
            digest = hashlib.sha1(
                normalized.casefold().encode("utf-8"),
                usedforsecurity=False,
            ).hexdigest()[:8]
            return f"{slug[:63]}-{digest}"
        return slug[:72]
    digest = hashlib.sha1(
        normalized.casefold().encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:8]
    return f"branch-{digest}"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")

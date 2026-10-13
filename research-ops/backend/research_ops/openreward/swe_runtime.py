"""Isolated SWE-bench runtime behind the OpenReward sandbox HTTP contract.

The runtime service is trusted infrastructure and may talk to the host Docker
daemon.  Policy and verifier containers never receive the Docker socket, host
mounts, credentials, or network access.  A policy edits one long-lived
``/testbed`` workspace; submission extracts its diff and evaluates that diff in
a second clean container with the official SWE-bench harness.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import importlib.metadata
import io
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
import types
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

_IMAGE_REF = re.compile(r"^[A-Za-z0-9._:/-]+$")
_MEMORY_LIMIT = re.compile(r"^[1-9][0-9]*(?:[bkmg])?$", re.IGNORECASE)
_R2E_STATUS = {"PASSED", "FAILED", "SKIPPED", "ERROR", "XFAIL", "XPASS"}
_R2E_TEST_RESULT = re.compile(r"^(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\s+(\S+)", re.MULTILINE)
_WORKDIR = PurePosixPath("/testbed")
_SWEBENCH_VERSION = "4.0.4"
_SWEBENCH_REVISION = "4b6773064a070c689874d30171e78a3f429a3fbf"
_SWEGYM_VERSION = "2.0.13"
_SWEGYM_REVISION = "242429c188fcfd06aad13fce9a54d450470bf0ac"
_LOGGER = logging.getLogger(__name__)


class SweRuntimeError(RuntimeError):
    """Base error for the SWE runtime."""


class SweTaskError(SweRuntimeError):
    """A task cannot be mapped to a canonical SWE-bench test spec."""


class SweInfrastructureError(SweRuntimeError):
    """Docker or the official verifier failed before producing a reward."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise SweTaskError("SWE verifier test collections must be arrays")
    return tuple(str(item) for item in value)


def _r2e_test_files(verifier: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    names = _strings(verifier.get("test_file_names"))
    codes = _strings(verifier.get("test_file_codes"))
    if not names or len(names) != len(codes):
        raise SweTaskError("R2E verifier requires matching test_file_names and test_file_codes")
    files: list[tuple[str, str]] = []
    total_bytes = 0
    for name, code in zip(names, codes, strict=True):
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or not path.parts
            or any(
                part in {".", ".."}
                or not re.fullmatch(r"[A-Za-z0-9._-]+", part)
                for part in path.parts
            )
        ):
            raise SweTaskError("R2E test filename must be a relative safe path")
        raw = code.encode("utf-8")
        total_bytes += len(raw)
        if total_bytes > 5_000_000:
            raise SweTaskError("R2E evaluator fixtures exceed the allowed size")
        files.append((str(path), code))
    return tuple(files)


def _r2e_expected_statuses(value: Any) -> tuple[tuple[str, str], ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SweTaskError("R2E expected_output_json is not valid JSON") from exc
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        value = {
            _text(item.get("name")): item.get("status")
            for item in value
            if isinstance(item, Mapping)
        }
    if not isinstance(value, Mapping) or not value:
        raise SweTaskError("R2E verifier requires non-empty expected test statuses")
    statuses: list[tuple[str, str]] = []
    for key, status in value.items():
        name = _text(key)
        normalized = _text(status).upper()
        if not name or normalized not in _R2E_STATUS:
            raise SweTaskError("R2E expected_output_json contains an invalid test status")
        statuses.append((name, normalized))
    return tuple(sorted(statuses))


def _env_int(environ: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(str(environ.get(name, default)))
    except (TypeError, ValueError) as exc:
        raise SweInfrastructureError(f"{name} must be an integer") from exc
    if value <= 0:
        raise SweInfrastructureError(f"{name} must be positive")
    return value


def _env_float(environ: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(str(environ.get(name, default)))
    except (TypeError, ValueError) as exc:
        raise SweInfrastructureError(f"{name} must be numeric") from exc
    if value <= 0:
        raise SweInfrastructureError(f"{name} must be positive")
    return value


def _env_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = str(environ.get(name, str(default))).strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise SweInfrastructureError(f"{name} must be boolean")


def workspace_path(value: Any) -> PurePosixPath:
    """Resolve a policy path under ``/testbed`` without touching the host."""

    raw = _text(value)
    if not raw:
        raise SweTaskError("workspace path is required")
    path = PurePosixPath(raw)
    if not path.is_absolute():
        path = _WORKDIR / path
    if ".." in path.parts or path == _WORKDIR or not path.is_relative_to(_WORKDIR):
        raise SweTaskError("workspace path must name a file below /testbed")
    return path


@dataclass(frozen=True)
class SweBenchTask:
    task_id: str
    repo: str
    version: str
    base_commit: str
    problem_statement: str
    test_patch: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    benchmark_family: str = "swebench"
    explicit_image_ref: str | None = None
    r2e_test_files: tuple[tuple[str, str], ...] = ()
    r2e_expected_statuses: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_spec(cls, task_spec: Mapping[str, Any]) -> SweBenchTask:
        family = _text(task_spec.get("benchmark_family")).casefold().replace("-", "")
        if family not in {"swebench", "swegym", "r2e"}:
            raise SweTaskError(
                "this runtime accepts benchmark_family=swebench, swegym, or r2e"
            )
        setup = _mapping(task_spec.get("setup"))
        verifier = _mapping(task_spec.get("verifier"))
        sandbox = _mapping(task_spec.get("sandbox"))
        required = {
            "task_id": _text(task_spec.get("task_id")),
            "repo_ref": _text(task_spec.get("repo_ref")),
            "base_commit": _text(task_spec.get("base_commit")),
            "query": _text(task_spec.get("query")),
        }
        if family != "r2e":
            required["version"] = _text(setup.get("version"))
            required["test_patch"] = str(verifier.get("test_patch") or "")
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise SweTaskError(f"SWE task is missing required fields: {','.join(missing)}")
        image_ref = _text(sandbox.get("image_ref")) or None
        if image_ref is not None and not _IMAGE_REF.fullmatch(image_ref):
            raise SweTaskError("SWE image reference contains unsupported characters")
        if family == "r2e":
            if image_ref is None:
                raise SweTaskError("R2E task requires sandbox.image_ref")
            return cls(
                task_id=required["task_id"],
                repo=required["repo_ref"],
                version="r2e",
                base_commit=required["base_commit"],
                problem_statement=required["query"],
                test_patch="",
                fail_to_pass=(),
                pass_to_pass=(),
                benchmark_family=family,
                explicit_image_ref=image_ref,
                r2e_test_files=_r2e_test_files(verifier),
                r2e_expected_statuses=_r2e_expected_statuses(
                    verifier.get("expected_tests", verifier.get("expected_output_json"))
                ),
            )
        return cls(
            task_id=required["task_id"],
            repo=required["repo_ref"],
            version=required["version"],
            base_commit=required["base_commit"],
            problem_statement=required["query"],
            test_patch=required["test_patch"],
            fail_to_pass=_strings(verifier.get("fail_to_pass")),
            pass_to_pass=_strings(verifier.get("pass_to_pass")),
            benchmark_family=family,
            explicit_image_ref=image_ref,
        )

    def harness_instance(self) -> dict[str, Any]:
        return {
            "instance_id": self.task_id,
            "repo": self.repo,
            "version": self.version,
            "base_commit": self.base_commit,
            "problem_statement": self.problem_statement,
            # SWE-Gym's fork reads these two fields while constructing the
            # pre-built instance evaluation script.  Empty hints are safe and
            # avoid exposing any hidden trajectory context to the policy.
            "hints_text": "",
            "environment_setup_commit": self.base_commit,
            "test_patch": self.test_patch,
            "FAIL_TO_PASS": list(self.fail_to_pass),
            "PASS_TO_PASS": list(self.pass_to_pass),
        }


@dataclass(frozen=True)
class HarnessPlan:
    task: SweBenchTask
    image_ref: str
    eval_script: str
    harness_version: str
    test_spec: Any = field(repr=False)
    requires_head_match: bool = True


@dataclass(frozen=True)
class GradeResult:
    resolved: bool
    patch_applied: bool
    fail_to_pass: float
    pass_to_pass: float
    fail_to_pass_passed: int
    fail_to_pass_total: int
    pass_to_pass_passed: int
    pass_to_pass_total: int


class SweHarness(Protocol):
    version: str

    def plan(self, task: SweBenchTask) -> HarnessPlan: ...

    def grade(
        self,
        plan: HarnessPlan,
        test_output: str,
        *,
        patch_applied: bool,
    ) -> GradeResult: ...


def _namespace_package(name: str, path: Path) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__package__ = name
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


def _source_version(package_root: Path) -> str:
    try:
        source = (package_root / "__init__.py").read_text(encoding="utf-8")
    except OSError as exc:
        raise SweInfrastructureError("SWE-bench source root is invalid") from exc
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    if match is None:
        raise SweInfrastructureError("SWE-bench source version cannot be determined")
    return match.group(1)


def _source_revision(source_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        result = None
    except (OSError, subprocess.SubprocessError) as exc:
        raise SweInfrastructureError("SWE-bench source revision cannot be read") from exc
    revision = result.stdout.strip() if result is not None else ""
    if result is not None and result.returncode == 0 and revision:
        return revision

    # The lightweight K8s runtime image intentionally has no git binary.  A
    # mounted source checkout still carries enough metadata for an exact
    # revision check (including packed refs and detached HEADs).
    git_path = source_root / ".git"
    try:
        if git_path.is_file():
            pointer = git_path.read_text(encoding="utf-8").strip()
            if pointer.startswith("gitdir:"):
                git_path = (source_root / pointer.split(":", 1)[1].strip()).resolve()
        head = (git_path / "HEAD").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{40}", head):
            return head
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            ref_file = git_path / ref
            if ref_file.is_file():
                value = ref_file.read_text(encoding="utf-8").strip()
                if re.fullmatch(r"[0-9a-f]{40}", value):
                    return value
            packed = git_path / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    fields = line.split()
                    if len(fields) == 2 and fields[1] == ref and re.fullmatch(
                        r"[0-9a-f]{40}", fields[0]
                    ):
                        return fields[0]
    except OSError:
        pass
    # A source archive used in a read-only K8s mount has no .git directory.
    # The materializer writes this marker after fetching an exact commit.
    for marker_name in (".source_revision", "SOURCE_REVISION"):
        marker = source_root / marker_name
        try:
            value = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if re.fullmatch(r"[0-9a-f]{40}", value):
            return value
    raise SweInfrastructureError("SWE-bench source revision cannot be read")


def _load_official_harness(
    *,
    harness_root: str | Path | None,
    expected_revision: str,
) -> tuple[str, Any, Any]:
    """Load only official verifier modules, not dataset/Modal CLI dependencies.

    SWE-bench 4.0.4 imports collection, dataset and Modal modules from its
    package initializers.  The online runtime needs none of those.  Namespace
    packages below point Python at the unmodified official source while a tiny
    utility shim provides the two pure helpers imported by test-spec/log-parser
    modules.  Test generation and grading remain official harness functions.
    """

    if harness_root:
        source_root = Path(harness_root).expanduser().resolve()
        package_root = source_root / "swebench"
        revision = _source_revision(source_root)
        if expected_revision and revision != expected_revision:
            raise SweInfrastructureError("SWE-bench source revision does not match pin")
        version = _source_version(package_root)
        identity = f"{version}+git.{revision[:12]}"
    else:
        try:
            distribution = importlib.metadata.distribution("swebench")
        except importlib.metadata.PackageNotFoundError as exc:
            raise SweInfrastructureError(
                "install swebench==4.0.4 without dependencies or set ORS_SWE_HARNESS_ROOT"
            ) from exc
        package_root = Path(distribution.locate_file("swebench")).resolve()
        version = distribution.version
        identity = version
    if version != _SWEBENCH_VERSION:
        raise SweInfrastructureError("SWE-bench harness version does not match 4.0.4 pin")

    for name in tuple(sys.modules):
        if name == "swebench" or name.startswith("swebench."):
            del sys.modules[name]
    _namespace_package("swebench", package_root)
    harness_path = package_root / "harness"
    _namespace_package("swebench.harness", harness_path)
    _namespace_package("swebench.harness.test_spec", harness_path / "test_spec")

    try:
        from unidiff import PatchSet
    except ImportError as exc:
        raise SweInfrastructureError("unidiff is required by the SWE harness") from exc

    utility = types.ModuleType("swebench.harness.utils")

    def get_modified_files(patch: str) -> list[str]:
        files = [item.source_file for item in PatchSet(patch) if item.source_file != "/dev/null"]
        return [item[2:] for item in files if item.startswith("a/")]

    def ansi_escape(text: str) -> str:
        return re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])").sub("", text)

    utility.get_modified_files = get_modified_files  # type: ignore[attr-defined]
    utility.ansi_escape = ansi_escape  # type: ignore[attr-defined]
    sys.modules[utility.__name__] = utility

    test_spec = importlib.import_module("swebench.harness.test_spec.test_spec")
    grading = importlib.import_module("swebench.harness.grading")
    return identity, test_spec.make_test_spec, grading.get_eval_report


def _load_swegym_harness(
    *,
    harness_root: str | Path,
    expected_revision: str,
) -> tuple[str, Any, Any]:
    """Load the pinned SWE-Gym fork without importing its CLI dependencies.

    SWE-Gym keeps its additional repository/version specifications in a fork
    of SWE-bench (2.0.13).  The online runtime only needs ``make_test_spec``
    and ``get_eval_report``; loading those modules through a namespace package
    keeps dataset, Docker, and CLI dependencies out of the ORS process.
    """

    source_root = Path(harness_root).expanduser().resolve()
    package_root = source_root / "swebench"
    revision = _source_revision(source_root)
    if expected_revision and revision != expected_revision:
        raise SweInfrastructureError("SWE-Gym harness source revision does not match pin")
    version = _source_version(package_root)
    if version != _SWEGYM_VERSION:
        raise SweInfrastructureError("SWE-Gym harness version does not match 2.0.13 pin")

    for name in tuple(sys.modules):
        if name == "swebench" or name.startswith("swebench."):
            del sys.modules[name]
    _namespace_package("swebench", package_root)
    harness_path = package_root / "harness"
    _namespace_package("swebench.harness", harness_path)

    try:
        from unidiff import PatchSet
    except ImportError as exc:
        raise SweInfrastructureError("unidiff is required by the SWE-Gym harness") from exc

    utility = types.ModuleType("swebench.harness.utils")

    def get_modified_files(patch: str) -> list[str]:
        files = [item.source_file for item in PatchSet(patch) if item.source_file != "/dev/null"]
        return [item[2:] for item in files if item.startswith("a/")]

    def ansi_escape(text: str) -> str:
        return re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])").sub("", text)

    def get_requirements(_: Mapping[str, Any]) -> str:
        # Pre-built SWE-Gym images already contain the task environment.  The
        # fork only needs a syntactically valid body when it builds an unused
        # image-generation script, so do not make the ORS service fetch GitHub
        # files at request time.
        return ""

    def get_environment_yml(_: Mapping[str, Any], env_name: str) -> str:
        return f"name: {env_name}\nchannels: []\ndependencies: []\n"

    def get_test_directives(instance: Mapping[str, Any]) -> list[str]:
        diff_pat = r"diff --git a/.* b/(.*)"
        non_test_exts = {".md", ".rst", ".txt", ".json", ".yaml", ".yml"}
        directives = [
            item
            for item in re.findall(diff_pat, str(instance.get("test_patch") or ""))
            if not any(item.endswith(ext) for ext in non_test_exts)
        ]
        if str(instance.get("repo") or "") == "django/django":
            directives = [
                item.removesuffix(".py").removeprefix("tests/").replace("/", ".")
                for item in directives
            ]
        return directives

    utility.get_modified_files = get_modified_files  # type: ignore[attr-defined]
    utility.ansi_escape = ansi_escape  # type: ignore[attr-defined]
    utility.get_requirements = get_requirements  # type: ignore[attr-defined]
    utility.get_environment_yml = get_environment_yml  # type: ignore[attr-defined]
    utility.get_test_directives = get_test_directives  # type: ignore[attr-defined]
    sys.modules[utility.__name__] = utility

    try:
        importlib.import_module("swebench.harness.constants")
        importlib.import_module("swebench.harness.dockerfiles")
        importlib.import_module("swebench.harness.log_parsers")
        test_spec = importlib.import_module("swebench.harness.test_spec")
        grading = importlib.import_module("swebench.harness.grading")
    except (ImportError, OSError) as exc:
        raise SweInfrastructureError("SWE-Gym harness modules cannot be loaded") from exc
    return f"{version}+git.{revision[:12]}", test_spec.make_test_spec, grading.get_eval_report


class OfficialSwebenchHarness:
    """Small lazy adapter around the official ``swebench`` package."""

    runtime_name = "official-swebench"

    def __init__(
        self,
        *,
        namespace: str = "swebench",
        image_tag: str = "latest",
        harness_root: str | Path | None = None,
        expected_revision: str = _SWEBENCH_REVISION,
    ) -> None:
        try:
            version, make_test_spec, get_eval_report = _load_official_harness(
                harness_root=harness_root,
                expected_revision=expected_revision,
            )
        except SweRuntimeError:
            raise
        except (ImportError, OSError) as exc:  # pragma: no cover - deployment dependency check
            raise SweInfrastructureError(
                "official SWE-bench harness cannot be loaded"
            ) from exc
        self.version = version
        self.namespace = namespace
        self.image_tag = image_tag
        self._make_test_spec = make_test_spec
        self._get_eval_report = get_eval_report

    def plan(self, task: SweBenchTask) -> HarnessPlan:
        try:
            test_spec = self._make_test_spec(
                task.harness_instance(),
                namespace=self.namespace,
                instance_image_tag=self.image_tag,
            )
        except Exception as exc:
            raise SweTaskError(
                f"official SWE-bench cannot build test spec: {type(exc).__name__}"
            ) from exc
        image_ref = str(test_spec.instance_image_key)
        if task.explicit_image_ref is not None and task.explicit_image_ref != image_ref:
            raise SweTaskError("task image does not match the official harness image")
        return HarnessPlan(
            task=task,
            image_ref=image_ref,
            eval_script=str(test_spec.eval_script),
            harness_version=self.version,
            test_spec=test_spec,
        )

    def grade(
        self,
        plan: HarnessPlan,
        test_output: str,
        *,
        patch_applied: bool,
    ) -> GradeResult:
        if not patch_applied:
            return GradeResult(False, False, 0.0, 0.0, 0, len(plan.task.fail_to_pass), 0, 0)
        prediction = {
            "instance_id": plan.task.task_id,
            "model_name_or_path": "openreward-policy",
            "model_patch": "",  # Non-None: the patch was applied before the eval script.
        }
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as output:
                output.write(test_output)
                output.flush()
                report = self._get_eval_report(
                    test_spec=plan.test_spec,
                    prediction=prediction,
                    test_log_path=output.name,
                    include_tests_status=True,
                )
        except Exception as exc:
            raise SweInfrastructureError(
                f"official SWE-bench grading failed: {type(exc).__name__}"
            ) from exc
        instance = _mapping(_mapping(report).get(plan.task.task_id))
        statuses = _mapping(instance.get("tests_status"))
        f2p = _mapping(statuses.get("FAIL_TO_PASS"))
        p2p = _mapping(statuses.get("PASS_TO_PASS"))
        f2p_passed = len(f2p.get("success") or [])
        f2p_failed = len(f2p.get("failure") or [])
        p2p_passed = len(p2p.get("success") or [])
        p2p_failed = len(p2p.get("failure") or [])
        f2p_total = f2p_passed + f2p_failed
        p2p_total = p2p_passed + p2p_failed
        report_applied = bool(instance.get("patch_successfully_applied"))
        if not report_applied:
            f2p_total = len(plan.task.fail_to_pass)
            p2p_total = len(plan.task.pass_to_pass)
        return GradeResult(
            resolved=bool(instance.get("resolved")),
            patch_applied=report_applied,
            fail_to_pass=(
                f2p_passed / f2p_total if f2p_total else float(report_applied)
            ),
            pass_to_pass=(
                p2p_passed / p2p_total if p2p_total else float(report_applied)
            ),
            fail_to_pass_passed=f2p_passed,
            fail_to_pass_total=f2p_total,
            pass_to_pass_passed=p2p_passed,
            pass_to_pass_total=p2p_total,
        )


class SwegymHarness:
    """Adapter for the executable SWE-Gym training environments."""

    runtime_name = "swegym"

    def __init__(
        self,
        *,
        harness_root: str | Path,
        expected_revision: str = _SWEGYM_REVISION,
        image_namespace: str = "xingyaoww",
    ) -> None:
        try:
            version, make_test_spec, get_eval_report = _load_swegym_harness(
                harness_root=harness_root,
                expected_revision=expected_revision,
            )
        except SweRuntimeError:
            raise
        except (ImportError, OSError) as exc:  # pragma: no cover - deployment dependency check
            raise SweInfrastructureError("SWE-Gym harness cannot be loaded") from exc
        self.version = version
        self.image_namespace = image_namespace.strip().rstrip("/")
        if not self.image_namespace or not _IMAGE_REF.fullmatch(self.image_namespace):
            raise SweInfrastructureError("SWE-Gym image namespace is invalid")
        self._make_test_spec = make_test_spec
        self._get_eval_report = get_eval_report

    def plan(self, task: SweBenchTask) -> HarnessPlan:
        if task.benchmark_family != "swegym":
            raise SweTaskError("SWE-Gym harness received a non-swegym task")
        try:
            test_spec = self._make_test_spec(task.harness_instance())
        except Exception as exc:
            raise SweTaskError(
                f"SWE-Gym cannot build test spec: {type(exc).__name__}"
            ) from exc
        # Docker Hub rejects the double underscore used by SWE instance IDs;
        # SWE-Gym's publishing script rewrites it to ``_s_``.
        image_key = str(test_spec.instance_image_key).replace("__", "_s_")
        image_ref = f"{self.image_namespace}/{image_key}"
        if task.explicit_image_ref is not None:
            expected = task.explicit_image_ref.casefold()
            if expected != image_ref.casefold():
                raise SweTaskError("task image does not match the SWE-Gym harness image")
        return HarnessPlan(
            task=task,
            image_ref=image_ref,
            eval_script=str(test_spec.eval_script),
            harness_version=self.version,
            test_spec=test_spec,
        )

    def grade(
        self,
        plan: HarnessPlan,
        test_output: str,
        *,
        patch_applied: bool,
    ) -> GradeResult:
        if not patch_applied:
            return GradeResult(False, False, 0.0, 0.0, 0, len(plan.task.fail_to_pass), 0, 0)
        prediction = {
            "instance_id": plan.task.task_id.lower(),
            "model_name_or_path": "openreward-policy",
            "model_patch": "",
        }
        try:
            with tempfile.TemporaryDirectory(prefix="ors-swegym-") as directory:
                log_dir = Path(directory) / plan.task.task_id.lower()
                log_dir.mkdir()
                log_path = log_dir / "test_output.txt"
                log_path.write_text(test_output, encoding="utf-8")
                report = self._get_eval_report(
                    test_spec=plan.test_spec,
                    prediction=prediction,
                    log_path=str(log_path),
                    include_tests_status=True,
                )
        except Exception as exc:
            raise SweInfrastructureError(
                f"SWE-Gym grading failed: {type(exc).__name__}"
            ) from exc
        instance = _mapping(_mapping(report).get(plan.task.task_id.lower()))
        statuses = _mapping(instance.get("tests_status"))
        f2p = _mapping(statuses.get("FAIL_TO_PASS"))
        p2p = _mapping(statuses.get("PASS_TO_PASS"))
        f2p_passed = len(f2p.get("success") or [])
        f2p_failed = len(f2p.get("failure") or [])
        p2p_passed = len(p2p.get("success") or [])
        p2p_failed = len(p2p.get("failure") or [])
        f2p_total = f2p_passed + f2p_failed
        p2p_total = p2p_passed + p2p_failed
        report_applied = bool(instance.get("patch_successfully_applied"))
        if not report_applied:
            f2p_total = len(plan.task.fail_to_pass)
            p2p_total = len(plan.task.pass_to_pass)
        return GradeResult(
            resolved=bool(instance.get("resolved")),
            patch_applied=report_applied,
            fail_to_pass=f2p_passed / f2p_total if f2p_total else float(report_applied),
            pass_to_pass=p2p_passed / p2p_total if p2p_total else float(report_applied),
            fail_to_pass_passed=f2p_passed,
            fail_to_pass_total=f2p_total,
            pass_to_pass_passed=p2p_passed,
            pass_to_pass_total=p2p_total,
        )


@dataclass(frozen=True)
class R2eTestSpec:
    expected_statuses: tuple[tuple[str, str], ...]


def _r2e_test_key(value: str) -> str:
    parts = value.strip().split("::")
    if len(parts) > 1:
        return ".".join(parts[1:])
    return value.strip()


class R2eHarness:
    """Verifier adapter for the prebuilt, task-scoped R2E-Gym environments."""

    runtime_name = "r2e"
    version = "r2e-gym.manifest.v1"

    def plan(self, task: SweBenchTask) -> HarnessPlan:
        if task.benchmark_family != "r2e":
            raise SweTaskError("R2E harness received a non-r2e task")
        if task.explicit_image_ref is None:
            raise SweTaskError("R2E task is missing its pinned image reference")
        lines = [
            "#!/bin/bash",
            "set +e",
            "rm -rf /testbed/r2e_tests",
            "mkdir -p /testbed/r2e_tests",
        ]
        for name, code in task.r2e_test_files:
            path = PurePosixPath("/testbed/r2e_tests") / name
            encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
            lines.extend(
                [
                    f"mkdir -p -- {path.parent}",
                    f"printf '%s' '{encoded}' | base64 -d > {path}",
                ]
            )
        lines.append("/bin/bash /testbed/run_tests.sh")
        return HarnessPlan(
            task=task,
            image_ref=task.explicit_image_ref,
            eval_script="\n".join(lines) + "\n",
            harness_version=self.version,
            test_spec=R2eTestSpec(task.r2e_expected_statuses),
            # R2E image tags identify a task snapshot, but their /testbed Git
            # checkout is not guaranteed to remain at the manifest commit.
            requires_head_match=False,
        )

    def grade(
        self,
        plan: HarnessPlan,
        test_output: str,
        *,
        patch_applied: bool,
    ) -> GradeResult:
        expected = dict(plan.test_spec.expected_statuses)
        if not patch_applied:
            return GradeResult(False, False, 0.0, 0.0, 0, len(expected), 0, 0)
        observed: dict[str, str] = {}
        for status, node_id in _R2E_TEST_RESULT.findall(test_output):
            observed[_r2e_test_key(node_id)] = status
        matched = sum(observed.get(name) == status for name, status in expected.items())
        unexpected = [
            name
            for name, status in observed.items()
            if name not in expected or expected[name] != status
        ]
        resolved = matched == len(expected) and not unexpected
        total = len(expected)
        return GradeResult(
            resolved=resolved,
            patch_applied=True,
            fail_to_pass=matched / total if total else 0.0,
            pass_to_pass=1.0 if not unexpected else 0.0,
            fail_to_pass_passed=matched,
            fail_to_pass_total=total,
            pass_to_pass_passed=0 if unexpected else total,
            pass_to_pass_total=total,
        )


@dataclass(frozen=True)
class SweRuntimeConfig:
    cpus: float = 2.0
    memory: str = "8g"
    pids_limit: int = 512
    command_seconds: int = 120
    verify_seconds: int = 1800
    max_command_output_bytes: int = 256_000
    max_eval_output_bytes: int = 10_000_000
    max_file_bytes: int = 2_000_000
    max_patch_bytes: int = 5_000_000
    session_ttl_seconds: int = 3600
    pull_missing_images: bool = True

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> SweRuntimeConfig:
        runtime = os.environ if environ is None else environ
        memory = str(runtime.get("ORS_SWE_MEMORY", "8g")).strip().casefold()
        if not _MEMORY_LIMIT.fullmatch(memory):
            raise SweInfrastructureError("ORS_SWE_MEMORY is invalid")
        return cls(
            cpus=_env_float(runtime, "ORS_SWE_CPUS", 2.0),
            memory=memory,
            pids_limit=_env_int(runtime, "ORS_SWE_PIDS_LIMIT", 512),
            command_seconds=_env_int(runtime, "ORS_SWE_COMMAND_SECONDS", 120),
            verify_seconds=_env_int(runtime, "ORS_SWE_VERIFY_SECONDS", 1800),
            max_command_output_bytes=_env_int(
                runtime, "ORS_SWE_MAX_COMMAND_OUTPUT_BYTES", 256_000
            ),
            max_eval_output_bytes=_env_int(
                runtime, "ORS_SWE_MAX_EVAL_OUTPUT_BYTES", 10_000_000
            ),
            max_file_bytes=_env_int(runtime, "ORS_SWE_MAX_FILE_BYTES", 2_000_000),
            max_patch_bytes=_env_int(runtime, "ORS_SWE_MAX_PATCH_BYTES", 5_000_000),
            session_ttl_seconds=_env_int(runtime, "ORS_SWE_SESSION_TTL_SECONDS", 3600),
            pull_missing_images=_env_bool(runtime, "ORS_SWE_PULL_MISSING_IMAGES", True),
        )


@dataclass(frozen=True)
class CommandResult:
    text: str
    exit_code: int
    timed_out: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class EvaluationResult:
    patch_applied: bool
    patch_sha256: str
    patch_bytes: int
    test_output: str
    eval_exit_code: int | None
    eval_timed_out: bool


@dataclass(frozen=True)
class VerificationResult:
    grade: GradeResult
    patch_sha256: str
    patch_bytes: int
    eval_exit_code: int | None
    eval_timed_out: bool


class DockerSweEngine:
    """Launch policy and verifier containers with no host mounts or network."""

    def __init__(self, config: SweRuntimeConfig, *, client: Any | None = None) -> None:
        if client is None:
            try:
                import docker
            except ImportError as exc:  # pragma: no cover - deployment dependency check
                raise SweInfrastructureError(
                    "install the swe-runtime extra to launch SWE containers"
                ) from exc
            try:
                client = docker.from_env()
            except Exception as exc:  # pragma: no cover - depends on daemon
                raise SweInfrastructureError(
                    f"cannot connect to Docker: {type(exc).__name__}"
                ) from exc
        self.config = config
        self.client = client

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:
            return False

    def image_id(self, image_ref: str) -> str:
        try:
            return str(self.client.images.get(image_ref).id)
        except Exception as exc:
            raise SweInfrastructureError(
                f"cannot inspect SWE image: {type(exc).__name__}"
            ) from exc

    def start(self, plan: HarnessPlan, *, session_id: str, purpose: str) -> Any:
        try:
            self.client.images.get(plan.image_ref)
        except Exception as missing:
            if not self.config.pull_missing_images:
                raise SweInfrastructureError(
                    "official SWE image is not present locally"
                ) from missing
            try:
                self.client.images.pull(plan.image_ref)
            except Exception as exc:
                raise SweInfrastructureError(
                    f"cannot pull official SWE image: {type(exc).__name__}"
                ) from exc
        name = f"ors-swe-{purpose}-{session_id[-20:]}"
        container = None
        try:
            container = self.client.containers.run(
                plan.image_ref,
                command=[
                    "-lc",
                    "trap 'exit 0' TERM INT; while :; do sleep 3600; done",
                ],
                entrypoint="/bin/bash",
                detach=True,
                name=name,
                working_dir=str(_WORKDIR),
                network_disabled=True,
                nano_cpus=int(self.config.cpus * 1_000_000_000),
                mem_limit=self.config.memory,
                pids_limit=self.config.pids_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                init=True,
                labels={
                    "research-ops.runtime": "openreward-swe",
                    "research-ops.purpose": purpose,
                    "research-ops.task-sha256": hashlib.sha256(
                        plan.task.task_id.encode("utf-8")
                    ).hexdigest(),
                },
            )
            head = self._exec_raw(container, ["git", "rev-parse", "HEAD"])
            if head.exit_code != 0 or (
                plan.requires_head_match and head.text.strip() != plan.task.base_commit
            ):
                self.remove(container)
                raise SweInfrastructureError(
                    "SWE image HEAD does not match task base_commit"
                )
            baseline = self._exec_raw(
                container,
                [
                    "/bin/bash",
                    "-lc",
                    (
                        "set -e; git add -A; git write-tree > /tmp/ors-baseline-tree; "
                        "git reset --mixed -q HEAD"
                    ),
                ],
            )
            if baseline.exit_code != 0:
                self.remove(container)
                raise SweInfrastructureError("cannot snapshot official SWE image baseline")
            return container
        except SweRuntimeError:
            self.remove(container)
            raise
        except Exception as exc:
            self.remove(container)
            raise SweInfrastructureError(
                f"cannot start SWE container: {type(exc).__name__}"
            ) from exc

    def remove(self, container: Any | None) -> None:
        if container is None:
            return
        with suppress(Exception):
            container.remove(force=True)

    def _exec_raw(
        self,
        container: Any,
        command: Sequence[str],
        *,
        workdir: PurePosixPath = _WORKDIR,
    ) -> CommandResult:
        try:
            result = container.exec_run(list(command), workdir=str(workdir), demux=False)
        except Exception as exc:
            raise SweInfrastructureError(
                f"Docker exec failed: {type(exc).__name__}"
            ) from exc
        raw = result.output or b""
        if isinstance(raw, tuple):
            raw = b"".join(item or b"" for item in raw)
        text = raw if isinstance(raw, str) else bytes(raw).decode("utf-8", errors="replace")
        return CommandResult(text=text, exit_code=int(result.exit_code))

    def exec_shell(
        self,
        container: Any,
        command: str,
        *,
        seconds: int | None = None,
        max_bytes: int | None = None,
    ) -> CommandResult:
        limit = int(max_bytes or self.config.max_command_output_bytes)
        timeout = int(seconds or self.config.command_seconds)
        output = f"/tmp/ors-output-{secrets.token_hex(8)}"
        wrapper = r'''
set +e
output="$1"
seconds="$2"
limit="$3"
command="$4"
timeout --signal=KILL "${seconds}s" /bin/bash -lc "$command" >"$output" 2>&1
rc=$?
bytes=$(wc -c <"$output")
head -c "$limit" "$output"
rm -f "$output"
if [ "$bytes" -gt "$limit" ]; then
  printf '\n[OpenReward output truncated: %s bytes]\n' "$bytes"
fi
exit "$rc"
'''
        result = self._exec_raw(
            container,
            ["/bin/bash", "-c", wrapper, "ors", output, str(timeout), str(limit), command],
        )
        return CommandResult(
            text=result.text,
            exit_code=result.exit_code,
            timed_out=result.exit_code in {124, 137},
            truncated="[OpenReward output truncated:" in result.text,
        )

    def read_file(self, container: Any, path: PurePosixPath) -> CommandResult:
        result = self._exec_raw(
            container,
            ["head", "-c", str(self.config.max_file_bytes + 1), "--", str(path)],
        )
        encoded = result.text.encode("utf-8", errors="replace")
        truncated = len(encoded) > self.config.max_file_bytes
        text = encoded[: self.config.max_file_bytes].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[OpenReward file truncated]\n"
        return CommandResult(text, result.exit_code, truncated=truncated)

    def write_file(self, container: Any, path: PurePosixPath, content: str) -> int:
        raw = content.encode("utf-8")
        if len(raw) > self.config.max_file_bytes:
            raise SweTaskError("file content exceeds ORS_SWE_MAX_FILE_BYTES")
        parent = path.parent
        mkdir = self._exec_raw(container, ["mkdir", "-p", "--", str(parent)])
        if mkdir.exit_code != 0:
            raise SweInfrastructureError("cannot create parent directory in SWE workspace")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as bundle:
            info = tarfile.TarInfo(path.name)
            info.size = len(raw)
            info.mode = 0o644
            bundle.addfile(info, io.BytesIO(raw))
        try:
            written = container.put_archive(str(parent), archive.getvalue())
        except Exception as exc:
            raise SweInfrastructureError(
                f"cannot write SWE file: {type(exc).__name__}"
            ) from exc
        if not written:
            raise SweInfrastructureError("Docker rejected SWE file archive")
        return len(raw)

    def _put_bytes(self, container: Any, path: PurePosixPath, raw: bytes) -> None:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as bundle:
            info = tarfile.TarInfo(path.name)
            info.size = len(raw)
            info.mode = 0o600
            bundle.addfile(info, io.BytesIO(raw))
        try:
            if not container.put_archive(str(path.parent), archive.getvalue()):
                raise SweInfrastructureError("Docker rejected verifier input")
        except SweRuntimeError:
            raise
        except Exception as exc:
            raise SweInfrastructureError(
                f"cannot write verifier input: {type(exc).__name__}"
            ) from exc

    def extract_patch(self, container: Any) -> bytes:
        patch_path = PurePosixPath(f"/tmp/ors-model-{secrets.token_hex(8)}.patch")
        command = (
            "set -e; git add -A; "
            "trap 'git reset --mixed -q >/dev/null 2>&1 || true; "
            f"rm -f {patch_path}' EXIT; "
            "baseline=$(cat /tmp/ors-baseline-tree); "
            f"git -c core.fileMode=false diff --cached --binary \"$baseline\" > {patch_path}; "
            f"bytes=$(wc -c < {patch_path}); "
            f"if [ \"$bytes\" -gt {self.config.max_patch_bytes} ]; then exit 90; fi; "
            f"cat {patch_path}"
        )
        result = self._exec_raw(container, ["/bin/bash", "-lc", command])
        if result.exit_code == 90:
            raise SweTaskError("policy patch exceeds ORS_SWE_MAX_PATCH_BYTES")
        if result.exit_code != 0:
            raise SweInfrastructureError("cannot extract policy git diff")
        patch = result.text.encode("utf-8")
        if len(patch) > self.config.max_patch_bytes:
            raise SweTaskError("policy patch exceeds ORS_SWE_MAX_PATCH_BYTES")
        return patch

    def verify(self, plan: HarnessPlan, patch: bytes, *, session_id: str) -> EvaluationResult:
        verifier = self.start(plan, session_id=session_id, purpose="verify")
        eval_result: CommandResult | None = None
        patch_applied = not patch
        try:
            if patch:
                patch_path = PurePosixPath("/tmp/ors-model.patch")
                self._put_bytes(verifier, patch_path, patch)
                apply_result = self._exec_raw(
                    verifier,
                    ["git", "apply", "--verbose", str(patch_path)],
                )
                patch_applied = apply_result.exit_code == 0
            if patch_applied:
                eval_path = PurePosixPath("/tmp/ors-eval.sh")
                self._put_bytes(verifier, eval_path, plan.eval_script.encode("utf-8"))
                eval_result = self.exec_shell(
                    verifier,
                    f"/bin/bash {eval_path}",
                    seconds=self.config.verify_seconds,
                    max_bytes=self.config.max_eval_output_bytes,
                )
        finally:
            self.remove(verifier)
        return EvaluationResult(
            patch_applied=patch_applied,
            patch_sha256=hashlib.sha256(patch).hexdigest(),
            patch_bytes=len(patch),
            test_output=eval_result.text if eval_result else "",
            eval_exit_code=eval_result.exit_code if eval_result else None,
            eval_timed_out=bool(eval_result and eval_result.timed_out),
        )


@dataclass
class RuntimeSession:
    session_id: str
    task: SweBenchTask
    plan: HarnessPlan
    image_id: str
    container: Any | None
    created_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    terminal_result: dict[str, Any] | None = None


def _backend_result(
    *,
    text: str,
    reward: float | None = None,
    finished: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "output": {
            "text": text,
            "reward": reward,
            "finished": finished,
            "metadata": dict(metadata or {}),
        }
    }


class SweRuntimeService:
    def __init__(
        self,
        engine: Any,
        harness: SweHarness,
        *,
        ttl_seconds: int,
    ) -> None:
        self.engine = engine
        self.harness = harness
        self.runtime_name = str(
            getattr(harness, "runtime_name", "official-swebench")
        )
        self.ttl_seconds = ttl_seconds
        self.sessions: dict[str, RuntimeSession] = {}
        self.lock = asyncio.Lock()

    async def create(self, task_id: str, task_spec: Mapping[str, Any]) -> dict[str, Any]:
        await self.reap_expired()
        task = SweBenchTask.from_spec(task_spec)
        if task_id and task_id != task.task_id:
            raise SweTaskError("request task_id does not match task_spec.task_id")
        plan = self.harness.plan(task)
        session_id = f"ors-swe-{secrets.token_hex(12)}"
        container = await asyncio.to_thread(
            self.engine.start,
            plan,
            session_id=session_id,
            purpose="policy",
        )
        try:
            image_id = await asyncio.to_thread(self.engine.image_id, plan.image_ref)
        except Exception:
            await asyncio.to_thread(self.engine.remove, container)
            raise
        session = RuntimeSession(
            session_id,
            task,
            plan,
            image_id,
            container,
            time.monotonic(),
        )
        async with self.lock:
            self.sessions[session_id] = session
        return {
            "session_id": session_id,
            "sandbox_id": session_id,
            "runtime": self.runtime_name,
            "harness_version": plan.harness_version,
            "image_id": image_id,
        }

    async def get(self, session_id: str) -> RuntimeSession:
        async with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    async def bash(self, session: RuntimeSession, command: str) -> dict[str, Any]:
        if not command.strip():
            raise SweTaskError("command is required")
        async with session.lock:
            if session.terminal_result is not None or session.container is None:
                raise SweTaskError("SWE session is already finished")
            result = await asyncio.to_thread(
                self.engine.exec_shell,
                session.container,
                command,
            )
        return _backend_result(
            text=result.text,
            metadata={
                "runtime": self.runtime_name,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "truncated": result.truncated,
            },
        )

    async def read(self, session: RuntimeSession, raw_path: Any) -> dict[str, Any]:
        path = workspace_path(raw_path)
        async with session.lock:
            if session.terminal_result is not None or session.container is None:
                raise SweTaskError("SWE session is already finished")
            result = await asyncio.to_thread(self.engine.read_file, session.container, path)
        if result.exit_code != 0:
            raise SweTaskError("file does not exist or cannot be read")
        return _backend_result(
            text=result.text,
            metadata={
                "runtime": self.runtime_name,
                "path": str(path),
                "truncated": result.truncated,
            },
        )

    async def write(self, session: RuntimeSession, raw_path: Any, content: Any) -> dict[str, Any]:
        path = workspace_path(raw_path)
        text = str(content) if content is not None else ""
        async with session.lock:
            if session.terminal_result is not None or session.container is None:
                raise SweTaskError("SWE session is already finished")
            written = await asyncio.to_thread(
                self.engine.write_file,
                session.container,
                path,
                text,
            )
        return _backend_result(
            text=f"wrote {written} bytes",
            metadata={"runtime": self.runtime_name, "path": str(path), "bytes": written},
        )

    async def submit(self, session: RuntimeSession) -> dict[str, Any]:
        async with session.lock:
            if session.terminal_result is not None:
                return dict(session.terminal_result)
            if session.container is None:
                raise SweInfrastructureError("SWE policy container is missing")
            try:
                patch = await asyncio.to_thread(self.engine.extract_patch, session.container)
                evaluation = await asyncio.to_thread(
                    self.engine.verify,
                    session.plan,
                    patch,
                    session_id=session.session_id,
                )
            finally:
                await asyncio.to_thread(self.engine.remove, session.container)
                session.container = None
            grade = self.harness.grade(
                session.plan,
                evaluation.test_output,
                patch_applied=evaluation.patch_applied,
            )
            result = VerificationResult(
                grade=grade,
                patch_sha256=evaluation.patch_sha256,
                patch_bytes=evaluation.patch_bytes,
                eval_exit_code=evaluation.eval_exit_code,
                eval_timed_out=evaluation.eval_timed_out,
            )
            output = _backend_result(
                text=json.dumps(
                    {
                        "resolved": grade.resolved,
                        "fail_to_pass": grade.fail_to_pass,
                        "pass_to_pass": grade.pass_to_pass,
                    },
                    sort_keys=True,
                ),
                reward=1.0 if grade.resolved else 0.0,
                finished=True,
                metadata={
                    "runtime": self.runtime_name,
                    "harness_version": session.plan.harness_version,
                    "image_id": session.image_id,
                    "resolved": grade.resolved,
                    "patch_applied": grade.patch_applied,
                    "patch_sha256": result.patch_sha256,
                    "patch_bytes": result.patch_bytes,
                    "fail_to_pass": grade.fail_to_pass,
                    "fail_to_pass_passed": grade.fail_to_pass_passed,
                    "fail_to_pass_total": grade.fail_to_pass_total,
                    "pass_to_pass": grade.pass_to_pass,
                    "pass_to_pass_passed": grade.pass_to_pass_passed,
                    "pass_to_pass_total": grade.pass_to_pass_total,
                    "eval_exit_code": result.eval_exit_code,
                    "eval_timed_out": result.eval_timed_out,
                },
            )
            session.terminal_result = output
            return dict(output)

    async def delete(self, session_id: str) -> None:
        async with self.lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            return
        async with session.lock:
            await asyncio.to_thread(self.engine.remove, session.container)
            session.container = None

    async def reap_expired(self) -> int:
        cutoff = time.monotonic() - self.ttl_seconds
        async with self.lock:
            expired = [key for key, item in self.sessions.items() if item.created_at < cutoff]
        for session_id in expired:
            await self.delete(session_id)
        return len(expired)

    async def shutdown(self) -> None:
        async with self.lock:
            session_ids = list(self.sessions)
        for session_id in session_ids:
            await self.delete(session_id)


def _build_engine(config: SweRuntimeConfig, environ: Mapping[str, str]) -> Any:
    """Select the deployment-owned engine without importing Kubernetes eagerly."""

    engine_name = str(environ.get("ORS_SWE_ENGINE", "docker")).strip().casefold()
    if engine_name == "kubernetes":
        from .swe_k8s_runtime import KubernetesSweEngine, KubernetesSweRuntimeConfig

        return KubernetesSweEngine(
            KubernetesSweRuntimeConfig.from_environ(config, environ)
        )
    if engine_name != "docker":
        raise SweInfrastructureError("ORS_SWE_ENGINE must be docker or kubernetes")
    return DockerSweEngine(config)


def create_swe_runtime_app(
    service: SweRuntimeService | None = None,
    *,
    auth_token: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    runtime = os.environ if environ is None else environ
    if service is None:
        config = SweRuntimeConfig.from_environ(runtime)
        harness_family = _text(runtime.get("ORS_SWE_HARNESS_FAMILY") or "swebench")
        harness_root = str(runtime.get("ORS_SWE_HARNESS_ROOT", "")).strip()
        family = harness_family.casefold().replace("-", "")
        if family == "swegym":
            if not harness_root:
                raise SweInfrastructureError(
                    "ORS_SWE_HARNESS_ROOT is required for SWE-Gym runtime"
                )
            harness: SweHarness = SwegymHarness(
                harness_root=harness_root,
                expected_revision=str(
                    runtime.get("ORS_SWE_HARNESS_REVISION", _SWEGYM_REVISION)
                ).strip(),
                image_namespace=str(
                    runtime.get("ORS_SWEGYM_IMAGE_NAMESPACE", "xingyaoww")
                ),
            )
        elif family == "swebench":
            harness = OfficialSwebenchHarness(
                namespace=str(runtime.get("ORS_SWE_IMAGE_NAMESPACE", "swebench")),
                image_tag=str(runtime.get("ORS_SWE_IMAGE_TAG", "latest")),
                harness_root=harness_root or None,
                expected_revision=str(
                    runtime.get("ORS_SWE_HARNESS_REVISION", _SWEBENCH_REVISION)
                ).strip(),
            )
        elif family == "r2e":
            harness = R2eHarness()
        else:
            raise SweInfrastructureError(
                "ORS_SWE_HARNESS_FAMILY must be swebench, swegym, or r2e"
            )
        service = SweRuntimeService(
            _build_engine(config, runtime),
            harness,
            ttl_seconds=config.session_ttl_seconds,
        )
    token = (
        str(auth_token)
        if auth_token is not None
        else str(runtime.get("ORS_SWE_SANDBOX_TOKEN", ""))
    ).strip()
    if _env_bool(runtime, "ORS_SWE_REQUIRE_AUTH", False) and not token:
        raise SweInfrastructureError(
            "ORS_SWE_SANDBOX_TOKEN is required when ORS_SWE_REQUIRE_AUTH=true"
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def reap_loop() -> None:
            interval = min(60, max(1, service.ttl_seconds))
            while True:
                await asyncio.sleep(interval)
                await service.reap_expired()

        reaper = asyncio.create_task(reap_loop())
        try:
            yield
        finally:
            reaper.cancel()
            with suppress(asyncio.CancelledError):
                await reaper
            await service.shutdown()

    app = FastAPI(title="OpenReward SWE runtime", version="1", lifespan=lifespan)

    def check_auth(authorization: str | None) -> None:
        if not token:
            return
        supplied = str(authorization or "")
        expected = f"Bearer {token}"
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    def translate(exc: Exception) -> HTTPException:
        # Keep client responses bounded and credential-safe, but retain the
        # exception class/traceback in the runtime logs for infrastructure
        # diagnosis (image pulls and Pod startup otherwise look identical).
        _LOGGER.exception("SWE runtime request failed (%s)", type(exc).__name__)
        if isinstance(exc, KeyError):
            return HTTPException(status_code=404, detail="sandbox not found")
        if isinstance(exc, SweTaskError):
            return HTTPException(status_code=400, detail=str(exc))
        if isinstance(exc, SweInfrastructureError):
            return HTTPException(status_code=502, detail=str(exc))
        return HTTPException(status_code=500, detail="unexpected SWE runtime failure")

    async def body(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="request body must be JSON") from exc
        if not isinstance(payload, Mapping):
            raise HTTPException(status_code=400, detail="request body must be an object")
        return dict(payload)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok" if await asyncio.to_thread(service.engine.ping) else "degraded",
            "runtime": service.runtime_name,
            "harness_version": service.harness.version,
            "active_sessions": len(service.sessions),
        }

    @app.post("/v1/sandboxes")
    async def create_sandbox(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        payload = await body(request)
        try:
            result = await service.create(
                _text(payload.get("task_id")),
                _mapping(payload.get("task_spec")),
            )
        except Exception as exc:
            raise translate(exc) from exc
        return JSONResponse(result)

    async def session_for(sandbox_id: str) -> RuntimeSession:
        try:
            return await service.get(sandbox_id)
        except Exception as exc:
            raise translate(exc) from exc

    @app.post("/v1/sandboxes/{sandbox_id}/exec")
    async def execute(
        sandbox_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        payload = await body(request)
        incoming = _mapping(payload.get("input"))
        try:
            result = await service.bash(
                await session_for(sandbox_id),
                str(incoming.get("command") or ""),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise translate(exc) from exc
        return JSONResponse(result)

    @app.post("/v1/sandboxes/{sandbox_id}/files/read")
    async def read_file(
        sandbox_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        payload = await body(request)
        incoming = _mapping(payload.get("input"))
        try:
            result = await service.read(
                await session_for(sandbox_id),
                incoming.get("path"),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise translate(exc) from exc
        return JSONResponse(result)

    @app.post("/v1/sandboxes/{sandbox_id}/files/write")
    async def write_file(
        sandbox_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        payload = await body(request)
        incoming = _mapping(payload.get("input"))
        try:
            result = await service.write(
                await session_for(sandbox_id),
                incoming.get("path"),
                incoming.get("content"),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise translate(exc) from exc
        return JSONResponse(result)

    @app.post("/v1/sandboxes/{sandbox_id}/verify")
    async def verify(
        sandbox_id: str,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        try:
            result = await service.submit(await session_for(sandbox_id))
        except HTTPException:
            raise
        except Exception as exc:
            raise translate(exc) from exc
        return JSONResponse(result)

    @app.delete("/v1/sandboxes/{sandbox_id}")
    async def delete_sandbox(
        sandbox_id: str,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        await service.delete(sandbox_id)
        return JSONResponse({"deleted": True})

    return app

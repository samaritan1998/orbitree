"""Isolated, task-bundle-aware PowerPoint runtime for OpenReward.

The policy works in one disposable container with ``/inputs`` mounted read-only
and ``/workspace`` writable.  Rendering happens in a second clean container so
the policy cannot forge slide images or replace verifier binaries.  Structural
checks are local and deterministic; content grounding and visual quality are
delegated to a configured rubric service.  A valid deck without a reachable
rubric is terminal but deliberately unusable for training (``reward=null``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import secrets
import tarfile
import time
import zipfile
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from xml.etree import ElementTree

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,239}$")
_IMAGE_REF = re.compile(r"^[A-Za-z0-9._:/-]+$")
_MEMORY_LIMIT = re.compile(r"^[1-9][0-9]*(?:[bkmg])?$", re.IGNORECASE)
_SLIDE_NAME = re.compile(r"^ppt/slides/slide([1-9][0-9]*)\.xml$")
_UNBOUNDED_ROOT_SCAN = re.compile(
    r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:find|du)\s+/(?:\s|$)",
    re.IGNORECASE,
)
_CLEAN_SCRIPT_EXECUTION = re.compile(
    r"(?:^|[;&|]\s*)(?:python(?:3(?:\.\d+)*)?\s+)?\S*/clean\.py(?:\s|$)",
    re.IGNORECASE,
)
_WORKSPACE = PurePosixPath("/workspace")
_INPUTS = PurePosixPath("/inputs")
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class PptRuntimeError(RuntimeError):
    """Base error for the PPT runtime."""


class PptTaskError(PptRuntimeError):
    """A request or task bundle is invalid."""


class PptInfrastructureError(PptRuntimeError):
    """Sandbox, rendering, storage, or the rubric service failed."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _env_int(environ: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(str(environ.get(name, default)))
    except (TypeError, ValueError) as exc:
        raise PptInfrastructureError(f"{name} must be an integer") from exc
    if value <= 0:
        raise PptInfrastructureError(f"{name} must be positive")
    return value


def _env_float(environ: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(str(environ.get(name, default)))
    except (TypeError, ValueError) as exc:
        raise PptInfrastructureError(f"{name} must be numeric") from exc
    if value <= 0:
        raise PptInfrastructureError(f"{name} must be positive")
    return value


def _env_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = str(environ.get(name, str(default))).strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise PptInfrastructureError(f"{name} must be boolean")


def _absolute_directory(environ: Mapping[str, str], name: str) -> Path:
    raw = _text(environ.get(name))
    if not raw:
        raise PptInfrastructureError(f"{name} is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise PptInfrastructureError(f"{name} must be an absolute path")
    return path.resolve()


def presentation_path(value: Any, *, allow_inputs: bool = False) -> PurePosixPath:
    """Resolve a container path without allowing traversal or host paths."""

    raw = _text(value)
    if not raw:
        raise PptTaskError("presentation path is required")
    path = PurePosixPath(raw)
    if not path.is_absolute():
        path = _WORKSPACE / path
    roots = (_WORKSPACE, _INPUTS) if allow_inputs else (_WORKSPACE,)
    if ".." in path.parts or path in roots:
        raise PptTaskError("presentation path must name a file")
    if not any(path.is_relative_to(root) for root in roots):
        raise PptTaskError("presentation path is outside the task sandbox")
    return path


@dataclass(frozen=True)
class PptTask:
    task_id: str
    query: str
    output_path: PurePosixPath
    assets: tuple[PurePosixPath, ...]
    min_slides: int
    max_slides: int
    requires_grounding: bool
    requires_visual_qa: bool

    @classmethod
    def from_spec(cls, task_spec: Mapping[str, Any]) -> PptTask:
        task_id = _text(task_spec.get("task_id"))
        query = _text(task_spec.get("query") or task_spec.get("prompt"))
        family = _text(task_spec.get("benchmark_family")).casefold()
        if not _TASK_ID.fullmatch(task_id):
            raise PptTaskError("PPT task_id is invalid")
        if not query:
            raise PptTaskError("PPT task query is empty")
        if family not in {"ppt_online", "ppt", "presentation"}:
            raise PptTaskError("this runtime accepts benchmark_family=ppt_online only")
        output_path = presentation_path(task_spec.get("output_path") or "/workspace/slides.pptx")
        if output_path.suffix.casefold() != ".pptx":
            raise PptTaskError("PPT output_path must end in .pptx")

        assets: list[PurePosixPath] = []
        raw_assets = task_spec.get("assets") or []
        if not isinstance(raw_assets, Sequence) or isinstance(raw_assets, (str, bytes)):
            raise PptTaskError("PPT assets must be an array")
        for item in raw_assets:
            raw_path = item.get("path") if isinstance(item, Mapping) else item
            path = presentation_path(raw_path, allow_inputs=True)
            if not path.is_relative_to(_INPUTS):
                raise PptTaskError("PPT reference assets must be below /inputs")
            assets.append(path)

        verifier = _mapping(task_spec.get("verifier"))
        try:
            min_slides = int(verifier.get("min_slides", 1))
            max_slides = int(verifier.get("max_slides", 100))
        except (TypeError, ValueError) as exc:
            raise PptTaskError("PPT slide limits must be integers") from exc
        if min_slides < 1 or max_slides < min_slides:
            raise PptTaskError("PPT slide limits are invalid")
        if verifier.get("editable_pptx_required") is not True:
            raise PptTaskError("PPT task must require an editable artifact")
        if verifier.get("render_required") is not True:
            raise PptTaskError("PPT task must require clean rendering")
        return cls(
            task_id=task_id,
            query=query,
            output_path=output_path,
            assets=tuple(dict.fromkeys(assets)),
            min_slides=min_slides,
            max_slides=max_slides,
            requires_grounding=bool(verifier.get("content_grounding_required")),
            requires_visual_qa=bool(verifier.get("visual_qa_required")),
        )

    def resolve_inputs(self, bundle_root: Path) -> Path:
        inputs = (bundle_root / self.task_id / "inputs").resolve()
        expected_parent = (bundle_root / self.task_id).resolve()
        if not inputs.is_relative_to(expected_parent) or not inputs.is_dir():
            raise PptTaskError("PPT task bundle is missing its inputs directory")
        missing = [
            str(path)
            for path in self.assets
            if not (inputs / path.relative_to(_INPUTS)).resolve().is_file()
            or not (inputs / path.relative_to(_INPUTS)).resolve().is_relative_to(inputs)
        ]
        if missing:
            raise PptTaskError("PPT task bundle is missing declared reference assets")
        return inputs


@dataclass(frozen=True)
class PptInspection:
    slides: int
    text_runs: int
    characters: int
    bytes: int
    sha256: str
    texts: tuple[str, ...]

    @property
    def editable(self) -> bool:
        return self.text_runs > 0


def inspect_pptx(raw: bytes, *, max_uncompressed_bytes: int = 200_000_000) -> PptInspection:
    """Inspect editable OOXML content with zip-bomb and path checks."""

    if not raw:
        raise PptTaskError("submitted PPTX is empty")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (OSError, zipfile.BadZipFile) as exc:
        raise PptTaskError("submitted artifact is not a valid PPTX archive") from exc
    with archive:
        names = archive.namelist()
        if "[Content_Types].xml" not in names or "ppt/presentation.xml" not in names:
            raise PptTaskError("submitted archive is missing PPTX package parts")
        total = 0
        slide_parts: list[tuple[int, str]] = []
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            if path.is_absolute() or ".." in path.parts:
                raise PptTaskError("submitted PPTX contains an unsafe package path")
            total += item.file_size
            if total > max_uncompressed_bytes:
                raise PptTaskError("submitted PPTX expands beyond the safety limit")
            match = _SLIDE_NAME.fullmatch(item.filename)
            if match:
                slide_parts.append((int(match.group(1)), item.filename))
        if not slide_parts:
            raise PptTaskError("submitted PPTX contains no slides")
        slide_parts.sort()
        texts: list[str] = []
        text_runs = 0
        for _, name in slide_parts:
            try:
                root = ElementTree.fromstring(archive.read(name))
            except (ElementTree.ParseError, KeyError, zipfile.BadZipFile) as exc:
                raise PptTaskError("submitted PPTX contains invalid slide XML") from exc
            for element in root.iter():
                if element.tag.endswith("}t") and element.text:
                    value = element.text.strip()
                    if value:
                        texts.append(value)
                        text_runs += 1
    return PptInspection(
        slides=len(slide_parts),
        text_runs=text_runs,
        characters=sum(len(item) for item in texts),
        bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        texts=tuple(texts),
    )


@dataclass(frozen=True)
class PptRuntimeConfig:
    policy_image: str
    verifier_image: str
    bundle_root: Path
    artifact_root: Path
    skill_root: Path | None = None
    skill_in_image: bool = False
    cpus: float = 2.0
    memory: str = "8g"
    pids_limit: int = 512
    command_seconds: int = 180
    render_seconds: int = 300
    max_command_output_bytes: int = 512_000
    max_text_file_bytes: int = 4_000_000
    max_artifact_bytes: int = 100_000_000
    max_render_images: int = 40
    session_ttl_seconds: int = 3600

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> PptRuntimeConfig:
        runtime = os.environ if environ is None else environ
        policy_image = _text(runtime.get("ORS_PPT_POLICY_IMAGE"))
        verifier_image = _text(runtime.get("ORS_PPT_VERIFIER_IMAGE")) or policy_image
        if not policy_image or not _IMAGE_REF.fullmatch(policy_image):
            raise PptInfrastructureError("ORS_PPT_POLICY_IMAGE is missing or invalid")
        if not verifier_image or not _IMAGE_REF.fullmatch(verifier_image):
            raise PptInfrastructureError("ORS_PPT_VERIFIER_IMAGE is invalid")
        memory = _text(runtime.get("ORS_PPT_MEMORY") or "8g").casefold()
        if not _MEMORY_LIMIT.fullmatch(memory):
            raise PptInfrastructureError("ORS_PPT_MEMORY is invalid")
        skill_value = _text(runtime.get("ORS_PPT_SKILL_ROOT"))
        skill_root: Path | None = None
        if skill_value:
            candidate = Path(skill_value).expanduser()
            if not candidate.is_absolute() or not candidate.resolve().is_dir():
                raise PptInfrastructureError(
                    "ORS_PPT_SKILL_ROOT must be an existing absolute directory"
                )
            skill_root = candidate.resolve()
        skill_in_image = _env_bool(runtime, "ORS_PPT_SKILL_IN_IMAGE", False)
        if skill_root is None and not skill_in_image:
            raise PptInfrastructureError(
                "configure ORS_PPT_SKILL_ROOT or set ORS_PPT_SKILL_IN_IMAGE=true"
            )
        return cls(
            policy_image=policy_image,
            verifier_image=verifier_image,
            bundle_root=_absolute_directory(runtime, "ORS_PPT_TASK_BUNDLE_ROOT"),
            artifact_root=_absolute_directory(runtime, "ORS_PPT_ARTIFACT_ROOT"),
            skill_root=skill_root,
            skill_in_image=skill_in_image,
            cpus=_env_float(runtime, "ORS_PPT_CPUS", 2.0),
            memory=memory,
            pids_limit=_env_int(runtime, "ORS_PPT_PIDS_LIMIT", 512),
            command_seconds=_env_int(runtime, "ORS_PPT_COMMAND_SECONDS", 180),
            render_seconds=_env_int(runtime, "ORS_PPT_RENDER_SECONDS", 300),
            max_command_output_bytes=_env_int(
                runtime, "ORS_PPT_MAX_COMMAND_OUTPUT_BYTES", 512_000
            ),
            max_text_file_bytes=_env_int(
                runtime, "ORS_PPT_MAX_TEXT_FILE_BYTES", 4_000_000
            ),
            max_artifact_bytes=_env_int(
                runtime, "ORS_PPT_MAX_ARTIFACT_BYTES", 100_000_000
            ),
            max_render_images=_env_int(runtime, "ORS_PPT_MAX_RENDER_IMAGES", 40),
            session_ttl_seconds=_env_int(runtime, "ORS_PPT_SESSION_TTL_SECONDS", 3600),
        )


@dataclass(frozen=True)
class CommandResult:
    text: str
    exit_code: int
    timed_out: bool = False
    truncated: bool = False


class PptEngine(Protocol):
    def ping(self) -> bool: ...

    def image_id(self, image_ref: str) -> str: ...

    def start(self, task: PptTask, inputs: Path, *, session_id: str) -> Any: ...

    def remove(self, container: Any | None) -> None: ...

    def exec_shell(self, container: Any, command: str) -> CommandResult: ...

    def read_file(self, container: Any, path: PurePosixPath) -> bytes: ...

    def write_file(self, container: Any, path: PurePosixPath, raw: bytes) -> int: ...

    def render(self, raw: bytes, *, session_id: str) -> list[bytes]: ...


class DockerPptEngine:
    """Launch policy and clean-render containers without network or Docker mounts."""

    def __init__(self, config: PptRuntimeConfig, *, client: Any | None = None) -> None:
        if client is None:
            try:
                import docker
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise PptInfrastructureError(
                    "install the ppt-runtime extra to launch PPT containers"
                ) from exc
            try:
                client = docker.from_env()
            except Exception as exc:  # pragma: no cover - depends on daemon
                raise PptInfrastructureError(
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
        if image_ref == "policy":
            image_ref = self.config.policy_image
        try:
            return str(self.client.images.get(image_ref).id)
        except Exception as exc:
            raise PptInfrastructureError(
                f"cannot inspect PPT image: {type(exc).__name__}"
            ) from exc

    def _container(self, image: str, *, name: str, volumes: Mapping[str, Any] | None = None) -> Any:
        try:
            self.client.images.get(image)
            return self.client.containers.run(
                image,
                command=["-lc", "mkdir -p /workspace /tmp; while :; do sleep 3600; done"],
                entrypoint="/bin/bash",
                detach=True,
                name=name,
                working_dir=str(_WORKSPACE),
                network_disabled=True,
                nano_cpus=int(self.config.cpus * 1_000_000_000),
                mem_limit=self.config.memory,
                pids_limit=self.config.pids_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                init=True,
                tmpfs={"/tmp": "rw,noexec,nosuid,size=1g"},
                volumes=dict(volumes or {}),
                labels={"research-ops.runtime": "openreward-ppt"},
            )
        except Exception as exc:
            raise PptInfrastructureError(
                f"cannot start PPT container: {type(exc).__name__}"
            ) from exc

    def start(self, task: PptTask, inputs: Path, *, session_id: str) -> Any:
        volumes: dict[str, Any] = {
            str(inputs): {"bind": str(_INPUTS), "mode": "ro"}
        }
        if self.config.skill_root is not None:
            volumes[str(self.config.skill_root)] = {
                "bind": "/home/pptuser/.claude/skills/pptx",
                "mode": "ro",
            }
        container = self._container(
            self.config.policy_image,
            name=f"ors-ppt-policy-{session_id[-20:]}",
            volumes=volumes,
        )
        container.labels["research-ops.task-sha256"] = hashlib.sha256(
            task.task_id.encode("utf-8")
        ).hexdigest()
        return container

    def remove(self, container: Any | None) -> None:
        if container is not None:
            with suppress(Exception):
                container.remove(force=True)

    def _exec_raw(self, container: Any, command: Sequence[str]) -> CommandResult:
        try:
            result = container.exec_run(list(command), workdir=str(_WORKSPACE), demux=False)
        except Exception as exc:
            raise PptInfrastructureError(
                f"Docker exec failed: {type(exc).__name__}"
            ) from exc
        raw = result.output or b""
        if isinstance(raw, tuple):
            raw = b"".join(item or b"" for item in raw)
        text = raw if isinstance(raw, str) else bytes(raw).decode("utf-8", errors="replace")
        return CommandResult(text, int(result.exit_code))

    def exec_shell(self, container: Any, command: str) -> CommandResult:
        output = f"/tmp/ors-ppt-output-{secrets.token_hex(8)}"
        wrapper = r'''
set +e
output="$1"; seconds="$2"; limit="$3"; command="$4"
timeout --signal=KILL "${seconds}s" /bin/bash -lc "$command" >"$output" 2>&1
rc=$?; bytes=$(wc -c <"$output"); head -c "$limit" "$output"; rm -f "$output"
if [ "$bytes" -gt "$limit" ]; then printf '\n[OpenReward output truncated: %s bytes]\n' "$bytes"; fi
exit "$rc"
'''
        result = self._exec_raw(
            container,
            [
                "/bin/bash",
                "-c",
                wrapper,
                "ors",
                output,
                str(self.config.command_seconds),
                str(self.config.max_command_output_bytes),
                command,
            ],
        )
        return CommandResult(
            result.text,
            result.exit_code,
            timed_out=result.exit_code in {124, 137},
            truncated="[OpenReward output truncated:" in result.text,
        )

    def read_file(self, container: Any, path: PurePosixPath) -> bytes:
        try:
            stream, _ = container.get_archive(str(path))
            archive = io.BytesIO(b"".join(stream))
            with tarfile.open(fileobj=archive, mode="r:*") as bundle:
                members = [item for item in bundle.getmembers() if item.isfile()]
                if len(members) != 1:
                    raise PptTaskError("requested path is not one regular file")
                member = members[0]
                if member.size > self.config.max_artifact_bytes:
                    raise PptTaskError("requested file exceeds the PPT artifact limit")
                handle = bundle.extractfile(member)
                if handle is None:
                    raise PptInfrastructureError("Docker archive omitted file content")
                return handle.read(self.config.max_artifact_bytes + 1)
        except PptRuntimeError:
            raise
        except Exception as exc:
            raise PptTaskError("requested file does not exist or cannot be read") from exc

    def write_file(self, container: Any, path: PurePosixPath, raw: bytes) -> int:
        if len(raw) > self.config.max_text_file_bytes:
            raise PptTaskError("text file exceeds ORS_PPT_MAX_TEXT_FILE_BYTES")
        mkdir = self._exec_raw(container, ["mkdir", "-p", "--", str(path.parent)])
        if mkdir.exit_code != 0:
            raise PptInfrastructureError("cannot create PPT workspace directory")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as bundle:
            info = tarfile.TarInfo(path.name)
            info.size = len(raw)
            # Docker extracts archives as root. Keep sidecar-created text
            # sources writable by whichever non-root user the policy image uses.
            info.mode = 0o666
            bundle.addfile(info, io.BytesIO(raw))
        try:
            if not container.put_archive(str(path.parent), archive.getvalue()):
                raise PptInfrastructureError("Docker rejected PPT file archive")
        except PptRuntimeError:
            raise
        except Exception as exc:
            raise PptInfrastructureError(
                f"cannot write PPT workspace file: {type(exc).__name__}"
            ) from exc
        return len(raw)

    def _put_artifact(self, container: Any, path: PurePosixPath, raw: bytes) -> None:
        if len(raw) > self.config.max_artifact_bytes:
            raise PptTaskError("PPT artifact exceeds ORS_PPT_MAX_ARTIFACT_BYTES")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as bundle:
            info = tarfile.TarInfo(path.name)
            info.size = len(raw)
            info.mode = 0o644
            bundle.addfile(info, io.BytesIO(raw))
        try:
            if not container.put_archive(str(path.parent), archive.getvalue()):
                raise PptInfrastructureError("Docker rejected PPT verifier input")
        except PptRuntimeError:
            raise
        except Exception as exc:
            raise PptInfrastructureError(
                f"cannot write PPT verifier input: {type(exc).__name__}"
            ) from exc

    def render(self, raw: bytes, *, session_id: str) -> list[bytes]:
        verifier = self._container(
            self.config.verifier_image,
            name=f"ors-ppt-render-{session_id[-20:]}",
        )
        try:
            self._put_artifact(verifier, PurePosixPath("/workspace/slides.pptx"), raw)
            command = (
                "set -e; mkdir -p /workspace/render; "
                "/usr/bin/soffice --headless --convert-to pdf "
                "--outdir /workspace/render /workspace/slides.pptx >/tmp/soffice.log 2>&1; "
                "/usr/bin/pdftoppm -png -r 120 /workspace/render/slides.pdf "
                "/workspace/render/slide; "
                "find /workspace/render -maxdepth 1 -type f -name 'slide-*.png' -printf '%f\\n' "
                "| sort -V"
            )
            result = self._exec_raw(
                verifier,
                [
                    "/usr/bin/timeout",
                    f"{self.config.render_seconds}s",
                    "/bin/bash",
                    "-lc",
                    command,
                ],
            )
            if result.exit_code in {126, 127}:
                raise PptInfrastructureError("PPT verifier image is missing render binaries")
            if result.exit_code != 0:
                raise PptTaskError("LibreOffice could not render the submitted PPTX")
            names = [line.strip() for line in result.text.splitlines() if line.strip()]
            if not names or len(names) > self.config.max_render_images:
                raise PptTaskError("PPT render count is empty or exceeds the configured limit")
            return [
                self.read_file(verifier, PurePosixPath(f"/workspace/render/{name}"))
                for name in names
            ]
        finally:
            self.remove(verifier)


@dataclass(frozen=True)
class RubricResult:
    reward: float
    rubric: dict[str, Any]
    grader_id: str | None = None
    usable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class PptRubric(Protocol):
    async def evaluate(
        self,
        *,
        task: PptTask,
        task_spec: Mapping[str, Any],
        artifact_path: Path,
        render_paths: Sequence[Path],
        inputs_path: Path,
        inspection: PptInspection,
    ) -> RubricResult: ...


class HttpPptRubric:
    """Shared-path JSON bridge for an internal PPT content/visual grader."""

    def __init__(
        self,
        base_url: str,
        *,
        token_env: str = "ORS_PPT_RUBRIC_TOKEN",
        environ: Mapping[str, str] | None = None,
        timeout_seconds: float = 900.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        value = _text(base_url).rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise PptInfrastructureError("ORS_PPT_RUBRIC_URL must use HTTP or HTTPS")
        self.base_url = value
        self.token_env = token_env
        self.environ = os.environ if environ is None else environ
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def evaluate(
        self,
        *,
        task: PptTask,
        task_spec: Mapping[str, Any],
        artifact_path: Path,
        render_paths: Sequence[Path],
        inputs_path: Path,
        inspection: PptInspection,
    ) -> RubricResult:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        token = _text(self.environ.get(self.token_env))
        if token:
            headers["Authorization"] = (
                token if token.casefold().startswith("bearer ") else f"Bearer {token}"
            )
        body = {
            "task_id": task.task_id,
            "query": task.query,
            "task_spec": dict(task_spec),
            "artifact_path": str(artifact_path),
            "render_paths": [str(item) for item in render_paths],
            "inputs_path": str(inputs_path),
            "inspection": {
                "slides": inspection.slides,
                "text_runs": inspection.text_runs,
                "characters": inspection.characters,
                "sha256": inspection.sha256,
            },
            "requirements": {
                "content_grounding": task.requires_grounding,
                "visual_qa": task.requires_visual_qa,
                "editable": True,
            },
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                trust_env=False,
                transport=self.transport,
            ) as client:
                response = await client.post("/v1/evaluate", headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise PptInfrastructureError(
                f"PPT rubric request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise PptInfrastructureError(
                f"PPT rubric service returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise PptInfrastructureError("PPT rubric service returned non-JSON") from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("usable"), bool):
            raise PptInfrastructureError("PPT rubric service omitted boolean usable")
        try:
            reward = float(payload["reward"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PptInfrastructureError("PPT rubric service omitted numeric reward") from exc
        if not 0.0 <= reward <= 1.0:
            raise PptInfrastructureError("PPT rubric reward must be between 0 and 1")
        rubric = _mapping(payload.get("rubric"))
        required = {"content_grounding", "structure", "visual_quality", "editability"}
        if not required.issubset(rubric):
            raise PptInfrastructureError("PPT rubric service omitted required dimensions")
        normalized_rubric: dict[str, float] = {}
        for name in sorted(required):
            try:
                score = float(rubric[name])
            except (TypeError, ValueError) as exc:
                raise PptInfrastructureError(
                    "PPT rubric dimensions must be numeric"
                ) from exc
            if not 0.0 <= score <= 1.0:
                raise PptInfrastructureError(
                    "PPT rubric dimensions must be between 0 and 1"
                )
            normalized_rubric[name] = score
        return RubricResult(
            reward,
            normalized_rubric,
            _text(payload.get("grader_id")) or None,
            usable=bool(payload["usable"]),
            metadata=_mapping(payload.get("metadata")),
        )


@dataclass
class PptSession:
    session_id: str
    task: PptTask
    task_spec: dict[str, Any]
    inputs_path: Path
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
    images: Sequence[bytes] = (),
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    blocks.extend(
        {
            "type": "image",
            "data": base64.b64encode(raw).decode("ascii"),
            "mimeType": "image/png",
        }
        for raw in images
    )
    return {
        "output": {
            "blocks": blocks,
            "reward": reward,
            "finished": finished,
            "metadata": dict(metadata or {}),
        }
    }


class PptRuntimeService:
    def __init__(
        self,
        engine: PptEngine,
        *,
        bundle_root: Path,
        artifact_root: Path,
        rubric: PptRubric | None,
        ttl_seconds: int,
    ) -> None:
        self.engine = engine
        self.bundle_root = bundle_root.resolve()
        self.artifact_root = artifact_root.resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.rubric = rubric
        self.ttl_seconds = ttl_seconds
        self.sessions: dict[str, PptSession] = {}
        self.lock = asyncio.Lock()

    async def create(self, task_id: str, task_spec: Mapping[str, Any]) -> dict[str, Any]:
        await self.reap_expired()
        task = PptTask.from_spec(task_spec)
        if task_id and task_id != task.task_id:
            raise PptTaskError("request task_id does not match task_spec.task_id")
        inputs = task.resolve_inputs(self.bundle_root)
        session_id = f"ors-ppt-{secrets.token_hex(12)}"
        container = await asyncio.to_thread(
            self.engine.start,
            task,
            inputs,
            session_id=session_id,
        )
        try:
            image_id = await asyncio.to_thread(self.engine.image_id, "policy")
        except Exception:
            await asyncio.to_thread(self.engine.remove, container)
            raise
        session = PptSession(
            session_id,
            task,
            dict(task_spec),
            inputs,
            image_id,
            container,
            time.monotonic(),
        )
        async with self.lock:
            self.sessions[session_id] = session
        return {
            "session_id": session_id,
            "runtime": "ppt-sandbox-rubric",
            "image_id": image_id,
            "asset_count": len(task.assets),
            "rubric_ready": self.rubric is not None,
        }

    async def get(self, session_id: str) -> PptSession:
        async with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    @staticmethod
    def _ensure_active(session: PptSession) -> Any:
        if session.terminal_result is not None or session.container is None:
            raise PptTaskError("PPT session is already finished")
        return session.container

    async def bash(self, session: PptSession, command: str) -> dict[str, Any]:
        if not command.strip():
            raise PptTaskError("command is required")
        if _UNBOUNDED_ROOT_SCAN.search(command):
            raise PptTaskError(
                "unbounded filesystem-root scans are not allowed; inspect only "
                "/workspace, /inputs, or /opt/claude-skills/pptx"
            )
        axes = _mapping(session.task_spec.get("task_axes"))
        if (
            _text(axes.get("interaction_mode")).casefold() == "edit"
            and _CLEAN_SCRIPT_EXECUTION.search(command)
        ):
            raise PptTaskError(
                "clean.py is disabled for existing-deck edit tasks because it can "
                "delete valid slide parts; save directly with python-pptx or repack "
                "the bounded unpacked tree without a cleanup pass"
            )
        async with session.lock:
            result = await asyncio.to_thread(
                self.engine.exec_shell,
                self._ensure_active(session),
                command,
            )
        return _backend_result(
            text=result.text,
            metadata={
                "runtime": "ppt-sandbox-rubric",
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "truncated": result.truncated,
            },
        )

    async def read(self, session: PptSession, raw_path: Any, *, inspect: bool) -> dict[str, Any]:
        path = presentation_path(raw_path, allow_inputs=True)
        async with session.lock:
            raw = await asyncio.to_thread(
                self.engine.read_file,
                self._ensure_active(session),
                path,
            )
        if inspect or path.suffix.casefold() == ".pptx":
            details = inspect_pptx(raw)
            text = json.dumps(
                {
                    "path": str(path),
                    "slides": details.slides,
                    "text_runs": details.text_runs,
                    "characters": details.characters,
                    "sha256": details.sha256,
                    "texts": list(details.texts),
                },
                ensure_ascii=False,
            )
        else:
            text = raw.decode("utf-8", errors="replace")
        return _backend_result(
            text=text,
            metadata={"runtime": "ppt-sandbox-rubric", "path": str(path), "bytes": len(raw)},
        )

    async def write(self, session: PptSession, raw_path: Any, content: Any) -> dict[str, Any]:
        path = presentation_path(raw_path)
        raw = str(content if content is not None else "").encode("utf-8")
        async with session.lock:
            written = await asyncio.to_thread(
                self.engine.write_file,
                self._ensure_active(session),
                path,
                raw,
            )
        return _backend_result(
            text=f"wrote {written} bytes",
            metadata={"runtime": "ppt-sandbox-rubric", "path": str(path), "bytes": written},
        )

    async def edit(
        self,
        session: PptSession,
        raw_path: Any,
        old_text: Any,
        new_text: Any,
    ) -> dict[str, Any]:
        path = presentation_path(raw_path)
        old = str(old_text or "")
        if not old:
            raise PptTaskError("old_text is required")
        async with session.lock:
            container = self._ensure_active(session)
            raw = await asyncio.to_thread(self.engine.read_file, container, path)
            content = raw.decode("utf-8")
            if content.count(old) != 1:
                raise PptTaskError("old_text must occur exactly once")
            updated = content.replace(old, str(new_text if new_text is not None else ""))
            written = await asyncio.to_thread(
                self.engine.write_file,
                container,
                path,
                updated.encode("utf-8"),
            )
        return _backend_result(
            text=f"edited {written} bytes",
            metadata={"runtime": "ppt-sandbox-rubric", "path": str(path), "bytes": written},
        )

    async def render(self, session: PptSession, raw_path: Any) -> dict[str, Any]:
        path = presentation_path(raw_path)
        async with session.lock:
            raw = await asyncio.to_thread(
                self.engine.read_file,
                self._ensure_active(session),
                path,
            )
            inspection = inspect_pptx(raw)
            images = await asyncio.to_thread(
                self.engine.render,
                raw,
                session_id=session.session_id,
            )
        if len(images) != inspection.slides:
            raise PptTaskError("clean renderer did not produce exactly one image per slide")
        return _backend_result(
            text=f"rendered {len(images)} slides in a clean verifier container",
            images=images,
            metadata={
                "runtime": "ppt-sandbox-rubric",
                "slides": inspection.slides,
                "artifact_sha256": inspection.sha256,
                "clean_renderer": True,
            },
        )

    def _artifact_directory(self, session: PptSession) -> Path:
        task_hash = hashlib.sha256(session.task.task_id.encode("utf-8")).hexdigest()[:20]
        path = (self.artifact_root / task_hash / session.session_id).resolve()
        if not path.is_relative_to(self.artifact_root):
            raise PptInfrastructureError("PPT artifact path escaped configured root")
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def submit(self, session: PptSession, raw_path: Any) -> dict[str, Any]:
        path = presentation_path(raw_path or session.task.output_path)
        if path != session.task.output_path:
            raise PptTaskError("submitted path does not match task output_path")
        async with session.lock:
            if session.terminal_result is not None:
                return dict(session.terminal_result)
            container = self._ensure_active(session)
            try:
                raw = await asyncio.to_thread(self.engine.read_file, container, path)
                inspection = inspect_pptx(raw)
                if not session.task.min_slides <= inspection.slides <= session.task.max_slides:
                    raise PptTaskError("submitted PPTX violates the required slide range")
                if not inspection.editable:
                    raise PptTaskError("submitted PPTX has no editable text content")
                images = await asyncio.to_thread(
                    self.engine.render,
                    raw,
                    session_id=session.session_id,
                )
                if len(images) != inspection.slides:
                    raise PptTaskError(
                        "clean renderer did not produce exactly one image per slide"
                    )
            except PptTaskError as exc:
                # Keep a bounded copy of failed submissions for post-run
                # diagnosis.  The session container is removed below, so
                # without this copy a structural/render failure would leave
                # only a scalar zero reward and no way to inspect the OOXML
                # that caused it.  This does not make the artifact usable or
                # alter its reward; it is explicitly diagnostic evidence.
                try:
                    diagnostic_destination = self._artifact_directory(session)
                    diagnostic_path = diagnostic_destination / "invalid-slides.pptx"
                    diagnostic_path.write_bytes(raw)
                except Exception:
                    diagnostic_path = None
                output = _backend_result(
                    text=str(exc),
                    reward=0.0,
                    finished=True,
                    metadata={
                        "runtime": "ppt-sandbox-rubric",
                        "usable": True,
                        "reward_source": "deterministic-invalid-artifact",
                        "structural_pass": False,
                        **(
                            {
                                "diagnostic_artifact_id": diagnostic_path.relative_to(
                                    self.artifact_root
                                ).as_posix()
                            }
                            if diagnostic_path is not None
                            else {}
                        ),
                    },
                )
                session.terminal_result = output
                await asyncio.to_thread(self.engine.remove, session.container)
                session.container = None
                return dict(output)

            destination = self._artifact_directory(session)
            artifact_path = destination / "slides.pptx"
            artifact_path.write_bytes(raw)
            render_paths: list[Path] = []
            for index, image in enumerate(images, start=1):
                image_path = destination / f"slide-{index:03d}.png"
                image_path.write_bytes(image)
                render_paths.append(image_path)
            metadata: dict[str, Any] = {
                "runtime": "ppt-sandbox-rubric",
                "usable": False,
                "reward_source": "rubric-unavailable",
                "structural_pass": True,
                "clean_renderer": True,
                "slides": inspection.slides,
                "text_runs": inspection.text_runs,
                "characters": inspection.characters,
                "artifact_sha256": inspection.sha256,
                "artifact_id": destination.relative_to(self.artifact_root).as_posix(),
            }
            reward: float | None = None
            text = "PPT structure and rendering passed; rubric score is unavailable"
            if self.rubric is not None:
                try:
                    grade = await self.rubric.evaluate(
                        task=session.task,
                        task_spec=session.task_spec,
                        artifact_path=artifact_path,
                        render_paths=render_paths,
                        inputs_path=session.inputs_path,
                        inspection=inspection,
                    )
                except PptInfrastructureError as exc:
                    metadata["rubric_error"] = type(exc).__name__
                else:
                    metadata.update(
                        {
                            "rubric": grade.rubric,
                            **{
                                f"rubric_{name}": score
                                for name, score in grade.rubric.items()
                            },
                            "grader_id": grade.grader_id,
                            "grader_metadata": grade.metadata,
                        }
                    )
                    if grade.usable:
                        reward = grade.reward
                        metadata.update(
                            {
                                "usable": True,
                                "reward_source": "ppt-rubric-service",
                            }
                        )
                        text = (
                            "PPT structure, rendering, grounding and visual rubric completed"
                        )
                    else:
                        metadata.update(
                            {
                                "usable": False,
                                "reward_source": "ppt-rubric-unusable",
                                "diagnostic_reward": grade.reward,
                            }
                        )
                        text = "PPT rubric completed but is not usable for training"
            output = _backend_result(
                text=text,
                reward=reward,
                finished=True,
                metadata=metadata,
            )
            session.terminal_result = output
            await asyncio.to_thread(self.engine.remove, session.container)
            session.container = None
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


def create_ppt_runtime_app(
    service: PptRuntimeService | None = None,
    *,
    auth_token: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    runtime = os.environ if environ is None else environ
    if service is None:
        config = PptRuntimeConfig.from_environ(runtime)
        rubric_url = _text(runtime.get("ORS_PPT_RUBRIC_URL"))
        rubric = (
            HttpPptRubric(
                rubric_url,
                environ=runtime,
                timeout_seconds=_env_int(runtime, "ORS_PPT_RUBRIC_TIMEOUT_SECONDS", 5400),
            )
            if rubric_url
            else None
        )
        engine_name = _text(runtime.get("ORS_PPT_ENGINE") or "docker").casefold()
        if engine_name == "docker":
            engine: PptEngine = DockerPptEngine(config)
        elif engine_name == "kubernetes":
            from .ppt_k8s_runtime import KubernetesPptEngine, KubernetesPptRuntimeConfig

            engine = KubernetesPptEngine(
                KubernetesPptRuntimeConfig.from_environ(config, runtime)
            )
        else:
            raise PptInfrastructureError("ORS_PPT_ENGINE must be docker or kubernetes")
        service = PptRuntimeService(
            engine,
            bundle_root=config.bundle_root,
            artifact_root=config.artifact_root,
            rubric=rubric,
            ttl_seconds=config.session_ttl_seconds,
        )
    token = (
        str(auth_token)
        if auth_token is not None
        else str(runtime.get("ORS_PPT_RUNTIME_TOKEN", ""))
    ).strip()
    if _env_bool(runtime, "ORS_PPT_REQUIRE_AUTH", False) and not token:
        raise PptInfrastructureError(
            "ORS_PPT_RUNTIME_TOKEN is required when ORS_PPT_REQUIRE_AUTH=true"
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

    app = FastAPI(title="OpenReward PPT runtime", version="1", lifespan=lifespan)

    def check_auth(authorization: str | None) -> None:
        if token and not secrets.compare_digest(
            str(authorization or ""),
            f"Bearer {token}",
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    def translate(exc: Exception) -> HTTPException:
        if isinstance(exc, KeyError):
            return HTTPException(status_code=404, detail="presentation session not found")
        if isinstance(exc, PptTaskError):
            return HTTPException(status_code=400, detail=str(exc))
        if isinstance(exc, PptInfrastructureError):
            return HTTPException(status_code=502, detail=str(exc))
        return HTTPException(status_code=500, detail="unexpected PPT runtime failure")

    async def body(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="request body must be JSON") from exc
        if not isinstance(payload, Mapping):
            raise HTTPException(status_code=400, detail="request body must be an object")
        return dict(payload)

    async def incoming(request: Request) -> dict[str, Any]:
        return _mapping((await body(request)).get("input"))

    async def session_for(session_id: str) -> PptSession:
        try:
            return await service.get(session_id)
        except Exception as exc:
            raise translate(exc) from exc

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok" if await asyncio.to_thread(service.engine.ping) else "degraded",
            "runtime": "ppt-sandbox-rubric",
            "active_sessions": len(service.sessions),
            "rubric_ready": service.rubric is not None,
        }

    @app.post("/v1/presentations")
    async def create_presentation(
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

    @app.post("/v1/presentations/{session_id}/exec")
    async def execute(
        session_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        values = await incoming(request)
        try:
            result = await service.bash(
                await session_for(session_id),
                _text(values.get("command")),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise translate(exc) from exc
        return JSONResponse(result)

    async def read_action(request: Request, *, inspect: bool) -> dict[str, Any]:
        payload = await body(request)
        values = _mapping(payload.get("input"))
        session_id = _text(payload.get("session_id"))
        try:
            return await service.read(
                await session_for(session_id),
                values.get("path"),
                inspect=inspect,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise translate(exc) from exc

    @app.post("/v1/presentations/{session_id}/files/read")
    async def read_file(
        session_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        values = await incoming(request)
        try:
            result = await service.read(
                await session_for(session_id), values.get("path"), inspect=False
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise translate(exc) from exc
        return JSONResponse(result)

    @app.post("/v1/presentations/read")
    async def inspect_presentation(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        return JSONResponse(await read_action(request, inspect=True))

    async def write_action(request: Request) -> dict[str, Any]:
        payload = await body(request)
        values = _mapping(payload.get("input"))
        try:
            return await service.write(
                await session_for(_text(payload.get("session_id"))),
                values.get("path"),
                values.get("content"),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise translate(exc) from exc

    @app.post("/v1/presentations/{session_id}/files/write")
    async def write_file(
        session_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        values = await incoming(request)
        try:
            result = await service.write(
                await session_for(session_id),
                values.get("path"),
                values.get("content"),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise translate(exc) from exc
        return JSONResponse(result)

    @app.post("/v1/presentations/write")
    async def write_presentation_source(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        return JSONResponse(await write_action(request))

    @app.post("/v1/presentations/{session_id}/files/edit")
    async def edit_file(
        session_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        values = await incoming(request)
        try:
            result = await service.edit(
                await session_for(session_id),
                values.get("path"),
                values.get("old_text"),
                values.get("new_text"),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise translate(exc) from exc
        return JSONResponse(result)

    async def terminal_action(
        request: Request,
        *,
        submit: bool,
    ) -> dict[str, Any]:
        payload = await body(request)
        values = _mapping(payload.get("input"))
        try:
            session = await session_for(_text(payload.get("session_id")))
            if submit:
                return await service.submit(session, values.get("path"))
            return await service.render(session, values.get("path"))
        except HTTPException:
            raise
        except Exception as exc:
            raise translate(exc) from exc

    @app.post("/v1/presentations/render")
    async def render_presentation(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        return JSONResponse(await terminal_action(request, submit=False))

    @app.post("/v1/presentations/verify")
    async def verify_presentation(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        return JSONResponse(await terminal_action(request, submit=True))

    @app.delete("/v1/presentations/{session_id}")
    async def delete_presentation(
        session_id: str,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_auth(authorization)
        await service.delete(session_id)
        return JSONResponse({"deleted": True})

    return app

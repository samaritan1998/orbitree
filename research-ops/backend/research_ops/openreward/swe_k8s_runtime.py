"""Kubernetes-native engine for the OpenReward SWE-bench sidecar.

The service pod owns only the Kubernetes API client.  Each policy/verifier
workspace is a separate, network-isolated Pod.  A small CFS exchange directory
is mounted at ``/ors-io`` solely for bounded patch, eval-script, and file
transfers; the repository itself remains inside the task image at ``/testbed``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .swe_runtime import (
    _IMAGE_REF,
    CommandResult,
    EvaluationResult,
    HarnessPlan,
    SweInfrastructureError,
    SweRuntimeConfig,
    SweTaskError,
    _text,
)

_WORKDIR = PurePosixPath("/testbed")
_IO_DIR = PurePosixPath("/ors-io")
_EXIT_MARKER = re.compile(r"\n(__ORS_SWE_EXIT_[0-9a-f]+__)(-?[0-9]+)\s*$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_DOCKER_MEMORY = re.compile(r"^([1-9][0-9]*)([bkmg]?)$", re.IGNORECASE)


def _absolute_directory(
    environ: Mapping[str, str],
    name: str,
    *,
    create: bool = False,
) -> Path:
    raw = _text(environ.get(name))
    path = Path(raw).expanduser()
    if not raw or not path.is_absolute():
        raise SweInfrastructureError(f"{name} must be an absolute directory")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SweInfrastructureError(f"{name} is unavailable") from exc
    if not resolved.is_dir():
        raise SweInfrastructureError(f"{name} is not a directory")
    return resolved


def _dns_label(environ: Mapping[str, str], name: str, default: str) -> str:
    value = _text(environ.get(name) or default).casefold()
    if not _DNS_LABEL.fullmatch(value):
        raise SweInfrastructureError(f"{name} must be a Kubernetes DNS label")
    return value


def _image_map(environ: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Load an explicit canonical-image -> mirror mapping, if configured.

    A mapping file is intentionally opt-in.  It is useful when an internal
    registry has a verified copy whose repository/tag naming differs from the
    official SWE-bench name (for example, a double-underscore tag).  The
    harness still computes and validates the official image reference; this
    mapping only changes the Pod image sent to Kubernetes.
    """

    raw = _text(environ.get("ORS_SWE_K8S_IMAGE_MAP_PATH"))
    if not raw:
        return ()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SweInfrastructureError("ORS_SWE_K8S_IMAGE_MAP_PATH must be absolute")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SweInfrastructureError("ORS_SWE_K8S_IMAGE_MAP_PATH is invalid") from exc
    if not isinstance(payload, Mapping):
        raise SweInfrastructureError("SWE image map must be a JSON object")
    pairs: list[tuple[str, str]] = []
    for source, target in payload.items():
        source_ref = _text(source)
        target_ref = _text(target)
        if not source_ref or not target_ref or not _IMAGE_REF.fullmatch(source_ref):
            raise SweInfrastructureError("SWE image map contains an invalid source")
        if not _IMAGE_REF.fullmatch(target_ref):
            raise SweInfrastructureError("SWE image map contains an invalid target")
        pairs.append((source_ref, target_ref))
    return tuple(pairs)


def _kubernetes_memory_quantity(value: str) -> str:
    """Translate the runtime's Docker-style memory limit to a K8s quantity."""

    match = _DOCKER_MEMORY.fullmatch(_text(value))
    if match is None:
        raise SweInfrastructureError("ORS_SWE_MEMORY is not a Docker memory quantity")
    amount, suffix = match.groups()
    return amount + {"": "", "b": "", "k": "Ki", "m": "Mi", "g": "Gi"}[suffix.casefold()]


@dataclass(frozen=True)
class KubernetesSweRuntimeConfig:
    runtime: SweRuntimeConfig
    namespace: str
    host_root: Path
    work_root: Path
    image_pull_secret: str
    image_prefix: str
    swegym_image_prefix: str
    image_map: tuple[tuple[str, str], ...] = ()
    pod_start_seconds: int = 900
    delete_seconds: int = 180
    preserve_workspace: bool = False
    kubeconfig: Path | None = None

    @classmethod
    def from_environ(
        cls,
        runtime: SweRuntimeConfig,
        environ: Mapping[str, str] | None = None,
    ) -> KubernetesSweRuntimeConfig:
        values = os.environ if environ is None else environ
        host_root = _absolute_directory(values, "ORS_SWE_K8S_HOST_ROOT")
        work_root = _absolute_directory(
            values,
            "ORS_SWE_K8S_WORK_ROOT",
            create=True,
        )
        if not work_root.is_relative_to(host_root):
            raise SweInfrastructureError(
                "ORS_SWE_K8S_WORK_ROOT must be below ORS_SWE_K8S_HOST_ROOT"
            )
        prefix = _text(values.get("ORS_SWE_K8S_IMAGE_PREFIX")).rstrip("/")
        swegym_value = values.get("ORS_SWE_K8S_SWEGYM_IMAGE_PREFIX")
        swegym_prefix = (
            _text(swegym_value).rstrip("/")
            if swegym_value is not None
            else "registry.example.com/acg-agi/swegym"
        )
        # Registry references are intentionally opaque, but must not carry a
        # path traversal or shell metacharacter.
        if (
            prefix
            and not prefix.startswith(("http://", "https://"))
            and not re.fullmatch(r"[A-Za-z0-9._:/-]+", prefix)
        ):
            raise SweInfrastructureError("ORS_SWE_K8S_IMAGE_PREFIX is invalid")
        if swegym_prefix and not re.fullmatch(r"[A-Za-z0-9._:/-]+", swegym_prefix):
            raise SweInfrastructureError("ORS_SWE_K8S_SWEGYM_IMAGE_PREFIX is invalid")
        image_map = _image_map(values)
        kubeconfig_value = _text(values.get("ORS_SWE_K8S_KUBECONFIG"))
        kubeconfig: Path | None = None
        if kubeconfig_value:
            candidate = Path(kubeconfig_value).expanduser()
            try:
                candidate = candidate.resolve(strict=True)
            except OSError as exc:
                raise SweInfrastructureError("ORS_SWE_K8S_KUBECONFIG is unavailable") from exc
            if not candidate.is_file():
                raise SweInfrastructureError("ORS_SWE_K8S_KUBECONFIG must be an existing file")
            kubeconfig = candidate
        return cls(
            runtime=runtime,
            namespace=_dns_label(values, "ORS_SWE_K8S_NAMESPACE", "rl-training"),
            host_root=host_root,
            work_root=work_root,
            image_pull_secret=_text(
                values.get("ORS_SWE_K8S_IMAGE_PULL_SECRET") or "iregistry-secret"
            ),
            image_prefix=prefix,
            swegym_image_prefix=swegym_prefix,
            image_map=image_map,
            pod_start_seconds=max(1, int(values.get("ORS_SWE_K8S_POD_START_SECONDS", 900))),
            delete_seconds=max(1, int(values.get("ORS_SWE_K8S_DELETE_SECONDS", 180))),
            preserve_workspace=str(values.get("ORS_SWE_K8S_PRESERVE_WORKSPACE", "false")).casefold()
            in {"1", "true", "yes", "on"},
            kubeconfig=kubeconfig,
        )


@dataclass
class KubernetesSweHandle:
    session_id: str
    pod_name: str
    container_name: str
    session_root: Path
    io_root: Path
    image_id: str


class KubernetesSweEngine:
    """Launch policy and verifier Pods and expose the Docker-engine shape."""

    def __init__(
        self,
        config: KubernetesSweRuntimeConfig,
        *,
        core_api: Any | None = None,
        stream_fn: Any | None = None,
    ) -> None:
        self.config = config
        if core_api is None:
            core_api, default_stream = self._build_client(config)
            stream_fn = stream_fn or default_stream
        if stream_fn is None:
            try:
                from kubernetes.stream import stream as stream_fn
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise SweInfrastructureError(
                    "install the ppt-k8s extra to execute commands in SWE Pods"
                ) from exc
        self.core = core_api
        self.stream = stream_fn
        self._image_ids: dict[str, str] = {}
        self._image_lock = threading.Lock()

    @staticmethod
    def _build_client(config: KubernetesSweRuntimeConfig) -> tuple[Any, Any]:
        try:
            from kubernetes import client
            from kubernetes import config as kube_config
            from kubernetes.stream import stream
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise SweInfrastructureError("install the ppt-k8s extra to launch SWE Pods") from exc
        configuration = client.Configuration()
        try:
            if config.kubeconfig is None:
                kube_config.load_incluster_config(client_configuration=configuration)
            else:
                kube_config.load_kube_config(
                    config_file=str(config.kubeconfig),
                    client_configuration=configuration,
                )
        except Exception as exc:  # pragma: no cover - deployment environment
            raise SweInfrastructureError(
                f"cannot configure Kubernetes client: {type(exc).__name__}"
            ) from exc
        configuration.proxy = None
        return client.CoreV1Api(client.ApiClient(configuration)), stream

    @staticmethod
    def _status(exc: Exception) -> int | None:
        return getattr(exc, "status", None)

    def _host_path(self, path: Path, *, label: str) -> Path:
        try:
            resolved = path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise SweInfrastructureError(f"{label} is unavailable") from exc
        if not resolved.is_relative_to(self.config.host_root):
            raise SweInfrastructureError(f"{label} is outside ORS_SWE_K8S_HOST_ROOT")
        return resolved

    @staticmethod
    def _pod_name(role: str, session_id: str) -> str:
        suffix = re.sub(r"[^a-z0-9-]+", "-", session_id.casefold()).strip("-")[-32:]
        return f"ors-swe-{role}-{suffix}-{secrets.token_hex(3)}"[:63].rstrip("-")

    def _resolve_image(self, image_ref: str) -> str:
        raw = _text(image_ref)
        if not raw:
            raise SweInfrastructureError("SWE harness omitted its image reference")
        for source, target in self.config.image_map:
            if raw == source:
                return target
        if raw.startswith("swebench/"):
            if not self.config.image_prefix:
                raise SweInfrastructureError(
                    "official SWE image requires ORS_SWE_K8S_IMAGE_PREFIX; "
                    "mirror the pinned image into the internal registry first"
                )
            return f"{self.config.image_prefix}/{raw.split('/', 1)[1]}"
        if raw.startswith(("xingyaoww/", "swegym/")):
            if not self.config.swegym_image_prefix:
                return raw
            return f"{self.config.swegym_image_prefix}/{raw.split('/', 1)[1]}"
        return raw

    def _resources(self) -> dict[str, Any]:
        cpu = self.config.runtime.cpus
        memory = _kubernetes_memory_quantity(self.config.runtime.memory)
        return {
            "requests": {"cpu": str(max(0.1, cpu / 2)), "memory": memory},
            "limits": {"cpu": str(cpu), "memory": memory},
        }

    def _pod_body(
        self,
        *,
        name: str,
        role: str,
        image: str,
        io_root: Path,
        active_deadline_seconds: int,
    ) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": self.config.namespace,
                "labels": {
                    "app.kubernetes.io/name": "openreward-swe-sandbox",
                    "app.kubernetes.io/part-of": "research-ops",
                    "research-ops.runtime": "openreward-swe-sandbox",
                    "research-ops.role": role,
                },
            },
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "enableServiceLinks": False,
                "activeDeadlineSeconds": active_deadline_seconds,
                "terminationGracePeriodSeconds": 5,
                "imagePullSecrets": [{"name": self.config.image_pull_secret}],
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [
                    {
                        "name": role,
                        "image": image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": [
                            "/bin/bash",
                            "-lc",
                            "trap 'exit 0' TERM INT; while :; do sleep 3600; done",
                        ],
                        "workingDir": "/testbed",
                        "resources": self._resources(),
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "volumeMounts": [
                            {"name": "io", "mountPath": "/ors-io"},
                            {"name": "tmp", "mountPath": "/tmp"},
                        ],
                    }
                ],
                "volumes": [
                    {"name": "io", "hostPath": {"path": str(io_root), "type": "Directory"}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "2Gi"}},
                ],
            },
        }

    @staticmethod
    def _container_status(pod: Any, name: str) -> Any | None:
        for item in getattr(getattr(pod, "status", None), "container_statuses", None) or []:
            if getattr(item, "name", None) == name:
                return item
        return None

    def _logs(self, pod_name: str, container_name: str) -> str:
        try:
            value = self.core.read_namespaced_pod_log(
                pod_name,
                self.config.namespace,
                container=container_name,
                tail_lines=100,
                _request_timeout=30,
            )
        except Exception:
            return ""
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        return str(value)[-10000:]

    def _wait_running(self, pod_name: str, container_name: str) -> str:
        deadline = time.monotonic() + self.config.pod_start_seconds
        last = "Pending"
        while time.monotonic() < deadline:
            pod = self.core.read_namespaced_pod(
                pod_name,
                self.config.namespace,
                _request_timeout=30,
            )
            phase = _text(getattr(getattr(pod, "status", None), "phase", ""))
            status = self._container_status(pod, container_name)
            state = getattr(status, "state", None)
            if state is not None and getattr(state, "running", None) is not None:
                image_id = _text(getattr(status, "image_id", ""))
                return image_id
            waiting = getattr(state, "waiting", None) if state is not None else None
            last = _text(getattr(waiting, "reason", "")) or phase or last
            if last in {
                "ErrImagePull",
                "ImagePullBackOff",
                "InvalidImageName",
                "CreateContainerConfigError",
            }:
                raise SweInfrastructureError(f"SWE Pod cannot start ({last})")
            if phase in {"Failed", "Succeeded"}:
                raise SweInfrastructureError(
                    f"SWE Pod terminated before ready ({phase}): "
                    f"{self._logs(pod_name, container_name)}"
                )
            time.sleep(1)
        raise SweInfrastructureError(f"SWE Pod start timed out ({last})")

    def _delete_pod(self, pod_name: str) -> None:
        try:
            self.core.delete_namespaced_pod(
                pod_name,
                self.config.namespace,
                grace_period_seconds=0,
                propagation_policy="Background",
                _request_timeout=30,
            )
        except Exception as exc:
            if self._status(exc) == 404:
                return
            raise SweInfrastructureError(f"cannot delete SWE Pod: {type(exc).__name__}") from exc
        deadline = time.monotonic() + self.config.delete_seconds
        while time.monotonic() < deadline:
            try:
                self.core.read_namespaced_pod(
                    pod_name,
                    self.config.namespace,
                    _request_timeout=30,
                )
            except Exception as exc:
                if self._status(exc) == 404:
                    return
                raise SweInfrastructureError(
                    f"cannot confirm SWE Pod deletion: {type(exc).__name__}"
                ) from exc
            time.sleep(0.5)
        raise SweInfrastructureError("SWE Pod deletion timed out")

    def ping(self) -> bool:
        try:
            self.core.list_namespaced_pod(
                self.config.namespace,
                label_selector="research-ops.runtime=openreward-swe-sandbox",
                limit=1,
                _request_timeout=30,
            )
        except Exception:
            return False
        return True

    def image_id(self, image_ref: str) -> str:
        with self._image_lock:
            image_id = self._image_ids.get(image_ref)
        if not image_id:
            raise SweInfrastructureError("SWE image has not been resolved by a running Pod")
        return image_id

    def start(self, plan: HarnessPlan, *, session_id: str, purpose: str) -> KubernetesSweHandle:
        image = self._resolve_image(plan.image_ref)
        root = (self.config.work_root / session_id / purpose).resolve()
        if not root.is_relative_to(self.config.work_root):
            raise SweInfrastructureError("SWE session workspace escaped configured root")
        io_root = root / "io"
        io_root.mkdir(parents=True, exist_ok=False)
        pod_name = self._pod_name(purpose, session_id)
        body = self._pod_body(
            name=pod_name,
            role=purpose,
            image=image,
            io_root=io_root,
            active_deadline_seconds=self.config.runtime.session_ttl_seconds + 600,
        )
        try:
            self.core.create_namespaced_pod(
                self.config.namespace,
                body,
                _request_timeout=30,
            )
            image_id = self._wait_running(pod_name, purpose)
            handle = KubernetesSweHandle(
                session_id=session_id,
                pod_name=pod_name,
                container_name=purpose,
                session_root=root,
                io_root=io_root,
                image_id=image_id or image,
            )
            head = self.exec_shell(handle, "git rev-parse HEAD")
            if head.exit_code != 0 or (
                plan.requires_head_match and head.text.strip() != plan.task.base_commit
            ):
                raise SweInfrastructureError("SWE image HEAD does not match task base_commit")
            baseline = self.exec_shell(
                handle,
                "set -e; git add -A; git write-tree > /tmp/ors-baseline-tree; "
                "git reset --mixed -q HEAD",
            )
            if baseline.exit_code != 0:
                raise SweInfrastructureError("cannot snapshot SWE image baseline")
            with self._image_lock:
                self._image_ids[plan.image_ref] = handle.image_id
            return handle
        except Exception:
            self._delete_pod(pod_name)
            if not self.config.preserve_workspace:
                shutil.rmtree(root.parent, ignore_errors=True)
            raise

    def remove(self, container: KubernetesSweHandle | None) -> None:
        if container is None:
            return
        self._delete_pod(container.pod_name)
        if not self.config.preserve_workspace:
            root = container.session_root.resolve()
            if root.is_relative_to(self.config.work_root):
                shutil.rmtree(root.parent, ignore_errors=True)

    def _exec_raw(self, container: KubernetesSweHandle, command: Sequence[str]) -> CommandResult:
        try:
            output = self.stream(
                self.core.connect_get_namespaced_pod_exec,
                container.pod_name,
                self.config.namespace,
                container=container.container_name,
                command=list(command),
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _request_timeout=self.config.runtime.command_seconds + 60,
            )
        except Exception as exc:
            raise SweInfrastructureError(f"Kubernetes exec failed: {type(exc).__name__}") from exc
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return CommandResult(str(output), 0)

    def exec_shell(
        self,
        container: KubernetesSweHandle,
        command: str,
        *,
        seconds: int | None = None,
        max_bytes: int | None = None,
    ) -> CommandResult:
        marker = f"__ORS_SWE_EXIT_{secrets.token_hex(8)}__"
        output_path = f"/tmp/ors-swe-output-{secrets.token_hex(8)}"
        wrapper = (
            "set +e; output=$1; seconds=$2; limit=$3; command=$4; marker=$5; "
            'timeout --signal=KILL "${seconds}s" /bin/bash -lc "$command" >"$output" 2>&1; '
            'rc=$?; bytes=$(wc -c <"$output"); head -c "$limit" "$output"; '
            'rm -f "$output"; if [ "$bytes" -gt "$limit" ]; then '
            "printf '\\n[OpenReward output truncated: %s bytes]\\n' \"$bytes\"; fi; "
            'printf \'\\n%s%s\\n\' "$marker" "$rc"; exit 0'
        )
        args = [
            "/bin/bash",
            "-c",
            wrapper,
            "ors",
            output_path,
            str(seconds or self.config.runtime.command_seconds),
            str(max_bytes or self.config.runtime.max_command_output_bytes),
            command,
            marker,
        ]
        raw = self._exec_raw(container, args).text
        match = _EXIT_MARKER.search(raw)
        if not match or match.group(1) != marker:
            raise SweInfrastructureError("Kubernetes exec omitted the command exit marker")
        text = raw[: match.start()]
        code = int(match.group(2))
        return CommandResult(
            text=text,
            exit_code=code,
            timed_out=code in {124, 137},
            truncated="[OpenReward output truncated:" in text,
        )

    def read_file(self, container: KubernetesSweHandle, path: PurePosixPath) -> CommandResult:
        result = self.exec_shell(
            container,
            f"head -c {self.config.runtime.max_file_bytes + 1} -- {shlex.quote(str(path))}",
        )
        raw = result.text.encode("utf-8", errors="replace")
        truncated = len(raw) > self.config.runtime.max_file_bytes
        text = raw[: self.config.runtime.max_file_bytes].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[OpenReward file truncated]\n"
        return CommandResult(text, result.exit_code, result.timed_out, truncated)

    def write_file(self, container: KubernetesSweHandle, path: PurePosixPath, content: str) -> int:
        raw = content.encode("utf-8")
        if len(raw) > self.config.runtime.max_file_bytes:
            raise SweTaskError("file content exceeds ORS_SWE_MAX_FILE_BYTES")
        name = f"input-{secrets.token_hex(8)}"
        source = container.io_root / name
        source.write_bytes(raw)
        try:
            result = self.exec_shell(
                container,
                f"mkdir -p -- {shlex.quote(str(path.parent))} && "
                f"cp -- {shlex.quote(str(_IO_DIR / name))} {shlex.quote(str(path))}",
            )
            if result.exit_code != 0:
                raise SweInfrastructureError("cannot write SWE workspace file")
        finally:
            source.unlink(missing_ok=True)
        return len(raw)

    def extract_patch(self, container: KubernetesSweHandle) -> bytes:
        name = f"patch-{secrets.token_hex(8)}"
        target = container.io_root / name
        command = (
            "set -e; git add -A; "
            "trap 'git reset --mixed -q >/dev/null 2>&1 || true' EXIT; "
            "baseline=$(cat /tmp/ors-baseline-tree); "
            'git -c core.fileMode=false diff --cached --binary "$baseline" > '
            f"{shlex.quote(str(_IO_DIR / name))}; "
            f"bytes=$(wc -c < {shlex.quote(str(_IO_DIR / name))}); "
            f'test "$bytes" -le {self.config.runtime.max_patch_bytes}'
        )
        result = self.exec_shell(container, command)
        if result.exit_code != 0:
            raise SweTaskError("cannot extract policy git diff")
        try:
            patch = target.read_bytes()
        except OSError as exc:
            raise SweInfrastructureError("SWE patch exchange file is unavailable") from exc
        finally:
            target.unlink(missing_ok=True)
        if len(patch) > self.config.runtime.max_patch_bytes:
            raise SweTaskError("policy patch exceeds ORS_SWE_MAX_PATCH_BYTES")
        return patch

    def verify(self, plan: HarnessPlan, patch: bytes, *, session_id: str) -> EvaluationResult:
        verifier = self.start(plan, session_id=f"{session_id}-verify", purpose="verifier")
        patch_applied = not patch
        eval_result: CommandResult | None = None
        try:
            if patch:
                patch_name = "model.patch"
                (verifier.io_root / patch_name).write_bytes(patch)
                applied = self.exec_shell(
                    verifier,
                    f"git apply --verbose {shlex.quote(str(_IO_DIR / patch_name))}",
                )
                patch_applied = applied.exit_code == 0
            if patch_applied:
                eval_name = "eval.sh"
                (verifier.io_root / eval_name).write_text(plan.eval_script, encoding="utf-8")
                eval_result = self.exec_shell(
                    verifier,
                    f"/bin/bash {shlex.quote(str(_IO_DIR / eval_name))}",
                    seconds=self.config.runtime.verify_seconds,
                    max_bytes=self.config.runtime.max_eval_output_bytes,
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

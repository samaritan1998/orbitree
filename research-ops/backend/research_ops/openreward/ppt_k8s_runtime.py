"""Kubernetes-native sandbox engine for the OpenReward PPT runtime."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .ppt_runtime import (
    CommandResult,
    PptInfrastructureError,
    PptRuntimeConfig,
    PptTask,
    PptTaskError,
    _env_bool,
    _env_int,
    _text,
)

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_EXIT_MARKER = re.compile(r"\n(__ORS_EXIT_[0-9a-f]+__)(-?[0-9]+)\s*$")
_WORKSPACE = PurePosixPath("/workspace")
_INPUTS = PurePosixPath("/inputs")


def _absolute_directory(
    environ: Mapping[str, str],
    name: str,
    *,
    create: bool = False,
) -> Path:
    value = _text(environ.get(name))
    path = Path(value).expanduser()
    if not value or not path.is_absolute():
        raise PptInfrastructureError(f"{name} must be an absolute directory")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PptInfrastructureError(f"{name} is unavailable") from exc
    if not resolved.is_dir():
        raise PptInfrastructureError(f"{name} is not a directory")
    return resolved


def _dns_label(environ: Mapping[str, str], name: str, default: str) -> str:
    value = _text(environ.get(name) or default).casefold()
    if not _DNS_LABEL.fullmatch(value):
        raise PptInfrastructureError(f"{name} must be a Kubernetes DNS label")
    return value


def _memory_quantity(value: str) -> str:
    match = re.fullmatch(r"([1-9][0-9]*)([bkmg])?", value.casefold())
    if not match:  # PptRuntimeConfig already validates this; keep a defensive gate.
        raise PptInfrastructureError("ORS_PPT_MEMORY is invalid")
    amount, suffix = match.groups()
    return {
        None: amount,
        "b": amount,
        "k": f"{amount}Ki",
        "m": f"{amount}Mi",
        "g": f"{amount}Gi",
    }[suffix]


@dataclass(frozen=True)
class KubernetesPptRuntimeConfig:
    runtime: PptRuntimeConfig
    namespace: str
    work_root: Path
    host_root: Path
    image_pull_secret: str
    pod_start_seconds: int = 600
    delete_seconds: int = 120
    preserve_workspace: bool = False
    kubeconfig: Path | None = None
    node_binary: Path | None = None

    @classmethod
    def from_environ(
        cls,
        runtime: PptRuntimeConfig,
        environ: Mapping[str, str] | None = None,
    ) -> KubernetesPptRuntimeConfig:
        values = os.environ if environ is None else environ
        host_root = _absolute_directory(values, "ORS_PPT_K8S_HOST_ROOT")
        work_root = _absolute_directory(values, "ORS_PPT_K8S_WORK_ROOT", create=True)
        if not work_root.is_relative_to(host_root):
            raise PptInfrastructureError(
                "ORS_PPT_K8S_WORK_ROOT must be below ORS_PPT_K8S_HOST_ROOT"
            )
        for label, path in (
            ("ORS_PPT_TASK_BUNDLE_ROOT", runtime.bundle_root),
            ("ORS_PPT_ARTIFACT_ROOT", runtime.artifact_root),
        ):
            if not path.resolve().is_relative_to(host_root):
                raise PptInfrastructureError(f"{label} must be below ORS_PPT_K8S_HOST_ROOT")
        if runtime.skill_root is not None and not runtime.skill_root.is_relative_to(host_root):
            raise PptInfrastructureError(
                "ORS_PPT_SKILL_ROOT must be below ORS_PPT_K8S_HOST_ROOT"
            )
        kubeconfig_value = _text(values.get("ORS_PPT_K8S_KUBECONFIG"))
        kubeconfig: Path | None = None
        if kubeconfig_value:
            candidate = Path(kubeconfig_value).expanduser()
            if not candidate.is_absolute() or not candidate.resolve().is_file():
                raise PptInfrastructureError(
                    "ORS_PPT_K8S_KUBECONFIG must be an existing absolute file"
                )
            kubeconfig = candidate.resolve()
        node_value = _text(values.get("ORS_PPT_K8S_NODE_BINARY"))
        node_binary: Path | None = None
        if node_value:
            candidate = Path(node_value).expanduser()
            try:
                candidate = candidate.resolve(strict=True)
            except OSError as exc:
                raise PptInfrastructureError(
                    "ORS_PPT_K8S_NODE_BINARY is unavailable"
                ) from exc
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                raise PptInfrastructureError(
                    "ORS_PPT_K8S_NODE_BINARY must be an executable file"
                )
            if not candidate.is_relative_to(host_root):
                raise PptInfrastructureError(
                    "ORS_PPT_K8S_NODE_BINARY must be below ORS_PPT_K8S_HOST_ROOT"
                )
            node_binary = candidate
        return cls(
            runtime=runtime,
            namespace=_dns_label(values, "ORS_PPT_K8S_NAMESPACE", "rl-training"),
            work_root=work_root,
            host_root=host_root,
            image_pull_secret=_dns_label(
                values,
                "ORS_PPT_K8S_IMAGE_PULL_SECRET",
                "iregistry-secret",
            ),
            pod_start_seconds=_env_int(values, "ORS_PPT_K8S_POD_START_SECONDS", 600),
            delete_seconds=_env_int(values, "ORS_PPT_K8S_DELETE_SECONDS", 120),
            preserve_workspace=_env_bool(
                values,
                "ORS_PPT_K8S_PRESERVE_WORKSPACE",
                False,
            ),
            kubeconfig=kubeconfig,
            node_binary=node_binary,
        )


@dataclass
class KubernetesPptHandle:
    session_id: str
    pod_name: str
    container_name: str
    session_root: Path
    workspace: Path
    inputs: Path
    image_id: str


class KubernetesPptEngine:
    """Launch isolated policy and renderer Pods with CFS-backed workspaces."""

    def __init__(
        self,
        config: KubernetesPptRuntimeConfig,
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
                raise PptInfrastructureError(
                    "install the ppt-k8s extra to execute commands in PPT Pods"
                ) from exc
        self.core = core_api
        self.stream = stream_fn
        self._image_ids: dict[str, str] = {}
        self._image_lock = threading.Lock()

    @staticmethod
    def _build_client(config: KubernetesPptRuntimeConfig) -> tuple[Any, Any]:
        try:
            from kubernetes import client
            from kubernetes import config as kube_config
            from kubernetes.stream import stream
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise PptInfrastructureError(
                "install the ppt-k8s extra to launch PPT Pods"
            ) from exc
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
            raise PptInfrastructureError(
                f"cannot configure Kubernetes client: {type(exc).__name__}"
            ) from exc
        configuration.proxy = None
        api_client = client.ApiClient(configuration)
        return client.CoreV1Api(api_client), stream

    @staticmethod
    def _api_status(exc: Exception) -> int | None:
        return getattr(exc, "status", None)

    def _host_path(self, path: Path, *, label: str) -> Path:
        try:
            resolved = path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise PptInfrastructureError(f"{label} is unavailable") from exc
        if not resolved.is_relative_to(self.config.host_root):
            raise PptInfrastructureError(f"{label} is outside ORS_PPT_K8S_HOST_ROOT")
        return resolved

    @staticmethod
    def _pod_name(role: str, session_id: str) -> str:
        suffix = re.sub(r"[^a-z0-9-]+", "-", session_id.casefold()).strip("-")[-32:]
        nonce = secrets.token_hex(3)
        return f"ors-ppt-{role}-{suffix}-{nonce}"[:63].rstrip("-")

    def _resources(self) -> dict[str, Any]:
        cpu_limit = self.config.runtime.cpus
        cpu_request = max(0.1, cpu_limit / 2)
        return {
            "requests": {
                "cpu": f"{cpu_request:g}",
                "memory": _memory_quantity(self.config.runtime.memory),
            },
            "limits": {
                "cpu": f"{cpu_limit:g}",
                "memory": _memory_quantity(self.config.runtime.memory),
            },
        }

    def _pod_body(
        self,
        *,
        name: str,
        role: str,
        image: str,
        workspace: Path,
        inputs: Path | None,
        command: Sequence[str],
        active_deadline_seconds: int,
    ) -> dict[str, Any]:
        volumes: list[dict[str, Any]] = [
            {
                "name": "workspace",
                "hostPath": {"path": str(workspace), "type": "Directory"},
            },
            {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
        ]
        mounts: list[dict[str, Any]] = [
            {"name": "workspace", "mountPath": "/workspace"},
            {"name": "tmp", "mountPath": "/tmp"},
        ]
        if inputs is not None:
            volumes.append(
                {
                    "name": "inputs",
                    "hostPath": {"path": str(inputs), "type": "Directory"},
                }
            )
            mounts.append({"name": "inputs", "mountPath": "/inputs", "readOnly": True})
        if self.config.runtime.skill_root is not None:
            skill = self._host_path(
                self.config.runtime.skill_root,
                label="ORS_PPT_SKILL_ROOT",
            )
            volumes.append(
                {
                    "name": "ppt-skill",
                    "hostPath": {"path": str(skill), "type": "Directory"},
                }
            )
            mounts.append(
                {
                    "name": "ppt-skill",
                    "mountPath": "/home/pptuser/.claude/skills/pptx",
                    "readOnly": True,
                }
            )
        if role == "policy" and self.config.node_binary is not None:
            node_binary = self._host_path(
                self.config.node_binary,
                label="ORS_PPT_K8S_NODE_BINARY",
            )
            if not node_binary.is_file() or not os.access(node_binary, os.X_OK):
                raise PptInfrastructureError(
                    "ORS_PPT_K8S_NODE_BINARY must be an executable file"
                )
            volumes.append(
                {
                    "name": "node-runtime",
                    "hostPath": {"path": str(node_binary), "type": "File"},
                }
            )
            mounts.append(
                {
                    "name": "node-runtime",
                    "mountPath": "/usr/local/bin/node",
                    "readOnly": True,
                }
            )
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": self.config.namespace,
                "labels": {
                    "app.kubernetes.io/name": "openreward-ppt-sandbox",
                    "app.kubernetes.io/part-of": "research-ops",
                    "research-ops.runtime": "openreward-ppt-sandbox",
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
                        "command": list(command),
                        "workingDir": "/workspace",
                        "resources": self._resources(),
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "volumeMounts": mounts,
                    }
                ],
                "volumes": volumes,
            },
        }

    @staticmethod
    def _container_status(pod: Any, name: str) -> Any | None:
        status = getattr(pod, "status", None)
        for item in getattr(status, "container_statuses", None) or []:
            if getattr(item, "name", None) == name:
                return item
        return None

    def _logs(self, pod_name: str, container_name: str) -> str:
        try:
            value = self.core.read_namespaced_pod_log(
                pod_name,
                self.config.namespace,
                container=container_name,
                tail_lines=80,
                _request_timeout=30,
            )
        except Exception:
            return ""
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        return str(value)[-8000:]

    def _wait_running(self, pod_name: str, container_name: str) -> str:
        deadline = time.monotonic() + self.config.pod_start_seconds
        last_state = "Pending"
        while time.monotonic() < deadline:
            try:
                pod = self.core.read_namespaced_pod(
                    pod_name,
                    self.config.namespace,
                    _request_timeout=30,
                )
            except Exception as exc:
                if self._api_status(exc) == 404:
                    time.sleep(1)
                    continue
                raise PptInfrastructureError(
                    f"cannot inspect PPT Pod: {type(exc).__name__}"
                ) from exc
            phase = _text(getattr(getattr(pod, "status", None), "phase", ""))
            container = self._container_status(pod, container_name)
            state = getattr(container, "state", None)
            if state is not None and getattr(state, "running", None) is not None:
                image_id = _text(getattr(container, "image_id", ""))
                return image_id or self.config.runtime.policy_image
            waiting = getattr(state, "waiting", None) if state is not None else None
            last_state = _text(getattr(waiting, "reason", "")) or phase or last_state
            if phase in {"Failed", "Succeeded"}:
                logs = self._logs(pod_name, container_name)
                raise PptInfrastructureError(
                    f"PPT policy Pod terminated before becoming ready ({phase}): {logs}"
                )
            time.sleep(1)
        raise PptInfrastructureError(f"PPT policy Pod start timed out ({last_state})")

    def _wait_terminal(self, pod_name: str, container_name: str, timeout: int) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pod = self.core.read_namespaced_pod(
                pod_name,
                self.config.namespace,
                _request_timeout=30,
            )
            phase = _text(getattr(getattr(pod, "status", None), "phase", ""))
            if phase in {"Succeeded", "Failed"}:
                return pod
            time.sleep(1)
        raise PptInfrastructureError("PPT renderer Pod timed out")

    def ping(self) -> bool:
        try:
            self.core.list_namespaced_pod(
                self.config.namespace,
                label_selector="research-ops.runtime=openreward-ppt-sandbox",
                limit=1,
                _request_timeout=30,
            )
        except Exception:
            return False
        return True

    def image_id(self, image_ref: str) -> str:
        if image_ref == "policy":
            image_ref = self.config.runtime.policy_image
        with self._image_lock:
            image_id = self._image_ids.get(image_ref)
        if not image_id:
            raise PptInfrastructureError("PPT image has not been resolved by a running Pod")
        return image_id

    def start(self, task: PptTask, inputs: Path, *, session_id: str) -> KubernetesPptHandle:
        del task
        inputs = self._host_path(inputs, label="PPT task inputs")
        session_root = (self.config.work_root / session_id).resolve()
        if not session_root.is_relative_to(self.config.work_root):
            raise PptInfrastructureError("PPT session workspace escaped configured root")
        workspace = session_root / "workspace"
        workspace.mkdir(parents=True, exist_ok=False)
        pod_name = self._pod_name("policy", session_id)
        container_name = "policy"
        body = self._pod_body(
            name=pod_name,
            role=container_name,
            image=self.config.runtime.policy_image,
            workspace=workspace,
            inputs=inputs,
            command=(
                "/bin/bash",
                "-lc",
                "mkdir -p /workspace /tmp; while :; do sleep 3600; done",
            ),
            active_deadline_seconds=self.config.runtime.session_ttl_seconds + 600,
        )
        try:
            self.core.create_namespaced_pod(
                self.config.namespace,
                body,
                _request_timeout=30,
            )
            image_id = self._wait_running(pod_name, container_name)
        except Exception:
            self._delete_pod(pod_name)
            if not self.config.preserve_workspace:
                shutil.rmtree(session_root, ignore_errors=True)
            raise
        with self._image_lock:
            self._image_ids[self.config.runtime.policy_image] = image_id
        return KubernetesPptHandle(
            session_id=session_id,
            pod_name=pod_name,
            container_name=container_name,
            session_root=session_root,
            workspace=workspace,
            inputs=inputs,
            image_id=image_id,
        )

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
            if self._api_status(exc) != 404:
                raise PptInfrastructureError(
                    f"cannot delete PPT Pod: {type(exc).__name__}"
                ) from exc
            return
        deadline = time.monotonic() + self.config.delete_seconds
        while time.monotonic() < deadline:
            try:
                self.core.read_namespaced_pod(
                    pod_name,
                    self.config.namespace,
                    _request_timeout=30,
                )
            except Exception as exc:
                if self._api_status(exc) == 404:
                    return
                raise PptInfrastructureError(
                    f"cannot confirm PPT Pod deletion: {type(exc).__name__}"
                ) from exc
            time.sleep(0.5)
        raise PptInfrastructureError("PPT Pod deletion timed out")

    def remove(self, container: KubernetesPptHandle | None) -> None:
        if container is None:
            return
        self._delete_pod(container.pod_name)
        if not self.config.preserve_workspace:
            root = container.session_root.resolve()
            if root.is_relative_to(self.config.work_root):
                shutil.rmtree(root, ignore_errors=True)

    def exec_shell(self, container: KubernetesPptHandle, command: str) -> CommandResult:
        marker = f"__ORS_EXIT_{secrets.token_hex(8)}__"
        wrapper = r'''
set +e
output="$1"; seconds="$2"; limit="$3"; command="$4"; marker="$5"
timeout --signal=KILL "${seconds}s" /bin/bash -lc "$command" >"$output" 2>&1
rc=$?; bytes=$(wc -c <"$output"); head -c "$limit" "$output"; rm -f "$output"
if [ "$bytes" -gt "$limit" ]; then printf '\n[OpenReward output truncated: %s bytes]\n' "$bytes"; fi
printf '\n%s%s\n' "$marker" "$rc"
exit 0
'''
        output_path = f"/tmp/ors-ppt-output-{secrets.token_hex(8)}"
        arguments = [
            "/bin/bash",
            "-c",
            wrapper,
            "ors",
            output_path,
            str(self.config.runtime.command_seconds),
            str(self.config.runtime.max_command_output_bytes),
            command,
            marker,
        ]
        try:
            output = self.stream(
                self.core.connect_get_namespaced_pod_exec,
                container.pod_name,
                self.config.namespace,
                container=container.container_name,
                command=arguments,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _request_timeout=self.config.runtime.command_seconds + 60,
            )
        except Exception as exc:
            raise PptInfrastructureError(
                f"Kubernetes exec failed: {type(exc).__name__}"
            ) from exc
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        text = str(output)
        match = _EXIT_MARKER.search(text)
        if not match or match.group(1) != marker:
            raise PptInfrastructureError("Kubernetes exec omitted the command exit marker")
        exit_code = int(match.group(2))
        text = text[: match.start()]
        return CommandResult(
            text=text,
            exit_code=exit_code,
            timed_out=exit_code in {124, 137},
            truncated="[OpenReward output truncated:" in text,
        )

    @staticmethod
    def _local_path(
        container: KubernetesPptHandle,
        path: PurePosixPath,
        *,
        write: bool,
    ) -> Path:
        if path.is_relative_to(_WORKSPACE):
            root = container.workspace.resolve()
            relative = path.relative_to(_WORKSPACE)
        elif not write and path.is_relative_to(_INPUTS):
            root = container.inputs.resolve()
            relative = path.relative_to(_INPUTS)
        else:
            raise PptTaskError("requested path is outside the PPT workspace")
        candidate = root.joinpath(*relative.parts)
        if write:
            parent = candidate.parent.resolve(strict=False)
            if not parent.is_relative_to(root):
                raise PptTaskError("requested path escaped the PPT workspace")
            return candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise PptTaskError("requested file does not exist or cannot be read") from exc
        if not resolved.is_file() or not resolved.is_relative_to(root):
            raise PptTaskError("requested file does not exist or cannot be read")
        return resolved

    def read_file(self, container: KubernetesPptHandle, path: PurePosixPath) -> bytes:
        candidate = self._local_path(container, path, write=False)
        try:
            size = candidate.stat().st_size
            if size > self.config.runtime.max_artifact_bytes:
                raise PptTaskError("requested file exceeds the PPT artifact limit")
            return candidate.read_bytes()
        except PptTaskError:
            raise
        except OSError as exc:
            raise PptTaskError("requested file does not exist or cannot be read") from exc

    def write_file(
        self,
        container: KubernetesPptHandle,
        path: PurePosixPath,
        raw: bytes,
    ) -> int:
        if len(raw) > self.config.runtime.max_text_file_bytes:
            raise PptTaskError("text file exceeds ORS_PPT_MAX_TEXT_FILE_BYTES")
        candidate = self._local_path(container, path, write=True)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        temporary = candidate.with_name(f".{candidate.name}.ors-{secrets.token_hex(6)}")
        try:
            temporary.write_bytes(raw)
            temporary.chmod(0o666)
            temporary.replace(candidate)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise PptInfrastructureError(
                f"cannot write PPT workspace file: {type(exc).__name__}"
            ) from exc
        return len(raw)

    def render(self, raw: bytes, *, session_id: str) -> list[bytes]:
        if len(raw) > self.config.runtime.max_artifact_bytes:
            raise PptTaskError("PPT artifact exceeds ORS_PPT_MAX_ARTIFACT_BYTES")
        render_root = (
            self.config.work_root / session_id / f"render-{secrets.token_hex(6)}"
        ).resolve()
        if not render_root.is_relative_to(self.config.work_root):
            raise PptInfrastructureError("PPT renderer workspace escaped configured root")
        workspace = render_root / "workspace"
        output_dir = workspace / "render"
        output_dir.mkdir(parents=True, exist_ok=False)
        (workspace / "slides.pptx").write_bytes(raw)
        pod_name = self._pod_name("render", session_id)
        container_name = "render"
        command = r'''
set -euo pipefail
mkdir -p /workspace/render /tmp/libreoffice
/usr/bin/soffice -env:UserInstallation=file:///tmp/libreoffice --headless \
  --convert-to pdf --outdir /workspace/render /workspace/slides.pptx \
  >/tmp/soffice.log 2>&1
/usr/bin/pdftoppm -png -r 120 /workspace/render/slides.pdf /workspace/render/slide
'''
        body = self._pod_body(
            name=pod_name,
            role=container_name,
            image=self.config.runtime.verifier_image,
            workspace=workspace,
            inputs=None,
            command=(
                "/usr/bin/timeout",
                f"{self.config.runtime.render_seconds}s",
                "/bin/bash",
                "-lc",
                command,
            ),
            active_deadline_seconds=self.config.runtime.render_seconds + 60,
        )
        try:
            self.core.create_namespaced_pod(
                self.config.namespace,
                body,
                _request_timeout=30,
            )
            pod = self._wait_terminal(
                pod_name,
                container_name,
                self.config.runtime.render_seconds + self.config.pod_start_seconds,
            )
            phase = _text(getattr(getattr(pod, "status", None), "phase", ""))
            container = self._container_status(pod, container_name)
            state = getattr(container, "state", None)
            terminated = getattr(state, "terminated", None) if state is not None else None
            exit_code = getattr(terminated, "exit_code", None)
            image_id = _text(getattr(container, "image_id", ""))
            if image_id:
                with self._image_lock:
                    self._image_ids[self.config.runtime.verifier_image] = image_id
            if phase != "Succeeded" or exit_code not in {None, 0}:
                if exit_code in {126, 127}:
                    raise PptInfrastructureError(
                        "PPT verifier image is missing render binaries"
                    )
                raise PptTaskError("LibreOffice could not render the submitted PPTX")
            images = sorted(
                output_dir.glob("slide-*.png"),
                key=lambda path: [
                    int(part) if part.isdigit() else part
                    for part in re.split(r"([0-9]+)", path.name)
                ],
            )
            if not images or len(images) > self.config.runtime.max_render_images:
                raise PptTaskError(
                    "PPT render count is empty or exceeds the configured limit"
                )
            return [path.read_bytes() for path in images]
        finally:
            self._delete_pod(pod_name)
            if not self.config.preserve_workspace:
                shutil.rmtree(render_root, ignore_errors=True)

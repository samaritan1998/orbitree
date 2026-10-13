"""Open Reward Standard (ORS) compatibility layer.

The project already has domain-specific HTTP clients (most notably the
AgentOS client).  This package keeps the *environment boundary* separate from
those clients and exposes the small, stable ORS contract used by rollout
harnesses:

``task -> session -> prompt/tools -> tool call -> reward/finished``.

The implementation intentionally does not depend on the optional
``openreward`` PyPI package.  It speaks the documented HTTP/SSE wire protocol
directly, which keeps the Research Ops control plane lightweight and makes the
four adapters usable in local tests before a hosted environment is available.
"""

from .adapters import (
    BackendResult,
    DelegatingEnvironment,
    EnvironmentBackend,
    MiaodaEnvironment,
    MockBackend,
    PptEnvironment,
    SweEnvironment,
    ToolUseEnvironment,
    build_default_environments,
)
from .backends import CallbackBackend, MiaodaAgentOSBackend
from .client import ORSClient, ORSClientError, ORSClientSession
from .contracts import (
    BackendContractError,
    OpenRewardBackendContract,
    backend_readiness,
    environment_references,
    load_backend_contract,
)
from .environment import ORSEnvironment, ToolDefinition
from .miaoda_online import MiaodaDatawheelGoblinBackend, MiaodaOnlineConfig, MiaodaOnlineError
from .mock_backends import (
    MiaodaMockBackend,
    PptMockBackend,
    SweMockBackend,
    ToolUseMockBackend,
    build_mock_backend,
)
from .ppt_k8s_runtime import (
    KubernetesPptEngine,
    KubernetesPptHandle,
    KubernetesPptRuntimeConfig,
)
from .ppt_runtime import (
    DockerPptEngine,
    HttpPptRubric,
    PptInspection,
    PptRuntimeConfig,
    PptRuntimeService,
    PptTask,
    RubricResult,
    create_ppt_runtime_app,
    inspect_pptx,
    presentation_path,
)
from .runtime import (
    OpenRewardRuntimeError,
    build_backends_from_contract,
    build_environments_from_contract,
    load_task_catalogs,
    load_task_catalogs_many,
)
from .server import ORSServer, create_openreward_app
from .service_backends import (
    HttpOperation,
    PptOfficeBackend,
    RemoteBackendConfigurationError,
    RemoteBackendError,
    RemoteBackendProtocolError,
    RemoteORSBackend,
    RemoteORSConfig,
    RemoteServiceBackend,
    RemoteServiceConfig,
    SweSandboxBackend,
    ToolRegistryBackend,
)
from .slideforge_reward import (
    SlideForgeCommandRunner,
    SlideForgeRewardConfig,
    SlideForgeRewardError,
    SlideForgeRewardInfrastructureError,
    SlideForgeRewardService,
    SlideForgeRewardTaskError,
    create_slideforge_reward_app,
    derive_slideforge_dimensions,
    map_slideforge_evaluation,
)
from .slideforge_tasks import (
    SlideForgeTaskMaterializationError,
    materialize_slideforge_tasks,
)
from .slime_dataset import build_slime_selector_rows
from .swe_k8s_runtime import KubernetesSweEngine, KubernetesSweHandle, KubernetesSweRuntimeConfig
from .teacher_routing import (
    RuntimeTeacherRoute,
    TeacherRoute,
    TeacherRouting,
    TeacherRoutingError,
    load_runtime_teacher_route,
    load_teacher_routing,
    materialize_runtime_routes,
    resolve_teacher_route,
    validate_runtime_teacher_route,
    write_runtime_routes,
)
from .types import (
    ImageBlock,
    Split,
    TextBlock,
    ToolOutput,
    ToolSpec,
)

__all__ = [
    "BackendContractError",
    "BackendResult",
    "CallbackBackend",
    "DelegatingEnvironment",
    "EnvironmentBackend",
    "ImageBlock",
    "KubernetesPptEngine",
    "KubernetesPptHandle",
    "KubernetesPptRuntimeConfig",
    "MiaodaEnvironment",
    "MiaodaAgentOSBackend",
    "MiaodaDatawheelGoblinBackend",
    "MiaodaOnlineConfig",
    "MiaodaOnlineError",
    "MiaodaMockBackend",
    "MockBackend",
    "ORSClient",
    "ORSClientError",
    "ORSClientSession",
    "ORSEnvironment",
    "ORSServer",
    "OpenRewardBackendContract",
    "OpenRewardRuntimeError",
    "PptEnvironment",
    "PptMockBackend",
    "PptOfficeBackend",
    "PptInspection",
    "PptRuntimeConfig",
    "PptRuntimeService",
    "PptTask",
    "RemoteBackendConfigurationError",
    "RemoteBackendError",
    "RemoteBackendProtocolError",
    "RemoteORSBackend",
    "RemoteORSConfig",
    "RemoteServiceBackend",
    "RemoteServiceConfig",
    "RubricResult",
    "SlideForgeCommandRunner",
    "SlideForgeRewardConfig",
    "SlideForgeRewardError",
    "SlideForgeRewardInfrastructureError",
    "SlideForgeRewardService",
    "SlideForgeRewardTaskError",
    "SlideForgeTaskMaterializationError",
    "Split",
    "SweEnvironment",
    "KubernetesSweEngine",
    "KubernetesSweHandle",
    "KubernetesSweRuntimeConfig",
    "SweMockBackend",
    "SweSandboxBackend",
    "TextBlock",
    "ToolDefinition",
    "ToolOutput",
    "ToolSpec",
    "ToolUseEnvironment",
    "ToolUseMockBackend",
    "ToolRegistryBackend",
    "RuntimeTeacherRoute",
    "TeacherRoute",
    "TeacherRouting",
    "TeacherRoutingError",
    "backend_readiness",
    "build_backends_from_contract",
    "build_default_environments",
    "build_environments_from_contract",
    "build_mock_backend",
    "build_slime_selector_rows",
    "create_ppt_runtime_app",
    "create_openreward_app",
    "create_slideforge_reward_app",
    "derive_slideforge_dimensions",
    "environment_references",
    "HttpOperation",
    "HttpPptRubric",
    "DockerPptEngine",
    "inspect_pptx",
    "load_backend_contract",
    "load_runtime_teacher_route",
    "load_teacher_routing",
    "load_task_catalogs",
    "load_task_catalogs_many",
    "materialize_runtime_routes",
    "map_slideforge_evaluation",
    "materialize_slideforge_tasks",
    "presentation_path",
    "resolve_teacher_route",
    "validate_runtime_teacher_route",
    "write_runtime_routes",
]

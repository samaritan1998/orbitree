"""HTTP adapters for AgentOS/Datawheel integrations.

The research-ops service deliberately talks to AgentOS over HTTP.  The
``miaoda-agent-sdk`` remains an implementation/reference client outside this
control plane; keeping the protocol boundary here makes retries, leases and
durable local state explicit.
"""

from .client import (
    AgentOSBatchSnapshot,
    AgentOSClient,
    AgentOSClientError,
    AgentOSConfigurationError,
    AgentOSHTTPError,
    normalize_datawheel_base_url,
)
from .entrypoint import (
    AgentOSApplication,
    AgentOSEndToEndRollout,
    AgentOSEntrypointClient,
    AgentOSFirstCodingRollout,
    AgentOSRolloutPhaseError,
    AgentOSStreamResult,
)
from .miaoda import (
    MiaodaBatchPayloadError,
    build_miaoda_dataset_batch_payload,
    build_miaoda_e2e_batch_payload,
    miaoda_batch_identity,
)

__all__ = [
    "AgentOSBatchSnapshot",
    "AgentOSApplication",
    "AgentOSClient",
    "AgentOSClientError",
    "AgentOSConfigurationError",
    "AgentOSEndToEndRollout",
    "AgentOSHTTPError",
    "AgentOSEntrypointClient",
    "AgentOSFirstCodingRollout",
    "AgentOSRolloutPhaseError",
    "AgentOSStreamResult",
    "MiaodaBatchPayloadError",
    "build_miaoda_dataset_batch_payload",
    "build_miaoda_e2e_batch_payload",
    "miaoda_batch_identity",
    "normalize_datawheel_base_url",
]

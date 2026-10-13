from __future__ import annotations

from scripts.run_agentos_selective_retry import (
    eligible_failure_category,
    residual_failure_category,
)


def test_selective_retry_accepts_only_service_and_timeout_failures() -> None:
    assert eligible_failure_category(
        {
            "status": "failed",
            "exception_type": "PromptiCallError",
            "error_msg": "HTTP 500: 请求后端转发实例失败: connection refused",
        }
    ) == "model_service_http_500_transport"
    assert eligible_failure_category(
        {
            "status": "failed",
            "exception_type": "PipelineStreamTimeoutError",
            "error_msg": "stream timed out",
        }
    ) == "pipeline_stream_timeout"
    assert (
        eligible_failure_category(
            {
                "status": "failed",
                "exception_type": "EntryPointResponseError",
                "error_msg": "Response is not valid JSON",
            }
        )
        is None
    )
    assert (
        eligible_failure_category(
            {
                "status": "failed",
                "exception_type": "AgentLimitError",
                "error_msg": "maximum iteration reached",
            }
        )
        is None
    )


def test_selective_retry_labels_residual_failures() -> None:
    assert residual_failure_category(
        {"exception_type": "EntryPointResponseError", "error_msg": "Response is not valid JSON"}
    ) == "application_invalid_json"
    assert residual_failure_category(
        {"exception_type": "EntryPointResponseError", "error_msg": "reason=未知错误"}
    ) == "unclassified_agent_failure"

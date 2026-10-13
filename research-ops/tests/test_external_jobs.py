from research_ops.services.external_jobs import external_job_is_active, external_job_phase


def test_external_job_statuses_preserve_provider_detail_but_classify_consistently() -> None:
    assert external_job_is_active("Created")
    assert external_job_is_active("submitted_waiting_capacity")
    assert external_job_phase("running_checkpoint_save") == "active"
    assert external_job_phase("succeeded_100_steps_finite_fp8_audits") == "completed"
    assert external_job_phase("failed_post_success_worker_ack_write") == "failed"
    assert external_job_phase("superseded_alias") == "cancelled"

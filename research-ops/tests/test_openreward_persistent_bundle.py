from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "deploy/ltp/openreward-mopd-persistent-0803"
pytestmark = pytest.mark.skipif(
    not BUNDLE.is_dir(), reason="Private deployment bundles are not distributed in the public release"
)


def test_persistent_b256_bundle_keeps_trainer_and_rollout_in_one_job() -> None:
    config = yaml.safe_load((BUNDLE / "config-b256.yaml").read_text())
    job = config["jobargs"]
    env = config["envargs"]

    assert job["num_workers"] + 1 == int(env["nnodes"]) == 6
    assert int(env["actor_num_nodes"]) == 4
    assert int(env["actor_gpus_per_node"]) == 8
    assert int(env["rollout_num_gpus"]) == 16
    assert 4 * 8 + 16 == 6 * 8
    assert env["colocate"] == "false"

    assert env["rollout_batch_size"] == "256"
    assert env["global_batch_size"] == "256"
    assert int(env["global_batch_size"]) % 4 == 0
    assert env["over_sampling_batch_size"] == "16"
    assert env["dynamic_source_manifest"].endswith(
        "config/openreward-four-domain-dynamic-sources.json"
    )
    assert env["dynamic_task_store_path"].endswith(
        "data/openreward_four_domain_dynamic_store_v1"
    )
    assert "prompt_copies" not in env
    assert "domain_weights" not in env
    assert env["sglang_server_concurrency"] == "128"
    assert int(env["num_rollout"]) - int(env["start_rollout_id"]) > 1
    assert (
        int(env["num_rollout"]) - int(env["start_rollout_id"])
    ) * int(env["rollout_batch_size"]) == 25_088

    assert env["save_interval"] == "10"
    assert env["save_optimizer"] == "true"
    assert env["resume_if_present"] == "true"
    assert env["save_debug_rollout_data"] == "false"
    assert env["managed_policy_route_file"].startswith("/srv/research-ops/")


def test_persistent_bundle_executes_the_shared_live_loop_once() -> None:
    run = (BUNDLE / "run.sh").read_text()
    assert run.count("gpu_keeper_filter.py") == 3
    assert "export SKIP_GPU_KEEPER=true" in run
    assert run.count("openreward-mopd-live-0723/run.sh") == 1


def test_b256_canary_matches_formal_batch_and_topology_for_one_update() -> None:
    formal = yaml.safe_load((BUNDLE / "config-b256.yaml").read_text())
    canary = yaml.safe_load((BUNDLE / "config-b256-canary.yaml").read_text())
    formal_env = formal["envargs"]
    canary_env = canary["envargs"]

    for key in (
        "nnodes",
        "actor_num_nodes",
        "actor_gpus_per_node",
        "rollout_num_gpus",
        "rollout_batch_size",
        "global_batch_size",
        "dynamic_source_manifest",
        "dynamic_task_store_path",
        "over_sampling_batch_size",
        "seq_len",
        "context_parallel_size",
        "expert_model_parallel_size",
        "max_tokens_per_gpu",
        "sglang_server_concurrency",
    ):
        assert canary_env[key] == formal_env[key]
    assert int(canary_env["num_rollout"]) - int(canary_env["start_rollout_id"]) == 1
    assert canary_env["output_checkpoint"] != formal_env["output_checkpoint"]
    assert canary_env["run_root"] != formal_env["run_root"]


def test_persistent_launcher_uses_stateful_shuffled_real_task_stream() -> None:
    run = (ROOT / "deploy/ltp/openreward-mopd-live-0723/run.sh").read_text()

    assert "build_openreward_dynamic_task_store.py" in run
    assert "--rollout-shuffle" in run
    assert "SLIME_OPENREWARD_TASK_STORE_ROOT" in run
    assert "--over-sampling-batch-size" in run
    assert "--rollout-function-path slime_openreward_live_plugin.generate_rollout" in run


def test_train_z_short_b32_closure_runs_second_batch_after_refresh() -> None:
    config = yaml.safe_load(
        (BUNDLE / "config-train-z-fresh-i23-short-b32-closure-r1.yaml").read_text()
    )
    job = config["jobargs"]
    env = config["envargs"]

    assert job["num_workers"] + 1 == int(env["nnodes"]) == 2
    assert int(env["actor_num_nodes"]) == 1
    assert int(env["actor_gpus_per_node"]) == 8
    assert int(env["rollout_num_gpus"]) == 8
    assert env["colocate"] == "false"

    assert env["train_driver"] == "sync_validate"
    assert env["update_weights_interval"] == "1"
    assert env["start_rollout_id"] == "1"
    assert env["num_rollout"] == "3"
    assert env["expected_iteration_override"] == "1"

    assert env["rollout_batch_size"] == "32"
    assert env["global_batch_size"] == "32"
    assert env["over_sampling_batch_size"] == "32"
    assert env["prompt_copies"] == "64"
    assert env["seq_len"] == "8192"
    assert env["max_turns"] == "2"
    assert env["max_new_tokens"] == "256"

    assert env["pure_opd"] == "true"
    assert env["use_rollout_logprobs"] == "true"
    assert env["validate_transition"] == "true"
    assert env["load_optimizer"] == "false"
    assert env["load_rng"] == "false"
    assert env["resume_if_present"] == "false"
    assert env["hf_checkpoint"].endswith("demo-teacher-base/iter_0000023_hf")
    assert env["teacher_routes_file"].endswith("teachers/runtime-routes-r9.json")
    assert "short_b32_closure_r1" in env["run_root"]
    assert "short-b32-closure-r1" in env["output_checkpoint"]


def test_sync_validate_driver_has_one_update_then_validation_rollout() -> None:
    driver = (ROOT / "scripts/train_sync_validate_rollout.py").read_text()
    assert "actor_model.async_train(start_rollout_id, first_data_ref)" in driver
    assert "actor_model.save_model(start_rollout_id, force_sync=True)" in driver
    assert "actor_model.update_weights()" in driver
    assert "generate.remote(validation_rollout_id)" in driver
    assert "transition.json" in driver


def test_short_b32_skipseed_canary_stamps_initial_version_without_full_sync() -> None:
    config = yaml.safe_load(
        (BUNDLE / "config-train-z-fresh-i23-short-b32-fetch-r7-skipseed.yaml").read_text()
    )
    env = config["envargs"]
    assert env["skip_initial_weight_sync"] == "true"
    assert env["initial_weight_version"] == "0"
    assert env["disable_rollout_global_dataset"] == "true"
    assert env["hf_checkpoint"].endswith("demo-teacher-base/iter_0000023_hf")
    assert "fetch_r7_skipseed" in env["run_root"]

    driver = (ROOT / "scripts/train_sync_validate_rollout.py").read_text()
    assert "SLIME_SYNC_VALIDATE_SKIP_INITIAL_WEIGHT_SYNC" in driver
    assert "initial_policy_weight_version_set" in driver


def test_short_b32_reload_canary_canonicalizes_live_samples_before_train() -> None:
    config = yaml.safe_load(
        (BUNDLE / "config-train-z-fresh-i23-short-b32-fetch-r9-reload.yaml").read_text()
    )
    env = config["envargs"]

    assert env["reload_rollout_data_before_train"] == "true"
    assert env["save_debug_rollout_data"] == "true"
    assert "disable_rollout_global_dataset" not in env
    assert env["skip_initial_weight_sync"] == "true"
    assert env["initial_weight_version"] == "0"
    assert "fetch_r9_reload" in env["run_root"]

    run = (ROOT / "deploy/ltp/openreward-mopd-live-0723/run.sh").read_text()
    assert "SLIME_RELOAD_ROLLOUT_DATA_BEFORE_TRAIN" in run
    assert "requires SAVE_DEBUG_ROLLOUT_DATA=true" in run


def test_short_b32_temp0fix_canary_keeps_greedy_generation_explicit() -> None:
    config = yaml.safe_load(
        (BUNDLE / "config-train-z-fresh-i23-short-b32-fetch-r10-temp0fix.yaml").read_text()
    )
    env = config["envargs"]

    assert env["rollout_temperature"] == "0.0"
    assert env["reload_rollout_data_before_train"] == "true"
    assert env["packing_safety_margin"] == "0.90"
    assert env["data_pad_size_multiplier"] == "128"

    run = (ROOT / "deploy/ltp/openreward-mopd-live-0723/run.sh").read_text()
    assert 'ROLLOUT_TEMPERATURE="${ROLLOUT_TEMPERATURE:-0.0}"' in run
    assert '--rollout-temperature "$ROLLOUT_TEMPERATURE"' in run


def test_fresh_256k_b256_one_epoch_formal_bundle() -> None:
    config = yaml.safe_load(
        (BUNDLE / "config-b256-fresh-async-256k-e1.yaml").read_text()
    )
    job = config["jobargs"]
    env = config["envargs"]

    assert job["num_workers"] + 1 == int(env["nnodes"]) == 4
    assert int(env["actor_num_nodes"]) * int(env["actor_gpus_per_node"]) == 16
    assert int(env["rollout_num_gpus"]) == 16

    assert env["train_driver"] == "async"
    assert env["start_rollout_id"] == "1"
    assert env["num_rollout"] == "22"
    assert env["expected_iteration_override"] == "21"
    assert int(env["rollout_batch_size"]) == int(env["global_batch_size"]) == 256
    assert 21 * int(env["rollout_batch_size"]) == 5376

    assert int(env["seq_len"]) == 262_144
    assert int(env["context_parallel_size"]) == 8
    assert int(env["max_tokens_per_gpu"]) == 32_768
    assert int(env["context_parallel_size"]) * int(env["max_tokens_per_gpu"]) == int(
        env["seq_len"]
    )
    assert env["packing_safety_margin"] == "1.0"

    assert env["slime_root"].endswith("slime_v0.3.0_prof_qw36_moe_sync_r2")
    assert env["teacher_routes_file"].endswith("runtime-routes-r12-short34.json")
    assert env["validate_qwen35_fused_expert_sync"] == "true"
    assert env["qwen_gdn_backend"] == "fla"
    assert env["sglang_moe_runner_backend"] == "triton"
    assert env["rollout_temperature"] == "0.0"
    assert env["pure_opd"] == "true"


def test_reserved_cluster_b_formal_bundle_only_changes_lifecycle_paths() -> None:
    regular = yaml.safe_load(
        (BUNDLE / "config-b256-fresh-async-256k-e1.yaml").read_text()
    )
    book = yaml.safe_load(
        (BUNDLE / "config-b256-fresh-async-256k-e1-reserved-cluster-b-r1.yaml").read_text()
    )

    assert regular["jobargs"]["num_workers"] == book["jobargs"]["num_workers"] == 3
    assert regular["jobargs"]["image"] == book["jobargs"]["image"]
    assert regular["jobargs"]["job_name"] != book["jobargs"]["job_name"]

    lifecycle_keys = {
        "output_checkpoint",
        "run_root",
        "ray_head_port",
        "ray_dashboard_port",
    }
    regular_env = regular["envargs"]
    book_env = book["envargs"]
    for key in regular_env.keys() - lifecycle_keys:
        assert book_env[key] == regular_env[key]
    for key in lifecycle_keys:
        assert book_env[key] != regular_env[key]


def test_reserved_cluster_b_retry_uses_fresh_teacher_route_without_training_drift() -> None:
    first = yaml.safe_load(
        (BUNDLE / "config-b256-fresh-async-256k-e1-reserved-cluster-b-r1.yaml").read_text()
    )
    retry = yaml.safe_load(
        (BUNDLE / "config-b256-fresh-async-256k-e1-reserved-cluster-b-r2.yaml").read_text()
    )

    assert first["jobargs"]["num_workers"] == retry["jobargs"]["num_workers"] == 3
    assert first["jobargs"]["image"] == retry["jobargs"]["image"]
    assert first["jobargs"]["job_name"] != retry["jobargs"]["job_name"]

    retry_keys = {
        "teacher_routes_file",
        "output_checkpoint",
        "run_root",
        "ray_head_port",
        "ray_dashboard_port",
    }
    first_env = first["envargs"]
    retry_env = retry["envargs"]
    for key in first_env.keys() - retry_keys:
        assert retry_env[key] == first_env[key]
    for key in retry_keys:
        assert retry_env[key] != first_env[key]
    assert retry_env["teacher_routes_file"].endswith(
        "runtime-routes-book2-r13-short34.json"
    )

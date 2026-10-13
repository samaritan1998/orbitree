from pathlib import Path

from scripts.analyze_openreward_mopd_gpu_util import analyze


def test_analyze_separates_keeper_fill_from_idle_util(tmp_path: Path) -> None:
    keeper = tmp_path / "gpu_keeper"
    keeper.mkdir()
    (keeper / "node_0.log").write_text(
        "[GPU 0] util= 35.0% free= 170.0GiB duty=0.40 mode=FILL cooldown_left=0.0s\n"
        "[GPU 0] util= 92.0% free= 120.0GiB duty=0.00 mode=IDLE cooldown_left=5.0s\n",
        encoding="utf-8",
    )
    (tmp_path / "train.log").write_text(
        "[train-driver] entrypoint=train_async.py update_weights_interval=1\n"
        "perf 1: {'perf/rollout_time': 12.5}\n"
        "Timer actor_train end (elapsed: 8.0s)\n",
        encoding="utf-8",
    )

    result = analyze(tmp_path)

    assert result["driver"] == {"entrypoint": "train_async.py", "update_weights_interval": 1}
    assert result["keeper"]["fill_fraction"] == 0.5
    assert result["keeper"]["idle_mode_raw_util"]["mean"] == 92.0
    assert result["timers"]["paired_updates"] == 1
    assert result["timers"]["theoretical_overlap_speedup"] == 20.5 / 12.5

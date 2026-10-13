from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/slime_openreward_live_plugin.py"
SPEC = importlib.util.spec_from_file_location("slime_openreward_live_plugin", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


def test_miaoda_publishes_current_slime_policy_origin(
    tmp_path: Path, monkeypatch
) -> None:
    fake_slime = types.ModuleType("slime")
    fake_rollout = types.ModuleType("slime.rollout")
    fake_sglang = types.ModuleType("slime.rollout.sglang_rollout")
    fake_sglang.get_model_url = (
        lambda _args, _model, endpoint: f"http://198.51.100.8:31000{endpoint}"
    )
    monkeypatch.setitem(sys.modules, "slime", fake_slime)
    monkeypatch.setitem(sys.modules, "slime.rollout", fake_rollout)
    monkeypatch.setitem(sys.modules, "slime.rollout.sglang_rollout", fake_sglang)
    route = tmp_path / "policy-route.json"
    monkeypatch.setenv("SLIME_OPENREWARD_MANAGED_POLICY_ROUTE_FILE", str(route))
    monkeypatch.setenv("FULL_JOB_NAME", "persistent-b4096-test")
    plugin._PUBLISHED_POLICY_ROUTES.clear()

    sample = SimpleNamespace(
        metadata={"openreward_env": "miaoda", "openreward_mode": "managed"}
    )
    plugin._publish_managed_policy_route(SimpleNamespace(), sample)

    payload = json.loads(route.read_text())
    assert payload["status"] == "ready"
    assert payload["protocol"] == "slime-sglang-policy-route.v1"
    assert payload["upstream_url"] == "http://198.51.100.8:31000"
    assert route.stat().st_mode & 0o777 == 0o600


def test_non_miaoda_sample_does_not_publish_policy_route(
    tmp_path: Path, monkeypatch
) -> None:
    route = tmp_path / "policy-route.json"
    monkeypatch.setenv("SLIME_OPENREWARD_MANAGED_POLICY_ROUTE_FILE", str(route))
    sample = SimpleNamespace(
        metadata={"openreward_env": "tooluse", "openreward_mode": "interactive"}
    )
    plugin._publish_managed_policy_route(SimpleNamespace(), sample)
    assert not route.exists()


def test_one_candidate_fetch_gets_one_explicit_miaoda_optimizer_batch() -> None:
    samples = [
        SimpleNamespace(
            metadata={
                "openreward_env": domain,
                "openreward_mode": "managed" if domain == "miaoda" else "interactive",
                "openreward_pure_opd": True,
            }
        )
        for domain in ("miaoda", "swe", "miaoda", "ppt", "miaoda", "tooluse")
    ]
    groups = [[sample] for sample in samples]

    count = plugin._annotate_miaoda_optimizer_batch(
        groups,
        rollout_id=7,
        fetch_index=0,
        run_identity="formal-b256",
    )

    assert count == 3
    managed = [sample for sample in samples if sample.metadata["openreward_env"] == "miaoda"]
    batch_ids = {
        sample.metadata["openreward_optimizer_batch_id"] for sample in managed
    }
    assert len(batch_ids) == 1
    assert all(
        sample.metadata["openreward_optimizer_batch_expected_count"] == 3
        for sample in managed
    )
    assert [
        sample.metadata["openreward_optimizer_batch_member_index"]
        for sample in managed
    ] == [0, 1, 2]
    assert all(
        "openreward_optimizer_batch_id" not in sample.metadata
        for sample in samples
        if sample.metadata["openreward_env"] != "miaoda"
    )


def test_replacement_fetch_uses_a_new_explicit_batch_id() -> None:
    class Source:
        def get_samples(self, count):
            return [
                [
                    SimpleNamespace(
                        metadata={
                            "openreward_env": "miaoda",
                            "openreward_mode": "managed",
                            "openreward_pure_opd": True,
                        }
                    )
                ]
                for _ in range(count)
            ]

        def add_samples(self, samples):
            return samples

    source = plugin._OptimizerBatchDataSource(Source(), rollout_id=4)
    first = source.get_samples(2)
    second = source.get_samples(2)

    first_id = first[0][0].metadata["openreward_optimizer_batch_id"]
    second_id = second[0][0].metadata["openreward_optimizer_batch_id"]
    assert first_id != second_id
    assert all(
        group[0].metadata["openreward_optimizer_batch_expected_count"] == 2
        for group in first + second
    )

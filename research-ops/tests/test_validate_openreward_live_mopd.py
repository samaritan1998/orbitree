from scripts.validate_openreward_live_mopd import _weight_versions_changed


def test_weight_versions_changed_handles_two_rollouts() -> None:
    assert _weight_versions_changed(["0", "1"])
    assert not _weight_versions_changed(["0", "0"])


def test_weight_versions_changed_checks_each_adjacent_pair() -> None:
    assert _weight_versions_changed(["0", "1", "2"])
    assert not _weight_versions_changed(["0", "1", "1"])

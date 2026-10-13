from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check-public-release.py"
SPEC = importlib.util.spec_from_file_location("public_release_guard", SCRIPT)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def test_public_release_guard_allows_documentation_and_loopback_addresses():
    for address in ("127.0.0.1", "0.0.0.0", "192.0.2.5", "198.51.100.8", "203.0.113.9"):
        assert not guard.problems("example.md", address.encode(), [])


def test_public_release_guard_rejects_private_networks_and_local_runtime_files():
    address = ".".join(("10", "8", "4", "2")).encode()
    assert "private-network-address" in guard.problems("example.md", address, [])
    assert "runtime-or-credential-file" in guard.problems(".env", b"anything", [])
    assert "private-runtime-directory" in guard.problems("research-ops/data/result.json", b"{}", [])


def test_public_release_guard_supports_private_denylist_without_reporting_values():
    assert guard.problems("README.md", b"fictional-private-label", [b"private-label"]) == [
        "private-deny-term"
    ]


def test_public_release_guard_does_not_distribute_arbitrary_binary_data():
    assert guard.problems("model.safetensors", b"binary\x00data", []) == ["unreviewed-binary-file"]

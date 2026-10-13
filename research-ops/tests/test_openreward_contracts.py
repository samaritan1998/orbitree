from __future__ import annotations

import json
from pathlib import Path

import pytest
from research_ops.openreward import (
    BackendContractError,
    backend_readiness,
    environment_references,
    load_backend_contract,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = PROJECT_ROOT / "config" / "openreward-backends.example.yaml"


def test_real_backend_example_covers_all_four_domains_without_inline_values() -> None:
    contract = load_backend_contract(EXAMPLE)
    assert contract.version == 1
    assert contract.miaoda.adapter == "datawheel-agentos-goblin"
    assert contract.swe.adapter == "sandbox"
    assert contract.ppt.adapter == "office-runtime"
    assert contract.tooluse.adapter == "openreward-proxy"
    assert contract.tooluse.remote_environment == "toolathlongym"

    references = environment_references(contract)
    assert set(references) == {"miaoda", "swe", "ppt", "tooluse"}
    assert "DATAWHEEL_TOKEN" in references["miaoda"]
    assert "GOBLIN_REWARD_RESULT_ROOT" in references["miaoda"]
    assert "ORS_SWE_SANDBOX_URL" in references["swe"]
    assert "ORS_PPT_RUNTIME_URL" in references["ppt"]
    assert "ORS_TOOLUSE_SERVICE_URL" in references["tooluse"]
    assert all(
        name.isupper() and "=" not in name
        for names in references.values()
        for name in names
    )


def test_backend_readiness_reports_names_but_never_runtime_values() -> None:
    contract = load_backend_contract(EXAMPLE)
    references = environment_references(contract)
    runtime = {
        name: f"private-runtime-value-{index}"
        for index, name in enumerate(
            name for names in references.values() for name in names
        )
    }
    ready = backend_readiness(contract, environ=runtime)
    assert all(item["ready"] for item in ready.values())
    serialized = json.dumps(ready)
    assert "private-runtime-value" not in serialized

    missing = backend_readiness(contract, environ={})
    assert all(not item["ready"] for item in missing.values())
    assert "DATAWHEEL_TOKEN" in missing["miaoda"]["missing_env"]


def test_backend_contract_rejects_inline_or_invalid_environment_reference(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace(
            "datawheel_token_env: DATAWHEEL_TOKEN",
            "datawheel_token_env: inline-secret-value",
        ),
        encoding="utf-8",
    )
    with pytest.raises(BackendContractError):
        load_backend_contract(invalid)

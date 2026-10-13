from __future__ import annotations

import json
from pathlib import Path

from research_ops.openreward import build_slime_selector_rows, load_task_catalogs

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANARY_CATALOG = PROJECT_ROOT / "config" / "openreward-swe-canary.json"


def test_swe_selector_rows_exclude_server_side_runtime_and_verifier_data() -> None:
    catalog = load_task_catalogs(CANARY_CATALOG)
    rows = build_slime_selector_rows(
        catalog,
        environment="swe",
        split="train",
        mode="interactive",
    )

    assert len(rows) == 8
    assert rows[0] == {
        "text": catalog["swe"]["train"][0]["query"],
        "metadata": {
            "openreward_env": "swe",
            "openreward_mode": "interactive",
            "openreward_split": "train",
            "openreward_task_index": 0,
            "openreward_task_id": "astropy__astropy-13306",
        },
    }
    expected_metadata_keys = {
        "openreward_env",
        "openreward_mode",
        "openreward_split",
        "openreward_task_index",
        "openreward_task_id",
    }
    assert all(set(row) == {"text", "metadata"} for row in rows)
    assert all(set(row["metadata"]) == expected_metadata_keys for row in rows)
    serialized = json.dumps(rows, ensure_ascii=False)
    first_task = catalog["swe"]["train"][0]
    hidden_values = (
        first_task["base_commit"],
        first_task["gold_patch_sha256"],
        first_task["verifier"]["test_patch"],
    )
    assert all(value not in serialized for value in hidden_values)
    assert '"repo_ref"' not in serialized
    assert '"base_commit"' not in serialized
    assert '"sandbox"' not in serialized
    assert "verifier" not in serialized
    assert "test_patch" not in serialized

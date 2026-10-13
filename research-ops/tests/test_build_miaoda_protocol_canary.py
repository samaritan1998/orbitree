from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_miaoda_protocol_canary.py"
SPEC = importlib.util.spec_from_file_location("build_miaoda_protocol_canary", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def test_protocol_canary_repeats_clean_query_with_independent_ids() -> None:
    values = builder.build_canary_records(
        {
            "id": "record-one",
            "case_id": 6,
            "status": "completed",
            "delegate_arg": {
                "query": "[[ORS_MOPD_EPISODE:ep-00000000000000000000000000000001]]\n"
                "build a real app"
            },
        },
        batch_task_id="batch-source",
    )

    assert len(values) == 8
    assert {value["query"] for value in values} == {"build a real app"}
    assert len({value["id"] for value in values}) == 8
    assert [
        value["metadata"]["protocol_repeat_index"] for value in values
    ] == list(range(8))
    assert all(
        value["metadata"]["source_batch_id"] == "batch-source"
        for value in values
    )

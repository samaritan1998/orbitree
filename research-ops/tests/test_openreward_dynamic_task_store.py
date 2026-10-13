from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import duckdb

ROOT = Path(__file__).resolve().parents[1]
BUILD_PATH = ROOT / "scripts/build_openreward_dynamic_task_store.py"
RESOLVE_PATH = ROOT / "scripts/openreward_dynamic_tasks.py"
sys.path.insert(0, str(ROOT / "scripts"))


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _module("build_openreward_dynamic_task_store", BUILD_PATH)
resolver = _module("openreward_dynamic_tasks", RESOLVE_PATH)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _sources(tmp_path: Path) -> Path:
    miaoda = tmp_path / "miaoda.jsonl"
    miaoda.write_text(
        json.dumps(
            {
                "id": "miaoda-1",
                "query": "build an app",
                "metadata": {"split": "train"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    swe = tmp_path / "swe.parquet"
    execution = {
        "test_file_names": ["test_fix.py"],
        "test_file_codes": ["def test_fix(): assert True"],
    }
    expected = {"test_fix.py::test_fix": "passed"}
    connection = duckdb.connect()
    connection.execute(
        """
        CREATE TABLE swe_fixture AS
        SELECT 'repo' AS repo_name, 'r2e:test' AS docker_image,
               '0123456789abcdef' AS commit_hash, 'fix it' AS problem_statement,
               ? AS execution_result_content, ? AS expected_output_json
        """,
        [json.dumps(execution), json.dumps(expected)],
    )
    connection.table("swe_fixture").write_parquet(str(swe))
    connection.close()

    ppt_root = tmp_path / "ppt"
    ppt_task = ppt_root / "ppt-1" / "task.json"
    _write_json(
        ppt_task,
        {
            "task_id": "ppt-1",
            "query": "make slides",
            "benchmark_family": "ppt_online",
            "assets": [],
            "output_path": "/workspace/slides.pptx",
            "verifier": {
                "editable_pptx_required": True,
                "render_required": True,
            },
        },
    )

    tool_root = tmp_path / "toolathlon"
    tool_task = tool_root / "tasks/finalpool/tool-1"
    (tool_task / "docs").mkdir(parents=True)
    (tool_task / "preprocess").mkdir()
    (tool_task / "evaluation").mkdir()
    (tool_task / "docs/task.md").write_text("use the tool", encoding="utf-8")
    _write_json(tool_task / "task_config.json", {"needed_mcp_servers": ["canvas"]})
    (tool_task / "preprocess/main.py").write_text("pass\n", encoding="utf-8")
    (tool_task / "evaluation/main.py").write_text("pass\n", encoding="utf-8")

    manifest = tmp_path / "sources.json"
    _write_json(
        manifest,
        {
            "schema_version": "openreward.dynamic_sources.v1",
            "sources": {
                "miaoda": {
                    "kind": "miaoda_jsonl",
                    "path": str(miaoda),
                    "expected_count": 1,
                },
                "swe": {
                    "kind": "r2e_parquet",
                    "pattern": str(swe),
                    "image_prefix": "registry/",
                    "expected_count": 1,
                },
                "ppt": {
                    "kind": "ppt_task_bundles",
                    "root": str(ppt_root),
                    "expected_count": 1,
                },
                "tooluse": {
                    "kind": "toolathlon_finalpool",
                    "root": str(tool_root),
                    "repository": "EnvCommons/toolathlon-gym",
                    "revision": "",
                    "expected_count": 1,
                },
            },
        },
    )
    return manifest


def test_dynamic_store_contains_every_real_task_once_and_resolves_lazily(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "store"
    receipt = builder.build_store(
        _sources(tmp_path), output, training_objective="pure_opd"
    )

    assert receipt["counts"] == {"miaoda": 1, "swe": 1, "ppt": 1, "tooluse": 1}
    assert receipt["rows"] == 4
    prompts = [
        json.loads(line)
        for line in (output / "prompts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {row["metadata"]["openreward_env"] for row in prompts} == {
        "miaoda",
        "swe",
        "ppt",
        "tooluse",
    }
    assert all("openreward_task" not in row["metadata"] for row in prompts)

    monkeypatch.setenv("SLIME_OPENREWARD_TASK_STORE_ROOT", str(output))
    for index, row in enumerate(prompts):
        sample = SimpleNamespace(index=index, metadata=row["metadata"])
        if sample.metadata["openreward_env"] == "miaoda":
            sample.metadata.update(
                {
                    "openreward_optimizer_batch_id": "optimizer-r1",
                    "openreward_optimizer_batch_expected_count": 8,
                    "openreward_optimizer_batch_member_index": 3,
                }
            )
        assert resolver.materialize_task_reference(sample) is True
        assert sample.metadata["openreward_task"]["training_objective"] == "pure_opd"
        assert sample.metadata["openreward_task"]["task_reward_required"] is False
        assert sample.metadata["openreward_task_id"].endswith(f":online:{index}")
        if sample.metadata["openreward_env"] == "miaoda":
            assert sample.metadata["openreward_task"]["episode_id"].endswith(
                f":online:{index}"
            )
            assert sample.metadata["openreward_task"]["optimizer_batch_id"] == (
                "optimizer-r1"
            )
            assert (
                sample.metadata["openreward_task"]["optimizer_batch_expected_count"]
                == 8
            )
            assert (
                sample.metadata["openreward_task"]["optimizer_batch_member_index"]
                == 3
            )
        resolver.clear_materialized_task(sample)
        assert "openreward_task" not in sample.metadata


def test_dynamic_store_reuses_matching_complete_artifact(tmp_path: Path) -> None:
    manifest = _sources(tmp_path)
    output = tmp_path / "store"
    first = builder.build_store(manifest, output, training_objective="pure_opd")
    second = builder.build_store(
        manifest,
        output,
        training_objective="pure_opd",
        reuse_valid=True,
    )

    assert first["reused"] is False
    assert second["reused"] is True
    assert second["prompt_sha256"] == first["prompt_sha256"]


def test_dynamic_store_rebuilds_a_same_size_corrupt_artifact(tmp_path: Path) -> None:
    manifest = _sources(tmp_path)
    output = tmp_path / "store"
    first = builder.build_store(manifest, output, training_objective="pure_opd")
    task_store = output / "tasks.jsonl"
    original = task_store.read_bytes()
    task_store.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    rebuilt = builder.build_store(
        manifest,
        output,
        training_objective="pure_opd",
        reuse_valid=True,
    )

    assert rebuilt["reused"] is False
    assert rebuilt["task_store_sha256"] == first["task_store_sha256"]

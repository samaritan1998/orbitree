from __future__ import annotations

import json
from pathlib import Path

from scripts.run_ppt_trajectory_canary_sharded import (
    merge_images,
    merge_rows,
    partitions,
)


def test_partitions_are_contiguous_and_balanced() -> None:
    assert partitions(10, 3) == [(0, 4), (4, 3), (7, 3)]


def test_merge_rows_preserves_shard_and_row_order(tmp_path: Path) -> None:
    shard_dirs = []
    expected_ids = ["row-a", "row-b", "row-c"]
    cursor = 0
    for shard_index, shard_size in enumerate((2, 1)):
        shard = tmp_path / "shards" / f"part_{shard_index:02d}"
        shard.mkdir(parents=True)
        shard_dirs.append(shard)
        with (
            (shard / "data.jsonl").open("w", encoding="utf-8") as data_out,
            (shard / "rows_preview.jsonl").open("w", encoding="utf-8") as preview_out,
        ):
            for _ in range(shard_size):
                trajectory_id = expected_ids[cursor]
                data_out.write(json.dumps({"trajectory_id": trajectory_id}) + "\n")
                preview_out.write(
                    json.dumps(
                        {
                            "trajectory_id": trajectory_id,
                            "task_id": f"task-{cursor}",
                            "attempt_index": 1,
                        }
                    )
                    + "\n"
                )
                cursor += 1

    summaries, digest = merge_rows(shard_dirs, tmp_path)

    assert [row["trajectory_id"] for row in summaries] == expected_ids
    assert len(digest) == 64
    index = [json.loads(line) for line in (tmp_path / "row_index.jsonl").read_text().splitlines()]
    assert [row["trajectory_id"] for row in index] == expected_ids
    assert index[0]["offset"] == 0
    assert index[1]["offset"] == index[0]["length"]


def test_merge_images_deduplicates_by_digest_filename(tmp_path: Path) -> None:
    shard_dirs = []
    for index in range(2):
        shard = tmp_path / "shards" / f"part_{index:02d}"
        image_dir = shard / "assets" / "images"
        image_dir.mkdir(parents=True)
        (image_dir / "same-digest.png").write_bytes(b"same-image")
        shard_dirs.append(shard)

    count = merge_images(shard_dirs, tmp_path)

    assert count == 1
    assert (tmp_path / "assets" / "images" / "same-digest.png").read_bytes() == b"same-image"

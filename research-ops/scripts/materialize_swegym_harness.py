#!/usr/bin/env python3
"""Materialize the pinned SWE-Gym verifier source for the K8s ORS runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "swe_gym_harness"
REVISION = "242429c188fcfd06aad13fce9a54d450470bf0ac"
ARCHIVE_SHA256 = "23d23f75d52cd66d0d5cf73ac0963da01674aeb3684a5995d6ffb130e2a1bab1"
ARCHIVE_URL = f"https://codeload.github.com/SWE-Gym/SWE-Bench-Fork/tar.gz/{REVISION}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(name: str) -> bool:
    parts = Path(name).parts
    return bool(parts) and all(part not in {"", ".", ".."} for part in parts)


def materialize(
    output: Path,
    *,
    force: bool = False,
    archive_path: Path | None = None,
) -> dict[str, object]:
    output = output.expanduser().resolve()
    marker = output / ".source_revision"
    if (
        output.exists()
        and marker.is_file()
        and marker.read_text(encoding="utf-8").strip() == REVISION
    ):
        return {"status": "already_present", "output": str(output), "revision": REVISION}
    if output.exists() and not force:
        raise RuntimeError(f"refusing to replace existing harness: {output}")

    with tempfile.TemporaryDirectory(prefix="swegym-harness-") as temporary:
        archive = Path(temporary) / "source.tar.gz"
        if archive_path is not None:
            shutil.copy2(archive_path.expanduser().resolve(strict=True), archive)
        else:
            request = urllib.request.Request(
                ARCHIVE_URL,
                headers={"User-Agent": "research-ops-openreward"},
            )
            with urllib.request.urlopen(request, timeout=120) as response, archive.open(
                "wb"
            ) as stream:
                shutil.copyfileobj(response, stream)
        if _sha256(archive) != ARCHIVE_SHA256:
            raise RuntimeError("SWE-Gym harness archive hash does not match the pinned source")
        extracted = Path(temporary) / "extracted"
        extracted.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            if any(not _safe_member(member.name) for member in members):
                raise RuntimeError("SWE-Gym harness archive contains an unsafe path")
            bundle.extractall(extracted)
        roots = list(extracted.glob("SWE-Bench-Fork-*/swebench"))
        if len(roots) != 1:
            raise RuntimeError("SWE-Gym harness archive has an unexpected layout")
        source = roots[0]
        staged = Path(temporary) / "staged"
        staged.mkdir()
        shutil.copytree(source, staged / "swebench")
        license_path = source.parent / "LICENSE"
        if license_path.is_file():
            shutil.copy2(license_path, staged / "LICENSE")
        (staged / ".source_revision").write_text(REVISION + "\n", encoding="utf-8")
        manifest = {
            "schema_version": "openreward.swegym_harness.v1",
            "revision": REVISION,
            "archive_url": ARCHIVE_URL,
            "archive_sha256": ARCHIVE_SHA256,
            "source_license": "MIT",
        }
        (staged / "MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            shutil.rmtree(output)
        shutil.move(str(staged), str(output))
    return {"status": "materialized", "output": str(output), "revision": REVISION}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            materialize(args.output, force=args.force, archive_path=args.archive),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

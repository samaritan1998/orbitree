#!/usr/bin/env python3
"""Validate Qwen3.5/Qwen3.6 fused-MoE online weight conversion."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import types
from pathlib import Path

import torch


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    converter_path = (
        args.slime_root
        / "slime/backends/megatron_utils/megatron_to_hf/qwen3_5.py"
    )
    updater_path = (
        args.slime_root
        / "slime/backends/megatron_utils/update_weight/update_weight_from_distributed.py"
    )
    spec = importlib.util.spec_from_file_location("qwen3_5_sync_validation", converter_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load converter: {converter_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    model_args = types.SimpleNamespace(
        hidden_size=8,
        moe_ffn_hidden_size=2,
        kv_channels=2,
        num_attention_heads=4,
        num_query_groups=2,
    )
    source_shards = []
    for ep_rank in range(8):
        source = torch.full((1, 2, 8), float(ep_rank))
        source_shards.extend(
            module.convert_qwen3_5_to_hf(
                model_args,
                "module.module.decoder.layers.0.mlp.experts.linear_fc2",
                source,
            )
        )
    converted = module.coalesce_qwen3_5_fused_expert_tensors(source_shards)
    if len(converted) != 1:
        raise RuntimeError(f"expected one coalesced FC2 tensor, got {len(converted)}")
    name, tensor = converted[0]
    expected_name = "model.language_model.layers.0.mlp.experts.down_proj"
    if name != expected_name or tuple(tensor.shape) != (8, 8, 2):
        raise RuntimeError(f"unexpected coalesced FC2: name={name}, shape={tuple(tensor.shape)}")
    for ep_rank in range(8):
        if not torch.all(tensor[ep_rank] == float(ep_rank)):
            raise RuntimeError(f"EP rank ordering changed at rank {ep_rank}")

    updater_source = updater_path.read_text(encoding="utf-8")
    hook = "coalesce_qwen3_5_fused_expert_tensors(converted_hf_tensors)"
    if hook not in updater_source:
        raise RuntimeError("distributed updater does not call the Qwen3.5 EP coalescer")

    receipt = {
        "schema_version": "qwen3_5.fused_expert_sync_validation.v1",
        "status": "passed",
        "source_ep_shards": 8,
        "source_shape_per_rank": [1, 2, 8],
        "coalesced_shape": list(tensor.shape),
        "converted_name": name,
        "converter_sha256": _sha256(converter_path),
        "updater_sha256": _sha256(updater_path),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()

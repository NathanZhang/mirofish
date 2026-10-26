"""Explicit provisioning; the runtime never downloads model code or weights."""
import json
from pathlib import Path
from huggingface_hub import HfApi, snapshot_download

if __name__ == "__main__":
    revision = HfApi().model_info("BAAI/bge-m3").sha
    target = Path(__file__).resolve().parents[2] / "data/graphiti-local/bge-m3"
    snapshot_download("BAAI/bge-m3", revision=revision, local_dir=target,
                      allow_patterns=["config.json", "tokenizer*", "special_tokens_map.json",
                                      "sentencepiece.bpe.model", "model.safetensors", "pytorch_model.bin"])
    (target / "provenance.json").write_text(json.dumps({
        "model": "BAAI/bge-m3", "revision": revision, "dimension": 1024,
        "pooling": "cls", "normalize": "l2", "device": "cpu",
    }, indent=2))
    print(f"Downloaded {revision} to {target}")

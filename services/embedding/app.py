"""CPU BGE-M3 dense embeddings. Downloads are an explicit setup step only."""
import os
from contextlib import asynccontextmanager
from pathlib import Path

import torch
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from transformers import AutoModel, AutoTokenizer

MODEL = "BAAI/bge-m3"
MAX_TOKENS = 8192


@asynccontextmanager
async def lifespan(app):
    path = Path(os.environ["EMBEDDING_MODEL_PATH"])
    torch.set_num_threads(int(os.environ.get("EMBEDDING_CPU_THREADS", "4")))
    app.state.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
    app.state.model = AutoModel.from_pretrained(path, local_files_only=True).cpu().eval()
    yield
    del app.state.model


app = FastAPI(lifespan=lifespan)


class EmbeddingRequest(BaseModel):
    model: str
    input: str | list[str]
    encoding_format: str = "float"


@app.get("/health")
def health():
    return {"ready": hasattr(app.state, "model"), "model": MODEL, "dimension": 1024}


@app.post("/v1/embeddings")
def embeddings(body: EmbeddingRequest, authorization: str = Header(default="")):
    token = os.environ.get("GRAPH_EMBEDDING_API_KEY", "")
    if not token or authorization != f"Bearer {token}":
        raise HTTPException(401, "Invalid embedding credential")
    if body.model != MODEL or body.encoding_format != "float":
        raise HTTPException(400, "Expected BAAI/bge-m3 and float encoding")
    texts = [body.input] if isinstance(body.input, str) else body.input
    if not texts or len(texts) > 16 or any(not s.strip() for s in texts):
        raise HTTPException(400, "Expected 1–16 nonempty strings")
    tokenizer = app.state.tokenizer
    # Reject instead of silently truncating source evidence.
    encoded = tokenizer(texts, padding=True, truncation=False, return_tensors="pt")
    if encoded["input_ids"].shape[1] > MAX_TOKENS:
        raise HTTPException(413, "Input exceeds 8192 tokens; split the source")
    with torch.inference_mode():
        vectors = app.state.model(**encoded).last_hidden_state[:, 0]
        vectors = torch.nn.functional.normalize(vectors, p=2, dim=1)
    if vectors.shape[1] != 1024 or not torch.isfinite(vectors).all():
        raise HTTPException(503, "Invalid embedding output")
    return {"object": "list", "model": MODEL,
            "data": [{"object": "embedding", "index": i, "embedding": row}
                     for i, row in enumerate(vectors.tolist())],
            "usage": {"prompt_tokens": int(encoded["attention_mask"].sum()),
                      "total_tokens": int(encoded["attention_mask"].sum())}}

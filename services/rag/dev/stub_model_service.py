"""DEV ONLY: a fake model-service so rag-service can run end to end before the real one exists.

- /v1/embed uses the same free hashing embedder as ingest (fake-hash-v1), so its vectors match the
  current placeholder index in index/ and vector search returns sensible chunks.
- /v1/chat does not call any LLM. It echoes what it received, so you can check the prompt rag built.

Run from the repo root:
    uvicorn services.rag.dev.stub_model_service:app --port 8082
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

from fastapi import FastAPI, Response
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))   # repo root, for `ingest`
from ingest.embed import FakeEmbedder, l2_normalize  # noqa: E402

POD_NAME = os.environ.get("POD_NAME") or f"stub-model-service@{socket.gethostname()}"
embedder = FakeEmbedder(model="fake-hash-v1", dim=256)
app = FastAPI(title="model-service (stub)")


class EmbedRequest(BaseModel):
    input: str


class ChatRequest(BaseModel):
    messages: list[dict]
    temperature: float = 0.2


@app.post("/v1/embed")
def embed(body: EmbedRequest, response: Response):
    response.headers["X-Served-By"] = POD_NAME
    vec = l2_normalize(embedder.embed([body.input]))[0]
    return {"embedding": vec.tolist(), "model": embedder.model}


@app.post("/v1/chat")
def chat(body: ChatRequest, response: Response):
    response.headers["X-Served-By"] = POD_NAME
    user = body.messages[-1]["content"]
    return {"answer": f"[STUB ANSWER - no LLM was called]\nThe prompt rag-service sent was:\n\n{user}",
            "model": "stub"}

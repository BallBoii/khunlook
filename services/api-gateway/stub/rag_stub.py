"""Fake rag-service. LOCAL TESTING ONLY: used by deploy/docker/compose.yaml, never deployed.

Stands in for rag-service, vector-db and model-service so the frontend and gateway can be
tested on a laptop without the RAG stack or the internet. It follows the agreed contract:

    POST /v1/answer  {"question": ...}  ->  {"answer", "sources", "served_by"}
    GET  /v1/trace                      ->  {"ok": true, "served_by"}

`served_by` invents vector-db and model-service pod names, so the page shows a full 5-pod chain.

Test hooks:  a question containing "midterm" -> "not covered" answer with no sources
             a question containing "fail"    -> 500 error (tests the page's error message)
Env:         STUB_DELAY_S (default 1.5) = how long to "think" before answering
"""
from __future__ import annotations

import asyncio
import os
import random
import socket

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

POD = os.environ.get("POD_NAME") or socket.gethostname()
DELAY_S = float(os.environ.get("STUB_DELAY_S", "1.5"))
app = FastAPI(title="rag-service stub")


def fake_pod(name: str) -> str:
    return f"{name}-stub-{random.choice(['a1b2', 'c3d4'])}"


def chain() -> list[dict]:
    return [{"service": "rag-service", "pod": POD},
            {"service": "vector-db", "pod": fake_pod("vector-db")},
            {"service": "model-service", "pod": fake_pod("model-service")}]


class Question(BaseModel):
    question: str
    top_k: int | None = None


@app.middleware("http")
async def served_by(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Served-By"] = POD
    return response


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/v1/answer")
async def answer(q: Question):
    await asyncio.sleep(DELAY_S)  # pretend the LLM is thinking
    if "midterm" in q.question.lower():
        return {"answer": "That isn't covered in the course slides.", "sources": [], "served_by": chain()}
    if "fail" in q.question.lower():
        return JSONResponse({"error": "model_unavailable", "detail": "stub: simulated LLM outage"}, status_code=500)
    return {
        "answer": ("(stub) The on-site demo is on November 17, 2026, 13:00-16:00. "
                   "Have the demo set up before your time slot, then return all equipment afterwards."),
        "sources": [
            {"doc_id": "sds-final-project-2026", "doc_title": "SDS Final Project", "page": 10,
             "chunk_id": "stub-1", "score": 0.83},
            {"doc_id": "sds-final-project-2026", "doc_title": "SDS Final Project", "page": 11,
             "chunk_id": "stub-2", "score": 0.71},
        ],
        "served_by": chain(),
    }


@app.get("/v1/trace")
def trace():
    return {"ok": True, "served_by": chain()}

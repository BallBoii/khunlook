"""rag-service: question in, grounded answer out.

    POST /v1/ask     {"question": "When is the demo?"}
    POST /v1/answer  same, but `served_by` is the [{service, pod}] list api-gateway and the frontend expect
    GET  /v1/trace   cheap end-to-end check (vector-db + model-service readiness, no LLM call)
      1. embed the question             -> model-service  /v1/embed   (placeholder contract)
      2. search for similar chunks      -> vector-db      /v1/search
      3. build the prompt (chunks + question)
      4. ask the LLM                    -> model-service  /v1/chat    (placeholder contract)
      5. return the answer + sources + which pods served each step

Stateless: any replica can answer any request, so a pod lost with its Pi is simply replaced.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .clients import ModelServiceClient, UpstreamError, VectorDBClient
from .prompt import build_messages, select_context

POD_NAME = os.environ.get("POD_NAME") or socket.gethostname()
log = logging.getLogger("rag")


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(os.environ.get("LOG_LEVEL", "info").upper())


def log_json(level: int, **fields) -> None:
    log.log(level, json.dumps({"ts": round(time.time(), 3), "pod": POD_NAME, **fields}, ensure_ascii=False))


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


class APIError(Exception):
    def __init__(self, status: int, error: str, detail: str, **extra):
        self.status, self.error, self.detail, self.extra = status, error, detail, extra


# ---------------------------------------------------------------- models

class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(None, ge=1, le=20)


# ---------------------------------------------------------------- app

def make_http_client() -> httpx.AsyncClient:
    """One pooled client for the pod's lifetime. Tests replace this to fake the other services."""
    return httpx.AsyncClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    app.state.http = make_http_client()
    app.state.model = ModelServiceClient(app.state.http)
    app.state.vdb = VectorDBClient(app.state.http)
    log_json(logging.INFO, event="started", model_service_url=app.state.model.base_url,
             vector_db_url=app.state.vdb.base_url)
    yield
    await app.state.http.aclose()


app = FastAPI(title="rag-service", version="1.0.0", lifespan=lifespan)


def error_response(status: int, error: str, detail: str, **extra) -> JSONResponse:
    return JSONResponse({"error": error, "detail": detail, **extra}, status_code=status)


@app.exception_handler(APIError)
async def _api_error(_: Request, e: APIError):
    return error_response(e.status, e.error, e.detail, **e.extra)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, e: RequestValidationError):
    detail = "; ".join(f"{'.'.join(str(p) for p in err['loc'][1:]) or 'body'}: {err['msg']}" for err in e.errors())
    return error_response(400, "invalid_request", detail)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_: Request, e: StarletteHTTPException):
    return error_response(e.status_code, "not_found" if e.status_code == 404 else "http_error", str(e.detail))


@app.middleware("http")
async def served_by(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id
    request.state.metrics = {}
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as e:  # keep the headers + error format even on bugs
        log_json(logging.ERROR, event="unhandled_error", request_id=request_id, error=repr(e))
        response = error_response(500, "internal_error", "unexpected server error")
    response.headers["X-Served-By"] = POD_NAME
    response.headers["X-Request-ID"] = request_id
    path = request.url.path
    log_json(logging.DEBUG if path in ("/healthz", "/readyz") else logging.INFO,
             request_id=request_id, endpoint=f"{request.method} {path}", status=response.status_code,
             latency_ms=round((time.perf_counter() - t0) * 1000, 2), **request.state.metrics)
    return response


# ---------------------------------------------------------------- routes

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    # Deliberately does NOT check vector-db/model-service: if they are down, marking every rag pod
    # unready too would only hide the real failure. /v1/ask reports it with a clear 502 instead.
    return {"status": "ready"}


def _source(n: int, hit: dict) -> dict:
    return {"n": n, "id": hit["id"], "doc_id": hit["doc_id"], "doc_title": hit["doc_title"],
            "page": hit["page"], "score": hit["score"], "text": hit["text"]}


@app.post("/v1/ask")
async def ask(body: AskRequest, request: Request):
    rid = request.state.request_id
    model: ModelServiceClient = request.app.state.model
    vdb: VectorDBClient = request.app.state.vdb
    top_k = body.top_k or env_int("TOP_K", 5)
    served_by = {"rag-service": POD_NAME}
    timings: dict[str, float] = {}

    def lap(step: str, t0: float) -> None:
        timings[step] = round((time.perf_counter() - t0) * 1000, 1)

    # 1+2. Retrieve. Normal path: embed the question, then vector search.
    #      If model-service can't embed, fall back to keyword (BM25) search so we still find context.
    retrieval = "vector"
    try:
        t0 = time.perf_counter()
        vector = await model.embed(body.question, rid, served_by)
        lap("embed_ms", t0)
        t0 = time.perf_counter()
        hits = await vdb.search(vector, top_k, env_float("MIN_SCORE", 0.0), rid, served_by)
        lap("search_ms", t0)
    except UpstreamError as e:
        if e.service == "vector-db" and e.error == "dimension_mismatch":
            raise APIError(502, "embedding_mismatch",
                           f"model-service's embedding model does not match the index in vector-db ({e.detail})")
        if e.service == "vector-db" or os.environ.get("KEYWORD_FALLBACK", "true").lower() != "true":
            raise APIError(502, f"{e.service.replace('-', '_')}_unavailable", e.detail)
        log_json(logging.WARNING, event="embed_failed_using_keyword_search", request_id=rid, reason=e.detail)
        retrieval = "keyword"
        try:
            t0 = time.perf_counter()
            hits = await vdb.keyword_search(body.question, top_k, rid, served_by)
            lap("search_ms", t0)
        except UpstreamError as e2:
            raise APIError(502, f"{e2.service.replace('-', '_')}_unavailable", e2.detail)

    # 3. Build the prompt from the best chunks that fit the budget.
    hits = select_context(hits, env_int("MAX_CONTEXT_CHARS", 6000))
    messages = build_messages(body.question, hits)
    sources = [_source(n, h) for n, h in enumerate(hits, start=1)]

    # 4. Ask the LLM. If it fails, still return what we found so the client can show the passages.
    try:
        t0 = time.perf_counter()
        answer = await model.chat(messages, rid, served_by)
        lap("chat_ms", t0)
    except UpstreamError as e:
        raise APIError(502, "model_service_unavailable", e.detail, sources=sources, served_by=served_by)

    request.state.metrics = {"retrieval": retrieval, "num_sources": len(sources),
                             "best_score": sources[0]["score"] if sources else None, **timings}
    # 5. Respond to the API gateway.
    return {"answer": answer, "sources": sources, "retrieval": retrieval,
            "served_by": served_by, "timings": timings, "request_id": rid}


# ---------------------------------------------------------------- api-gateway contract

CHAIN_ORDER = {"rag-service": 0, "vector-db": 1, "model-service": 2}


def to_chain(served_by: dict) -> list[dict]:
    """{"model-service (embed)": pod, ...} -> [{"service", "pod"}, ...] in request order, no duplicates."""
    chain: list[dict] = []
    for key, pod in served_by.items():
        hop = {"service": key.split(" (")[0], "pod": pod}
        if hop not in chain:
            chain.append(hop)
    return sorted(chain, key=lambda h: CHAIN_ORDER.get(h["service"], 99))


@app.post("/v1/answer")
async def answer(body: AskRequest, request: Request):
    result = await ask(body, request)
    return {**result, "served_by": to_chain(result["served_by"])}


@app.get("/v1/trace")
async def trace(request: Request):
    """Follows the same path as a question but never calls the LLM (model-service /readyz only checks its key)."""
    rid = request.state.request_id
    timeout = env_float("TRACE_TIMEOUT_S", 2)
    targets = {"vector-db": request.app.state.vdb.base_url, "model-service": request.app.state.model.base_url}

    async def probe(service: str, base: str) -> tuple[dict, str | None]:
        try:
            r = await request.app.state.http.get(base + "/readyz", headers={"X-Request-ID": rid}, timeout=timeout)
        except httpx.HTTPError as e:
            return {"service": service, "pod": None}, f"{service}: {type(e).__name__}"
        hop = {"service": service, "pod": r.headers.get("x-served-by")}
        return hop, None if r.status_code == 200 else f"{service}: HTTP {r.status_code}"

    probes = await asyncio.gather(*(probe(svc, base) for svc, base in targets.items()))
    chain = [{"service": "rag-service", "pod": POD_NAME}] + [h for h, _ in probes if h["pod"]]
    problems = [p for _, p in probes if p]
    ok = not problems
    return JSONResponse({"ok": ok, "served_by": sorted(chain, key=lambda h: CHAIN_ORDER[h["service"]]),
                         **({"detail": "; ".join(problems)} if problems else {})}, status_code=200 if ok else 502)

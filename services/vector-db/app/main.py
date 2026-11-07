"""vector-db: read-only semantic + keyword search over the index baked into the image."""
from __future__ import annotations

import json
import logging
import math
import os
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .search import IndexValidationError, load_index

POD_NAME = os.environ.get("POD_NAME") or socket.gethostname()
log = logging.getLogger("vector-db")


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(os.environ.get("LOG_LEVEL", "info").upper())


def log_json(level: int, **fields) -> None:
    log.log(level, json.dumps({"ts": round(time.time(), 3), "pod": POD_NAME, **fields}, ensure_ascii=False))


class APIError(Exception):
    def __init__(self, status: int, error: str, detail: str):
        self.status, self.error, self.detail = status, error, detail


# ---------------------------------------------------------------- models

class SearchRequest(BaseModel):
    vector: list[float] = Field(min_length=1)
    top_k: int = Field(4, ge=1)
    min_score: float = 0.0
    doc_ids: list[str] | None = None


class KeywordRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(4, ge=1)
    doc_ids: list[str] | None = None


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    app.state.index, app.state.not_ready_reason = None, "loading"
    index_dir = os.environ.get("INDEX_DIR", "/app/index")
    t0 = time.perf_counter()
    try:
        app.state.index = load_index(index_dir)
        app.state.not_ready_reason = None
        m = app.state.index.manifest
        log_json(logging.INFO, event="index_loaded", index_dir=index_dir, index_version=m["index_version"],
                 embedding_model=m["embedding_model"], num_chunks=m["num_chunks"],
                 load_ms=round((time.perf_counter() - t0) * 1000, 1))
    except IndexValidationError as e:  # stay up but never ready: don't serve wrong data
        app.state.not_ready_reason = str(e)
        log_json(logging.ERROR, event="index_invalid", index_dir=index_dir, reason=str(e))
    yield


app = FastAPI(title="vector-db", version="1.0.0", lifespan=lifespan)


def max_top_k() -> int:
    return int(os.environ.get("MAX_TOP_K", "20"))


def get_index(request: Request):
    idx = request.app.state.index
    if idx is None:
        raise APIError(503, "not_ready", f"index not loaded: {request.app.state.not_ready_reason}")
    return idx


def error_response(status: int, error: str, detail: str) -> JSONResponse:
    return JSONResponse({"error": error, "detail": detail}, status_code=status)


@app.exception_handler(APIError)
async def _api_error(_: Request, e: APIError):
    return error_response(e.status, e.error, e.detail)


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


def _results(request: Request, idx, hits: list[tuple[int, float]], method: str, top_k: int) -> dict:
    request.state.metrics = {"top_k": top_k, "num_results": len(hits),
                             "best_score": round(hits[0][1], 4) if hits else None}
    fields = ("doc_id", "doc_title", "source_file", "page", "chunk_index", "text")
    return {"method": method, "index_version": idx.manifest["index_version"],
            "results": [{"id": idx.chunks[i]["id"], "score": round(s, 6), **{k: idx.chunks[i][k] for k in fields}}
                        for i, s in hits]}


# ---------------------------------------------------------------- routes

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz(request: Request):
    idx = get_index(request)
    return {"status": "ready", "index_version": idx.manifest["index_version"]}


@app.get("/v1/info")
def info(request: Request):
    idx = get_index(request)
    m = idx.manifest
    return {"index_version": m["index_version"], "embedding_model": m["embedding_model"],
            "embedding_dim": m["embedding_dim"], "num_chunks": m["num_chunks"], "built_at": m.get("built_at"),
            "documents": idx.documents()}


@app.post("/v1/search")
def search(body: SearchRequest, request: Request):
    idx = get_index(request)
    if len(body.vector) != idx.dim:
        raise APIError(400, "dimension_mismatch", f"vector has {len(body.vector)} dims, index expects {idx.dim}")
    if not all(math.isfinite(x) for x in body.vector) or not any(body.vector):
        raise APIError(400, "invalid_vector", "vector must be finite and non-zero")
    top_k = min(body.top_k, max_top_k())
    return _results(request, idx, idx.vector_search(body.vector, top_k, body.min_score, body.doc_ids), "vector", top_k)


@app.post("/v1/search/keyword")
def keyword_search(body: KeywordRequest, request: Request):
    idx = get_index(request)
    top_k = min(body.top_k, max_top_k())
    return _results(request, idx, idx.keyword_search(body.query, top_k, body.doc_ids), "keyword", top_k)


@app.get("/v1/chunks/{chunk_id}")
def get_chunk(chunk_id: str, request: Request):
    idx = get_index(request)
    if chunk_id not in idx.by_id:
        raise APIError(404, "not_found", f"no chunk with id {chunk_id}")
    return idx.chunks[idx.by_id[chunk_id]]

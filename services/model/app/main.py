"""model-service: the only service that talks to OpenRouter. rag-service calls it for
  POST /v1/embed  question text -> embedding vector   (baai/bge-m3 by default)
  POST /v1/chat   prompt messages -> LLM answer       (google/gemma-3-4b-it by default)
Stateless, so any replica on any Pi can serve any request.
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
from typing import Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .openrouter import OpenRouter, UpstreamError, l2_normalize

POD_NAME = os.environ.get("POD_NAME") or socket.gethostname()
log = logging.getLogger("model-service")


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


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


class Settings:
    def __init__(self):
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        self.embed_model = os.environ.get("EMBED_MODEL", "baai/bge-m3")
        self.embed_dim = int(os.environ.get("EMBED_DIM", "1024"))  # 0 = don't check
        self.chat_model = os.environ.get("CHAT_MODEL", "google/gemma-3-4b-it")
        self.embed_timeout = env_float("EMBED_TIMEOUT_S", 8)     # rag waits 10 s
        self.chat_timeout = env_float("CHAT_TIMEOUT_S", 55)      # rag waits 60 s
        self.max_retries = int(os.environ.get("MAX_RETRIES", "2"))
        self.max_concurrency = int(os.environ.get("MAX_CONCURRENCY", "8"))
        self.max_input_chars = int(os.environ.get("MAX_INPUT_CHARS", "8000"))
        self.max_tokens_cap = int(os.environ.get("MAX_TOKENS_CAP", "1024"))
        self.referer = os.environ.get("OPENROUTER_REFERER", "")
        self.title = os.environ.get("OPENROUTER_TITLE", "khunlook")


# ---------------------------------------------------------------- models

class EmbedRequest(BaseModel):
    input: str | list[str]


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)
    temperature: float = Field(0.2, ge=0, le=2)
    max_tokens: int | None = Field(None, ge=1)


# ---------------------------------------------------------------- app

def make_http_client(timeout: float) -> httpx.AsyncClient:  # tests replace this with a MockTransport client
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    s = app.state.settings = Settings()
    app.state.http = make_http_client(s.chat_timeout)
    app.state.router = OpenRouter(app.state.http, s.api_key, s.base_url, s.max_retries, s.referer, s.title)
    app.state.slots = asyncio.Semaphore(s.max_concurrency)
    log_json(logging.INFO if s.api_key else logging.ERROR, event="started", embed_model=s.embed_model,
             chat_model=s.chat_model, api_key_set=bool(s.api_key))
    yield
    await app.state.http.aclose()


app = FastAPI(title="model-service", version="1.0.0", lifespan=lifespan)


def error_response(status: int, error: str, detail: str) -> JSONResponse:
    return JSONResponse({"error": error, "detail": detail}, status_code=status)


@app.exception_handler(APIError)
async def _api_error(_: Request, e: APIError):
    return error_response(e.status, e.error, e.detail)


@app.exception_handler(UpstreamError)
async def _upstream_error(_: Request, e: UpstreamError):
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


def require_key(request: Request) -> Settings:
    s: Settings = request.app.state.settings
    if not s.api_key:
        raise APIError(503, "not_configured", "OPENROUTER_API_KEY is not set")
    return s


# ---------------------------------------------------------------- routes

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz(request: Request):
    s = require_key(request)
    return {"status": "ready", "embed_model": s.embed_model, "chat_model": s.chat_model}


@app.get("/v1/info")
def info(request: Request):
    s: Settings = request.app.state.settings
    return {"embed_model": s.embed_model, "embed_dim": s.embed_dim, "chat_model": s.chat_model}


@app.post("/v1/embed")
async def embed(body: EmbedRequest, request: Request):
    """One string -> {"embedding": [...]}; a list of strings -> {"embeddings": [[...], ...]}. L2-normalized."""
    s = require_key(request)
    single = isinstance(body.input, str)
    texts = [body.input] if single else body.input
    if not texts or any(not t.strip() for t in texts):
        raise APIError(400, "invalid_request", "input must be non-empty text")
    if sum(len(t) for t in texts) > s.max_input_chars:
        raise APIError(400, "input_too_long", f"input exceeds {s.max_input_chars} characters")
    request.state.metrics = {"model": s.embed_model, "num_inputs": len(texts)}
    async with request.app.state.slots:
        vecs = await request.app.state.router.embed(s.embed_model, texts, s.embed_timeout)
    if s.embed_dim and any(len(v) != s.embed_dim for v in vecs):
        raise APIError(502, "dimension_mismatch",
                       f"{s.embed_model} returned {len(vecs[0])} dims, expected {s.embed_dim} (EMBED_DIM)")
    vecs = [l2_normalize(v) for v in vecs]
    out = {"model": s.embed_model, "dim": len(vecs[0])}
    return {**out, "embedding": vecs[0]} if single else {**out, "embeddings": vecs}


@app.post("/v1/chat")
async def chat(body: ChatRequest, request: Request):
    s = require_key(request)
    if sum(len(m.content) for m in body.messages) > s.max_input_chars * 4:
        raise APIError(400, "input_too_long", "messages are too long")
    max_tokens = min(body.max_tokens or s.max_tokens_cap, s.max_tokens_cap)
    request.state.metrics = {"model": s.chat_model, "num_messages": len(body.messages)}
    async with request.app.state.slots:
        answer, usage = await request.app.state.router.chat(
            s.chat_model, [m.model_dump() for m in body.messages], body.temperature, max_tokens, s.chat_timeout)
    request.state.metrics.update(prompt_tokens=usage.get("prompt_tokens"),
                                 completion_tokens=usage.get("completion_tokens"))
    return {"answer": answer, "model": s.chat_model, "usage": usage}

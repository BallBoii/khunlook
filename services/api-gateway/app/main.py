"""api-gateway: the single entry point for the chat API.

Request flow:
    browser -> frontend (nginx) -> api-gateway -> rag-service -> vector-db / model-service

The gateway does not answer questions itself. It adds the policy around rag-service:

    validate the question  ->  rate limit  ->  cap requests in flight
    ->  call rag-service (2 s connect timeout, one retry if the connection fails)
    ->  add this pod to the `served_by` chain  ->  return answer + sources + chain

It keeps no state that matters (limits live in memory), so any replica on any Pi can serve any
request, and a replacement pod after a pulled Pi is identical from the start.

Endpoints: POST /api/v1/chat, GET /api/v1/trace, GET /api/v1/info, GET /healthz, GET /readyz.
Settings come from environment variables (see Settings and deploy/k8s/api-gateway/api-gateway.yaml).
"""
from __future__ import annotations

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
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

VERSION = "1.0.0"
SERVICE = "api-gateway"
POD_NAME = os.environ.get("POD_NAME") or socket.gethostname()   # set from the pod name in k8s
log = logging.getLogger(SERVICE)


# =============================================================================================
# Settings
# =============================================================================================

def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


class Settings:
    """Every tunable, read once from environment variables."""

    def __init__(self) -> None:
        self.rag_url = os.environ.get("RAG_URL", "http://rag-service:8080").rstrip("/")
        # Rate limits: per client (token bucket) and per replica for all clients together.
        self.rate_per_min = env_float("RATE_PER_MIN", 10)
        self.rate_burst = env_float("RATE_BURST", 5)
        self.global_per_min = env_float("GLOBAL_PER_MIN", 30)      # protects the paid LLM budget
        self.max_inflight = int(env_float("MAX_INFLIGHT", 8))      # protects the Pi's 1 GB of RAM
        # Timeouts (seconds) for calls to rag-service.
        self.connect_timeout = env_float("CONNECT_TIMEOUT_S", 2)   # short: a dead pod never answers
        self.chat_timeout = env_float("CHAT_TIMEOUT_S", 45)        # long: LLM answers are slow
        self.trace_timeout = env_float("TRACE_TIMEOUT_S", 3)
        # Input limits.
        self.max_question_chars = int(env_float("MAX_QUESTION_CHARS", 1000))
        self.max_top_k = int(env_float("MAX_TOP_K", 10))


# =============================================================================================
# Logging: one JSON line per event on stdout (read with `kubectl logs -l app=api-gateway`)
# =============================================================================================

def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(os.environ.get("LOG_LEVEL", "info").upper())


def log_json(level: int, **fields) -> None:
    log.log(level, json.dumps({"ts": round(time.time(), 3), "pod": POD_NAME, **fields}, ensure_ascii=False))


# =============================================================================================
# Rate limiting
# =============================================================================================

class TokenBucket:
    """A bucket of `capacity` tokens, refilled at `rate` tokens per second. Each request takes one."""

    def __init__(self, capacity: float, rate: float, now: float) -> None:
        self.capacity, self.rate = capacity, rate
        self.tokens, self.updated = capacity, now

    def take(self, now: float) -> float:
        """Take a token. Returns 0 if allowed, otherwise the seconds until one is available."""
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.rate


class RateLimiter:
    """One bucket per client plus one global bucket, kept in this replica's memory.

    With 2 replicas the real limits are about double. That is accepted: sharing the counters
    would need a stateful store, which the fault-tolerance design avoids.
    """

    MAX_CLIENTS = 10_000   # bounds memory; when full, all buckets reset (only makes us more lenient)

    def __init__(self, per_min: float, burst: float, global_per_min: float, clock=time.monotonic) -> None:
        self.per_min, self.burst, self.clock = per_min, burst, clock
        self.clients: dict[str, TokenBucket] = {}
        self.global_bucket = TokenBucket(global_per_min, global_per_min / 60, clock())

    def check(self, client: str) -> float:
        """0 if the request may go ahead, otherwise the seconds to wait."""
        now = self.clock()
        bucket = self.clients.get(client)
        if bucket is None:
            if len(self.clients) >= self.MAX_CLIENTS:
                self.clients.clear()
            bucket = self.clients[client] = TokenBucket(self.burst, self.per_min / 60, now)
        return bucket.take(now) or self.global_bucket.take(now)


def client_key(request: Request) -> str:
    """Who is asking, for the per-client limit.

    X-Real-IP is set by our nginx from the real connection and overwrites anything the browser
    sent, so it can't be faked. Behind a NodePort it may be a node IP shared by several users;
    the global bucket still protects the LLM budget in that case.
    """
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


# =============================================================================================
# Errors: every error response is {"error": <code>, "detail": <text>, "request_id": <id>}
# =============================================================================================

class APIError(Exception):
    def __init__(self, status: int, error: str, detail: str, headers: dict | None = None):
        self.status, self.error, self.detail, self.headers = status, error, detail, headers or {}


def error_response(request: Request, status: int, error: str, detail: str, headers: dict | None = None):
    body = {"error": error, "detail": detail, "request_id": getattr(request.state, "request_id", None)}
    return JSONResponse(body, status_code=status, headers=headers)


# =============================================================================================
# Request model
# =============================================================================================

class ChatRequest(BaseModel):
    question: str                                   # any language; Thai is passed through as is
    top_k: int | None = Field(default=None, ge=1)   # optional; rag-service picks its own default

    @field_validator("question")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question is empty")
        return v


# =============================================================================================
# Calling rag-service
# =============================================================================================

async def call_upstream(app: FastAPI, method: str, path: str, request_id: str, timeout: float,
                        json_body: dict | None = None) -> httpx.Response:
    """Call rag-service, retrying once only when the connection itself failed.

    A failed connect means nothing was sent, so even a POST is safe to repeat. Keep-alive is
    off (see lifespan), so the retry opens a new connection and kube-proxy picks a replica
    again, usually the surviving one right after a Pi was pulled. Slow answers and 5xx are
    never retried: rag-service and model-service already retry the LLM.
    """
    s: Settings = app.state.settings
    client: httpx.AsyncClient = app.state.http
    headers = {"X-Request-ID": request_id}
    to = httpx.Timeout(timeout, connect=s.connect_timeout)
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            return await client.request(method, f"{s.rag_url}{path}", json=json_body, headers=headers, timeout=to)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:      # nothing sent: safe to retry
            last = e
            log_json(logging.WARNING, event="upstream_connect_failed", request_id=request_id,
                     path=path, attempt=attempt, reason=type(e).__name__)
        except httpx.TimeoutException as e:                          # sent, but too slow
            raise APIError(504, "upstream_timeout", f"rag-service did not answer within {timeout:g} s") from e
        except httpx.HTTPError as e:                                 # connection broke mid-way
            raise APIError(502, "upstream_error", f"rag-service connection broke: {type(e).__name__}") from e
    raise APIError(503, "upstream_unavailable", "rag-service is unreachable, it may be recovering") from last


def parse_json(resp: httpx.Response) -> dict | None:
    """The response body as a dict, or None if it isn't a JSON object."""
    try:
        body = resp.json()
        return body if isinstance(body, dict) else None
    except ValueError:
        return None


def served_by_entry() -> dict:
    """This gateway's entry in the pod chain."""
    return {"service": SERVICE, "pod": POD_NAME}


def upstream_chain(resp: httpx.Response, body: dict | None) -> list[dict]:
    """The chain rag-service reports in its body, or at least its own pod from X-Served-By."""
    chain = (body or {}).get("served_by")
    if isinstance(chain, list):
        return [c for c in chain if isinstance(c, dict) and "service" in c and "pod" in c]
    pod = resp.headers.get("x-served-by")
    return [{"service": "rag-service", "pod": pod}] if pod else []


# =============================================================================================
# The app
# =============================================================================================

def create_app(transport: httpx.AsyncBaseTransport | None = None, settings: Settings | None = None,
               clock=time.monotonic) -> FastAPI:
    """Build the FastAPI app. `transport` and `clock` let tests plug in a fake rag-service and time."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _setup_logging()
        # No keep-alive: a reused connection could stay pinned to a pod on an unplugged Pi and
        # hang until the 45 s timeout. A new connection per call also spreads load over replicas.
        app.state.http = httpx.AsyncClient(transport=transport, limits=httpx.Limits(max_keepalive_connections=0))
        log_json(logging.INFO, event="startup", version=VERSION, rag_url=app.state.settings.rag_url)
        yield
        await app.state.http.aclose()

    app = FastAPI(title=SERVICE, version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)
    s = app.state.settings = settings or Settings()
    app.state.limiter = RateLimiter(s.rate_per_min, s.rate_burst, s.global_per_min, clock=clock)
    app.state.inflight = 0   # chats waiting on rag-service right now (one worker, so a plain int)

    # ---- error handlers: turn every failure into the shared error shape ----------------------

    @app.exception_handler(APIError)
    async def _api_error(request: Request, e: APIError):
        return error_response(request, e.status, e.error, e.detail, e.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, e: RequestValidationError):
        first = e.errors()[0] if e.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        return error_response(request, 400, "invalid_request", f"{where}: {first.get('msg', 'invalid')}".strip(": "))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, e: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(e.status_code, "http_error")
        return error_response(request, e.status_code, code, str(e.detail))

    # ---- middleware: request ID, pod header and one log line for every request ---------------

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = (request.headers.get("x-request-id") or uuid.uuid4().hex)[:64]
        request.state.upstream = {}            # filled by /chat for the log line
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Served-By"] = POD_NAME
        response.headers["X-Request-ID"] = request.state.request_id
        if request.url.path not in ("/healthz", "/readyz"):   # probes would flood the logs
            log_json(logging.INFO, event="request", request_id=request.state.request_id,
                     method=request.method, path=request.url.path, status=response.status_code,
                     latency_ms=round((time.perf_counter() - start) * 1000, 1),
                     client=client_key(request), **request.state.upstream)
        return response

    # ---- probes ------------------------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        # Never checks rag-service: otherwise a rag-service failover would mark every gateway
        # pod unready, and the page would get no response at all instead of a clear 503.
        return {"status": "ready"}

    # ---- API ---------------------------------------------------------------------------------

    @app.get("/api/v1/info")
    def info():
        """Version, pod name and current limits (for debugging)."""
        return {"service": SERVICE, "version": VERSION, "pod": POD_NAME, "limits": {
            "rate_per_min": s.rate_per_min, "rate_burst": s.rate_burst, "global_per_min": s.global_per_min,
            "max_inflight": s.max_inflight, "chat_timeout_s": s.chat_timeout,
            "max_question_chars": s.max_question_chars}}

    @app.post("/api/v1/chat")
    async def chat(body: ChatRequest, request: Request):
        """Answer a question: limits first, then rag-service, then add this pod to the chain."""
        if len(body.question) > s.max_question_chars:
            raise APIError(400, "invalid_request", f"question is longer than {s.max_question_chars} characters")

        wait = app.state.limiter.check(client_key(request))
        if wait:
            secs = max(1, round(wait + 0.5))
            raise APIError(429, "rate_limited", f"too many questions, try again in {secs} s",
                           headers={"Retry-After": str(secs)})
        if app.state.inflight >= s.max_inflight:
            raise APIError(503, "busy", "too many questions in progress, try again shortly",
                           headers={"Retry-After": "2"})

        payload = {"question": body.question}
        if body.top_k is not None:
            payload["top_k"] = min(body.top_k, s.max_top_k)

        app.state.inflight += 1
        try:
            resp = await call_upstream(app, "POST", "/v1/answer", request.state.request_id, s.chat_timeout, payload)
        finally:
            app.state.inflight -= 1

        data = parse_json(resp)
        request.state.upstream = {"upstream_status": resp.status_code,
                                  "upstream_pod": resp.headers.get("x-served-by")}
        if resp.status_code >= 500:
            raise APIError(502, "upstream_error", (data or {}).get("detail") or f"rag-service returned {resp.status_code}")
        if resp.status_code >= 400:
            # rag-service's own client errors (e.g. a question it rejects) pass through unchanged.
            data = data or {"error": "upstream_rejected", "detail": f"rag-service returned {resp.status_code}"}
            data["request_id"] = request.state.request_id
            return JSONResponse(data, status_code=resp.status_code)
        if data is None or "answer" not in data:
            raise APIError(502, "upstream_error", "rag-service returned an unexpected response")

        return {
            "request_id": request.state.request_id,
            "answer": data["answer"],
            "sources": data.get("sources") or [],
            "served_by": [served_by_entry(), *upstream_chain(resp, data)],
        }

    @app.get("/api/v1/trace")
    async def trace(request: Request):
        """Cheap end-to-end check for the demo panel: same path as a chat, but no LLM call.

        Always answers with at least this gateway in `served_by`, so the panel can show which
        part of the chain is still alive when rag-service is down.
        """
        start = time.perf_counter()
        chain = [served_by_entry()]

        def result(ok: bool, status: int, **extra):
            return JSONResponse({"ok": ok, "served_by": chain, **extra,
                                 "latency_ms": round((time.perf_counter() - start) * 1000, 1),
                                 "request_id": request.state.request_id}, status_code=status)

        try:
            resp = await call_upstream(app, "GET", "/v1/trace", request.state.request_id, s.trace_timeout)
        except APIError as e:
            return result(False, e.status, error=e.error, detail=e.detail)
        data = parse_json(resp)
        chain += upstream_chain(resp, data)
        ok = resp.status_code < 400 and bool((data or {}).get("ok", True))
        return result(ok, 200 if ok else 502)

    return app


app = create_app()   # what uvicorn runs: `uvicorn app.main:app`

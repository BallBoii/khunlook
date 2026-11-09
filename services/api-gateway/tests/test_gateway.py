"""api-gateway tests. rag-service is faked with httpx.MockTransport: no network, no Docker.

Run from the repo root:  make test   (or: python -m pytest services/api-gateway/tests -q)

Sections: happy path · validation · limits · failover behaviour · trace and probes
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

# Load app/main.py under its own module name: vector-db's tests also import a package called
# `app`, and both suites run in one pytest process (`make test`).
_spec = importlib.util.spec_from_file_location(
    "api_gateway_main", Path(__file__).resolve().parents[1] / "app" / "main.py")
gw = importlib.util.module_from_spec(_spec)
sys.modules["api_gateway_main"] = gw
_spec.loader.exec_module(gw)
POD_NAME, RateLimiter, Settings, create_app = gw.POD_NAME, gw.RateLimiter, gw.Settings, gw.create_app

CHAIN = [{"service": "rag-service", "pod": "rag-1"}, {"service": "vector-db", "pod": "vdb-1"},
         {"service": "model-service", "pod": "model-1"}]
ANSWER = {"answer": "Nov 17", "sources": [{"doc_title": "SDS Final Project", "page": 10}], "served_by": CHAIN}


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make_client(handler, clock=None, **env) -> TestClient:
    settings = Settings()
    for k, v in env.items():
        setattr(settings, k, v)
    app = create_app(transport=httpx.MockTransport(handler), settings=settings, clock=clock or FakeClock())
    return TestClient(app)


def ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/answer":
        return httpx.Response(200, json=ANSWER, headers={"X-Served-By": "rag-1"})
    if request.url.path == "/v1/trace":
        return httpx.Response(200, json={"ok": True, "served_by": CHAIN})
    return httpx.Response(404)


def ask(c: TestClient, q="When is the demo?", **headers):
    return c.post("/api/v1/chat", json={"question": q}, headers=headers)


# ---------------------------------------------------------------- happy path

def test_chat_returns_answer_sources_and_full_chain():
    with make_client(ok_handler) as c:
        r = ask(c)
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "Nov 17"
    assert body["sources"][0]["page"] == 10
    assert [h["service"] for h in body["served_by"]] == ["api-gateway", "rag-service", "vector-db", "model-service"]
    assert body["served_by"][0]["pod"] == POD_NAME
    assert r.headers["X-Served-By"] == POD_NAME
    assert body["request_id"] == r.headers["X-Request-ID"]


def test_request_id_is_forwarded_upstream_and_returned():
    seen = {}

    def handler(request):
        seen["rid"] = request.headers.get("x-request-id")
        return ok_handler(request)

    with make_client(handler) as c:
        r = ask(c, **{"X-Request-ID": "abc123"})
    assert seen["rid"] == "abc123"
    assert r.json()["request_id"] == "abc123"


def test_chain_falls_back_to_upstream_header_when_body_has_none():
    def handler(request):
        return httpx.Response(200, json={"answer": "x"}, headers={"X-Served-By": "rag-9"})

    with make_client(handler) as c:
        chain = ask(c).json()["served_by"]
    assert chain[1] == {"service": "rag-service", "pod": "rag-9"}


def test_question_is_trimmed_and_top_k_capped():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return ok_handler(request)

    with make_client(handler) as c:
        c.post("/api/v1/chat", json={"question": "  hi  ", "top_k": 99})
    assert b'"question":"hi"' in seen["body"] and b'"top_k":10' in seen["body"]


# ---------------------------------------------------------------- validation

@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "   "}, {"question": 5}, {"question": "x" * 1001}])
def test_bad_questions_are_400_without_calling_upstream(payload):
    calls = []

    def handler(request):
        calls.append(request)
        return ok_handler(request)

    with make_client(handler) as c:
        r = c.post("/api/v1/chat", json=payload)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"
    assert calls == []


def test_unknown_route_uses_error_shape():
    with make_client(ok_handler) as c:
        r = c.get("/api/v1/nope")
    assert r.status_code == 404 and r.json()["error"] == "not_found"


# ---------------------------------------------------------------- limits

def test_per_client_rate_limit_then_refill():
    clock = FakeClock()
    with make_client(ok_handler, clock=clock, rate_burst=2, rate_per_min=6) as c:
        assert ask(c, **{"X-Real-IP": "1.1.1.1"}).status_code == 200
        assert ask(c, **{"X-Real-IP": "1.1.1.1"}).status_code == 200
        r = ask(c, **{"X-Real-IP": "1.1.1.1"})
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
        assert int(r.headers["Retry-After"]) >= 1
        assert ask(c, **{"X-Real-IP": "2.2.2.2"}).status_code == 200  # other clients unaffected
        clock.t += 10  # 6/min = one token every 10 s
        assert ask(c, **{"X-Real-IP": "1.1.1.1"}).status_code == 200


def test_global_limit_applies_across_clients():
    with make_client(ok_handler, rate_burst=5, global_per_min=3) as c:
        codes = [ask(c, **{"X-Real-IP": f"10.0.0.{i}"}).status_code for i in range(4)]
    assert codes == [200, 200, 200, 429]


def test_busy_when_too_many_in_flight():
    with make_client(ok_handler) as c:
        c.app.state.inflight = c.app.state.settings.max_inflight
        r = ask(c)
    assert r.status_code == 503 and r.json()["error"] == "busy"


def test_inflight_counter_returns_to_zero_after_errors():
    def handler(request):
        raise httpx.ConnectError("down")

    with make_client(handler) as c:
        ask(c)
        assert c.app.state.inflight == 0


def test_rate_limiter_bounds_memory():
    rl = RateLimiter(10, 5, 1e9, clock=FakeClock())
    rl.MAX_CLIENTS = 3
    for i in range(10):
        rl.check(str(i))
    assert len(rl.clients) <= 3


# ---------------------------------------------------------------- failover behaviour

def test_connect_failure_is_retried_once_then_succeeds():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("dead pod")
        return ok_handler(request)

    with make_client(handler) as c:
        r = ask(c)
    assert r.status_code == 200 and len(attempts) == 2


def test_connect_failure_twice_is_503_upstream_unavailable():
    attempts = []

    def handler(request):
        attempts.append(1)
        raise httpx.ConnectError("refused")

    with make_client(handler) as c:
        r = ask(c)
    assert r.status_code == 503 and r.json()["error"] == "upstream_unavailable"
    assert len(attempts) == 2


def test_read_timeout_is_504_and_not_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        raise httpx.ReadTimeout("slow LLM")

    with make_client(handler) as c:
        r = ask(c)
    assert r.status_code == 504 and r.json()["error"] == "upstream_timeout"
    assert len(attempts) == 1


def test_upstream_5xx_becomes_502():
    with make_client(lambda r: httpx.Response(500, json={"error": "x", "detail": "LLM down"})) as c:
        r = ask(c)
    assert r.status_code == 502 and r.json()["error"] == "upstream_error"
    assert r.json()["detail"] == "LLM down"


def test_upstream_4xx_passes_through_with_request_id():
    with make_client(lambda r: httpx.Response(422, json={"error": "too_vague", "detail": "ask more"})) as c:
        r = ask(c)
    assert r.status_code == 422 and r.json()["error"] == "too_vague" and r.json()["request_id"]


def test_upstream_garbage_is_502():
    with make_client(lambda r: httpx.Response(200, text="<html>")) as c:
        assert ask(c).status_code == 502


# ---------------------------------------------------------------- trace and probes

def test_trace_returns_chain():
    with make_client(ok_handler) as c:
        r = c.get("/api/v1/trace")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(r.json()["served_by"]) == 4


def test_trace_when_rag_is_down_still_reports_gateway():
    def handler(request):
        raise httpx.ConnectError("down")

    with make_client(handler) as c:
        r = c.get("/api/v1/trace")
    assert r.status_code == 503
    assert r.json()["ok"] is False and r.json()["served_by"][0]["service"] == "api-gateway"


def test_trace_when_rag_has_no_trace_endpoint_yet():
    with make_client(lambda r: httpx.Response(404, json={"error": "not_found"}, headers={"X-Served-By": "rag-1"})) as c:
        r = c.get("/api/v1/trace")
    assert r.status_code == 502 and r.json()["ok"] is False


def test_probes_do_not_touch_upstream():
    def handler(request):
        raise AssertionError("probe called rag-service")

    with make_client(handler) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code == 200
        assert c.get("/api/v1/info").json()["service"] == "api-gateway"


def test_thai_question_passes_through_unchanged():
    seen = {}

    def handler(request):
        import json
        seen["q"] = json.loads(request.read())["question"]
        return ok_handler(request)

    with make_client(handler) as c:
        r = ask(c, q="  เดโมวันไหน  ")
    assert r.status_code == 200 and seen["q"] == "เดโมวันไหน"

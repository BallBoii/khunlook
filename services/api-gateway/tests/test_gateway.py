"""api-gateway tests. rag-service is faked with httpx.MockTransport: no network, no Docker.

Run from the repo root:  make test   (or: python -m pytest services/api-gateway/tests -q)

Sections: happy path · validation · limits · failover behaviour · trace and probes
"""
from __future__ import annotations

import asyncio
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


def test_upstream_5xx_becomes_502_and_preserves_its_code():
    with make_client(lambda r: httpx.Response(500, json={"error": "x", "detail": "LLM down"})) as c:
        r = ask(c)
    assert r.status_code == 502 and r.json()["error"] == "x"
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


# ---------------------------------------------------------------- alignment regressions

def test_default_settings_match_the_cluster_service_and_timeout_budget(monkeypatch):
    for name in ("RAG_URL", "CHAT_TIMEOUT_S", "TRACE_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.rag_url == "http://rag:8080"
    assert settings.chat_timeout == 90
    assert settings.trace_timeout == 5


@pytest.mark.parametrize("raw_chain", [
    CHAIN,
    {"rag-service": "rag-1", "model-service (embed)": "model-1", "vector-db": "vdb-1",
     "model-service (chat)": "model-1"},
])
def test_chat_failure_keeps_passages_request_id_and_normalized_chain(raw_chain):
    sources = [{"n": 2, "doc_id": "2110221", "doc_title": "Computer Engineering Essentials",
                "page": 1, "text": "Course description."}]

    def handler(request):
        return httpx.Response(502, json={"error": "model_service_unavailable", "detail": "chat timed out",
                                        "sources": sources, "served_by": raw_chain, "request_id": "untrusted-id"},
                              headers={"X-Served-By": "rag-1"})

    with make_client(handler) as c:
        r = ask(c, **{"X-Request-ID": "request-from-browser"})
    body = r.json()
    assert r.status_code == 502 and body["error"] == "model_service_unavailable"
    assert body["sources"] == sources
    assert body["served_by"] == [{"service": "api-gateway", "pod": POD_NAME}, *CHAIN]
    assert body["request_id"] == r.headers["X-Request-ID"] == "request-from-browser"


def test_chain_retains_distinct_model_pods_and_ignores_invalid_hops():
    raw = {"rag-service": "rag-1", "model-service (embed)": "model-a", "vector-db": "vdb-1",
           "model-service (chat)": "model-b", "missing-pod": None, "invalid-pod": {"pod": "wrong"}}
    with make_client(lambda r: httpx.Response(200, json={**ANSWER, "served_by": raw})) as c:
        body = ask(c).json()
    assert [h["pod"] for h in body["served_by"]] == [POD_NAME, "rag-1", "vdb-1", "model-a", "model-b"]


def test_upstream_client_error_keeps_retry_after_and_gateway_chain():
    with make_client(lambda r: httpx.Response(429, json={"error": "rate_limited", "detail": "wait"},
                                             headers={"Retry-After": "7", "X-Served-By": "rag-1"})) as c:
        r = ask(c)
    assert r.status_code == 429 and r.headers["Retry-After"] == "7"
    assert [h["service"] for h in r.json()["served_by"]] == ["api-gateway", "rag-service"]


@pytest.mark.parametrize("answer", [None, 42, [], "", "   "])
def test_invalid_answer_is_an_upstream_error(answer):
    with make_client(lambda r: httpx.Response(200, json={**ANSWER, "answer": answer})) as c:
        r = ask(c)
    assert r.status_code == 502 and r.json()["error"] == "upstream_error"


def test_non_json_server_error_is_normalized():
    with make_client(lambda r: httpx.Response(503, text="<html>unavailable</html>",
                                             headers={"X-Served-By": "rag-1"})) as c:
        r = ask(c)
    assert r.status_code == 502 and r.json()["error"] == "upstream_error"
    assert r.json()["served_by"][1]["pod"] == "rag-1"


def test_trace_preserves_dependency_failure_and_partial_chain():
    def handler(request):
        return httpx.Response(502, json={"ok": False, "detail": "model-service: HTTP 503", "served_by": CHAIN[:2]})

    with make_client(handler) as c:
        r = c.get("/api/v1/trace")
    assert r.status_code == 502 and r.json()["ok"] is False
    assert r.json()["detail"] == "model-service: HTTP 503"
    assert [h["service"] for h in r.json()["served_by"]] == ["api-gateway", "rag-service", "vector-db"]


@pytest.mark.parametrize("body", [None, {}, {"ok": False}, {"ok": "true"}, {"ok": 1}, []])
def test_trace_requires_an_explicit_json_boolean_true(body):
    def handler(request):
        return httpx.Response(200, text="<html>OK</html>") if body is None else httpx.Response(200, json=body)

    with make_client(handler) as c:
        r = c.get("/api/v1/trace")
    assert r.status_code == 502 and r.json()["ok"] is False
    assert r.json()["detail"]


def test_trace_redirect_is_not_healthy():
    with make_client(lambda r: httpx.Response(302, json={"ok": True})) as c:
        r = c.get("/api/v1/trace")
    assert r.status_code == 502 and r.json()["ok"] is False


def test_overall_chat_deadline_includes_connection_retry():
    attempts, cancelled = [], []

    async def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            await asyncio.sleep(0.02)
            raise httpx.ConnectError("dead pod")
        try:
            await asyncio.sleep(10)  # MockTransport does not enforce httpx's per-operation timeouts.
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return ok_handler(request)

    with make_client(handler, chat_timeout=0.1) as c:
        r = ask(c)
        assert c.app.state.inflight == 0
    assert r.status_code == 504 and r.json()["error"] == "upstream_timeout"
    assert len(attempts) == 2 and cancelled == [True]


def test_trace_has_an_overall_deadline():
    async def handler(request):
        await asyncio.sleep(10)
        return ok_handler(request)

    with make_client(handler, trace_timeout=0.05) as c:
        r = c.get("/api/v1/trace")
    assert r.status_code == 504 and r.json()["ok"] is False
    assert r.json()["error"] == "upstream_timeout"


@pytest.mark.parametrize("failure", [None, "chat", "trace"])
def test_gateway_contract_with_actual_rag_routes(monkeypatch, failure):
    # Load RAG under a separate package name, without changing that service's files.
    package_name = "gateway_contract_rag"
    if package_name not in sys.modules:
        from types import ModuleType
        package = ModuleType(package_name)
        package.__path__ = [str(Path(__file__).resolve().parents[2] / "rag" / "app")]
        sys.modules[package_name] = package
    rag = importlib.import_module(f"{package_name}.main")
    sources = [{"id": "course-chunk", "score": 0.9, "doc_id": "2110221", "doc_title": "COMP ENG ESS",
                "page": 1, "text": "Overview of computer engineering."}]

    def dependencies(request):
        if request.url.path == "/v1/embed":
            return httpx.Response(200, json={"embedding": [0.1, 0.2]}, headers={"X-Served-By": "model-1"})
        if request.url.path == "/v1/search":
            return httpx.Response(200, json={"results": sources}, headers={"X-Served-By": "vdb-1"})
        if request.url.path == "/v1/chat":
            if failure == "chat":
                raise httpx.ReadTimeout("simulated slow LLM")
            return httpx.Response(200, json={"answer": "Computer engineering fundamentals [1]."},
                                  headers={"X-Served-By": "model-1"})
        assert request.url.path == "/readyz"
        if failure == "trace" and request.url.host == "model-service":
            return httpx.Response(503, json={"error": "not_configured"})
        return httpx.Response(200, json={"status": "ready"},
                              headers={"X-Served-By": "model-1" if request.url.host == "model-service" else "vdb-1"})

    monkeypatch.setenv("VECTOR_DB_URL", "http://vector-db:8080")
    monkeypatch.setenv("MODEL_SERVICE_URL", "http://model-service:8080")
    monkeypatch.setattr(rag, "make_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(dependencies)))
    with TestClient(rag.app), TestClient(create_app(transport=httpx.ASGITransport(app=rag.app))) as c:
        r = c.get("/api/v1/trace") if failure == "trace" else ask(c)
    body = r.json()
    assert body["request_id"] == r.headers["X-Request-ID"]
    assert body["served_by"][0]["service"] == "api-gateway"
    if failure == "trace":
        assert r.status_code == 502 and body["ok"] is False
        assert "model-service: HTTP 503" in body["detail"]
    else:
        assert [h["service"] for h in body["served_by"]] == ["api-gateway", "rag-service", "vector-db", "model-service"]
        assert body["sources"][0]["n"] == 1 and body["sources"][0]["doc_id"] == "2110221"
        if failure == "chat":
            assert r.status_code == 502 and body["error"] == "model_service_unavailable"
        else:
            assert r.status_code == 200 and "[1]" in body["answer"]

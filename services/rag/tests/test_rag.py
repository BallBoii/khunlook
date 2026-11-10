"""rag-service tests. vector-db and model-service are faked with httpx.MockTransport, so no network is needed.

Run from the repo root:  python -m pytest services/rag/tests -q
"""
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main  # noqa: E402
from app.prompt import SYSTEM_PROMPT  # noqa: E402

HITS = [
    {"id": "c9", "score": 0.91, "doc_id": "brief", "doc_title": "SDS Final Project", "source_file": "b.pdf",
     "page": 9, "chunk_index": 0, "text": "Demo on-site 13:00-16:00 November 17, 2026."},
    {"id": "c4", "score": 0.42, "doc_id": "brief", "doc_title": "SDS Final Project", "source_file": "b.pdf",
     "page": 4, "chunk_index": 0, "text": "Form groups of 5 students."},
]


class Fakes:
    """Records every downstream call and lets a test break individual endpoints."""

    def __init__(self):
        self.calls: list[tuple[str, dict, httpx.Headers]] = []
        self.fail: dict[str, httpx.Response | Exception] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = f"{request.url.host}{request.url.path}"
        body = json.loads(request.content or b"{}")
        self.calls.append((key, body, request.headers))
        if key in self.fail:
            f = self.fail[key]
            if isinstance(f, Exception):
                raise f
            return f
        pod = {"model-service": "model-pod-1", "vector-db": "vdb-pod-2"}[request.url.host]
        h = {"X-Served-By": pod}
        if key == "model-service/v1/embed":
            return httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3], "model": "m"}, headers=h)
        if key == "model-service/v1/chat":
            return httpx.Response(200, json={"answer": "The demo is on 17 November [1]."}, headers=h)
        if key in ("vector-db/v1/search", "vector-db/v1/search/keyword"):
            method = "keyword" if key.endswith("keyword") else "vector"
            return httpx.Response(200, json={"method": method, "index_version": "x", "results": HITS[:body["top_k"]]},
                                  headers=h)
        if key.endswith("/readyz"):
            return httpx.Response(200, json={"status": "ready"}, headers=h)
        return httpx.Response(404, json={"error": "not_found", "detail": key})

    def paths(self):
        return [k for k, _, _ in self.calls]


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()
    monkeypatch.setattr(main, "make_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(f.handler)))
    monkeypatch.setenv("VECTOR_DB_URL", "http://vector-db:8080")
    monkeypatch.setenv("MODEL_SERVICE_URL", "http://model-service:8080")
    return f


@pytest.fixture
def client(fakes):
    with TestClient(main.app) as c:
        yield c


def test_health_and_ready(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.headers["X-Served-By"] == main.POD_NAME and r.headers["X-Request-ID"]
    assert client.get("/readyz").json() == {"status": "ready"}


def test_ask_happy_path(client, fakes):
    r = client.post("/v1/ask", json={"question": "When is the demo?", "top_k": 2}, headers={"X-Request-ID": "req-7"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "The demo is on 17 November [1]."
    assert body["retrieval"] == "vector" and body["request_id"] == "req-7"
    assert [s["page"] for s in body["sources"]] == [9, 4] and body["sources"][0]["n"] == 1
    assert body["served_by"] == {"rag-service": main.POD_NAME, "model-service (embed)": "model-pod-1",
                                 "vector-db": "vdb-pod-2", "model-service (chat)": "model-pod-1"}
    assert fakes.paths() == ["model-service/v1/embed", "vector-db/v1/search", "model-service/v1/chat"]

    _, embed_req, embed_headers = fakes.calls[0]
    assert embed_req == {"input": "When is the demo?"} and embed_headers["x-request-id"] == "req-7"
    _, search_req, _ = fakes.calls[1]
    assert search_req["vector"] == [0.1, 0.2, 0.3] and search_req["top_k"] == 2

    _, chat_req, _ = fakes.calls[2]
    system, user = chat_req["messages"]
    assert system == {"role": "system", "content": SYSTEM_PROMPT}
    assert "[1] SDS Final Project, page 9:\nDemo on-site" in user["content"]
    assert "[2] SDS Final Project, page 4:" in user["content"]
    assert user["content"].endswith("Question: When is the demo?")


def test_embed_down_falls_back_to_keyword_search(client, fakes):
    fakes.fail["model-service/v1/embed"] = httpx.ConnectError("connection refused")
    body = client.post("/v1/ask", json={"question": "When is the demo?"}).json()
    assert body["retrieval"] == "keyword" and body["answer"]
    assert fakes.paths() == ["model-service/v1/embed", "vector-db/v1/search/keyword", "model-service/v1/chat"]


def test_embed_down_without_fallback_is_502(client, fakes, monkeypatch):
    monkeypatch.setenv("KEYWORD_FALLBACK", "false")
    fakes.fail["model-service/v1/embed"] = httpx.Response(500, json={"error": "boom", "detail": "provider down"})
    r = client.post("/v1/ask", json={"question": "hi"})
    assert r.status_code == 502 and r.json()["error"] == "model_service_unavailable"


def test_vector_db_down_is_502(client, fakes):
    fakes.fail["vector-db/v1/search"] = httpx.ConnectError("no route")
    r = client.post("/v1/ask", json={"question": "hi"})
    assert r.status_code == 502 and r.json()["error"] == "vector_db_unavailable"
    assert r.headers["X-Served-By"] == main.POD_NAME


def test_embedding_model_mismatch_is_reported(client, fakes):
    fakes.fail["vector-db/v1/search"] = httpx.Response(
        400, json={"error": "dimension_mismatch", "detail": "vector has 3 dims, index expects 256"})
    r = client.post("/v1/ask", json={"question": "hi"})
    assert r.status_code == 502 and r.json()["error"] == "embedding_mismatch" and "256" in r.json()["detail"]


def test_chat_down_still_returns_sources(client, fakes):
    fakes.fail["model-service/v1/chat"] = httpx.ReadTimeout("slow LLM")
    r = client.post("/v1/ask", json={"question": "When is the demo?"})
    body = r.json()
    assert r.status_code == 502 and body["error"] == "model_service_unavailable"
    assert [s["id"] for s in body["sources"]] == ["c9", "c4"] and "timed out" in body["detail"]


def test_no_hits_still_asks_llm_with_empty_context(client, fakes, monkeypatch):
    fakes.fail["vector-db/v1/search"] = httpx.Response(200, json={"results": []})
    body = client.post("/v1/ask", json={"question": "What is the capital of France?"}).json()
    assert body["sources"] == []
    _, chat_req, _ = fakes.calls[-1]
    assert "(no relevant passages were found)" in chat_req["messages"][1]["content"]


def test_context_budget_keeps_best_hits(client, fakes, monkeypatch):
    monkeypatch.setenv("MAX_CONTEXT_CHARS", "50")      # only the first hit fits
    body = client.post("/v1/ask", json={"question": "demo?"}).json()
    assert [s["id"] for s in body["sources"]] == ["c9"]


def test_bad_requests(client):
    assert client.post("/v1/ask", json={}).json()["error"] == "invalid_request"
    assert client.post("/v1/ask", json={"question": ""}).status_code == 400
    assert client.post("/v1/ask", json={"question": "x", "top_k": 99}).status_code == 400


def test_answer_returns_the_gateway_contract(client, fakes):
    r = client.post("/v1/answer", json={"question": "When is the demo?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "The demo is on 17 November [1]."
    assert body["sources"][0]["page"] == 9 and body["sources"][0]["doc_title"] == "SDS Final Project"
    assert body["served_by"] == [{"service": "rag-service", "pod": main.POD_NAME},
                                 {"service": "vector-db", "pod": "vdb-pod-2"},
                                 {"service": "model-service", "pod": "model-pod-1"}]


def test_answer_errors_keep_their_shape(client, fakes):
    fakes.fail["vector-db/v1/search"] = httpx.ConnectError("refused")
    r = client.post("/v1/answer", json={"question": "When is the demo?"})
    assert r.status_code == 502 and r.json()["error"] == "vector_db_unavailable"
    assert client.post("/v1/answer", json={"question": ""}).status_code == 400


def test_trace_checks_dependencies_without_calling_the_llm(client, fakes):
    r = client.get("/v1/trace")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert [h["service"] for h in r.json()["served_by"]] == ["rag-service", "vector-db", "model-service"]
    assert sorted(fakes.paths()) == ["model-service/readyz", "vector-db/readyz"]


def test_trace_reports_a_down_dependency(client, fakes):
    fakes.fail["model-service/readyz"] = httpx.Response(503, json={"error": "not_configured"})
    r = client.get("/v1/trace")
    body = r.json()
    assert r.status_code == 502 and body["ok"] is False and "model-service: HTTP 503" in body["detail"]
    assert "rag-service" in [h["service"] for h in body["served_by"]]

    fakes.fail["vector-db/readyz"] = httpx.ConnectError("refused")
    assert client.get("/v1/trace").json()["detail"].startswith("vector-db: ConnectError")

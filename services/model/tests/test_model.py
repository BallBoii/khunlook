"""model-service tests. OpenRouter is faked with httpx.MockTransport, so no network or API key is needed.

Run from the repo root:  python -m pytest services/model/tests -q
"""
import asyncio
import json
import math
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main, openrouter  # noqa: E402

DIM = 4


class FakeOpenRouter:
    def __init__(self):
        self.calls: list[tuple[str, dict, httpx.Headers]] = []
        self.responses: list[httpx.Response | Exception] = []  # consumed in order; empty = default success

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content)
        self.calls.append((path, body, request.headers))
        if self.responses:
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        if path == "/api/v1/embeddings":
            texts = body["input"]
            data = [{"index": i, "embedding": [3.0, 4.0, 0.0, 0.0]} for i in reversed(range(len(texts)))]
            return httpx.Response(200, json={"data": data})
        return httpx.Response(200, json={"choices": [{"message": {"content": "The demo is on 17 November."}}],
                                         "usage": {"prompt_tokens": 12, "completion_tokens": 7}})


@pytest.fixture
def fake(monkeypatch):
    f = FakeOpenRouter()
    monkeypatch.setattr(main, "make_http_client",
                        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(f.handler)))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("EMBED_DIM", str(DIM))
    monkeypatch.setattr(openrouter, "BACKOFF_S", 0)
    return f


@pytest.fixture
def client(fake):
    with TestClient(main.app) as c:
        yield c


def test_health_ready_and_headers(client):
    r = client.get("/healthz", headers={"X-Request-ID": "req-1"})
    assert r.status_code == 200 and r.headers["X-Served-By"] == main.POD_NAME and r.headers["X-Request-ID"] == "req-1"
    assert client.get("/readyz").json() == {"status": "ready", "embed_model": "baai/bge-m3",
                                            "chat_model": "google/gemma-3-4b-it"}


def test_not_ready_without_api_key(fake, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with TestClient(main.app) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code == 503
        r = c.post("/v1/embed", json={"input": "hi"})
        assert r.status_code == 503 and r.json()["error"] == "not_configured"


def test_embed_single_is_normalized_and_uses_configured_model(client, fake):
    r = client.post("/v1/embed", json={"input": "When is the demo?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "baai/bge-m3" and body["dim"] == DIM
    assert body["embedding"] == pytest.approx([0.6, 0.8, 0.0, 0.0])
    path, sent, headers = fake.calls[0]
    assert path == "/api/v1/embeddings" and sent["model"] == "baai/bge-m3" and sent["input"] == ["When is the demo?"]
    assert headers["authorization"] == "Bearer sk-test"


def test_embed_batch(client):
    body = client.post("/v1/embed", json={"input": ["a", "b"]}).json()
    assert len(body["embeddings"]) == 2 and math.isclose(sum(x * x for x in body["embeddings"][0]), 1.0)
    assert "embedding" not in body


def test_embed_dimension_mismatch_is_502(client, fake, monkeypatch):
    fake.responses = [httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})]
    r = client.post("/v1/embed", json={"input": "x"})
    assert r.status_code == 502 and r.json()["error"] == "dimension_mismatch"


@pytest.mark.parametrize("payload", [{}, {"input": ""}, {"input": "   "}, {"input": []}, {"input": ["ok", " "]}])
def test_embed_rejects_bad_input(client, payload):
    r = client.post("/v1/embed", json=payload)
    assert r.status_code == 400 and r.json()["error"] in ("invalid_request",)


def test_embed_input_too_long(client, monkeypatch):
    r = client.post("/v1/embed", json={"input": "x" * 9000})
    assert r.status_code == 400 and r.json()["error"] == "input_too_long"


def test_chat_happy_path(client, fake):
    msgs = [{"role": "system", "content": "Answer from context."}, {"role": "user", "content": "When is the demo?"}]
    r = client.post("/v1/chat", json={"messages": msgs, "temperature": 0.1})
    assert r.status_code == 200, r.text
    assert r.json() == {"answer": "The demo is on 17 November.", "model": "google/gemma-3-4b-it",
                        "usage": {"prompt_tokens": 12, "completion_tokens": 7}}
    path, sent, _ = fake.calls[0]
    assert path == "/api/v1/chat/completions" and sent["model"] == "google/gemma-3-4b-it"
    assert sent["messages"] == msgs and sent["temperature"] == 0.1 and sent["max_tokens"] == 1024


def test_chat_caps_max_tokens(client, fake):
    client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 99999})
    assert fake.calls[0][1]["max_tokens"] == 1024


@pytest.mark.parametrize("payload", [
    {"messages": []},
    {"messages": [{"role": "user", "content": ""}]},
    {"messages": [{"role": "tool", "content": "x"}]},
    {"messages": [{"role": "user", "content": "x"}], "temperature": 9},
])
def test_chat_rejects_bad_input(client, payload):
    r = client.post("/v1/chat", json=payload)
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


def test_chat_empty_answer_is_502(client, fake):
    fake.responses = [httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]})]
    r = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502 and r.json()["error"] == "empty_answer"


def test_retries_on_5xx_then_succeeds(client, fake):
    fake.responses = [httpx.Response(503, json={"error": {"message": "overloaded"}}), httpx.ConnectError("reset")]
    r = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and len(fake.calls) == 3


def test_gives_up_after_retries(client, fake):
    fake.responses = [httpx.Response(500, json={})] * 3
    r = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502 and r.json()["error"] == "upstream_error" and len(fake.calls) == 3


def test_rate_limit_is_429_after_retries(client, fake):
    fake.responses = [httpx.Response(429, json={"error": {"message": "slow down"}})] * 3
    r = client.post("/v1/embed", json={"input": "x"})
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"


def test_bad_key_is_not_retried_and_does_not_leak(client, fake):
    fake.responses = [httpx.Response(401, json={"error": {"message": "No auth credentials sk-test"}})]
    r = client.post("/v1/embed", json={"input": "x"})
    assert r.status_code == 502 and r.json()["error"] == "upstream_auth" and len(fake.calls) == 1
    assert "sk-test" not in r.text


def test_upstream_timeout_is_504(fake, monkeypatch):
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    monkeypatch.setattr(main, "make_http_client", lambda t: httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    monkeypatch.setenv("EMBED_TIMEOUT_S", "0.05")
    with TestClient(main.app) as c:
        r = c.post("/v1/embed", json={"input": "x"})
    assert r.status_code == 504 and r.json()["error"] == "upstream_timeout"

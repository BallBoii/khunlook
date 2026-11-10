"""HTTP clients for the two services rag-service depends on: model-service and vector-db.

Every call forwards X-Request-ID (so one question can be traced through all pods' logs) and records
the downstream pod's X-Served-By header (so the demo can show which containers served the request).
"""
from __future__ import annotations

import os

import httpx


class UpstreamError(Exception):
    """A dependency failed or answered with something we can't use."""

    def __init__(self, service: str, detail: str, status: int | None = None, error: str | None = None):
        super().__init__(f"{service}: {detail}")
        self.service, self.detail, self.status, self.error = service, detail, status, error


def _check(service: str, r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200:
        raise UpstreamError(service, body.get("detail") or f"HTTP {r.status_code}: {r.text[:200]}",
                            r.status_code, body.get("error"))
    return body


async def _post(http: httpx.AsyncClient, service: str, url: str, payload: dict, request_id: str,
                served_by: dict, served_key: str, timeout: float) -> dict:
    try:
        r = await http.post(url, json=payload, headers={"X-Request-ID": request_id}, timeout=timeout)
    except httpx.TimeoutException as e:
        raise UpstreamError(service, f"timed out after {timeout}s calling {url}") from e
    except httpx.TransportError as e:  # connection refused, DNS failure, pod gone...
        raise UpstreamError(service, f"cannot reach {url}: {e!r}") from e
    if "x-served-by" in r.headers:
        served_by[served_key] = r.headers["x-served-by"]
    return _check(service, r)


# =====================================================================================================
# model-service  (PLACEHOLDER CONTRACT - agree on paths and JSON shapes with the model-service owner)
# =====================================================================================================
#
#   POST {MODEL_SERVICE_URL}/v1/embed
#        request : {"input": "When is the demo?"}
#        response: {"embedding": [0.01, -0.2, ...], "model": "openai/text-embedding-3-small"}
#
#   POST {MODEL_SERVICE_URL}/v1/chat
#        request : {"messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
#                   "temperature": 0.2}
#        response: {"answer": "The demo is on 17 November ...", "model": "..."}
#
# If the owner picks different shapes, only EMBED_PATH/CHAT_PATH and the two _parse_* functions change.

EMBED_PATH = "/v1/embed"
CHAT_PATH = "/v1/chat"


def _parse_embed_response(body: dict) -> list[float]:
    vec = body.get("embedding")
    if not isinstance(vec, list) or not vec:
        raise UpstreamError("model-service", "embed response has no 'embedding' list")
    return vec


def _parse_chat_response(body: dict) -> str:
    answer = body.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise UpstreamError("model-service", "chat response has no 'answer' text")
    return answer


class ModelServiceClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str | None = None):
        self.http = http
        self.base_url = (base_url or os.environ.get("MODEL_SERVICE_URL", "http://model-service:8080")).rstrip("/")
        self.embed_timeout = float(os.environ.get("EMBED_TIMEOUT_S", "10"))
        self.chat_timeout = float(os.environ.get("CHAT_TIMEOUT_S", "60"))
        self.temperature = float(os.environ.get("CHAT_TEMPERATURE", "0.2"))

    async def embed(self, text: str, request_id: str, served_by: dict) -> list[float]:
        """Question text -> query vector. MUST use the same embedding model that built the index."""
        body = await _post(self.http, "model-service", self.base_url + EMBED_PATH, {"input": text},
                           request_id, served_by, "model-service (embed)", self.embed_timeout)
        return _parse_embed_response(body)

    async def chat(self, messages: list[dict], request_id: str, served_by: dict) -> str:
        """Prompt messages -> the LLM's answer text."""
        body = await _post(self.http, "model-service", self.base_url + CHAT_PATH,
                           {"messages": messages, "temperature": self.temperature},
                           request_id, served_by, "model-service (chat)", self.chat_timeout)
        return _parse_chat_response(body)


# =====================================================================================================
# vector-db  (real contract, see services/vector-db/app/main.py)
# =====================================================================================================

class VectorDBClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str | None = None):
        self.http = http
        self.base_url = (base_url or os.environ.get("VECTOR_DB_URL", "http://vector-db:8080")).rstrip("/")
        self.timeout = float(os.environ.get("VECTOR_DB_TIMEOUT_S", "5"))

    async def search(self, vector: list[float], top_k: int, min_score: float, request_id: str,
                     served_by: dict) -> list[dict]:
        body = await _post(self.http, "vector-db", self.base_url + "/v1/search",
                           {"vector": vector, "top_k": top_k, "min_score": min_score},
                           request_id, served_by, "vector-db", self.timeout)
        return body.get("results", [])

    async def keyword_search(self, query: str, top_k: int, request_id: str, served_by: dict) -> list[dict]:
        body = await _post(self.http, "vector-db", self.base_url + "/v1/search/keyword",
                           {"query": query, "top_k": top_k}, request_id, served_by, "vector-db", self.timeout)
        return body.get("results", [])

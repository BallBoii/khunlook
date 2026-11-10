"""Thin async OpenRouter client: embeddings + chat, with retries inside one overall deadline."""
from __future__ import annotations

import asyncio
import math

import httpx


BACKOFF_S = 0.5  # first retry delay; doubles each attempt


class UpstreamError(Exception):
    """OpenRouter failed or answered with something we can't use. Maps to an HTTP status for our caller."""

    def __init__(self, status: int, error: str, detail: str):
        super().__init__(detail)
        self.status, self.error, self.detail = status, error, detail


def _error_from(r: httpx.Response) -> UpstreamError:
    try:
        msg = (r.json().get("error") or {}).get("message", "")
    except (ValueError, AttributeError):
        msg = ""
    msg = msg or r.text[:200]
    if r.status_code in (401, 403):  # our key is wrong: the caller can't fix it, so don't echo details
        return UpstreamError(502, "upstream_auth", f"OpenRouter rejected the API key (HTTP {r.status_code})")
    if r.status_code == 429:
        return UpstreamError(429, "rate_limited", f"OpenRouter rate limit: {msg}")
    if r.status_code in (400, 404, 422):
        return UpstreamError(502, "upstream_rejected", f"OpenRouter rejected the request (HTTP {r.status_code}): {msg}")
    return UpstreamError(502, "upstream_error", f"OpenRouter HTTP {r.status_code}: {msg}")


def l2_normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


class OpenRouter:
    def __init__(self, http: httpx.AsyncClient, api_key: str, base_url: str, max_retries: int = 2,
                 referer: str = "", title: str = ""):
        self.http, self.base_url, self.max_retries = http, base_url.rstrip("/"), max_retries
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if referer:
            self.headers["HTTP-Referer"] = referer
        if title:
            self.headers["X-OpenRouter-Title"] = title

    async def _post(self, path: str, payload: dict, timeout: float) -> dict:
        last: UpstreamError | None = None
        try:
            async with asyncio.timeout(timeout):  # one deadline for all attempts, so we stay inside rag's timeout
                for attempt in range(self.max_retries + 1):
                    try:
                        r = await self.http.post(self.base_url + path, json=payload, headers=self.headers)
                    except httpx.TransportError as e:
                        last = UpstreamError(502, "upstream_unreachable", f"cannot reach OpenRouter: {e!r}")
                    else:
                        if r.status_code == 200:
                            try:
                                body = r.json()
                            except ValueError:
                                body = None
                            if isinstance(body, dict) and "error" not in body:
                                return body
                            last = UpstreamError(502, "upstream_error", "OpenRouter returned an unusable body")
                        else:
                            last = _error_from(r)
                            if r.status_code != 429 and r.status_code < 500:  # 4xx: retrying won't help
                                raise last
                    if attempt < self.max_retries:
                        await asyncio.sleep(BACKOFF_S * 2 ** attempt)
        except TimeoutError:
            raise UpstreamError(504, "upstream_timeout", f"OpenRouter did not answer within {timeout:g}s") from None
        raise last or UpstreamError(502, "upstream_error", "OpenRouter request failed")

    async def embed(self, model: str, texts: list[str], timeout: float) -> list[list[float]]:
        body = await self._post("/embeddings", {"model": model, "input": texts, "encoding_format": "float"}, timeout)
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise UpstreamError(502, "upstream_error", "OpenRouter embeddings response has the wrong shape")
        try:
            return [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
        except (KeyError, TypeError):
            raise UpstreamError(502, "upstream_error", "OpenRouter embeddings response has the wrong shape") from None

    async def chat(self, model: str, messages: list[dict], temperature: float, max_tokens: int | None,
                   timeout: float) -> tuple[str, dict]:
        payload: dict = {"model": model, "messages": messages, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        body = await self._post("/chat/completions", payload, timeout)
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            text = None
        if not isinstance(text, str) or not text.strip():
            raise UpstreamError(502, "empty_answer", "the LLM returned no text")
        return text, body.get("usage") or {}

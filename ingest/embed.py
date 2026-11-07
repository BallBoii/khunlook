"""Embedders (fake = free/offline, openrouter = paid), an on-disk cache, and L2 normalization."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("ingest")

STOPWORDS = frozenset(
    "a an and are as at be by for from has have how i in is it its of on or that the this to was what when "
    "where which who will with we you do does can our your us".split()
)


def l2_normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    norms = np.linalg.norm(m, axis=-1, keepdims=True)
    if np.any(norms == 0) or not np.all(np.isfinite(norms)):
        raise ValueError("cannot normalize a zero or non-finite vector")
    return (m / norms).astype(np.float32)


class FakeEmbedder:
    """Deterministic feature-hashing bag-of-words. Free, offline, lexical-only: for dev and tests."""

    paid = False

    def __init__(self, model: str = "fake-hash-v1", dim: int = 256):
        self.model, self.dim = model, dim

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for tok in re.findall(r"\w+", text.lower()):
                if tok in STOPWORDS:
                    continue
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "little")
                out[row, h % self.dim] += 1.0 if (h >> 63) else -1.0
            if not out[row].any():
                out[row, 0] = 1.0
        return out


class OpenRouterEmbedder:
    """OpenAI-compatible POST {base_url}/embeddings. Costs money - only used when explicitly selected."""

    paid = True

    def __init__(self, model: str, dim: int, base_url: str, api_key_env: str, max_retries: int = 5, **_):
        if not model or model == "CHANGE_ME" or not dim:
            raise ValueError("set embedding.openrouter.model and .dim in ingest/config.yaml first")
        self.model, self.dim, self.base_url, self.max_retries = model, int(dim), base_url.rstrip("/"), max_retries
        self.api_key_env = api_key_env

    def embed(self, texts: list[str]) -> np.ndarray:
        import httpx

        key = os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError(f"environment variable {self.api_key_env} is not set")
        for attempt in range(self.max_retries + 1):
            try:
                r = httpx.post(f"{self.base_url}/embeddings", timeout=120,
                               headers={"Authorization": f"Bearer {key}"},
                               json={"model": self.model, "input": texts})
            except httpx.TransportError as e:
                err = str(e)
            else:
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                if r.status_code == 200 and "data" in body:
                    break
                if r.status_code != 429 and r.status_code < 500:   # 4xx (bad key/model): retrying won't help
                    raise RuntimeError(f"embedding request failed: HTTP {r.status_code} {r.text[:300]}")
                err = f"HTTP {r.status_code}"
            if attempt == self.max_retries:
                raise RuntimeError(f"embedding request failed after {attempt + 1} attempts: {err}")
            log.warning("embedding request failed (%s), retrying in %ds", err, 2 ** attempt)
            time.sleep(2 ** attempt)
        data = sorted(body["data"], key=lambda d: d["index"])
        vecs = np.array([d["embedding"] for d in data], dtype=np.float32)
        if vecs.shape != (len(texts), self.dim):
            raise RuntimeError(f"expected {len(texts)} vectors of dim {self.dim}, got shape {vecs.shape} - "
                               "check embedding.openrouter.dim")
        return vecs


def make_embedder(cfg: dict, provider: str):
    if provider == "fake":
        return FakeEmbedder(**cfg["fake"])
    if provider == "openrouter":
        return OpenRouterEmbedder(**cfg["openrouter"])
    raise ValueError(f"unknown embedding provider {provider!r}")


class EmbeddingCache:
    """One .npy per sha256(model + text), so re-runs never pay twice for the same text."""

    def __init__(self, root: Path):
        self.root = root

    def _path(self, model: str, text: str) -> Path:
        return self.root / (hashlib.sha256(f"{model}\n{text}".encode()).hexdigest() + ".npy")

    def get(self, model: str, text: str) -> np.ndarray | None:
        p = self._path(model, text)
        return np.load(p) if p.exists() else None

    def put(self, model: str, text: str, vec: np.ndarray) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        np.save(self._path(model, text), vec.astype(np.float32))

    def missing(self, model: str, texts: list[str]) -> list[str]:
        return [t for t in texts if not self._path(model, t).exists()]


def embed_texts(texts: list[str], embedder, cache: EmbeddingCache | None, batch_size: int = 32) -> np.ndarray:
    """Embed with caching + batching; returns L2-normalized float32 [len(texts), dim]."""
    vecs: list[np.ndarray | None] = [cache.get(embedder.model, t) if cache else None for t in texts]
    todo = [i for i, v in enumerate(vecs) if v is None]
    for start in range(0, len(todo), batch_size):
        idx = todo[start:start + batch_size]
        out = embedder.embed([texts[i] for i in idx])
        for i, v in zip(idx, out):
            vecs[i] = v
            if cache:
                cache.put(embedder.model, texts[i], v)
        log.info("embedded %d/%d", min(start + batch_size, len(todo)), len(todo))
    if len(texts) == 0:
        return np.zeros((0, embedder.dim), dtype=np.float32)
    return l2_normalize(np.stack(vecs))

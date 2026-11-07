import json
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main  # noqa: E402
from app.search import BM25  # noqa: E402

TEXTS = [
    ("brief", "SDS Final Project", 9, "The demo is on Tuesday 17 November 2026, 13:00-16:00."),
    ("brief", "SDS Final Project", 3, "You need five Raspberry Pi 3 B+ boards as k3s worker nodes."),
    ("lec01", "Lecture 1", 1, "Software-defined systems separate the control plane from the data plane."),
    ("lec01", "Lecture 1", 2, "A Kubernetes Deployment keeps the desired number of pod replicas running."),
]


def write_index(d: Path, vectors=None, **manifest_overrides):
    d.mkdir(parents=True, exist_ok=True)
    chunks = [{"id": f"id-{i}", "doc_id": doc, "doc_title": title, "source_file": f"{doc}.pdf", "page": page,
               "chunk_index": 0, "text": text, "char_count": len(text)} for i, (doc, title, page, text) in enumerate(TEXTS)]
    if vectors is None:
        vectors = np.eye(len(TEXTS), 8, dtype=np.float32)      # chunk i points along axis i
    np.save(d / "vectors.npy", vectors)
    (d / "chunks.jsonl").write_text("\n".join(json.dumps(c) for c in chunks) + "\n", encoding="utf-8")
    manifest = {"schema_version": 1, "index_version": "abc12345", "embedding_model": "test-model",
                "embedding_dim": 8, "num_chunks": len(TEXTS), "distance": "cosine", "built_at": "2026-11-07T00:00:00+00:00",
                **manifest_overrides}
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    write_index(tmp_path / "index")
    monkeypatch.setenv("INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("MAX_TOP_K", "3")
    with TestClient(main.app) as c:
        yield c


def axis(i, scale=1.0, dim=8):
    v = [0.0] * dim
    v[i] = scale
    return v


def test_health_ready_info_and_headers(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.headers["X-Served-By"] == main.POD_NAME and r.headers["X-Request-ID"]
    assert client.get("/readyz").json() == {"status": "ready", "index_version": "abc12345"}
    info = client.get("/v1/info", headers={"X-Request-ID": "req-42"})
    assert info.headers["X-Request-ID"] == "req-42"
    body = info.json()
    assert body["embedding_dim"] == 8 and body["num_chunks"] == 4 and body["embedding_model"] == "test-model"
    assert {d["doc_id"]: d["pages"] for d in body["documents"]} == {"brief": 2, "lec01": 2}


def test_vector_search_normalizes_sorts_and_filters(client):
    q = [0.0] * 8
    q[1], q[0] = 5.0, 2.0                                   # unnormalized; closest to chunk 1, then chunk 0
    body = client.post("/v1/search", json={"vector": q, "top_k": 4, "min_score": 0.1}).json()
    assert body["method"] == "vector" and body["index_version"] == "abc12345"
    assert [r["id"] for r in body["results"]] == ["id-1", "id-0"]
    assert body["results"][0]["score"] == pytest.approx(5 / np.sqrt(29), abs=1e-5)
    assert set(body["results"][0]) == {"id", "score", "doc_id", "doc_title", "source_file", "page", "chunk_index", "text"}


def test_vector_search_doc_filter_and_min_score(client):
    body = client.post("/v1/search", json={"vector": axis(1), "doc_ids": ["lec01"], "min_score": 0.5}).json()
    assert body["results"] == []                             # lec01 chunks are orthogonal to the query
    body = client.post("/v1/search", json={"vector": axis(2), "doc_ids": ["lec01"]}).json()
    assert [r["doc_id"] for r in body["results"]] == ["lec01", "lec01"]


def test_top_k_is_capped(client):
    body = client.post("/v1/search", json={"vector": [1.0] * 8, "top_k": 50, "min_score": -1}).json()
    assert len(body["results"]) == 3                         # MAX_TOP_K=3 in this test


def test_dimension_mismatch_and_bad_requests(client):
    r = client.post("/v1/search", json={"vector": [1.0, 2.0]})
    assert r.status_code == 400 and r.json()["error"] == "dimension_mismatch" and "expects 8" in r.json()["detail"]
    r = client.post("/v1/search", json={"vector": [0.0] * 8})
    assert r.status_code == 400 and r.json()["error"] == "invalid_vector"
    r = client.post("/v1/search", json={"top_k": 4})
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"
    r = client.post("/v1/search", json={"vector": axis(0), "top_k": 0})
    assert r.status_code == 400


def test_keyword_search(client):
    body = client.post("/v1/search/keyword", json={"query": "When is the demo date?"}).json()
    assert body["method"] == "keyword" and body["results"][0]["page"] == 9 and body["results"][0]["score"] > 0
    body = client.post("/v1/search/keyword", json={"query": "raspberry pi", "doc_ids": ["lec01"]}).json()
    assert body["results"] == []
    assert client.post("/v1/search/keyword", json={"query": "the of is"}).json()["results"] == []


def test_get_chunk(client):
    assert client.get("/v1/chunks/id-2").json()["doc_id"] == "lec01"
    r = client.get("/v1/chunks/nope")
    assert r.status_code == 404 and r.json() == {"error": "not_found", "detail": "no chunk with id nope"}


@pytest.mark.parametrize("breakage", ["unnormalized", "wrong_dim", "missing"])
def test_invalid_index_stays_not_ready(tmp_path, monkeypatch, breakage):
    d = tmp_path / "index"
    if breakage == "unnormalized":
        write_index(d, vectors=np.eye(4, 8, dtype=np.float32) * 2)
    elif breakage == "wrong_dim":
        write_index(d, embedding_dim=16)
    monkeypatch.setenv("INDEX_DIR", str(d))
    with TestClient(main.app) as c:
        assert c.get("/healthz").status_code == 200           # alive...
        r = c.get("/readyz")
        assert r.status_code == 503 and r.json()["error"] == "not_ready"   # ...but never serves wrong data
        assert c.post("/v1/search", json={"vector": axis(0)}).status_code == 503
        assert r.headers["X-Served-By"]


def test_bm25_ranking():
    bm = BM25(["kubernetes pods pods", "kubernetes services", "raspberry pi"])
    s = bm.scores("pods")
    assert s[0] > 0 and s[1] == 0 and s[2] == 0
    s = bm.scores("kubernetes pods")
    assert s[0] > s[1] > s[2] == 0

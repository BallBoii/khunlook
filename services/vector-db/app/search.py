"""Index loading/validation, brute-force cosine search and BM25 keyword search. Read-only, in memory.

Also the `validate-index` tool:  python services/vector-db/app/search.py index/
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

STOPWORDS = frozenset(
    "a an and are as at be by for from has have how i in is it its of on or that the this to was what when "
    "where which who will with we you do does can our your us".split()
)
TOKEN_RE = re.compile(r"\w+")


class IndexValidationError(Exception):
    pass


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b, self.n = k1, b, len(docs)
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        lengths = np.zeros(self.n, dtype=np.float32)
        for i, doc in enumerate(docs):
            toks = tokenize(doc)
            lengths[i] = len(toks)
            for term, tf in Counter(toks).items():
                postings[term].append((i, tf))
        self.norm = k1 * (1 - b + b * lengths / max(float(lengths.mean()) if self.n else 0.0, 1e-9))
        self.postings = {t: (np.array([d for d, _ in p]), np.array([f for _, f in p], dtype=np.float32))
                         for t, p in postings.items()}

    def scores(self, query: str) -> np.ndarray:
        out = np.zeros(self.n, dtype=np.float32)
        for term in set(tokenize(query)):
            if term not in self.postings:
                continue
            docs, tf = self.postings[term]
            idf = math.log(1 + (self.n - len(docs) + 0.5) / (len(docs) + 0.5))
            out[docs] += idf * tf * (self.k1 + 1) / (tf + self.norm[docs])
        return out


@dataclass
class Index:
    manifest: dict
    chunks: list[dict]
    vectors: np.ndarray
    doc_ids: np.ndarray = field(init=False)
    by_id: dict[str, int] = field(init=False)
    bm25: BM25 = field(init=False)

    def __post_init__(self):
        self.doc_ids = np.array([c["doc_id"] for c in self.chunks])
        self.by_id = {c["id"]: i for i, c in enumerate(self.chunks)}
        self.bm25 = BM25([f"{c['doc_title']} {c['text']}" for c in self.chunks])

    @property
    def dim(self) -> int:
        return int(self.manifest["embedding_dim"])

    def documents(self) -> list[dict]:
        docs: dict[str, dict] = {}
        for c in self.chunks:
            d = docs.setdefault(c["doc_id"], {"doc_id": c["doc_id"], "title": c["doc_title"], "pages": set()})
            d["pages"].add(c["page"])
        return [{**d, "pages": len(d["pages"])} for d in docs.values()]

    def _top(self, scores: np.ndarray, top_k: int, doc_ids: list[str] | None, min_score: float) -> list[tuple[int, float]]:
        if doc_ids:
            scores = np.where(np.isin(self.doc_ids, doc_ids), scores, -np.inf)
        k = min(top_k, len(scores))
        if k <= 0:
            return []
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top], kind="stable")]
        return [(int(i), float(scores[i])) for i in top if np.isfinite(scores[i]) and scores[i] >= min_score]

    def vector_search(self, vector: list[float], top_k: int, min_score: float, doc_ids: list[str] | None):
        q = np.asarray(vector, dtype=np.float32)
        q = q / np.linalg.norm(q)
        return self._top(self.vectors @ q, top_k, doc_ids, min_score)

    def keyword_search(self, query: str, top_k: int, doc_ids: list[str] | None):
        return self._top(self.bm25.scores(query), top_k, doc_ids, min_score=1e-9)  # only docs matching a term


def validate(manifest: dict, chunks: list[dict], vectors: np.ndarray) -> None:
    def fail(msg: str):
        raise IndexValidationError(msg)

    for key in ("schema_version", "index_version", "embedding_model", "embedding_dim", "num_chunks"):
        if key not in manifest:
            fail(f"manifest.json missing {key!r}")
    if manifest["schema_version"] != 1:
        fail(f"unsupported schema_version {manifest['schema_version']}")
    n, d = manifest["num_chunks"], manifest["embedding_dim"]
    if vectors.dtype != np.float32:
        fail(f"vectors.npy dtype is {vectors.dtype}, expected float32")
    if vectors.shape != (n, d):
        fail(f"vectors.npy shape {vectors.shape} != (num_chunks={n}, embedding_dim={d})")
    if len(chunks) != n:
        fail(f"chunks.jsonl has {len(chunks)} rows, manifest says {n}")
    if not np.all(np.isfinite(vectors)):
        fail("vectors.npy contains NaN/inf")
    norms = np.linalg.norm(vectors, axis=1)
    if n and np.max(np.abs(norms - 1)) > 1e-3:
        fail(f"vectors are not L2-normalized (norm range {norms.min():.4f}..{norms.max():.4f})")
    ids = [c.get("id") for c in chunks]
    if len(set(ids)) != len(ids) or None in ids:
        fail("chunk ids are missing or not unique")
    required = ("doc_id", "doc_title", "source_file", "page", "chunk_index", "text")
    for i, c in enumerate(chunks):
        missing = [k for k in required if k not in c]
        if missing:
            fail(f"chunks.jsonl line {i + 1} missing {missing}")


def load_index(index_dir: str | Path) -> Index:
    d = Path(index_dir)
    try:
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        chunks = [json.loads(l) for l in (d / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        vectors = np.load(d / "vectors.npy", allow_pickle=False)
    except (OSError, ValueError) as e:
        raise IndexValidationError(f"cannot read index in {d}: {e}") from e
    validate(manifest, chunks, vectors)
    return Index(manifest, chunks, vectors)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "index"
    try:
        idx = load_index(path)
    except IndexValidationError as e:
        print(f"INVALID index {path}: {e}", file=sys.stderr)
        sys.exit(1)
    m = idx.manifest
    print(f"OK {path}: {m['num_chunks']} chunks, dim {m['embedding_dim']}, model {m['embedding_model']}, "
          f"index_version {m['index_version']}, {len(idx.documents())} documents")

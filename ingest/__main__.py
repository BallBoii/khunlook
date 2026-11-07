"""Offline ingestion CLI (laptop only).

    python -m ingest ingest   [--provider fake|openrouter] [--yes]   build index/ + query fixtures
    python -m ingest estimate [--provider openrouter]                free dry run: what a paid run would send
    python -m ingest eval                                            hit@1 / hit@4 of the fixtures vs the index
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml

from .embed import EmbeddingCache, embed_texts, make_embedder
from .pipeline import Chunk, build_chunks, load_manifest

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("ingest")


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key in ("corpus_dir", "index_dir", "cache_dir", "questions_file"):
        cfg[key] = (ROOT / cfg[key]).resolve()
    return cfg


def load_questions(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("questions") or []


def make_chunks(cfg: dict) -> list[Chunk]:
    manifest = yaml.safe_load((cfg["corpus_dir"] / "manifest.yaml").read_text(encoding="utf-8"))
    chunks = build_chunks(cfg["corpus_dir"], load_manifest(manifest), cfg["cleaning"], cfg["chunking"])
    if not chunks:
        raise SystemExit("no chunks produced - is corpus/manifest.yaml empty?")
    return chunks


def index_version(model: str, dim: int, chunks: list[Chunk]) -> str:
    h = hashlib.sha256(f"{model}|{dim}".encode())
    for c in chunks:
        h.update(f"|{c.id}|{c.text}".encode())
    return h.hexdigest()[:8]


def write_jsonl(path: Path, rows) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def cmd_estimate(cfg: dict, provider: str) -> dict:
    chunks = make_chunks(cfg)
    texts = [c.embedding_text() for c in chunks] + [q["question"] for q in load_questions(cfg["questions_file"])]
    model = cfg["embedding"].get(provider, {}).get("model", provider)
    missing = EmbeddingCache(cfg["cache_dir"]).missing(model, texts)
    chars = sum(len(t) for t in missing)
    est = {"provider": provider, "model": model, "chunks": len(chunks), "texts_total": len(texts),
           "uncached_texts": len(missing), "uncached_chars": chars, "approx_tokens": chars // 4}
    print(json.dumps(est, indent=2))
    return est


def cmd_ingest(cfg: dict, provider: str, assume_yes: bool) -> None:
    ecfg = cfg["embedding"]
    embedder = make_embedder(ecfg, provider)
    if embedder.paid:
        est = cmd_estimate(cfg, provider)
        if est["uncached_texts"] and not assume_yes:
            try:
                answer = input(f"This sends {est['uncached_texts']} texts (~{est['approx_tokens']} tokens) to "
                               f"{embedder.model} and COSTS MONEY. Continue? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() != "y":
                raise SystemExit("aborted - nothing was sent")
    chunks = make_chunks(cfg)
    questions = load_questions(cfg["questions_file"])
    cache = EmbeddingCache(cfg["cache_dir"]) if embedder.paid else None
    batch = ecfg.get(provider, {}).get("batch_size", 32)

    vectors = embed_texts([c.embedding_text() for c in chunks], embedder, cache, batch)
    qvecs = embed_texts([q["question"] for q in questions], embedder, cache, batch)

    out = cfg["index_dir"]
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / "vectors.npy", vectors)
    write_jsonl(out / "chunks.jsonl", (c.to_dict() for c in chunks))
    write_jsonl(out / "query_fixtures.jsonl", (
        {"id": q.get("id", f"q{i + 1:02d}"), "question": q["question"], "embedding_model": embedder.model,
         "expected": q.get("expected") or [], "vector": [round(float(x), 7) for x in v]}
        for i, (q, v) in enumerate(zip(questions, qvecs))))
    manifest = {"schema_version": 1, "index_version": index_version(embedder.model, embedder.dim, chunks),
                "embedding_model": embedder.model, "embedding_dim": embedder.dim, "num_chunks": len(chunks),
                "distance": "cosine", "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log.info("wrote %s: %d chunks, dim %d, index_version %s, %d fixtures",
             out, len(chunks), embedder.dim, manifest["index_version"], len(questions))


def cmd_eval(cfg: dict, top_k: int = 4) -> dict:
    idx = cfg["index_dir"]
    manifest = json.loads((idx / "manifest.json").read_text(encoding="utf-8"))
    vectors = np.load(idx / "vectors.npy")
    chunks = [json.loads(l) for l in (idx / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if l]
    fixtures = [json.loads(l) for l in (idx / "query_fixtures.jsonl").read_text(encoding="utf-8").splitlines() if l]

    hit1 = hit4 = n = 0
    first_hit_scores, miss_best = [], []
    for fx in fixtures:
        expected = {(e["doc_id"], int(e["page"])) for e in fx["expected"]}
        if not expected:
            print(f"  skip {fx['id']}: no expected pages in questions.yaml")
            continue
        if fx["embedding_model"] != manifest["embedding_model"]:
            raise SystemExit(f"fixture {fx['id']} was embedded with {fx['embedding_model']}, "
                             f"index uses {manifest['embedding_model']} - re-run ingest")
        n += 1
        scores = vectors @ np.asarray(fx["vector"], dtype=np.float32)
        top = np.argsort(-scores)[:top_k]
        ranks = [r for r, row in enumerate(top) if (chunks[row]["doc_id"], chunks[row]["page"]) in expected]
        hit1 += bool(ranks and ranks[0] == 0)
        hit4 += bool(ranks)
        if ranks:
            first_hit_scores.append(float(scores[top[ranks[0]]]))
        else:
            miss_best.append(float(scores[top[0]]))
            got = ", ".join(f"{chunks[r]['doc_id']}:p{chunks[r]['page']}({scores[r]:.2f})" for r in top)
            print(f"  MISS {fx['id']} {fx['question']!r} -> {got}")

    if not n:
        print("no fixtures with expected pages - fill in ingest/questions.yaml")
        return {}
    report = {"model": manifest["embedding_model"], "questions": n,
              f"hit@1": round(hit1 / n, 3), f"hit@{top_k}": round(hit4 / n, 3)}
    if first_hit_scores:
        # highest threshold that still keeps every correct hit, with a small safety margin
        report["correct_hit_score_min"] = round(min(first_hit_scores), 3)
        report["suggested_min_score"] = round(max(0.0, min(first_hit_scores) - 0.05), 2)
    if miss_best:
        report["miss_best_score_max"] = round(max(miss_best), 3)
    print(json.dumps(report, indent=2))
    return report


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m ingest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["ingest", "estimate", "eval"])
    p.add_argument("--provider", choices=["fake", "openrouter"], help="override embedding.provider in config")
    p.add_argument("--yes", action="store_true", help="skip the cost confirmation prompt")
    p.add_argument("--config", type=Path, default=ROOT / "ingest" / "config.yaml")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    cfg = load_config(args.config)
    provider = args.provider or cfg["embedding"]["provider"]
    if args.command == "ingest":
        cmd_ingest(cfg, provider, args.yes)
    elif args.command == "estimate":
        cmd_estimate(cfg, "openrouter" if provider == "fake" else provider)
    else:
        cmd_eval(cfg)


if __name__ == "__main__":
    main()

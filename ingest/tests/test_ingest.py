import importlib.util
import json
import sys
import uuid
from pathlib import Path

import numpy as np
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ingest import __main__ as cli  # noqa: E402
from ingest.embed import EmbeddingCache, FakeEmbedder, OpenRouterEmbedder, embed_texts, l2_normalize  # noqa: E402
from ingest.pipeline import Document, chunk_document, chunk_id, clean_pages, normalize_line, split_text  # noqa: E402

CFG = yaml.safe_load((ROOT / "ingest" / "config.yaml").read_text(encoding="utf-8"))
DOC = Document("deck", "Deck", "deck.pdf", "slides")


# ---------------------------------------------------------------- cleaning

def test_normalize_line_bullets_and_whitespace():
    assert normalize_line("  ▸   Pods   are\tcattle ") == "- Pods are cattle"
    assert normalize_line("• item") == "- item"
    assert normalize_line("plain   text") == "plain text"


def test_clean_pages_drops_repeated_headers_page_numbers_and_email():
    pages = [f"Software-Defined Systems\nreal content {i}\nkunwadee (AT) cp.eng.chula.ac.th\n{i}" for i in range(1, 6)]
    cleaned = clean_pages(pages, CFG["cleaning"])
    assert cleaned == [f"real content {i}" for i in range(1, 6)]


def test_clean_pages_keeps_numbers_that_are_not_the_page_number():
    cleaned = clean_pages(["Timeline\nSEP\n29\nNOV\n17\n1", "2110415 SEMESTER 1/2026\nbody\n2"], CFG["cleaning"])
    assert cleaned == ["Timeline\nSEP\n29\nNOV\n17", "2110415 SEMESTER 1/2026\nbody"]


def test_clean_pages_keeps_repeats_in_short_docs():
    assert clean_pages(["Same line", "Same line"], CFG["cleaning"]) == ["Same line", "Same line"]


# ---------------------------------------------------------------- chunking

CHUNK_CFG = {"min_chars": 80, "max_chars": 1200, "overlap_chars": 150}


def test_short_page_merges_into_next_page():
    pages = [(1, "Kubernetes"), (2, "x" * 100), (3, "y" * 100)]
    chunks = chunk_document(DOC, pages, CHUNK_CFG)
    assert [c.page for c in chunks] == [2, 3]
    assert chunks[0].text.startswith("Kubernetes\n\n")


def test_short_trailing_page_appends_to_previous():
    chunks = chunk_document(DOC, [(1, "x" * 100), (2, "Thanks!")], CHUNK_CFG)
    assert len(chunks) == 1 and chunks[0].text.endswith("Thanks!")


def test_empty_pages_are_skipped():
    assert [c.page for c in chunk_document(DOC, [(1, ""), (2, "z" * 100)], CHUNK_CFG)] == [2]


def test_split_long_text_respects_max_and_overlaps():
    lines = [f"- bullet number {i:03d} " + "w" * 40 for i in range(60)]
    pieces = split_text("\n".join(lines), 1200, 150)
    assert len(pieces) > 1
    assert all(len(p) <= 1200 for p in pieces)
    for a, b in zip(pieces, pieces[1:]):            # next chunk starts with the tail of the previous one
        assert b.split("\n")[0] in a.split("\n")
    assert all(l in "\n".join(pieces) for l in lines)


def test_split_hard_splits_a_single_huge_line():
    pieces = split_text("word " * 600, 1200, 150)
    assert all(len(p) <= 1200 for p in pieces) and len(pieces) >= 3


def test_chunk_ids_are_deterministic_uuid5():
    assert chunk_id("d", 3, 0) == chunk_id("d", 3, 0) == str(uuid.uuid5(uuid.NAMESPACE_URL, "d:3:0"))
    assert chunk_id("d", 3, 0) != chunk_id("d", 3, 1)
    c = chunk_document(DOC, [(9, "q" * 100)], CHUNK_CFG)[0]
    assert c.id == chunk_id("deck", 9, 0) and c.embedding_text() == "Deck - page 9\n" + "q" * 100


# ---------------------------------------------------------------- embedding

def test_l2_normalize():
    m = l2_normalize(np.array([[3.0, 4.0], [1.0, 0.0]]))
    assert m.dtype == np.float32 and np.allclose(np.linalg.norm(m, axis=1), 1)
    with pytest.raises(ValueError):
        l2_normalize(np.zeros((1, 3)))


def test_fake_embedder_is_deterministic_and_lexical():
    e = FakeEmbedder(dim=256)
    a, b = embed_texts(["demo date in November", "demo date in November"], e, None)
    c = embed_texts(["raspberry pi cluster"], e, None)[0]
    assert np.allclose(a, b) and float(a @ c) < 0.5


class CountingEmbedder(FakeEmbedder):
    paid = True

    def __init__(self):
        super().__init__(model="counting", dim=16)
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return super().embed(texts)


def test_cache_prevents_paying_twice_and_batches(tmp_path):
    e, cache = CountingEmbedder(), EmbeddingCache(tmp_path)
    texts = [f"text {i}" for i in range(5)]
    first = embed_texts(texts, e, cache, batch_size=2)
    assert [len(c) for c in e.calls] == [2, 2, 1]
    second = embed_texts(texts + ["new one"], e, cache, batch_size=2)
    assert e.calls[-1] == ["new one"] and len(e.calls) == 4
    assert np.allclose(first, second[:5])


@pytest.mark.parametrize("override", [{"model": "CHANGE_ME"}, {"model": ""}, {"dim": 0}])
def test_openrouter_requires_model_and_dim(override):
    with pytest.raises(ValueError):
        OpenRouterEmbedder(**{**CFG["embedding"]["openrouter"], **override})


# ---------------------------------------------------------------- end to end

def make_corpus(tmp_path: Path) -> dict:
    import fitz

    corpus = tmp_path / "corpus"
    (corpus / "raw").mkdir(parents=True)
    (corpus / "notes").mkdir()
    pdf = fitz.open()
    bodies = ["Final Project", "The demo is on Tuesday 17 November 2026 from 13:00 to 16:00 in the lab room.",
              "Each group needs five Raspberry Pi 3 B+ boards as worker nodes in a k3s cluster.",
              "Every service must run two replicas and survive unplugging a random Raspberry Pi."]
    for i, body in enumerate(bodies, 1):
        page = pdf.new_page()
        page.insert_textbox(fitz.Rect(40, 40, 560, 400), f"2110415 Software-Defined Systems\n{body}\n{i}", fontsize=11)
    pdf.save(corpus / "raw" / "brief.pdf")
    (corpus / "notes" / "rubric.md").write_text("Grading rubric: deployment 30%, fault tolerance 30%, demo 40%.",
                                                encoding="utf-8")
    (corpus / "manifest.yaml").write_text(yaml.safe_dump({"documents": [
        {"doc_id": "brief", "title": "SDS Final Project", "source_file": "brief.pdf"},
        {"doc_id": "rubric", "title": "Rubric", "source_file": "rubric.md", "page": 10}]}), encoding="utf-8")
    qfile = tmp_path / "questions.yaml"
    qfile.write_text(yaml.safe_dump({"questions": [
        {"id": "q1", "question": "When is the demo?", "expected": [{"doc_id": "brief", "page": 2}]},
        {"id": "q2", "question": "How many Raspberry Pi boards?", "expected": [{"doc_id": "brief", "page": 3}]},
        {"id": "q3", "question": "What is the grading rubric?", "expected": []}]}), encoding="utf-8")
    return {**CFG, "corpus_dir": corpus, "index_dir": tmp_path / "index", "cache_dir": tmp_path / "cache",
            "questions_file": qfile}


def load_vector_db_search():
    path = ROOT / "services" / "vector-db" / "app" / "search.py"
    spec = importlib.util.spec_from_file_location("vdb_search", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vdb_search"] = mod          # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


def test_end_to_end_fake_ingest_validates_and_evals(tmp_path):
    cfg = make_corpus(tmp_path)
    cli.cmd_ingest(cfg, "fake", assume_yes=False)

    idx = load_vector_db_search().load_index(cfg["index_dir"])   # same validation the service runs
    pages = {(c["doc_id"], c["page"]) for c in idx.chunks}
    assert pages == {("brief", 2), ("brief", 3), ("brief", 4), ("rubric", 10)}  # title page merged into p2
    assert all("2110415" not in c["text"] for c in idx.chunks)
    assert idx.manifest["embedding_model"] == "fake-hash-v1" and idx.vectors.shape == (4, 256)

    fixtures = [json.loads(l) for l in (cfg["index_dir"] / "query_fixtures.jsonl").read_text().splitlines()]
    assert len(fixtures) == 3 and len(fixtures[0]["vector"]) == 256

    report = cli.cmd_eval(cfg)
    assert report["questions"] == 2 and report["hit@4"] == 1.0

    version = idx.manifest["index_version"]
    cli.cmd_ingest(cfg, "fake", assume_yes=False)                 # reproducible
    assert json.loads((cfg["index_dir"] / "manifest.json").read_text())["index_version"] == version


def test_paid_ingest_aborts_without_confirmation(tmp_path, monkeypatch):
    cfg = make_corpus(tmp_path)
    paid = CountingEmbedder()
    monkeypatch.setattr(cli, "make_embedder", lambda *_: paid)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    with pytest.raises(SystemExit):
        cli.cmd_ingest(cfg, "openrouter", assume_yes=False)
    assert paid.calls == [] and not (cfg["index_dir"]).exists()

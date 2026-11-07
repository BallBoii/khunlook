"""Corpus -> clean text -> chunks.  extract -> clean -> chunk, all deterministic."""
from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("ingest")

BULLET_RE = re.compile(r"^[▸►▪▫•◦‣●○■□➢➤✓✔∙·\-–—*]+\s*")
SPACE_RE = re.compile(r"[ \t  -​　]+")


@dataclass
class Document:
    doc_id: str
    title: str
    source_file: str
    type: str          # slides | notes
    page: int = 1      # notes only


@dataclass
class Chunk:
    id: str
    doc_id: str
    doc_title: str
    source_file: str
    page: int
    chunk_index: int
    text: str
    char_count: int

    def embedding_text(self) -> str:
        return f"{self.doc_title} - page {self.page}\n{self.text}"

    def to_dict(self) -> dict:
        return asdict(self)


def chunk_id(doc_id: str, page: int, chunk_index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}:{page}:{chunk_index}"))


# ---------------------------------------------------------------- extract

def load_manifest(raw: dict) -> list[Document]:
    docs = []
    for d in raw.get("documents") or []:
        dtype = d.get("type") or ("notes" if d["source_file"].endswith(".md") else "slides")
        docs.append(Document(d["doc_id"], d["title"], d["source_file"], dtype, int(d.get("page", 1))))
    ids = [d.doc_id for d in docs]
    dupes = [i for i, n in Counter(ids).items() if n > 1]
    if dupes:
        raise ValueError(f"duplicate doc_id in corpus manifest: {dupes}")
    return docs


def source_path(corpus_dir: Path, doc: Document) -> Path:
    return corpus_dir / ("notes" if doc.type == "notes" else "raw") / doc.source_file


def extract_pages(path: Path, doc: Document) -> list[tuple[int, str]]:
    """Return [(page_number (1-based), raw_text)]."""
    if not path.exists():
        raise FileNotFoundError(f"{doc.doc_id}: {path} not found (add it or remove it from corpus/manifest.yaml)")
    if doc.type == "notes":
        return [(doc.page, path.read_text(encoding="utf-8"))]
    import fitz  # PyMuPDF; imported lazily so the rest is usable without it

    with fitz.open(path) as pdf:
        return [(i + 1, page.get_text("text")) for i, page in enumerate(pdf)]


# ---------------------------------------------------------------- clean

def normalize_line(line: str) -> str:
    line = SPACE_RE.sub(" ", line).strip()
    if BULLET_RE.match(line):
        rest = BULLET_RE.sub("", line)
        line = f"- {rest}" if rest else ""
    return line


def clean_pages(pages: list[str], cfg: dict, page_numbers: list[int] | None = None) -> list[str]:
    """Clean all pages of one document (needs the whole doc to spot repeated headers/footers)."""
    drop = [re.compile(p, re.IGNORECASE) for p in cfg.get("drop_patterns", [])]
    page_numbers = page_numbers or list(range(1, len(pages) + 1))
    lines_per_page = [[normalize_line(l) for l in p.splitlines()] for p in pages]

    repeated: set[str] = set()
    if len(pages) >= cfg.get("min_pages_for_repeat", 3):
        counts = Counter(l for lines in lines_per_page for l in set(lines) if l)
        limit = cfg.get("repeated_line_ratio", 0.6) * len(pages)
        repeated = {l for l, n in counts.items() if n >= limit}

    def keep(line: str, page_no: int) -> bool:
        return bool(line) and line not in repeated and line != str(page_no) and not any(r.fullmatch(line) for r in drop)

    out = []
    for lines, page_no in zip(lines_per_page, page_numbers):
        kept = [l if keep(l, page_no) else "" for l in lines]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
        out.append(text)
    return out


# ---------------------------------------------------------------- chunk

def _units(text: str, max_chars: int) -> list[str]:
    """Split text into lines (bullets/paragraphs); hard-split any line longer than max_chars on words."""
    units = []
    for line in (l for l in text.split("\n") if l.strip()):
        while len(line) > max_chars:
            cut = line.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            units.append(line[:cut].strip())
            line = line[cut:].strip()
        if line:
            units.append(line)
    return units


def split_text(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    for unit in _units(text, max_chars):
        if cur and len("\n".join(cur + [unit])) > max_chars:
            chunks.append("\n".join(cur))
            tail: list[str] = []          # carry trailing whole lines (<= overlap chars) into the next chunk
            for prev in reversed(cur):
                if len("\n".join([prev] + tail)) > overlap:
                    break
                tail.insert(0, prev)
            cur = tail if len("\n".join(tail + [unit])) <= max_chars else []
        cur.append(unit)
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def chunk_document(doc: Document, pages: list[tuple[int, str]], cfg: dict) -> list[Chunk]:
    """pages = [(page_number, clean_text)]. Merges short pages forward, splits long ones."""
    min_chars, max_chars, overlap = cfg["min_chars"], cfg["max_chars"], cfg["overlap_chars"]

    merged: list[tuple[int, str]] = []
    carry = ""
    for page_no, text in pages:
        if not text:
            continue
        text = f"{carry}\n\n{text}" if carry else text
        if len(text) < min_chars:
            carry = text                  # e.g. a section-title slide: attach it to the next page
            continue
        carry = ""
        merged.append((page_no, text))
    if carry:                             # short trailing page: append to the previous page if any
        if merged:
            page_no, text = merged[-1]
            merged[-1] = (page_no, f"{text}\n\n{carry}")
        else:
            merged.append((pages[-1][0], carry))

    chunks = []
    for page_no, text in merged:
        for i, piece in enumerate(split_text(text, max_chars, overlap)):
            chunks.append(Chunk(chunk_id(doc.doc_id, page_no, i), doc.doc_id, doc.title, doc.source_file,
                                page_no, i, piece, len(piece)))
    return chunks


def build_chunks(corpus_dir: Path, docs: list[Document], clean_cfg: dict, chunk_cfg: dict) -> list[Chunk]:
    all_chunks: list[Chunk] = []
    for doc in docs:
        raw = extract_pages(source_path(corpus_dir, doc), doc)
        cleaned = clean_pages([t for _, t in raw], clean_cfg, [p for p, _ in raw])
        for (page_no, _), text in zip(raw, cleaned):
            if len(text) < clean_cfg.get("empty_page_chars", 30):
                log.warning("%s page %d has almost no text (%d chars) - image-only? consider a note in corpus/notes/",
                            doc.doc_id, page_no, len(text))
        chunks = chunk_document(doc, list(zip([p for p, _ in raw], cleaned)), chunk_cfg)
        log.info("%s: %d pages -> %d chunks", doc.doc_id, len(raw), len(chunks))
        all_chunks.extend(chunks)
    return all_chunks

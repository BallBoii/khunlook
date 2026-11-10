"""Corpus -> clean text -> chunks.  extract -> clean -> chunk, all deterministic."""
from __future__ import annotations

import html
import logging
import re
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("ingest")

BULLET_RE = re.compile(r"^[▸►▪▫•◦‣●○■□➢➤✓✔∙·\-–—*]+\s*")
SPACE_RE = re.compile(r"[ \t  -​　]+")
BLOCK_END_RE = re.compile(r"<br\s*/?>|</(p|div|li|ul|ol|h\d|blockquote|tr)\s*>", re.IGNORECASE)
BLOCK_END_EOL_RE = re.compile(rf"(?:{BLOCK_END_RE.pattern})+[ \t]*$", re.IGNORECASE | re.MULTILINE)
TAG_RE = re.compile(r"<[^>]+>")
SOURCE_DIRS = {"slides": "raw", "notes": "notes", "course": "courses"}


@dataclass
class Document:
    doc_id: str
    title: str
    source_file: str
    type: str          # slides | notes | course
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
    kind: str = field(default="page", repr=False)   # not serialized: "course" chunks have no meaningful page

    def embedding_text(self) -> str:
        if self.kind == "course":
            return f"{self.doc_title}\n{self.text}"
        return f"{self.doc_title} - page {self.page}\n{self.text}"

    def to_dict(self) -> dict:
        d = asdict(self)
        del d["kind"]
        return d


def chunk_id(doc_id: str, page: int, chunk_index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}:{page}:{chunk_index}"))


# ---------------------------------------------------------------- extract

def markdown_title(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def load_manifest(raw: dict, corpus_dir: Path) -> list[Document]:
    """Explicit entries, plus `glob: courses/*.md` entries (one course per file: doc_id = file stem,
    title = the file's `# ` heading)."""
    docs = []
    for d in raw.get("documents") or []:
        if "glob" in d:
            dtype = d.get("type", "course")
            paths = sorted(corpus_dir.glob(d["glob"]))
            if not paths:
                raise FileNotFoundError(f"manifest glob {d['glob']!r} matched no files under {corpus_dir}")
            for p in paths:
                if p.parent != corpus_dir / SOURCE_DIRS[dtype]:
                    raise ValueError(f"{p}: {dtype} files must live in {corpus_dir / SOURCE_DIRS[dtype]}")
                docs.append(Document(p.stem, markdown_title(p), p.name, dtype))
            continue
        dtype = d.get("type") or ("notes" if d["source_file"].endswith(".md") else "slides")
        docs.append(Document(d["doc_id"], d["title"], d["source_file"], dtype, int(d.get("page", 1))))
    ids = [d.doc_id for d in docs]
    dupes = [i for i, n in Counter(ids).items() if n > 1]
    if dupes:
        raise ValueError(f"duplicate doc_id in corpus manifest: {dupes}")
    return docs


def source_path(corpus_dir: Path, doc: Document) -> Path:
    return corpus_dir / SOURCE_DIRS[doc.type] / doc.source_file


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


def clean_markdown(text: str) -> str:
    """Course markdown: HTML (from student reviews) -> plain text, drop **bold**, normalize lines."""
    text = BLOCK_END_EOL_RE.sub("", text)     # a block end at the end of a line is already a line break
    text = BLOCK_END_RE.sub("\n", text)       # other paragraph/list ends become line breaks (split points)
    text = TAG_RE.sub("", text)
    text = html.unescape(text).replace("**", "")
    lines = [normalize_line(l) for l in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


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


def markdown_sections(text: str) -> list[tuple[str, str]]:
    """[(heading line or "", body)] split on `## `; the `# title` line is dropped (it is the doc_title)."""
    sections: list[tuple[str, str]] = []
    heading, buf = "", []
    for line in text.split("\n"):
        if line.startswith("## "):
            sections.append((heading, "\n".join(buf).strip()))
            heading, buf = line, []
        elif line.startswith("# ") and not sections and not heading:
            continue
        else:
            buf.append(line)
    sections.append((heading, "\n".join(buf).strip()))
    return [(h, b) for h, b in sections if b]


def chunk_markdown(doc: Document, text: str, cfg: dict) -> list[Chunk]:
    """Pack whole `##` sections into chunks <= max_chars; split oversized sections (reviews, long
    tables) and repeat the heading (and a table's header row) at the top of every piece."""
    max_chars, overlap = cfg["max_chars"], cfg["overlap_chars"]
    blocks: list[str] = []
    for heading, body in markdown_sections(text):
        block = f"{heading}\n{body}" if heading else body
        if len(block) <= max_chars:
            blocks.append(block)
            continue
        prefix, lines = [heading] if heading else [], body.split("\n")
        if len(lines) > 2 and lines[0].startswith("|") and lines[1].startswith("|"):
            prefix, body = prefix + lines[:2], "\n".join(lines[2:])
        head = "\n".join(prefix)
        for piece in split_text(body, max_chars - len(head) - 1, overlap):
            blocks.append(f"{head}\n{piece}" if head else piece)

    texts: list[str] = []
    for b in blocks:
        if texts and len(texts[-1]) + 2 + len(b) <= max_chars:
            texts[-1] += "\n\n" + b
        else:
            texts.append(b)
    return [Chunk(chunk_id(doc.doc_id, 1, i), doc.doc_id, doc.title, doc.source_file, 1, i, t, len(t), kind="course")
            for i, t in enumerate(texts)]


def build_chunks(corpus_dir: Path, docs: list[Document], clean_cfg: dict, chunk_cfg: dict) -> list[Chunk]:
    all_chunks: list[Chunk] = []
    courses = 0
    for doc in docs:
        if doc.type == "course":
            path = source_path(corpus_dir, doc)
            chunks = chunk_markdown(doc, clean_markdown(path.read_text(encoding="utf-8")), chunk_cfg)
            if not chunks:
                log.warning("%s: no text", doc.doc_id)
            all_chunks.extend(chunks)
            courses += 1
            continue
        raw = extract_pages(source_path(corpus_dir, doc), doc)
        cleaned = clean_pages([t for _, t in raw], clean_cfg, [p for p, _ in raw])
        for (page_no, _), text in zip(raw, cleaned):
            if len(text) < clean_cfg.get("empty_page_chars", 30):
                log.warning("%s page %d has almost no text (%d chars) - image-only? consider a note in corpus/notes/",
                            doc.doc_id, page_no, len(text))
        chunks = chunk_document(doc, list(zip([p for p, _ in raw], cleaned)), chunk_cfg)
        log.info("%s: %d pages -> %d chunks", doc.doc_id, len(raw), len(chunks))
        all_chunks.extend(chunks)
    if courses:
        log.info("%d course files -> %d chunks", courses, sum(c.kind == "course" for c in all_chunks))
    return all_chunks

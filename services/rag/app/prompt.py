"""Turns the question + retrieved chunks into chat messages for the LLM."""
from __future__ import annotations

# Change the SYSTEM_PROMPT for other context.
SYSTEM_PROMPT = """You are KhunLook, a teaching assistant for the course 2110415 Software-Defined Systems.
Answer the question using ONLY the numbered context passages in the user's message.
- Cite the passages you used like [1] or [2][3].
- If the passages do not contain the answer, say you don't know. Do not guess.
- Answer in the same language as the question (Thai or English). Keep it short and clear."""


def select_context(hits: list[dict], max_chars: int) -> list[dict]:
    """Keep the best hits (already sorted by score) until the character budget is used up."""
    used, total = [], 0
    for h in hits:
        if total + len(h["text"]) > max_chars and used:   # always keep at least the best hit
            break
        used.append(h)
        total += len(h["text"])
    return used


def format_passage(n: int, hit: dict) -> str:
    return f"[{n}] {hit['doc_title']}, page {hit['page']}:\n{hit['text'].strip()}"


def build_messages(question: str, hits: list[dict]) -> list[dict]:
    if hits:
        context = "\n\n".join(format_passage(n, h) for n, h in enumerate(hits, start=1))
    else:
        context = "(no relevant passages were found)"
    user = f"Context passages:\n\n{context}\n\nQuestion: {question.strip()}"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]

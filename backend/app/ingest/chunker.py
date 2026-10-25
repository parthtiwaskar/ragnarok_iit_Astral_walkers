"""Paragraph-aware chunker: about 350 tokens with a 60-token overlap (DATA_PIPELINE.md §10)."""

from __future__ import annotations

import re
from dataclasses import dataclass

WORDS_PER_TOKEN = 0.75  # rough English ratio, avoids a tokenizer dependency


@dataclass
class Chunk:
    seq: int
    text: str
    start: int  # char offsets into the body
    end: int


def _tok(n_words: int) -> int:
    return int(n_words / WORDS_PER_TOKEN)


def chunk_text(body: str, max_tokens: int = 350, overlap_tokens: int = 60) -> list[Chunk]:
    if not body.strip():
        return []
    # paragraphs with their offsets
    paras: list[tuple[int, int]] = []
    for m in re.finditer(r"[^\n](?:.|\n(?!\n))*", body):
        paras.append((m.start(), m.end()))
    # split paragraphs that are too long on word boundaries
    pieces: list[tuple[int, int]] = []
    max_words = int(max_tokens * WORDS_PER_TOKEN)
    for s, e in paras:
        words = list(re.finditer(r"\S+", body[s:e]))
        if len(words) <= max_words:
            pieces.append((s, e))
            continue
        step = max_words
        for i in range(0, len(words), step):
            w = words[i : i + step]
            pieces.append((s + w[0].start(), s + w[-1].end()))
    chunks: list[Chunk] = []
    cur_start: int | None = None
    cur_end = 0
    cur_words = 0
    overlap_words = int(overlap_tokens * WORDS_PER_TOKEN)
    for s, e in pieces:
        n = len(body[s:e].split())
        if cur_start is not None and _tok(cur_words + n) > max_tokens:
            chunks.append(Chunk(len(chunks), body[cur_start:cur_end], cur_start, cur_end))
            # overlap: restart from the last `overlap_words` words of the previous chunk
            prev = list(re.finditer(r"\S+", body[cur_start:cur_end]))
            if overlap_words and len(prev) > overlap_words:
                cur_start = cur_start + prev[-overlap_words].start()
                cur_words = overlap_words
            else:
                cur_start = s
                cur_words = 0
        if cur_start is None:
            cur_start = s
        cur_end = e
        cur_words += n
    if cur_start is not None:
        chunks.append(Chunk(len(chunks), body[cur_start:cur_end], cur_start, cur_end))
    return chunks

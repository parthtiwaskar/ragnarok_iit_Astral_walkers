"""FTS5 BM25 retrieval over chunk text, joined with doc metadata for filters."""

from __future__ import annotations

import re

from app.search.filters import SearchFilters, where_clause
from app.store.repo import Repo
from app.tools.asof import Clock

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "how",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "which",
        "who",
        "will",
        "with",
        "would",
        "about",
        "into",
        "over",
        "after",
        "before",
        "than",
        "then",
        "there",
        "their",
        "they",
    ]
)


def normalize_query(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower())


def match_expr(q: str) -> str | None:
    """OR of quoted terms, so recall does not need every term in one chunk (BM25 still ranks by overlap)."""
    terms = [t for t in re.findall(r"[a-z0-9]+", q.lower()) if t not in STOPWORDS and len(t) > 1]
    if not terms:
        return None
    seen: list[str] = []
    for t in terms:
        if t not in seen:
            seen.append(t)
    return " OR ".join(f'"{t}"' for t in seen[:24])


def bm25_search(
    repo: Repo, query: str, f: SearchFilters, clock: Clock, top: int = 50
) -> list[tuple[str, float]]:
    expr = match_expr(query)
    if expr is None:
        return []
    where, params = where_clause(f, clock)
    sql = (
        "SELECT c.chunk_id AS chunk_id, bm25(chunks_fts) AS score FROM chunks_fts "
        "JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id JOIN docs d ON d.doc_id = c.doc_id "
        f"WHERE chunks_fts MATCH ? AND {where} ORDER BY score LIMIT ?"
    )
    rows = repo.db.query(sql, [expr, *params, top])
    return [(r["chunk_id"], float(r["score"])) for r in rows]


def allowed_chunks(repo: Repo, f: SearchFilters, clock: Clock) -> set[str]:
    where, params = where_clause(f, clock)
    rows = repo.db.query(
        f"SELECT c.chunk_id FROM chunks c JOIN docs d ON d.doc_id = c.doc_id WHERE {where}", params
    )
    return {r["chunk_id"] for r in rows}

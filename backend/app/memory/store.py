"""One SQLite memory store per agent. Each Agent object owns exactly one store; no other agent gets a handle."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from app.search.dense import Embedder
from app.tools.asof import Clock, memory_visible

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, tags TEXT, case_id TEXT, run INTEGER,
  knowledge_date DATE, confidence REAL DEFAULT 0.6, uses INTEGER DEFAULT 0, created_at TIMESTAMP,
  embedding BLOB);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, id UNINDEXED, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS query_log (
  fingerprint TEXT, query TEXT, filters TEXT, result_count INTEGER, useful INTEGER, case_id TEXT, run INTEGER,
  knowledge_date DATE, created_at TIMESTAMP, call_id TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

KINDS = ("procedural", "fact", "query", "precedent", "source_note", "turn_note")
# a "procedural" lesson that names a specific doc / table / event is really a fact (MEMORY.md §4)
SPECIFIC_ID = re.compile(
    r"\b(?:[A-Z]{1,6}-){1,3}[A-Z]?\d{1,6}\b|\bEVT-[A-Za-z0-9]+\b|\b[A-Z]{2,}-[A-Z]\d{2}\b"
)


@dataclass
class MemoryItem:
    id: str
    kind: str
    text: str
    tags: list[str] = field(default_factory=list)
    case_id: str | None = None
    run: int | None = None
    knowledge_date: str | None = None
    confidence: float = 0.6
    uses: int = 0
    created_at: str | None = None
    score: float = 0.0

    def render(self) -> str:
        return f"[{self.id}] ({self.kind}, conf {self.confidence:.1f}) {self.text}"

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def reclassify(kind: str, text: str) -> str:
    if kind == "procedural" and SPECIFIC_ID.search(text):
        return "fact"
    return kind if kind in KINDS else "fact"


class MemoryStore:
    def __init__(self, agent: str, path: Path, embedder: Embedder | None = None) -> None:
        self.agent = agent
        self.path = path
        self.embedder = embedder
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------- write ----------
    def _next_id(self) -> str:
        r = self._conn.execute("SELECT value FROM meta WHERE key='seq'").fetchone()
        n = int(r["value"]) + 1 if r else 1
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES('seq',?) ON CONFLICT(key) DO UPDATE SET value=?",
            (str(n), str(n)),
        )
        return f"{self.agent}-M{n:04d}"

    def _embed(self, text: str) -> bytes | None:
        if self.embedder is None:
            return None
        try:
            return self.embedder.embed_query(text).astype(np.float32).tobytes()
        except Exception:
            return None

    def add(
        self,
        kind: str,
        text: str,
        tags: list[str] | None = None,
        case_id: str | None = None,
        run: int | None = None,
        knowledge_date: str | None = None,
        confidence: float = 0.6,
    ) -> tuple[str, bool]:
        """Returns (memory_id, merged). Near-duplicate lessons (cosine > 0.95) are merged, confidence bumped."""
        kind = reclassify(kind, text)
        if kind == "procedural":
            knowledge_date = None
        text = text.strip()[:1000]
        emb = self._embed(text)
        with self._lock:
            if emb is not None and kind in ("procedural", "precedent", "fact"):
                q = np.frombuffer(emb, dtype=np.float32)
                for r in self._conn.execute(
                    "SELECT id, embedding, confidence FROM memories WHERE kind=?", (kind,)
                ):
                    if r["embedding"] is None:
                        continue
                    v = np.frombuffer(r["embedding"], dtype=np.float32)
                    if v.shape == q.shape and float(v @ q) > 0.95:
                        self._conn.execute(
                            "UPDATE memories SET confidence=MIN(1.0, confidence+0.1) WHERE id=?", (r["id"],)
                        )
                        self._conn.commit()
                        return str(r["id"]), True
            mid = self._next_id()
            self._conn.execute(
                "INSERT INTO memories(id,kind,text,tags,case_id,run,knowledge_date,confidence,uses,created_at,embedding)"
                " VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                (
                    mid,
                    kind,
                    text,
                    json.dumps(tags or []),
                    case_id,
                    run,
                    knowledge_date,
                    confidence,
                    _now(),
                    emb,
                ),
            )
            self._conn.execute("INSERT INTO memories_fts(text,id) VALUES(?,?)", (text, mid))
            self._conn.commit()
            return mid, False

    def adjust_confidence(self, memory_id: str, delta: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE memories SET confidence=MAX(0.0, MIN(1.0, confidence+?)) WHERE id=?",
                (delta, memory_id),
            )
            self._conn.commit()

    def log_query(
        self,
        fingerprint: str,
        query: str,
        filters: dict[str, Any],
        result_count: int,
        case_id: str,
        run: int | None,
        knowledge_date: str,
        call_id: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO query_log(fingerprint,query,filters,result_count,useful,case_id,run,knowledge_date,"
                "created_at,call_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    fingerprint,
                    query,
                    json.dumps(filters, sort_keys=True),
                    result_count,
                    int(result_count > 0),
                    case_id,
                    run,
                    knowledge_date,
                    _now(),
                    call_id,
                ),
            )
            self._conn.commit()

    # ---------- read ----------
    def _row(self, r: sqlite3.Row, score: float = 0.0) -> MemoryItem:
        return MemoryItem(
            id=r["id"],
            kind=r["kind"],
            text=r["text"],
            tags=json.loads(r["tags"] or "[]"),
            case_id=r["case_id"],
            run=r["run"],
            knowledge_date=r["knowledge_date"],
            confidence=float(r["confidence"] or 0.0),
            uses=int(r["uses"] or 0),
            created_at=r["created_at"],
            score=score,
        )

    def get(self, memory_id: str) -> MemoryItem | None:
        r = self._conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return self._row(r) if r else None

    def all(self) -> list[MemoryItem]:
        return [self._row(r) for r in self._conn.execute("SELECT * FROM memories ORDER BY id")]

    def recall(
        self, query: str, k: int, clock: Clock, kinds: tuple[str, ...] | None = None
    ) -> list[MemoryItem]:
        """Hybrid: FTS over text + cosine over embeddings, boosted by confidence and uses; clock-filtered."""
        with self._lock:
            rows = {r["id"]: r for r in self._conn.execute("SELECT * FROM memories")}
            if not rows:
                return []
            scores: dict[str, float] = {}
            terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2][:24]
            if terms:
                expr = " OR ".join(f'"{t}"' for t in dict.fromkeys(terms))
                try:
                    for rank, r in enumerate(
                        self._conn.execute(
                            "SELECT id FROM memories_fts WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) "
                            "LIMIT 50",
                            (expr,),
                        )
                    ):
                        scores[r["id"]] = scores.get(r["id"], 0.0) + 1.0 / (10 + rank)
                except sqlite3.OperationalError:
                    pass
            q = self._embed(query)
            if q is not None:
                qv = np.frombuffer(q, dtype=np.float32)
                for mid, r in rows.items():
                    if r["embedding"] is not None:
                        v = np.frombuffer(r["embedding"], dtype=np.float32)
                        if v.shape == qv.shape:
                            scores[mid] = scores.get(mid, 0.0) + max(0.0, float(v @ qv)) * 0.1
            out: list[MemoryItem] = []
            for mid, s in scores.items():
                r = rows[mid]
                if kinds and r["kind"] not in kinds:
                    continue
                if not memory_visible(r["kind"], r["knowledge_date"], clock):
                    continue
                boost = 0.02 * float(r["confidence"] or 0) + 0.002 * min(10, int(r["uses"] or 0))
                out.append(self._row(r, round(s + boost, 5)))
            out.sort(key=lambda m: (-m.score, m.id))
            top = out[:k]
            for m in top:
                self._conn.execute("UPDATE memories SET uses=uses+1 WHERE id=?", (m.id,))
            self._conn.commit()
            return top

    def zero_result_queries(self, clock: Clock) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM query_log WHERE result_count=0 AND (knowledge_date IS NULL OR knowledge_date<=?) "
            "ORDER BY created_at",
            (clock.iso,),
        )
        return [dict(r) for r in rows]

    def queries(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM query_log ORDER BY created_at")]

    def snapshot(self) -> dict[str, Any]:
        return {"memories": [m.as_dict() for m in self.all()], "queries": len(self.queries())}

    def reset(self) -> None:
        with self._lock:
            self._conn.executescript(
                "DELETE FROM memories; DELETE FROM memories_fts; DELETE FROM query_log; DELETE FROM meta;"
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")

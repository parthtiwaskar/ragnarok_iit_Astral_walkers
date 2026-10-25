"""Per-agent private, lasting memory (SQLite). Agents never share a store.

Facts obey the simulated clock: a fact is only recalled for cases whose as-of
date is on or after its knowledge_date. Procedural lessons are date-free.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

_WORD = re.compile(r"[a-z0-9]{3,}")
# A "lesson" worded as a rule is procedural; anything naming a doc/number is a fact.
_PROCEDURAL = re.compile(r"\b(always|never|when|prefer|should|check first)\b", re.I)


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


class Memory:
    def __init__(self, agent: str, path: str | Path = ":memory:") -> None:
        self.agent = agent
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS memories ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, text TEXT, case_id TEXT,"
            "knowledge_date TEXT, tags TEXT)"
        )
        self._db.commit()

    @staticmethod
    def classify(text: str) -> str:
        return "procedural" if _PROCEDURAL.search(text) else "fact"

    def add(self, text: str, *, case_id: str, knowledge_date: str, kind: str | None = None, tags: str = "") -> int:
        kind = kind or self.classify(text)
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO memories(kind,text,case_id,knowledge_date,tags) VALUES (?,?,?,?,?)",
                (kind, text, case_id, knowledge_date, tags),
            )
            self._db.commit()
            return int(cur.lastrowid or 0)

    def recall(self, query: str, as_of: str, k: int = 5) -> list[dict[str, Any]]:
        q = _tokens(query)
        with self._lock:
            rows = self._db.execute("SELECT id,kind,text,case_id,knowledge_date,tags FROM memories").fetchall()
        scored = []
        for id_, kind, text, case_id, kdate, tags in rows:
            if kind == "fact" and kdate > as_of:
                continue  # clock firewall: no knowledge from the future
            overlap = len(q & _tokens(text + " " + tags))
            if overlap:
                scored.append((overlap + (0.5 if kind == "procedural" else 0), id_, kind, text, case_id, kdate))
        scored.sort(key=lambda r: (-r[0], -r[1]))
        return [
            {"id": id_, "kind": kind, "text": text, "case_id": case_id, "knowledge_date": kdate}
            for _, id_, kind, text, case_id, kdate in scored[:k]
        ]

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT id,kind,text,case_id,knowledge_date FROM memories").fetchall()
        return [dict(zip(("id", "kind", "text", "case_id", "knowledge_date"), r)) for r in rows]

    def reset(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM memories")
            self._db.commit()

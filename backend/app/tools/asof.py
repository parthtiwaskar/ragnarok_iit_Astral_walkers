"""The as-of firewall: one place that decides what is visible as of a date (BRAINSTORM §4.4).

Applied in the tool layer to documents, trade rows (month-end rule), relation edges, feed items and
factual memories. Prompts are never relied on for this.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from app.store.repo import DocRecord, RelationRecord, Repo

FACTUAL_MEMORY_KINDS = frozenset({"fact", "query", "source_note", "precedent", "turn_note"})


@dataclass(frozen=True)
class Clock:
    as_of: date
    pin: int | None = None  # max ingest_seq visible (live packs land above it until a re-search refreshes)
    trade_rule: Literal["completed_month", "month_start"] = "completed_month"

    @property
    def iso(self) -> str:
        return self.as_of.isoformat()


def doc_visible(d: DocRecord | None, clock: Clock) -> bool:
    if d is None or not d.pub_date:
        return False
    if d.pub_date > clock.iso:
        return False
    return not (clock.pin is not None and d.ingest_seq > clock.pin)


def month_end(month: str) -> date:
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, calendar.monthrange(y, m)[1])


def month_start(month: str) -> date:
    return date(int(month[:4]), int(month[5:7]), 1)


def month_visible(month: str | None, clock: Clock) -> bool:
    """A trade row for month M is visible as of D only if month_end(M) <= D (default rule)."""
    if not month:
        return False
    try:
        edge = month_end(month) if clock.trade_rule == "completed_month" else month_start(month)
    except (ValueError, IndexError):
        return False
    return edge <= clock.as_of


def relation_visible(rel: RelationRecord, repo: Repo, clock: Clock) -> bool:
    """An edge only applies if its source doc is itself visible as of the date."""
    if not rel.pub_date or rel.pub_date > clock.iso:
        return False
    src = repo.get_doc(rel.src_doc)
    return src is not None and doc_visible(src, clock) and src.usable


def as_of_status(repo: Repo, doc_id: str, clock: Clock) -> dict[str, Any]:
    retracted_by: list[str] = []
    corrected_by: list[str] = []
    hidden = 0
    for rel in repo.relations_to(doc_id):
        if rel.kind not in ("retracts", "corrects"):
            continue
        if not relation_visible(rel, repo, clock):
            hidden += 1
            continue
        (retracted_by if rel.kind == "retracts" else corrected_by).append(rel.src_doc)
    return {
        "retracted": bool(retracted_by),
        "retracted_by": retracted_by[0] if retracted_by else None,
        "corrected_by": corrected_by[-1] if corrected_by else None,
        "_hidden_relations": hidden,
    }


def denials_for_event(repo: Repo, event_id: str, clock: Clock) -> list[str]:
    return [
        r.src_doc
        for r in repo.relations_to(event_id)
        if r.kind == "denies" and relation_visible(r, repo, clock)
    ]


def memory_visible(kind: str, knowledge_date: str | None, clock: Clock) -> bool:
    """Factual memories are recalled only if their knowledge_date <= the current as-of. Procedural: always."""
    if kind == "procedural":
        return True
    if knowledge_date is None:
        return kind not in FACTUAL_MEMORY_KINDS
    return knowledge_date <= clock.iso

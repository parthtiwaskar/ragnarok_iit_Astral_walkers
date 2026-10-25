"""Corrections / retractions / denials edges (DATA_PIPELINE.md §8). Edges are applied as-of in the tool layer."""

from __future__ import annotations

import re
from dataclasses import dataclass

CORR_TYPES = {"correction", "corrections", "clarification", "erratum"}
RETR_TYPES = {"retraction", "retractions", "withdrawal"}
DATE_RX = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
DENIAL_RX = re.compile(
    r"(?:reports?|claims?|rumou?rs?)[^.]{0,120}\b(?:are|is|were|was)\s+(?:false|untrue|unfounded|baseless|incorrect)"
    r"|no (?:closure|ban|strike|attack|blockade|incident) has (?:taken place|occurred|happened)"
    r"|has not been (?:closed|banned|blocked|attacked)"
    r"|remains? open to all traffic",
    re.IGNORECASE,
)


@dataclass
class Relation:
    src_doc: str
    dst: str  # doc id, or event id for denials
    kind: str  # corrects | retracts | denies
    pub_date: str


def id_pattern(known_ids: list[str]) -> re.Pattern[str]:
    """Learn the manifest ID shape: letters/dashes then digits (e.g. RM-DOC-0142)."""
    prefixes = sorted(
        {re.sub(r"\d+$", "", i) for i in known_ids if re.search(r"\d+$", i)}, key=len, reverse=True
    )
    if not prefixes:
        return re.compile(r"\b[A-Z]{1,6}(?:-[A-Z]{1,6})*-\d{2,6}\b")
    alt = "|".join(re.escape(p) for p in prefixes)
    return re.compile(rf"\b(?:{alt})\d+\b")


def classify_kind(source_type: str | None, title: str | None, body: str) -> str | None:
    st = (source_type or "").lower()
    head = f"{title or ''} {body[:200]}".strip().lower()
    if st in RETR_TYPES or head.startswith("retraction") or re.match(r"^(?:\S+\s){0,3}retracts?\b", head):
        return "retracts"
    if st in CORR_TYPES or head.startswith(("correction", "clarification")):
        return "corrects"
    return None


def find_relations(
    doc_id: str,
    source_type: str | None,
    title: str | None,
    body: str,
    pub_date: str,
    tier: int | None,
    event_id: str | None,
    outlet: str | None,
    known: dict[str, dict[str, str | None]],
    id_rx: re.Pattern[str],
) -> list[Relation]:
    """`known` maps doc_id -> {outlet, pub_date, title}. Returns edges from this doc."""
    out: list[Relation] = []
    kind = classify_kind(source_type, title, body)
    if kind:
        refs = {m.group(0) for m in id_rx.finditer(body)} - {doc_id}
        refs = {r for r in refs if r in known}
        if not refs:
            # fall back to headline/date references: same outlet, a date mentioned in the text, earlier pub date
            dates = set(DATE_RX.findall(body))
            for other, meta in known.items():
                if other == doc_id:
                    continue
                if meta.get("outlet") == outlet and meta.get("pub_date") in dates:
                    refs.add(other)
        for r in sorted(refs):
            out.append(Relation(doc_id, r, kind, pub_date))
    if tier is not None and tier <= 2 and event_id and DENIAL_RX.search(body):
        out.append(Relation(doc_id, event_id, "denies", pub_date))
    return out

"""Corrections / retractions / denials edges (DATA_PIPELINE.md §8). Edges are applied as-of in the tool layer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

CORR_TYPES = {"correction", "corrections", "clarification", "erratum"}
RETR_TYPES = {"retraction", "retractions", "withdrawal"}
ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]
NATURAL_DATE = re.compile(
    r"\b(\d{1,2})\s+("
    + "|".join(_MONTHS)
    + r")(?:\s+(\d{4}))?\b|\b("
    + "|".join(_MONTHS)
    + r")\s+(\d{1,2})(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)
# a "correction" that says the original story was wrong in substance withdraws it
WITHDRAWN = re.compile(
    r"\b(?:report|story|article)\s+was\s+(?:wrong|false|incorrect|untrue)\b|should not be relied on|"
    r"removed the (?:report|story|article)|we (?:retract|withdraw)|did not happen|has been withdrawn",
    re.IGNORECASE,
)
DENIAL_RX = re.compile(
    r"(?:reports?|claims?|rumou?rs?)[^.]{0,120}\b(?:are|is|were|was)\s+(?:false|untrue|unfounded|baseless|incorrect)"
    r"|no (?:closure|ban|strike|attack|blockade|incident) has (?:taken place|occurred|happened)"
    r"|has not been (?:closed|banned|blocked|attacked)"
    r"|remains? open to all traffic",
    re.IGNORECASE,
)
WORD = re.compile(r"[a-z]{4,}")


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
        return "retracts" if WITHDRAWN.search(body) else "corrects"
    return None


def referenced_dates(body: str, pub_date: str) -> set[str]:
    """ISO dates and natural ones ('On 9 September we reported'); a missing year is the latest before pub_date."""
    out = set(ISO_DATE.findall(body))
    try:
        pub = date.fromisoformat(pub_date)
    except ValueError:
        return out
    for m in NATURAL_DATE.finditer(body):
        if m.group(1):
            day, month, year = int(m.group(1)), m.group(2), m.group(3)
        else:
            day, month, year = int(m.group(5)), m.group(4), m.group(6)
        mon = _MONTHS.index(month.lower()) + 1
        y = int(year) if year else pub.year
        try:
            d = date(y, mon, day)
        except ValueError:
            continue
        if not year and d > pub:
            try:
                d = date(y - 1, mon, day)
            except ValueError:
                continue
        out.add(d.isoformat())
    return out


def _overlap(a: str, b: str) -> int:
    return len(set(WORD.findall(a.lower())) & set(WORD.findall(b.lower())))


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
            # headline/date references: same outlet, a date the text mentions, published no later than this doc
            dates = referenced_dates(body, pub_date)
            cands = [
                other
                for other, meta in known.items()
                if other != doc_id
                and meta.get("outlet") == outlet
                and meta.get("pub_date") in dates
                and (meta.get("pub_date") or "") <= pub_date
            ]
            if cands:
                text = f"{title or ''} {body}"
                best = max(cands, key=lambda o: (_overlap(text, str(known[o].get("title") or "")), o))
                refs = {best}
        for r in sorted(refs):
            out.append(Relation(doc_id, r, kind, pub_date))
    if tier is not None and tier <= 2 and event_id and DENIAL_RX.search(body):
        out.append(Relation(doc_id, event_id, "denies", pub_date))
    return out

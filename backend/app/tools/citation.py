"""Evidence hydration and the deterministic citation validator (SEARCH_AND_TOOLS.md §4).

LLMs supply only `doc_id + quote` or `table_id + row`. Date, tier, event and row values are filled in here
from the store, so an agent cannot misstate them (invariant 7).
"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from app.ingest.text import norm_for_match
from app.store.repo import Repo
from app.tools.asof import Clock, as_of_status, doc_visible, month_visible, table_visible
from app.trial.casefile import (
    WARNING_ISSUES,
    CitationCheck,
    CitationIssue,
    DocEvidence,
    Evidence,
    TradeEvidence,
)

# [DOC_ID, YYYY-MM-DD] | [DOC_ID P3, YYYY-MM-DD] | [TABLE_ID r08, YYYY-MM]
CITE_RX = re.compile(
    r"\[(?P<ref>[A-Za-z0-9_\-]+)(?: (?P<para>P\d+)| r(?P<row>\d+))?, (?P<date>\d{4}-\d{2}(?:-\d{2})?)\]"
)
PARA_RX = re.compile(r"\[P(\d+)\]")


def normalize_cite(m: re.Match[str]) -> str:
    ref, para, row, d = m.group("ref"), m.group("para"), m.group("row"), m.group("date")
    if row:
        return f"[{ref} r{int(row):02d}, {d}]"
    return f"[{ref} {para}, {d}]" if para else f"[{ref}, {d}]"


def cite_ref(c: str) -> str | None:
    """'[ORV-0009 r03, 2026-09]' -> 'ORV-0009#r03'; '[ORV-0016 P1, 2026-09-10]' -> 'ORV-0016'."""
    m = CITE_RX.search(c)
    if not m:
        return None
    return f"{m.group('ref')}#r{int(m.group('row')):02d}" if m.group("row") else m.group("ref")


def paragraph_of(body: str, quote: str, min_score: float = 80) -> str | None:
    """The [P#] paragraph that holds the quote, when the document uses paragraph anchors."""
    marks = list(PARA_RX.finditer(body))
    if not marks or not quote.strip():
        return None
    needle = norm_for_match(quote)
    best: tuple[float, str | None] = (0.0, None)
    for i, m in enumerate(marks):
        seg = body[m.end() : marks[i + 1].start() if i + 1 < len(marks) else len(body)]
        score = float(fuzz.partial_ratio(needle, norm_for_match(seg)))
        if score > best[0]:
            best = (score, f"P{m.group(1)}")
    return best[1] if best[0] >= min_score else None


class EvidenceIn(BaseModel):
    """What an agent submits. Either doc_id + quote, or table_id + row."""

    doc_id: str | None = Field(
        default=None, description="Runemark document id, copied exactly from a tool result"
    )
    quote: str | None = Field(default=None, description="exact text copied from the document (max 400 chars)")
    table_id: str | None = Field(default=None, description="trade table id")
    row: int | None = Field(default=None, description="trade row number (first column of the table)")


class HydrationError(ValueError):
    pass


def parse_trade_ref(ref: str) -> tuple[str, int] | None:
    """'TT-017#r08' | 'TT-017 r08' -> ('TT-017', 8)."""
    m = re.match(r"^\s*([A-Za-z0-9_\-]+)\s*(?:#|\s)r?(\d+)\s*$", ref)
    return (m.group(1), int(m.group(2))) if m else None


def hydrate(ev: EvidenceIn, repo: Repo, clock: Clock) -> Evidence:
    """Fill metadata from the store. Raises HydrationError for ids that do not exist or are not visible."""
    if ev.table_id or (ev.doc_id and ev.row is not None and not ev.quote):
        table_id = ev.table_id or ev.doc_id or ""
        if ev.row is None:
            parsed = parse_trade_ref(table_id)
            if parsed is None:
                raise HydrationError(f"trade evidence needs table_id and row (got {table_id!r})")
            table_id, row = parsed
        else:
            row = ev.row
        table = repo.get_trade_table(table_id)
        if table is None:
            raise HydrationError(
                f"trade table {table_id} not found; copy table ids exactly from tool results"
            )
        if not table_visible(table, repo, clock):
            raise HydrationError(f"trade table {table_id} is not available as of {clock.iso}")
        r = repo.trade_row(table_id, row)
        if r is None:
            raise HydrationError(f"row {row} not found in {table_id}")
        if not month_visible(r.month, clock):
            raise HydrationError(f"{table_id} row {row} is not visible as of {clock.iso}")
        return TradeEvidence(
            table_id=table_id, row=row, month=r.month or "", value=r.value, volume=r.volume, quality=r.quality
        )
    if not ev.doc_id:
        raise HydrationError("evidence needs doc_id + quote, or table_id + row")
    d = repo.get_doc(ev.doc_id.strip())
    if d is None:
        raise HydrationError(f"document {ev.doc_id} not found; copy doc ids exactly from tool results")
    if not doc_visible(d, clock):
        raise HydrationError(f"document {ev.doc_id} is not available as of {clock.iso}")
    quote = (ev.quote or "").strip()
    if not quote:
        raise HydrationError(f"evidence for {ev.doc_id} needs an exact quote from the document")
    return DocEvidence(
        paragraph=paragraph_of(d.body, quote),
        doc_id=d.doc_id,
        quote=quote[:400],
        pub_date=date.fromisoformat(d.pub_date or "1900-01-01"),
        tier=d.tier or 4,
        event_id=d.event_id or "unknown",
    )


def verify_citation(
    ev: Evidence,
    repo: Repo,
    clock: Clock,
    case_event_id: str | None,
    quote_min: float = 90,
    manifest_present: bool = True,
) -> CitationCheck:
    issues: list[CitationIssue] = []
    if isinstance(ev, DocEvidence):
        ref = ev.doc_id
        d = repo.get_doc(ev.doc_id)
        sim: float | None = None
        superseded: str | None = None
        if d is None or (manifest_present and repo.manifest_get(ev.doc_id) is None) or (d and d.off_manifest):
            issues.append("NOT_IN_MANIFEST")
        if d is not None:
            if not d.pub_date or d.pub_date > clock.iso or not doc_visible(d, clock):
                issues.append("AFTER_AS_OF")
            if d.status in ("QUARANTINED", "MISSING"):
                issues.append("QUARANTINED")
            st = as_of_status(repo, d.doc_id, clock)
            if st["retracted"]:
                issues.append("RETRACTED_AS_OF")
            if st["corrected_by"]:
                issues.append("CORRECTED_AS_OF")
                superseded = st["corrected_by"]
            if case_event_id:
                if d.is_background:
                    issues.append("BACKGROUND_ONLY")
                elif (d.event_id or "").lower() != case_event_id.lower():
                    issues.append("WRONG_EVENT")
            if ev.off_runemark:
                issues.append("OFF_RUNEMARK")
            haystack = norm_for_match(f"{d.title or ''}\n{d.body}")
            needle = norm_for_match(ev.quote)
            if needle and haystack:
                sim = round(float(fuzz.partial_ratio(needle, haystack)), 1)
                if sim < quote_min:
                    issues.append("QUOTE_MISMATCH")
            else:
                sim = 0.0
                issues.append("QUOTE_MISMATCH")
        valid = not any(i not in WARNING_ISSUES for i in issues)
        return CitationCheck(
            evidence_ref=ref,
            valid=valid,
            issues=sorted(set(issues)),
            quote_similarity=sim,
            superseded_by=superseded,
        )
    # trade evidence
    ref = ev.ref
    t = repo.get_trade_table(ev.table_id)
    r = repo.trade_row(ev.table_id, ev.row) if t else None
    if t is None or r is None:
        issues.append("NOT_IN_MANIFEST")
    else:
        if not month_visible(r.month, clock) or not table_visible(t, repo, clock):
            issues.append("AFTER_AS_OF")
        if r.quality != "OK":
            issues.append("DAMAGED_ROW")
    valid = not any(i not in WARNING_ISSUES for i in issues)
    return CitationCheck(evidence_ref=ref, valid=valid, issues=sorted(set(issues)))


def evidence_ref(ev: Evidence) -> str:
    return ev.doc_id if isinstance(ev, DocEvidence) else ev.ref


def verify_cite(c: str, repo: Repo, clock: Clock, event_id: str | None) -> bool:
    """A briefing citation string resolves: manifest, as-of, not retracted/quarantined, date and paragraph match."""
    m = CITE_RX.search(c)
    if not m:
        return False
    if m.group("row"):
        row = repo.trade_row(m.group("ref"), int(m.group("row")))
        if row is None:
            return False
        tev = TradeEvidence(
            table_id=m.group("ref"),
            row=int(m.group("row")),
            month=row.month or "",
            value=row.value,
            volume=row.volume,
            quality=row.quality,
        )
        return verify_citation(tev, repo, clock, event_id).valid and (row.month or "") == m.group("date")
    d = repo.get_doc(m.group("ref"))
    if d is None or not d.pub_date:
        return False
    if m.group("para") and f"[{m.group('para')}]" not in d.body:
        return False
    dev = DocEvidence(
        doc_id=d.doc_id,
        quote=(d.body or d.title or "x")[:120],
        pub_date=date.fromisoformat(d.pub_date),
        tier=d.tier or 4,
        event_id=d.event_id or "",
    )
    chk = verify_citation(dev, repo, clock, event_id)
    blocking = [i for i in chk.issues if i not in WARNING_ISSUES]
    return not blocking and d.pub_date == m.group("date")

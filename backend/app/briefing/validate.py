"""Briefing validator (TRAPS_AND_RELIABILITY.md §3): citations come only from the digest, dates match,
factual sentences carry a citation, each finding's ruling id is mentioned, assumptions link to rulings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.tools.citation import CITE_RX as CITATION
from app.tools.citation import normalize_cite as normalize_cite

RULING_ID = re.compile(r"RUL:[A-Za-z0-9\-]+:R\d+")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[\-*])|\n+")
META = re.compile(r"\b(INCOMPLETE|COMPLETE|status|no evidence found|unchallenged|failed)\b", re.I)
FACTUAL = re.compile(r"\d|%|\b[A-Z][a-z]{2,}\b")

SECTIONS = ("executive_summary", "established", "contested", "conceded", "outlook")


@dataclass
class BriefingCheck:
    ok: bool
    errors: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    invalid_citations: list[str] = field(default_factory=list)
    uncited_sentences: list[str] = field(default_factory=list)
    missing_rulings: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_SPLIT.split(text or "") if s and s.strip()]


def is_factual(s: str) -> bool:
    body = CITATION.sub("", s)
    body = RULING_ID.sub("", body)
    if META.search(body):
        return False
    words = body.split()
    if len(words) < 4:
        return False
    # ignore the sentence-initial capital
    rest = " ".join(words[1:])
    return bool(FACTUAL.search(rest)) or bool(re.search(r"\d", words[0]))


def validate_sections(sections: dict[str, Any], digest: dict[str, Any], verify: Any = None) -> BriefingCheck:
    """`verify(cite_str) -> bool` re-checks a citation against the store (optional)."""
    allowed: dict[str, str] = digest.get("allowed_citations", {})
    chk = BriefingCheck(ok=True)
    for name in SECTIONS:
        text = str(sections.get(name) or "")
        for m in CITATION.finditer(text):
            c = normalize_cite(m)
            chk.citations.append(c)
            if c not in allowed:
                chk.invalid_citations.append(c)
                chk.errors.append(
                    f"{name}: citation {c} is not in the rulings digest (or its date does not match)"
                )
            elif verify is not None and not verify(c):
                chk.invalid_citations.append(c)
                chk.errors.append(f"{name}: citation {c} fails verify_citation")
        for s in sentences(text):
            if is_factual(s) and not CITATION.search(s):
                chk.uncited_sentences.append(s)
                chk.errors.append(f"{name}: factual sentence without a citation: '{s[:90]}'")
    want = digest.get("ruling_ids", {})
    for finding, section in (
        ("ESTABLISHED", "established"),
        ("CONTESTED", "contested"),
        ("CONCEDED", "conceded"),
    ):
        text = str(sections.get(section) or "")
        miss = [rid for rid in want.get(finding, []) if rid not in text]
        if miss:
            chk.missing_rulings[section] = miss
            chk.errors.append(f"{section}: mention every ruling id: missing {miss}")
    all_rulings = {r for ids in want.values() for r in ids}
    for a in sections.get("assumptions", []) or []:
        rids = a.get("ruling_ids", []) if isinstance(a, dict) else getattr(a, "ruling_ids", [])
        aid = a.get("id") if isinstance(a, dict) else getattr(a, "id", "?")
        if not rids or not any(r in all_rulings for r in rids):
            chk.errors.append(f"assumption {aid} must reference at least one ruling id from the digest")
    chk.ok = not chk.errors
    return chk


def sanitize(sections: dict[str, Any], digest: dict[str, Any], chk: BriefingCheck) -> dict[str, Any]:
    """Remove offending sentences; add deterministic lines for findings Odin did not mention."""
    allowed = digest.get("allowed_citations", {})
    out = dict(sections)
    for name in SECTIONS:
        kept = []
        for s in sentences(str(sections.get(name) or "")):
            cites = [normalize_cite(m) for m in CITATION.finditer(s)]
            if any(c not in allowed or c in chk.invalid_citations for c in cites):
                continue
            if is_factual(s) and not cites:
                continue
            kept.append(s)
        out[name] = " ".join(kept)
    lookup = {e["ruling"]: e for k in ("established", "contested", "conceded") for e in digest.get(k, [])}
    for section, miss in chk.missing_rulings.items():
        lines = []
        for rid in miss:
            e = lookup.get(rid)
            if e is None:
                continue
            cite_text = " ".join(c["cite"] for c in e["citations"][:2])
            lines.append(f"{rid}: {e['statement']} {cite_text}".strip())
        if lines:
            out[section] = (str(out.get(section) or "") + "\n" + "\n".join(f"- {ln}" for ln in lines)).strip()
    all_rulings = {r for ids in digest.get("ruling_ids", {}).values() for r in ids}
    out["assumptions"] = [
        a
        for a in (sections.get("assumptions") or [])
        if any(r in all_rulings for r in (a.get("ruling_ids", []) if isinstance(a, dict) else []))
    ]
    return out

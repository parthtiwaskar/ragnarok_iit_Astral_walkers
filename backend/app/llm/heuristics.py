"""Generic text heuristics used ONLY by the fake policy player (tests and offline demo).

They read exposure-parameter statements out of document text with keyword patterns. Nothing here is
event-specific: no document ids, names or numbers from any corpus (invariant 16).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

DOC_TAG = re.compile(r"</?document[^>]*>")
WORD_NUM = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "a": 1,
    "an": 1,
    "several": 3,
    "a few": 2,
}
FRACTIONS = {
    "one third": 1 / 3,
    "a third": 1 / 3,
    "half": 0.5,
    "a quarter": 0.25,
    "one quarter": 0.25,
    "two thirds": 2 / 3,
    "three quarters": 0.75,
}
MONTH_DAYS = 30.44

REROUTE_WORDS = re.compile(r"divert|rerout|alternativ|redirect|absorb|feeder|haul|truck|rail link|by rail")
DURATION_CTX = re.compile(
    r"\b(last|lasting|closed|closure|shut|outage|strike|down|disruption|repairs?|take|"
    r"expected|expect|stay|remain|halt)\w*"
)
DURATION_NEG = re.compile(r"inventor|stock|warehouse|production|covers?\b|ago|since|earlier")
FULL_STOP = re.compile(
    r"closed to all|to all (?:commercial )?(?:traffic|freight)|(?:has|have) stopped|(?:operations|handling|traffic|"
    r"berths)\b[^.]{0,30}\b(?:stopped|halted|suspended|closed)|suspended (?:exports|all)|indefinite strike|"
    r"no vessel may transit|remain(?:s)? closed|closed to all freight|suspends? exports"
)


@dataclass
class Candidate:
    param: str
    value: float
    quote: str


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", DOC_TAG.sub(" ", text or "")).strip()


def sentences(text: str) -> list[str]:
    t = clean(text)
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", t) if len(s.strip()) > 12]


def _num(tok: str) -> float | None:
    tok = tok.strip().lower()
    if tok in WORD_NUM:
        return float(WORD_NUM[tok])
    try:
        return float(tok)
    except ValueError:
        return None


def _months_in_horizon(end: date, as_of: date, horizon: float) -> float:
    return round(max(0.0, min(horizon, (end - as_of).days / MONTH_DAYS)), 3)


def extract(text: str, pub_date: str | None, as_of: date, horizon: float = 3.0) -> list[Candidate]:
    """All parameter statements found in the text, each with the exact sentence as quote."""
    out: list[Candidate] = []
    try:
        pdate = date.fromisoformat(pub_date) if pub_date else as_of
    except ValueError:
        pdate = as_of
    for s in sentences(text):
        low = s.lower()
        quote = s[:400]
        # ---- reroute offset
        rr = False
        m = re.search(
            r"(?:absorb|carry|handle|take|move)\s+(?:up to |about |roughly |around |some |at most )?"
            r"(\d+(?:\.\d+)?)\s*(?:percent|%)",
            low,
        )
        if m and REROUTE_WORDS.search(low):
            out.append(Candidate("reroute_offset", float(m.group(1)) / 100, quote))
            rr = True
        m = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:percent|%)\s+of\s+(?:the\s+)?(?:\w+\s+)?(?:diverted|rerouted|redirected)",
            low,
        )
        if m and not rr:
            out.append(Candidate("reroute_offset", float(m.group(1)) / 100, quote))
            rr = True
        m = re.search(r"not exceed (one third|a third|half|a quarter|one quarter|two thirds)", low)
        if (m and REROUTE_WORDS.search(low)) or (m and "alternative" in low):
            out.append(Candidate("reroute_offset", FRACTIONS[m.group(1)], quote))
            rr = True
        # ---- route share
        if not rr and not REROUTE_WORDS.search(low):
            m = re.search(
                r"(\d+(?:\.\d+)?)\s*(?:percent|%)\s+of\s+(?:[\w']+\s+){0,4}?"
                r"(?:imports|exports|trade|cargo|shipments|freight|parts imports)\b",
                low,
            )
            if m and not re.search(r"\b(?:fell|fallen|dropped|collapsed|down|cut|lost)\b", low):
                out.append(Candidate("route_share", float(m.group(1)) / 100, quote))
            elif re.search(
                r"\b(?:about |roughly |around )?half of\b[^.]{0,60}\b(?:imports|exports|trade)\b", low
            ):
                out.append(Candidate("route_share", 0.5, quote))
        # ---- severity
        m = re.search(
            r"(?:at|to)\s+(?:about |roughly )?(\d+)\s*(?:percent|%)\s+(?:of\s+)?(?:normal\s+)?capacity", low
        )
        if m:
            out.append(Candidate("severity", round(1 - float(m.group(1)) / 100, 3), quote))
        elif re.search(r"half (?:of )?(?:normal )?capacity|half of normal", low):
            out.append(Candidate("severity", 0.5, quote))
        elif FULL_STOP.search(low):
            out.append(Candidate("severity", 1.0, quote))
        else:
            m = re.search(
                r"(?:throughput|traffic|volumes?|deliveries|handling)[^.]{0,30}(?:collapsed|fallen|fell|"
                r"dropped|plunged|down)\s+by\s+(\d+)\s*(?:percent|%)",
                low,
            )
            if m:
                out.append(Candidate("severity", float(m.group(1)) / 100, quote))
        # ---- duration (months of disruption inside the horizon)
        m = re.search(r"(?:reopen\w*|resum\w*|restart\w*|lift\w*)[^.]{0,80}?(\d{4}-\d{2}-\d{2})", low)
        if m:
            try:
                out.append(
                    Candidate(
                        "duration_months",
                        _months_in_horizon(date.fromisoformat(m.group(1)), as_of, horizon),
                        quote,
                    )
                )
                continue
            except ValueError:
                pass
        if DURATION_NEG.search(low) or not DURATION_CTX.search(low):
            continue
        m = re.search(
            r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|several)\s+"
            r"(weeks?|months?|days?)\b",
            low,
        )
        if m:
            n = _num(m.group(1))
            if n is not None:
                unit = m.group(2)
                days = n * (7 if unit.startswith("week") else 1 if unit.startswith("day") else MONTH_DAYS)
                out.append(
                    Candidate(
                        "duration_months",
                        _months_in_horizon(pdate + timedelta(days=days), as_of, horizon),
                        quote,
                    )
                )
                continue
        if re.search(
            r"\b(?:a|one|the whole|all) year\b|all summer|for (?:a )?year|twelve months|indefinite", low
        ):
            out.append(Candidate("duration_months", float(horizon), quote))
    return out


def param_keywords(param: str) -> str:
    return {
        "route_share": "share percent of imports exports trade transit through route",
        "severity": "closed traffic capacity suspended stopped operations",
        "duration_months": "how long closure last weeks months reopen resume date",
        "reroute_offset": "alternative route rail diverted rerouted cargo capacity absorb",
    }.get(param, param)


def params_in(text: str) -> list[str]:
    low = text.lower()
    out = []
    for p, words in (
        ("route_share", ("route_share", "route share")),
        ("severity", ("severity",)),
        ("duration_months", ("duration", "reopening")),
        ("reroute_offset", ("reroute", "alternative")),
    ):
        if any(w in low for w in words):
            out.append(p)
    return out

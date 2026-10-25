"""Pull the as-of date out of a question ("As of 25 January 2027, how exposed ...")."""
from __future__ import annotations

import re
from datetime import date

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
_ABBR = {m[:3]: i for m, i in MONTHS.items()} | {"sept": 9}
_MON = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\.?"
_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON},?\s+(\d{{4}})\b", re.I), lambda m: (int(m[3]), _month(m[2]), int(m[1]))),
    (re.compile(rf"\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I), lambda m: (int(m[3]), _month(m[1]), int(m[2]))),
]


def _month(name: str) -> int:
    n = name.lower().rstrip(".")
    return MONTHS.get(n) or _ABBR.get(n[:4] if n.startswith("sept") else n[:3], 0)


def extract_as_of(question: str) -> str | None:
    """The date after "as of" if there is one, otherwise the first date in the text. None if the text has no valid date."""
    found: list[tuple[int, str]] = []
    for rx, conv in _PATTERNS:
        for m in rx.finditer(question):
            try:
                y, mo, d = conv(m)
                found.append((m.start(), date(y, mo, d).isoformat()))
            except ValueError:
                continue
    if not found:
        return None
    after = re.search(r"as\s+of\b", question, re.I)
    if after:
        later = [f for f in found if f[0] >= after.start()]
        if later:
            return min(later)[1]
    return min(found)[1]

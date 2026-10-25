"""Trade notes: documents whose paragraphs report monthly trade values ("In July 2026, the value was USD 90.0M
and the volume was 250,000 tonnes."). They become trade tables keyed by the document id, with one row per
paragraph, so a row cites as `<doc_id>#r<paragraph>` and stays tied to the document's publication date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.ingest.trade_table import TradeRowParsed

_NAMES = [
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
MONTHS = {m: i for i, m in enumerate(_NAMES, start=1)} | {m[:3]: i for i, m in enumerate(_NAMES, start=1)}
PARA = re.compile(r"\[P(\d+)\]")
MONTH_YEAR = re.compile(r"\b(?:in\s+)?([A-Za-z]{3,9})\.?\s+(\d{4})\b", re.I)
NUM = r"(?P<num>-?[\d,]*\d(?:\.\d+)?|[^\s\d][^\s,;]*)"
VALUE = re.compile(
    r"\bvalue\s+(?:was|of|is|at)\s+(?:(?P<cur>[A-Z]{3})\s*)?"
    + NUM
    + r"(?P<mult>\s?(?:[KMB]n?|million|billion|thousand)\b)?",
    re.I,
)
VOLUME = re.compile(
    r"\bvolume\s+(?:was|of|is|at)\s+" + NUM + r"\s*(?P<unit>tonnes|tons|barrels|units|kg|containers|teu)?",
    re.I,
)
ROUTE = re.compile(
    r"\broute:\s*([A-Z][\w' -]+?)\s+(?:to|->|→)\s+([A-Z][\w' -]+?)\s*,\s*([a-z][\w -]+?)\s*[.\n]", re.I
)
TITLE_ROUTE = re.compile(
    r"([A-Z][\w' -]+?)\s+(?:to|->|→)\s+([A-Z][\w' -]+?)\s*,\s*([a-z][\w -]+?)\s*(?:,|$)", re.I
)
MISSING = {"", "na", "n/a", "-", "--", "?", "??", "unknown", "null", "tbc", "n.a."}


@dataclass
class TradeNote:
    table_id: str
    exporter: str | None
    importer: str | None
    sector: str | None
    unit: str
    rows: list[TradeRowParsed] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def period(self) -> tuple[str | None, str | None]:
        months = sorted(r.month for r in self.rows if r.month)
        return (months[0], months[-1]) if months else (None, None)


def _paragraphs(body: str) -> list[tuple[int, str]]:
    marks = list(PARA.finditer(body))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out.append((int(m.group(1)), " ".join(body[m.end() : end].split())))
    return out


def _number(raw: str) -> tuple[float | None, str]:
    s = raw.strip().rstrip(".,;")
    if s.lower() in MISSING:
        return None, "missing"
    try:
        return float(s.replace(",", "")), ""
    except ValueError:
        return None, "bad"


def parse_trade_note(doc_id: str, title: str | None, body: str, min_rows: int = 3) -> TradeNote | None:
    """None unless at least `min_rows` paragraphs report a month with a value or volume."""
    paras = _paragraphs(body)
    if not paras:
        return None
    exporter = importer = sector = None
    for _, text in paras[:3]:
        m = ROUTE.search(text + "\n")
        if m:
            exporter, importer, sector = m.group(1).strip(), m.group(2).strip(), m.group(3).strip().lower()
            break
    if exporter is None and title:
        m = TITLE_ROUTE.search(title.split(":", 1)[-1])
        if m:
            exporter, importer, sector = m.group(1).strip(), m.group(2).strip(), m.group(3).strip().lower()
    note = TradeNote(table_id=doc_id, exporter=exporter, importer=importer, sector=sector, unit="")
    seen: set[str] = set()
    unit_seen: list[str] = []
    for n, text in paras:
        low = text.lower()
        my = MONTH_YEAR.search(text)
        if not my or not ("value" in low or "volume" in low):
            continue
        mon = MONTHS.get(my.group(1).lower()) or MONTHS.get(my.group(1).lower()[:3])
        if mon is None:
            continue
        month = f"{int(my.group(2)):04d}-{mon:02d}"
        vm, qm = VALUE.search(text), VOLUME.search(text)
        value, vprob = _number(vm.group("num")) if vm else (None, "missing")
        volume, qprob = _number(qm.group("num")) if qm else (None, "missing")
        if vm and vprob == "":
            unit_seen.append(f"{(vm.group('cur') or '').upper()} {(vm.group('mult') or '').strip()}".strip())
        quality, reason = "OK", ""
        if vprob == "bad" or qprob == "bad":
            quality, reason = "BAD_NUMBER", "value/volume not a number"
        elif vprob == "missing" or qprob == "missing":
            quality, reason = "MISSING_VALUE", "value or volume missing"
        elif (value is not None and value < 0) or (volume is not None and volume < 0):
            quality, reason = "BAD_NUMBER", "negative value"
        if quality == "OK" and month in seen:
            quality, reason = "DUPLICATE_ROW", "month repeated"
        if quality == "OK":
            seen.add(month)
        note.rows.append(TradeRowParsed(n, month, value, volume, quality, text[:300], reason))
    if len(note.rows) < min_rows:
        return None
    counts = [u for u in unit_seen if u]
    note.unit = max(sorted(set(counts)), key=counts.count) if counts else ""
    ok = sorted(r.value for r in note.rows if r.quality == "OK" and r.value is not None)
    if len(ok) >= 3:
        med = ok[len(ok) // 2]
        for r in note.rows:
            if r.quality == "OK" and r.value is not None and med > 0 and r.value > 50 * med:
                r.quality, r.reason = "BAD_NUMBER", "outlier > 50x median"
    if exporter is None:
        note.flags.append("NO_ROUTE_LINE")
    return note

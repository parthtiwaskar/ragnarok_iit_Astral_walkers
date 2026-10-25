"""Trade-table parser with a quality flag per row (DATA_PIPELINE.md §6)."""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from app.ingest.header import KV, canonical_key, is_background_event

MONTH = re.compile(r"^(\d{4})-(\d{2})$")
MONTH_ANY = re.compile(r"(\d{4})-(\d{2})")
PAIR_SPLIT = re.compile(r"\s*(?:->|→|=>|>|\bto\b|/|–|—|\s-\s)\s*")
MISSING_TOKENS = {"", "na", "n/a", "-", "--", "?", "null", "none", "nan"}
COLUMN_ALIASES = {
    "row": "row",
    "no": "row",
    "#": "row",
    "n": "row",
    "row no": "row",
    "row number": "row",
    "month": "month",
    "period": "month",
    "date": "month",
    "yyyy-mm": "month",
    "value": "value",
    "trade value": "value",
    "val": "value",
    "volume": "volume",
    "vol": "volume",
    "qty": "volume",
    "quantity": "volume",
    "trade volume": "volume",
}


@dataclass
class TradeRowParsed:
    row: int | None
    month: str | None
    value: float | None
    volume: float | None
    quality: str
    raw_line: str
    reason: str = ""


@dataclass
class TradeTableParsed:
    table_id: str
    exporter: str | None
    importer: str | None
    sector: str | None
    unit: str | None
    period_start: str | None
    period_end: str | None
    event_id: str | None
    is_background: bool
    columns: list[str]
    header: dict[str, str]
    rows: list[TradeRowParsed] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def looks_like_trade_table(text: str) -> bool:
    head = text[:1500].lower()
    return ("table id" in head or "table_id" in head) and "month" in head


def _norm_col(c: str) -> str:
    k = re.sub(r"\s*\(.*?\)\s*", "", c.strip().lower())
    return COLUMN_ALIASES.get(k, k)


def _detect_delim(line: str) -> str:
    if "|" in line:
        return "|"
    if "\t" in line:
        return "\t"
    if "," in line:
        return ","
    return "  "


def _split(line: str, delim: str) -> list[str]:
    if delim == "  ":
        return [c for c in re.split(r"\s{2,}", line.strip())]
    parts = [c.strip() for c in line.strip().split(delim)]
    if delim == "|":  # tolerate leading/trailing pipes
        if parts and parts[0] == "":
            parts = parts[1:]
        if parts and parts[-1] == "":
            parts = parts[:-1]
    return parts


def _num(cell: str) -> tuple[float | None, str]:
    """Returns (number, problem) where problem is '' | 'missing' | 'bad'."""
    c = cell.strip()
    if c.lower() in MISSING_TOKENS:
        return None, "missing"
    c2 = c.replace(",", "").replace("_", "").replace(" ", "")
    try:
        return float(c2), ""
    except ValueError:
        return None, "bad"


def _months_between(a: str, b: str) -> list[str]:
    ya, ma = int(a[:4]), int(a[5:7])
    yb, mb = int(b[:4]), int(b[5:7])
    out = []
    while (ya, ma) <= (yb, mb):
        out.append(f"{ya:04d}-{ma:02d}")
        ma += 1
        if ma == 13:
            ya, ma = ya + 1, 1
    return out


def _is_column_line(line: str) -> bool:
    low = line.lower()
    if "month" not in low:
        return False
    cells = _split(line, _detect_delim(line))
    return len(cells) >= 3


def parse_trade_table(text: str, fallback_id: str | None = None) -> TradeTableParsed:
    lines = text.split("\n")
    header: dict[str, str] = {}
    i = 0
    col_line_idx: int | None = None
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if _is_column_line(line):
            col_line_idx = i
            break
        m = KV.match(line)
        if m:
            key = canonical_key(m.group(1)) or m.group(1).strip().lower()
            header.setdefault(key, m.group(2).strip())
        i += 1
    table_id = header.get("table_id") or fallback_id or "UNKNOWN-TABLE"
    exporter = importer = None
    pair = header.get("country_pair")
    if pair:
        parts = [p.strip() for p in PAIR_SPLIT.split(pair) if p.strip()]
        if len(parts) >= 2:
            exporter, importer = parts[0], parts[1]
    period_start = period_end = None
    if header.get("period"):
        found = MONTH_ANY.findall(header["period"])
        if found:
            period_start = f"{found[0][0]}-{found[0][1]}"
            period_end = f"{found[-1][0]}-{found[-1][1]}"
    event_raw = header.get("event_id")
    t = TradeTableParsed(
        table_id=table_id.strip(),
        exporter=exporter,
        importer=importer,
        sector=(header.get("sector") or "").strip().lower() or None,
        unit=header.get("unit"),
        period_start=period_start,
        period_end=period_end,
        event_id=("background" if is_background_event(event_raw) else event_raw.strip())
        if event_raw
        else None,
        is_background=is_background_event(event_raw),
        columns=[],
        header=header,
    )
    if col_line_idx is None:
        t.flags.append("NO_COLUMN_LINE")
        return t
    delim = _detect_delim(lines[col_line_idx])
    t.columns = [_norm_col(c) for c in _split(lines[col_line_idx], delim)]
    ncols = len(t.columns)
    data_lines = [ln for ln in lines[col_line_idx + 1 :] if ln.strip()]
    seen_rows: set[int] = set()
    seen_months: set[str] = set()
    for idx, raw in enumerate(data_lines):
        is_last = idx == len(data_lines) - 1
        cells = _split(raw, delim)
        rec = dict(zip(t.columns, cells, strict=False))
        row_no: int | None = None
        rcell = rec.get("row", cells[0] if cells else "")
        if re.fullmatch(r"\d{1,4}", rcell.strip()):
            row_no = int(rcell)
        month = rec.get("month", "").strip() or None
        if month and not MONTH.match(month):
            month = None
        value, vprob = _num(rec.get("value", "")) if "value" in rec else (None, "missing")
        volume, volprob = _num(rec.get("volume", "")) if "volume" in rec else (None, "missing")
        quality, reason = "OK", ""
        if len(cells) < ncols:
            quality = "TRUNCATED" if is_last else "MISSING_VALUE"
            reason = f"{len(cells)} of {ncols} cells"
        elif row_no is None or month is None:
            quality, reason = "BAD_NUMBER", "unparseable row number or month"
        elif vprob == "bad" or volprob == "bad":
            quality, reason = "BAD_NUMBER", "value/volume not a number"
        elif vprob == "missing" or volprob == "missing":
            quality, reason = "MISSING_VALUE", "empty value/volume"
        elif (value is not None and value < 0) or (volume is not None and volume < 0):
            quality, reason = "BAD_NUMBER", "negative value"
        if quality == "OK" and (
            (row_no is not None and row_no in seen_rows) or (month and month in seen_months)
        ):
            quality, reason = "DUPLICATE_ROW", "row number or month repeated"
        if quality == "OK":
            if row_no is not None:
                seen_rows.add(row_no)
            if month:
                seen_months.add(month)
        # damaged rows keep whatever numbers could be read (for display) but are never used in stats
        t.rows.append(TradeRowParsed(row_no, month, value, volume, quality, raw.strip(), reason))
    # outliers: > 50x the median of OK values
    ok_vals = [r.value for r in t.rows if r.quality == "OK" and r.value is not None]
    if len(ok_vals) >= 3:
        med = statistics.median(ok_vals)
        if med > 0:
            for r in t.rows:
                if r.quality == "OK" and r.value is not None and r.value > 50 * med:
                    r.quality, r.reason = "BAD_NUMBER", "outlier > 50x median"
    # gap: the table ends before the period end
    if t.period_start and t.period_end:
        expected = _months_between(t.period_start, t.period_end)
        present = {r.month for r in t.rows if r.month and r.quality == "OK"}
        last_present = max(present) if present else None
        missing = [m for m in expected if m not in present]
        tail = [m for m in missing if last_present is None or m > last_present]
        if tail:
            t.flags.append(f"TRUNCATED_GAP:{tail[0]}..{tail[-1]}")
        inner = [m for m in missing if m not in tail]
        if inner:
            t.flags.append(f"MISSING_MONTHS:{','.join(inner)}")
    return t

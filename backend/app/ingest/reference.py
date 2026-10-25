"""Reference-sheet parser: countries, ports, chokepoints, sectors, firms, outlets (DATA_PIPELINE.md §7).

Layout-agnostic: a `Sheet: <name>` header line (or the file name) names the sheet, then a delimited table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.ingest.header import KV, canonical_key

SHEET_NAMES = {
    "countries": "countries",
    "country": "countries",
    "ports": "ports",
    "port": "ports",
    "chokepoints": "chokepoints",
    "chokepoint": "chokepoints",
    "ports_chokepoints": "chokepoints",
    "routes": "chokepoints",
    "sectors": "sectors",
    "sector": "sectors",
    "firms": "firms",
    "firm": "firms",
    "companies": "firms",
    "outlets": "outlets",
    "outlet": "outlets",
    "sources": "outlets",
}


@dataclass
class ReferenceSheet:
    sheet: str
    rows: list[dict[str, str]] = field(default_factory=list)


def looks_like_reference(text: str, path: Path | None = None) -> bool:
    head = text[:300].lower()
    if re.search(r"^\s*(sheet|reference|reference sheet)\s*[:=]", head, re.M):
        return True
    return bool(path and "reference" in [p.lower() for p in path.parts])


def _delim(line: str) -> str:
    for d in ("|", "\t", ","):
        if d in line:
            return d
    return "  "


def _split(line: str, d: str) -> list[str]:
    if d == "  ":
        return [c.strip() for c in re.split(r"\s{2,}", line.strip())]
    return [c.strip() for c in line.strip().strip("|").split(d)]


def parse_reference(text: str, path: Path | None = None) -> ReferenceSheet:
    lines = [ln for ln in text.split("\n")]
    sheet = None
    i = 0
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        m = KV.match(ln)
        if m and canonical_key(m.group(1)) == "sheet":
            sheet = m.group(2).strip().lower()
            i += 1
            continue
        if m and not any(d in m.group(2) for d in ("|", "\t")) and _delim(ln) != "|":
            i += 1  # other metadata line (e.g. Updated: 2025-01-01)
            continue
        break
    if sheet is None and path is not None:
        sheet = path.stem.lower()
    sheet = SHEET_NAMES.get((sheet or "unknown").replace(" ", "_"), sheet or "unknown")
    out = ReferenceSheet(sheet=sheet)
    rest = [ln for ln in lines[i:] if ln.strip()]
    if not rest:
        return out
    d = _delim(rest[0])
    cols = [c.lower().replace(" ", "_") for c in _split(rest[0], d)]
    for ln in rest[1:]:
        cells = _split(ln, d)
        if not any(cells):
            continue
        row = {cols[j] if j < len(cols) else f"col{j}": cells[j] for j in range(len(cells))}
        out.rows.append(row)
    return out


def parse_routes(value: str) -> list[tuple[str, str]]:
    """'A>B; C>D' -> [(A, B), (C, D)]."""
    pairs: list[tuple[str, str]] = []
    for part in re.split(r"\s*;\s*", value or ""):
        bits = [b.strip() for b in re.split(r"\s*(?:->|→|>|/)\s*", part) if b.strip()]
        if len(bits) == 2:
            pairs.append((bits[0], bits[1]))
    return pairs

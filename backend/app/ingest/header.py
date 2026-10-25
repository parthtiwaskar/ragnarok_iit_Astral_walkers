"""Metadata header parser with a key alias map and validation (DATA_PIPELINE.md §5)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

KV = re.compile(r"^\s*([A-Za-z][A-Za-z_ /\-]{1,29})\s*[:=|]\s*(.+?)\s*$")
DATE_STRICT = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SEPARATOR = re.compile(r"^\s*(?:-{3,}|={3,}|\*{3,})\s*$")

ALIASES: dict[str, str] = {}
for canonical, names in {
    "doc_id": ["id", "doc id", "document id", "doc_id", "permanent id", "doc-id", "docid"],
    "title": ["title", "headline"],
    "outlet": ["outlet", "source", "publisher", "outlet name", "source name", "source_name"],
    "pub_date": ["pub date", "published", "publication date", "date", "pub_date", "publication_date"],
    "source_type": ["source type", "type", "doc type", "source_type", "document type", "doc_type"],
    "event_id": ["event", "event id", "event_id", "event-id"],
    "country": ["country", "countries"],
    "sector": ["sector", "sectors"],
    "tier": ["tier", "reliability", "reliability tier", "reliability_tier", "source tier"],
    # trade tables
    "table_id": ["table id", "table_id", "table"],
    "country_pair": ["country pair", "pair", "country_pair", "countries pair", "route"],
    "unit": ["unit", "units"],
    "period": ["period", "coverage"],
    # reference sheets
    "sheet": ["sheet", "reference", "reference sheet"],
}.items():
    for n in names:
        ALIASES[n] = canonical

DOC_KEYS = {"doc_id", "title", "outlet", "pub_date", "source_type", "event_id", "country", "sector", "tier"}
REQUIRED_DOC_KEYS = {"pub_date", "event_id", "tier", "source_type"}
BACKGROUND_VALUES = {"background", "none", "n/a", "na", "-", "no event", "no_event", "bg"}
MULTI_SPLIT = re.compile(r"\s*[;,]\s*")


@dataclass
class Header:
    fields: dict[str, str] = field(default_factory=dict)
    extra: dict[str, str] = field(default_factory=dict)
    body: str = ""
    lines_used: int = 0
    found: bool = False


def canonical_key(raw: str) -> str | None:
    k = re.sub(r"\s+", " ", raw.strip().lower().replace("_", " "))
    return ALIASES.get(k) or ALIASES.get(k.replace(" ", "_"))


def parse_header(text: str, max_lines: int = 40, min_keys: int = 3) -> Header:
    """Parse the leading key/value block. Stops at the first blank line (or non-KV line) after `min_keys`."""
    lines = text.split("\n")
    h = Header()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    matched = 0
    start = i
    end = i
    while i < len(lines) and i - start < max_lines:
        line = lines[i]
        if SEPARATOR.match(line) and matched >= min_keys:
            end = i + 1  # an explicit end-of-header rule (e.g. "---") is consumed
            break
        if not line.strip():
            if matched >= min_keys:
                end = i
                break
            i += 1
            continue
        m = KV.match(line)
        if not m:
            if matched >= min_keys:
                end = i
                break
            if i - start > 3:  # too much non-KV noise before a header: no header
                break
            i += 1
            continue
        key = canonical_key(m.group(1))
        if key is None:
            if matched >= min_keys:
                # an unknown key after a recognised block still belongs to the header
                h.extra[m.group(1).strip()] = m.group(2).strip()
            i += 1
            continue
        if key not in h.fields:
            h.fields[key] = m.group(2).strip()
            matched += 1
        i += 1
        end = i
    if matched >= min_keys:
        h.found = True
        h.lines_used = end
        h.body = "\n".join(lines[end:]).strip()
    else:
        h.fields = {}
        h.extra = {}
        h.body = text.strip()
    return h


@dataclass
class DocMeta:
    doc_id: str | None = None
    title: str | None = None
    outlet: str | None = None
    pub_date: date | None = None
    source_type: str | None = None
    event_id: str | None = None
    is_background: bool = False
    countries: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    tier: int | None = None
    flags: list[str] = field(default_factory=list)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    v = value.strip()
    if not DATE_STRICT.match(v):
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        return None


def parse_tier(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 4 else None
    m = re.search(r"([1-4])", str(value))
    return int(m.group(1)) if m else None


def split_multi(value: str | None) -> list[str]:
    if not value:
        return []
    return [v for v in (x.strip() for x in MULTI_SPLIT.split(value)) if v]


def is_background_event(event_id: str | None) -> bool:
    if event_id is None:
        return False
    v = event_id.strip().lower()
    return v in BACKGROUND_VALUES or v.startswith("background")


def doc_meta_from_fields(fields: dict[str, str]) -> DocMeta:
    meta = DocMeta()
    meta.doc_id = fields.get("doc_id")
    meta.title = fields.get("title")
    meta.outlet = fields.get("outlet")
    raw_date = fields.get("pub_date")
    meta.pub_date = parse_date(raw_date)
    if raw_date and meta.pub_date is None:
        meta.flags.append("BAD_DATE")
    meta.source_type = (fields.get("source_type") or "").strip().lower().replace(" ", "_") or None
    ev = fields.get("event_id")
    if ev:
        meta.is_background = is_background_event(ev)
        meta.event_id = "background" if meta.is_background else ev.strip()
    raw_tier = fields.get("tier")
    meta.tier = parse_tier(raw_tier)
    if raw_tier and meta.tier is None:
        meta.flags.append("BAD_TIER")
    meta.countries = split_multi(fields.get("country"))
    meta.sectors = [s.lower() for s in split_multi(fields.get("sector"))]
    return meta

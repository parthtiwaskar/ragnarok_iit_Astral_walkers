"""Manifest loader (csv | json | txt), layout-agnostic, with a column alias map."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.ingest.header import parse_date, parse_tier

COLS = {
    "doc_id": ["doc_id", "id", "doc id", "document id", "document_id", "permanent_id"],
    "file": ["file", "path", "filename", "file_name", "file path", "location"],
    "title": ["title", "headline"],
    "outlet": ["outlet", "source", "publisher"],
    "pub_date": ["pub_date", "date", "published", "publication date", "publication_date"],
    "tier": ["tier", "reliability", "reliability_tier"],
    "event_id": ["event_id", "event", "event id"],
    "format": ["format", "fmt", "file_type", "type"],
    "word_count": ["word_count", "words", "length_words"],
    "size": ["size", "bytes", "size_bytes"],
}


@dataclass
class ManifestEntry:
    doc_id: str
    file: str | None = None
    title: str | None = None
    outlet: str | None = None
    pub_date: str | None = None
    tier: int | None = None
    event_id: str | None = None
    format: str | None = None
    word_count: int | None = None
    size: int | None = None
    pack: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def find_manifests(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if (
            p.is_file()
            and p.stem.lower() in ("manifest", "manifest_delta")
            and p.suffix.lower()
            in (
                ".csv",
                ".json",
                ".txt",
                ".tsv",
            )
        ):
            out.append(p)
    return out


def _pick(row: dict[str, Any], key: str) -> Any:
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for alias in COLS[key]:
        if alias in low and low[alias] not in (None, ""):
            return low[alias]
    return None


def _to_int(v: Any) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _entry(row: dict[str, Any], pack: str | None) -> ManifestEntry | None:
    doc_id = _pick(row, "doc_id")
    if not doc_id:
        return None
    pd = _pick(row, "pub_date")
    pdate = parse_date(str(pd)) if pd else None
    return ManifestEntry(
        doc_id=str(doc_id).strip(),
        file=str(_pick(row, "file")).strip() if _pick(row, "file") else None,
        title=_pick(row, "title"),
        outlet=_pick(row, "outlet"),
        pub_date=pdate.isoformat() if pdate else None,
        tier=parse_tier(_pick(row, "tier")),
        event_id=str(_pick(row, "event_id")).strip() if _pick(row, "event_id") else None,
        format=str(_pick(row, "format")).strip().lower() if _pick(row, "format") else None,
        word_count=_to_int(_pick(row, "word_count")),
        size=_to_int(_pick(row, "size")),
        pack=pack,
        raw={str(k): v for k, v in row.items()},
    )


def load_manifest(path: Path, pack: str | None = None) -> list[ManifestEntry]:
    """Never raises: a broken manifest yields an empty list (the caller reports it)."""
    try:
        raw = path.read_bytes().decode("utf-8", errors="replace").lstrip("﻿")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            data = json.loads(raw)
            if isinstance(data, dict):
                for key in ("documents", "docs", "items", "manifest"):
                    if isinstance(data.get(key), list):
                        data = data[key]
                        break
            if isinstance(data, list):
                rows = [r for r in data if isinstance(r, dict)]
        else:
            sample = raw[:2048]
            delim = "\t" if "\t" in sample else ("|" if sample.count("|") > sample.count(",") else ",")
            reader = csv.DictReader(io.StringIO(raw), delimiter=delim)
            rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader]
    except (ValueError, csv.Error):
        return []
    out = []
    for r in rows:
        e = _entry(r, pack)
        if e is not None:
            out.append(e)
    return out

"""Search filters: agent-controllable metadata filters + always-on as-of/status filters."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.tools.asof import Clock

StrOrList = str | list[str] | None


def _as_list(v: StrOrList) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [x for x in v if isinstance(x, str) and x.strip()]


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: StrOrList = Field(default=None, description="event id or list of ids")
    country: StrOrList = None
    sector: StrOrList = None
    tier_min: int | None = Field(default=None, ge=1, le=4)
    tier_max: int | None = Field(default=None, ge=1, le=4)
    source_type: StrOrList = None
    date_from: date | None = None
    date_to: date | None = None
    include_background: bool = False
    format: Literal["pdf", "txt", "docx"] | None = None

    @field_validator("date_from", "date_to", mode="before")
    @classmethod
    def _empty_date(cls, v: object) -> object:
        return None if v in ("", None) else v

    def canonical(self) -> dict[str, Any]:
        """Sorted, lower-cased, defaults filled: the dedupe fingerprint form."""
        return {
            "event_id": sorted(x.strip().lower() for x in _as_list(self.event_id)),
            "country": sorted(x.strip().lower() for x in _as_list(self.country)),
            "sector": sorted(x.strip().lower() for x in _as_list(self.sector)),
            "tier_min": self.tier_min,
            "tier_max": self.tier_max,
            "source_type": sorted(x.strip().lower() for x in _as_list(self.source_type)),
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "include_background": self.include_background,
            "format": self.format,
        }

    def clamp(self, clock: Clock) -> SearchFilters:
        """Agents cannot pass a date later than as-of."""
        out = self.model_copy()
        if out.date_to is None or out.date_to > clock.as_of:
            out.date_to = clock.as_of
        if out.date_from is not None and out.date_from > clock.as_of:
            out.date_from = clock.as_of
        return out

    def active(self) -> dict[str, Any]:
        """Only the filters the caller actually set (for logs and the debug panel)."""
        return {k: v for k, v in self.canonical().items() if v not in (None, [], False)}


def where_clause(f: SearchFilters, clock: Clock, alias: str = "d") -> tuple[str, list[object]]:
    """SQL over the docs table. Always-on: pub_date <= as_of, usable status, on-manifest, ingest pin."""
    a = alias
    parts = [
        f"{a}.pub_date IS NOT NULL",
        f"{a}.pub_date <= ?",
        f"{a}.status NOT IN ('QUARANTINED','MISSING','DUPLICATE')",
        f"{a}.off_manifest = 0",
    ]
    params: list[object] = [clock.iso]
    if clock.pin is not None:
        parts.append(f"{a}.ingest_seq <= ?")
        params.append(clock.pin)
    events = [e.lower() for e in _as_list(f.event_id)]
    if events:
        ph = ",".join("?" * len(events))
        if f.include_background:
            parts.append(f"(lower({a}.event_id) IN ({ph}) OR {a}.is_background = 1)")
        else:
            parts.append(f"lower({a}.event_id) IN ({ph})")
        params.extend(events)
    elif not f.include_background:
        parts.append(f"{a}.is_background = 0")
    for col, values in (("countries", _as_list(f.country)), ("sectors", _as_list(f.sector))):
        if values:
            ph = ",".join("?" * len(values))
            parts.append(f"EXISTS (SELECT 1 FROM json_each({a}.{col}) j WHERE lower(j.value) IN ({ph}))")
            params.extend(v.lower() for v in values)
    if f.tier_min is not None:
        parts.append(f"{a}.tier >= ?")
        params.append(f.tier_min)
    if f.tier_max is not None:
        parts.append(f"{a}.tier <= ?")
        params.append(f.tier_max)
    st = [s.lower() for s in _as_list(f.source_type)]
    if st:
        parts.append(f"lower({a}.source_type) IN ({','.join('?' * len(st))})")
        params.extend(st)
    if f.date_from is not None:
        parts.append(f"{a}.pub_date >= ?")
        params.append(f.date_from.isoformat())
    if f.date_to is not None:
        parts.append(f"{a}.pub_date <= ?")
        params.append(min(f.date_to, clock.as_of).isoformat())
    if f.format:
        parts.append(f"{a}.format = ?")
        params.append(f.format)
    return " AND ".join(parts), params

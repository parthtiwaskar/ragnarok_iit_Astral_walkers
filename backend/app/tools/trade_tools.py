"""Trade statistics (deterministic). Damaged rows are excluded and reported, never used silently."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.store.repo import Repo, TradeRowRecord, TradeTableRecord
from app.tools.asof import Clock, month_visible


def event_month_split(month: str, event_date: date) -> str:
    """'pre' | 'post' | 'event'. The event month counts as post-event when the event falls in its first half."""
    em = event_date.strftime("%Y-%m")
    if month < em:
        return "pre"
    if month > em:
        return "post"
    return "post" if event_date.day <= 15 else "event"


@dataclass
class TableStats:
    table_id: str
    baseline: float | None
    baseline_months: list[str]
    post_mean: float | None
    post_months: list[str]
    observed_change: float | None
    rows_used: list[str]
    rows_excluded: list[dict[str, Any]]
    hidden_future_rows: int
    unit: str | None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "table_id": self.table_id,
            "baseline": self.baseline,
            "baseline_months": self.baseline_months,
            "post_event_mean": self.post_mean,
            "post_event_months": self.post_months,
            "observed_change": self.observed_change,
            "rows_used": self.rows_used,
            "rows_excluded": self.rows_excluded,
            "hidden_future_rows": self.hidden_future_rows,
            "unit": self.unit,
            "notes": self.notes,
        }


def trade_stats(
    repo: Repo,
    table_id: str,
    event_date: date | None,
    clock: Clock,
    baseline_months: int = 6,
    metric: str = "value",
) -> TableStats | None:
    t = repo.get_trade_table(table_id)
    if t is None:
        return None
    rows = repo.trade_rows(table_id)
    visible = [r for r in rows if month_visible(r.month, clock)]
    hidden = len(rows) - len(visible)
    excluded = [
        {"ref": r.ref, "month": r.month, "quality": r.quality, "reason": r.reason}
        for r in visible
        if r.quality != "OK"
    ]
    clean = [r for r in visible if r.quality == "OK" and _metric(r, metric) is not None]
    notes: list[str] = []
    if event_date is None:
        last = sorted(clean, key=lambda r: r.month or "")[-baseline_months:]
        base = _mean([_metric(r, metric) for r in last])
        notes.append("no event date: baseline = last clean months, no post-event split")
        return TableStats(
            table_id,
            base,
            [r.month or "" for r in last],
            None,
            [],
            None,
            [r.ref for r in last],
            excluded,
            hidden,
            t.unit,
            notes,
        )
    pre = sorted(
        (r for r in clean if r.month and event_month_split(r.month, event_date) == "pre"),
        key=lambda r: r.month or "",
    )[-baseline_months:]
    post = sorted(
        (r for r in clean if r.month and event_month_split(r.month, event_date) == "post"),
        key=lambda r: r.month or "",
    )
    base = _mean([_metric(r, metric) for r in pre])
    post_mean = _mean([_metric(r, metric) for r in post])
    change = (post_mean / base - 1.0) if (base and post_mean is not None) else None
    if len(pre) < baseline_months:
        notes.append(f"only {len(pre)} clean pre-event months available (wanted {baseline_months})")
    if not post:
        notes.append("no clean post-event month visible as of the date")
    return TableStats(
        table_id=table_id,
        baseline=round(base, 3) if base is not None else None,
        baseline_months=[r.month or "" for r in pre],
        post_mean=round(post_mean, 3) if post_mean is not None else None,
        post_months=[r.month or "" for r in post],
        observed_change=round(change, 4) if change is not None else None,
        rows_used=[r.ref for r in pre + post],
        rows_excluded=excluded,
        hidden_future_rows=hidden,
        unit=t.unit,
        notes=notes,
    )


def _metric(r: TradeRowRecord, metric: str) -> float | None:
    return r.volume if metric == "volume" else r.value


def _mean(xs: list[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def row_stats(
    repo: Repo,
    table_ids: list[str],
    event_date: date | None,
    clock: Clock,
    baseline_months: int = 6,
    metric: str = "value",
) -> dict[str, Any]:
    """Aggregate over a scope row's tables: baseline sum, observed change, k_obs anchor."""
    per = [
        s for s in (trade_stats(repo, t, event_date, clock, baseline_months, metric) for t in table_ids) if s
    ]
    base_sum = sum(s.baseline for s in per if s.baseline is not None)
    both = [s for s in per if s.baseline and s.post_mean is not None]
    change = None
    if both:
        change = (
            sum(s.post_mean for s in both if s.post_mean is not None)
            / sum(s.baseline for s in both if s.baseline)
            - 1.0
        )
    k_obs = max(0.0, -change) if change is not None else None
    return {
        "tables": [s.as_dict() for s in per],
        "baseline_value": round(base_sum, 3) if per else None,
        "observed_change": round(change, 4) if change is not None else None,
        "k_obs": round(k_obs, 4) if k_obs is not None else None,
        "rows_excluded": [x for s in per for x in s.rows_excluded],
    }


def table_summary(repo: Repo, t: TradeTableRecord, clock: Clock) -> dict[str, Any]:
    rows = repo.trade_rows(t.table_id)
    visible = [r for r in rows if month_visible(r.month, clock)]
    q: dict[str, int] = {}
    for r in visible:
        q[r.quality] = q.get(r.quality, 0) + 1
    return {
        "table_id": t.table_id,
        "exporter": t.exporter,
        "importer": t.importer,
        "sector": t.sector,
        "unit": t.unit,
        "period": [t.period_start, t.period_end],
        "event_id": t.event_id,
        "status": t.status,
        "flags": t.flags,
        "visible_rows": len(visible),
        "hidden_future_rows": len(rows) - len(visible),
        "quality": q,
    }


def tables_for(
    repo: Repo, event_id: str | None = None, country: str | None = None, sector: str | None = None
) -> list[TradeTableRecord]:
    out = []
    for t in repo.trade_tables():
        if event_id and (t.event_id or "").lower() != event_id.lower():
            continue
        if country and country.lower() not in {(t.exporter or "").lower(), (t.importer or "").lower()}:
            continue
        if sector and (t.sector or "").lower() != sector.lower():
            continue
        out.append(t)
    return out

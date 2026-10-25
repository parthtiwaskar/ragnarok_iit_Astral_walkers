"""Heimdall's deterministic event tools: list_events and event_profile (rule verdict). As-of aware."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.search.filters import SearchFilters
from app.search.hybrid import HybridSearcher
from app.store.repo import DocRecord, Repo
from app.tools.asof import Clock, as_of_status, denials_for_event, doc_visible

NON_REPORT_TYPES = frozenset({"retraction", "denial"})
STALE_DAYS = 120


def _event_docs(repo: Repo, event_id: str) -> list[DocRecord]:
    return repo.docs(
        "lower(event_id)=lower(?) AND status NOT IN ('QUARANTINED','MISSING','DUPLICATE') AND off_manifest=0",
        (event_id,),
    )


def event_profile(
    repo: Repo, event_id: str, clock: Clock, min_tier1: int = 1, min_tier2_outlets: int = 2
) -> dict[str, Any]:
    all_docs = _event_docs(repo, event_id)
    docs = [d for d in all_docs if doc_visible(d, clock)]
    if not docs:
        future = any(not doc_visible(d, clock) for d in all_docs)
        verdict = "NOT_YET_KNOWN" if future else "NOT_FOUND"
        reason = (
            "documents about this event exist only after the as-of date; nothing about them can be shown"
            if future
            else "no document in Runemark carries this event id"
        )
        return {"event_id": event_id, "rule_verdict": verdict, "reasons": [reason], "visible_docs": 0}
    denial_ids = set(denials_for_event(repo, event_id, clock))
    retracted: dict[str, str] = {}
    corrected: dict[str, str] = {}
    for d in docs:
        st = as_of_status(repo, d.doc_id, clock)
        if st["retracted"]:
            retracted[d.doc_id] = str(st["retracted_by"])
        if st["corrected_by"]:
            corrected[d.doc_id] = str(st["corrected_by"])
    reports = [
        d for d in docs if (d.source_type or "") not in NON_REPORT_TYPES and d.doc_id not in denial_ids
    ]
    per_tier: dict[str, int] = {}
    per_tier_ok: dict[str, int] = {}
    outlets: dict[str, list[str]] = {}
    for d in reports:
        k = f"tier{d.tier}"
        per_tier[k] = per_tier.get(k, 0) + 1
        if d.doc_id not in retracted:
            per_tier_ok[k] = per_tier_ok.get(k, 0) + 1
            outlets.setdefault(k, [])
            if d.outlet and d.outlet not in outlets[k]:
                outlets[k].append(d.outlet)
    t1_ok = [d for d in reports if d.tier == 1 and d.doc_id not in retracted]
    t2_outlets = outlets.get("tier2", [])
    strong = [d for d in reports if (d.tier or 9) <= 2]
    reasons: list[str] = []
    if denial_ids and not t1_ok:
        verdict = "REFUTED"
        reasons.append(f"official denial(s) by Tier 1-2 source: {', '.join(sorted(denial_ids))}")
    elif len(t1_ok) >= min_tier1 or len(t2_outlets) >= min_tier2_outlets:
        verdict = "VERIFIED"
        if t1_ok:
            reasons.append(
                f"{len(t1_ok)} non-retracted Tier 1 report(s): {', '.join(d.doc_id for d in t1_ok[:5])}"
            )
        if len(t2_outlets) >= min_tier2_outlets:
            reasons.append(
                f"{len(t2_outlets)} distinct non-retracted Tier 2 outlets: {', '.join(t2_outlets)}"
            )
    elif strong and all(d.doc_id in retracted for d in strong):
        verdict = "RETRACTED"
        reasons.append(
            "every Tier 1-2 report was retracted as of the date: "
            + ", ".join(f"{k} (by {v})" for k, v in retracted.items())
        )
    else:
        verdict = "UNCONFIRMED"
        reasons.append(
            "only Tier 3-4 sources report the event; no Tier 1 and fewer than "
            f"{min_tier2_outlets} independent Tier 2 outlets"
        )
    if denial_ids and verdict == "VERIFIED":
        reasons.append(
            f"note: denial(s) exist ({', '.join(sorted(denial_ids))}) but Tier 1 confirmation outweighs"
        )
    key_docs = sorted(reports, key=lambda d: (d.tier or 9, d.pub_date or ""))[:8]
    dates = sorted(d.pub_date for d in docs if d.pub_date)
    return {
        "event_id": event_id,
        "rule_verdict": verdict,
        "reasons": reasons,
        "visible_docs": len(docs),
        "per_tier": per_tier,
        "per_tier_not_retracted": per_tier_ok,
        "outlets_by_tier": outlets,
        "retractions": [{"doc_id": k, "retracted_by": v} for k, v in retracted.items()],
        "corrections": [{"doc_id": k, "corrected_by": v} for k, v in corrected.items()],
        "denials": sorted(denial_ids),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "key_docs": [
            {
                "doc_id": d.doc_id,
                "tier": d.tier,
                "pub_date": d.pub_date,
                "title": d.title,
                "outlet": d.outlet,
                "source_type": d.source_type,
            }
            for d in key_docs
        ],
    }


def list_events(repo: Repo, searcher: HybridSearcher, query: str | None, clock: Clock) -> dict[str, Any]:
    """Candidate events with visible doc counts per tier. Never reveals events known only after as-of."""
    visible = [
        d
        for d in repo.docs(
            "status NOT IN ('QUARANTINED','MISSING','DUPLICATE') AND off_manifest=0 "
            "AND is_background=0 AND event_id IS NOT NULL"
        )
        if doc_visible(d, clock)
    ]
    events: dict[str, dict[str, Any]] = {}
    for d in visible:
        e = events.setdefault(
            d.event_id or "",
            {
                "event_id": d.event_id,
                "docs": 0,
                "per_tier": {},
                "first_date": d.pub_date,
                "titles": [],
                "score": 0.0,
            },
        )
        e["docs"] += 1
        e["last_date"] = max(e.get("last_date") or "", d.pub_date or "")
        e["per_tier"][f"tier{d.tier}"] = e["per_tier"].get(f"tier{d.tier}", 0) + 1
        if d.pub_date and (e["first_date"] is None or d.pub_date < e["first_date"]):
            e["first_date"] = d.pub_date
        if len(e["titles"]) < 3 and d.title:
            e["titles"].append(d.title)
    future_match = False
    if query:
        hits = searcher.search(query, SearchFilters(), clock, k=10)["results"]
        # rank-weighted hits among the top 10: a few precise matches can beat many loose ones
        for rank, h in enumerate(hits):
            ev = events.get(h["event_id"] or "")
            if ev is not None:
                ev["score"] += 1.0 / (1 + rank)
        # a question about a disruption is about a current one: events silent for months weigh half
        for ev in events.values():
            last = ev.get("last_date")
            if last and (clock.as_of - date.fromisoformat(last)).days > STALE_DAYS:
                ev["score"] *= 0.5
        far = Clock(as_of=date(9999, 12, 31), pin=None, trade_rule=clock.trade_rule)
        future_hits = searcher.search(query, SearchFilters(), far, k=5)["results"]
        if future_hits:
            top = future_hits[0]["event_id"]
            if top and top not in events:
                future_match = True
    ranked = sorted(events.values(), key=lambda e: (-e["score"], -e["docs"], e["event_id"] or ""))
    for e in ranked:
        e["score"] = round(e["score"], 4)
    out: dict[str, Any] = {"candidates": ranked[:10]}
    if future_match:
        out["note"] = (
            "the best match for this query is an event that is only documented after the as-of "
            "date; its details cannot be shown (verdict NOT_YET_KNOWN)"
        )
        out["future_match"] = True
    if not ranked:
        out["note"] = "no events visible as of the date"
    return out

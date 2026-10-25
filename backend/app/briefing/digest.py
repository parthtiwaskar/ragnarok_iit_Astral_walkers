"""Rulings digest: the only input to Odin's briefing turn (CASE_FILE_SCHEMA.md §5)."""

from __future__ import annotations

from typing import Any

from app.tools.citation import evidence_ref
from app.trial.casefile import CaseFile, Claim, DocEvidence, PoolItem, Position, TradeEvidence
from app.trial.sufficiency import checks_for, valid_evidence


def cite(e: DocEvidence | TradeEvidence) -> str:
    if isinstance(e, DocEvidence):
        para = f" {e.paragraph}" if e.paragraph else ""
        return f"[{e.doc_id}{para}, {e.pub_date.isoformat()}]"
    return f"[{e.table_id} r{e.row:02d}, {e.month}]"


def citation_entry(e: DocEvidence | TradeEvidence) -> dict[str, Any]:
    if isinstance(e, DocEvidence):
        return {
            "ref": e.doc_id,
            "date": e.pub_date.isoformat(),
            "kind": "doc",
            "tier": e.tier,
            "paragraph": e.paragraph,
            "cite": cite(e),
        }
    return {
        "ref": f"{e.table_id}#r{e.row:02d}",
        "table_id": e.table_id,
        "row": e.row,
        "date": e.month,
        "kind": "trade",
        "cite": cite(e),
    }


def _claim_entry(cf: CaseFile, c: Claim, rid: str, why: str) -> dict[str, Any]:
    ev = valid_evidence(c, checks_for(cf, c.id))
    return {
        "ruling": rid,
        "claim_id": c.id,
        "side": c.side,
        "statement": c.statement,
        "type": c.type,
        "parameter": c.parameter,
        "bound_value": c.bound_value,
        "country": c.country,
        "sector": c.sector,
        "citations": [citation_entry(e) for e in ev],
        "why": why,
    }


def latest_positions(cf: CaseFile) -> dict[str, list[Position]]:
    out: dict[str, list[Position]] = {"thor": [], "loki": []}
    seen: set[tuple[str, str, str]] = set()
    for r in sorted(cf.runs, key=lambda x: -x.n):
        for p in r.positions:
            key = (p.side, p.country, p.sector)
            if key not in seen:
                seen.add(key)
                out[p.side].append(p)
    return out


def build_digest(cf: CaseFile, failures: list[str], status: str, reasons: list[str]) -> dict[str, Any]:
    eff = cf.effective_rulings()
    claims = {c.id: c for c in cf.all_claims()}
    established: list[dict[str, Any]] = []
    contested: list[dict[str, Any]] = []
    conceded: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for cid, rul in eff.items():
        c = claims.get(cid)
        if c is None:
            continue
        if c.status == "SUPERSEDED":
            continue
        entry = _claim_entry(cf, c, rul.id, rul.rationale)
        entry["basis"] = rul.basis
        entry["verdict"] = rul.verdict
        if rul.finding == "ESTABLISHED":
            established.append(entry)
        elif rul.finding == "CONTESTED":
            contested.append(entry)
        elif rul.finding == "CONCEDED":
            entry["by"] = c.side
            conceded.append(entry)
        else:
            entry["citation_issues"] = [k.model_dump() for k in rul.citation_issues if not k.valid]
            rejected.append(entry)
    last = cf.runs[-1] if cf.runs else None
    finals = [
        {
            "country": f.country,
            "sector": f.sector,
            "low": f.low,
            "high": f.high,
            "unit": "%",
            "contested_params": f.contested_params,
            "unsupported_params": f.unsupported_params,
            "rulings": f.supporting_rulings,
            "observed_anchor": f.observed_anchor,
        }
        for f in (last.final_ranges if last else [])
    ]
    pos = latest_positions(cf)
    allowed: dict[str, str] = {}
    for group in (established, contested, conceded):
        for e in group:
            for ci in e["citations"]:
                allowed[ci["cite"]] = ci["date"]
    ver = cf.verification
    verification: dict[str, Any] = {
        "verdict": ver.verdict if ver else None,
        "reasons": ver.reasons if ver else [],
        "docs": ver.supporting_docs if ver else [],
        "retractions_applied": ver.retractions_applied if ver else [],
    }
    # verification docs are citable in the verification / summary sections
    pool_by_ref: dict[str, PoolItem] = {p.ref: p for p in cf.pool}
    for d in verification["docs"]:
        p = pool_by_ref.get(d)
        if p is not None and p.pub_date and p.status not in ("RETRACTED_AS_OF", "QUARANTINED"):
            allowed[f"[{d}, {p.pub_date.isoformat()}]"] = p.pub_date.isoformat()
    return {
        "case_id": cf.case_id,
        "question": cf.question,
        "as_of": cf.as_of.isoformat(),
        "runs": len(cf.runs),
        "status": status,
        "status_reasons": reasons,
        "verification": verification,
        "event": {
            "event_id": cf.scope.event_id,
            "summary": cf.scope.event_summary,
            "event_date": cf.scope.event_date.isoformat() if cf.scope.event_date else None,
            "horizon": [cf.scope.horizon_start.isoformat(), cf.scope.horizon_end.isoformat()],
        }
        if cf.scope
        else None,
        "established": established,
        "contested": contested,
        "conceded": conceded,
        "rejected": rejected,
        "final_ranges": finals,
        "positions": {
            side: [{"country": p.country, "sector": p.sector, "low": p.low, "high": p.high} for p in ps]
            for side, ps in pos.items()
        },
        "data_quality": [f"{x.get('ref')}: {x.get('issue')}" for x in cf.data_quality],
        "traps": [_trap_line(t) for t in cf.traps],
        "failures": failures,
        "allowed_citations": allowed,
        "ruling_ids": {
            "ESTABLISHED": [e["ruling"] for e in established],
            "CONTESTED": [e["ruling"] for e in contested],
            "CONCEDED": [e["ruling"] for e in conceded],
            "REJECTED": [e["ruling"] for e in rejected],
        },
    }


def _trap_line(t: dict[str, Any]) -> str:
    kind = t.get("type")
    doc = t.get("doc_id")
    if kind == "INJECTION":
        return f"{doc} contained embedded instructions; ignored"
    if kind == "RETRACTION":
        return f"{doc} was retracted as of the date (by {t.get('by')}); not usable"
    if kind == "CORRECTION":
        return f"{doc} was corrected as of the date (by {t.get('by')}); corrected figure applies"
    if kind == "RUMOUR":
        return f"rumour: {t.get('detail', doc)}"
    if kind in ("WRONG_EVENT", "BACKGROUND"):
        return f"{doc} belongs to {t.get('event')}; context only"
    return f"{kind}: {doc}"


def all_citable_refs(cf: CaseFile) -> set[str]:
    out: set[str] = set()
    for c in cf.all_claims():
        out.update(evidence_ref(e) for e in valid_evidence(c, checks_for(cf, c.id)))
    return out

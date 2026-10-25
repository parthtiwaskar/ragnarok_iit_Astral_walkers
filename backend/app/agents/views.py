"""Role-filtered JSON views of the Case File, rendered into each turn's first message.

Built from `CaseFile.view_for(agent, phase)` so sealed opponent claims never reach a side during Round 1.
Memory content is never part of a view (it is added separately, per agent, from that agent's own store).
"""

from __future__ import annotations

from typing import Any

from app.tools.citation import evidence_ref
from app.trial.casefile import CaseFile, Claim, DocEvidence, Ruling
from app.trial.sufficiency import checks_for


def pool_summary(cf: CaseFile) -> list[dict[str, Any]]:
    return [
        {
            "ref": p.ref,
            "kind": p.kind,
            "tier": p.tier,
            "pub_date": p.pub_date,
            "event_id": p.event_id,
            "status": p.status,
            "flags": p.flags,
            "title": p.title,
            "added_in_run": p.added_in_run,
        }
        for p in cf.pool
    ]


def scope_view(cf: CaseFile) -> dict[str, Any] | None:
    if cf.scope is None:
        return None
    return {
        "event_id": cf.scope.event_id,
        "event_summary": cf.scope.event_summary,
        "event_date": cf.scope.event_date,
        "chokepoints": cf.scope.chokepoints,
        "horizon": [cf.scope.horizon_start, cf.scope.horizon_end],
        "rows": [
            {
                "country": r.country,
                "sector": r.sector,
                "trade_tables": r.trade_tables,
                "baseline_value": r.baseline_value,
                "unit": r.unit,
                "route_flags": r.route_flags,
            }
            for r in cf.scope.rows
        ],
    }


def evidence_view(e: Any) -> dict[str, Any]:
    if isinstance(e, DocEvidence):
        return {
            "doc_id": e.doc_id,
            "quote": e.quote,
            "tier": e.tier,
            "pub_date": e.pub_date,
            "event_id": e.event_id,
        }
    return {
        "table_id": e.table_id,
        "row": e.row,
        "month": e.month,
        "value": e.value,
        "volume": e.volume,
        "quality": e.quality,
    }


def claim_view(cf: CaseFile, c: Claim, with_checks: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": c.id,
        "run": c.run,
        "side": c.side,
        "statement": c.statement,
        "type": c.type,
        "premises": c.premises,
        "country": c.country,
        "sector": c.sector,
        "parameter": c.parameter,
        "bound_value": c.bound_value,
        "status": c.status,
        "unchallenged": c.unchallenged,
        "flags": c.flags,
        "supersedes": c.supersedes,
        "evidence": [evidence_view(e) for e in c.evidence],
    }
    if with_checks:
        out["validation"] = [
            {
                "ref": k.evidence_ref,
                "valid": k.valid,
                "issues": k.issues,
                "quote_similarity": k.quote_similarity,
                "superseded_by": k.superseded_by,
            }
            for k in checks_for(cf, c.id)
        ]
    return out


def ruling_view(r: Ruling) -> dict[str, Any]:
    return {
        "id": r.id,
        "claim_id": r.claim_id,
        "run": r.run,
        "verdict": r.verdict,
        "basis": r.basis,
        "finding": r.finding,
        "rationale": r.rationale,
        "adjusted_bound": r.adjusted_bound,
    }


def prior_rulings(cf: CaseFile, before_run: int) -> list[dict[str, Any]]:
    out = []
    for r in cf.runs:
        if r.n >= before_run:
            continue
        for rul in r.rulings:
            out.append(ruling_view(rul))
    return out


def r1_view(cf: CaseFile, side: str, run: int) -> dict[str, Any]:
    v = cf.view_for(side, "R1_SUBMIT")  # type: ignore[arg-type]
    prev_defs = [d.model_dump() for d in v.deficiencies(run - 1)] if run > 1 else []
    return {
        "phase": "R1_SUBMIT",
        "run": run,
        "side": side,
        "as_of": v.as_of,
        "question": v.question,
        "scope": scope_view(v),
        "verification": v.verification.model_dump() if v.verification else None,
        "pool": pool_summary(v),
        "traps_known": v.traps,
        "data_quality": v.data_quality,
        "earlier_claims": [claim_view(v, c) for c in v.all_claims() if c.run < run],
        "my_active_claims": [c.id for c in v.active_claims(side) if c.run < run],  # type: ignore[arg-type]
        "prior_rulings": prior_rulings(v, run),
        "deficiencies": prev_defs,
        "new_evidence": sorted(v.new_evidence(run)) if run > 1 else [],
    }


def attack_view(cf: CaseFile, side: str, run: int) -> dict[str, Any]:
    opponent = "loki" if side == "thor" else "thor"
    return {
        "phase": "R2_ATTACK",
        "run": run,
        "side": side,
        "as_of": cf.as_of,
        "scope": scope_view(cf),
        "opponent_claims": [claim_view(cf, c) for c in cf.active_claims(opponent)],  # type: ignore[arg-type]
        "my_claims": [claim_view(cf, c) for c in cf.active_claims(side)],  # type: ignore[arg-type]
        "pool": pool_summary(cf),
        "traps_known": cf.traps,
    }


def respond_view(cf: CaseFile, side: str, run: int) -> dict[str, Any]:
    r = cf.run(run)
    mine = {c.id for c in cf.all_claims() if c.side == side}
    items = []
    for a in r.attacks:
        if a.target_claim not in mine:
            continue
        c = cf.claim(a.target_claim)
        items.append(
            {
                "attack": {
                    "id": a.id,
                    "by": a.by,
                    "type": a.type,
                    "argument": a.argument,
                    "evidence": [evidence_view(e) for e in a.evidence],
                    "validation": [k.model_dump() for k in r.citation_checks.get(a.id, [])],
                },
                "target_claim": claim_view(cf, c) if c else None,
                "already_cited": [evidence_ref(e) for e in c.evidence] if c else [],
            }
        )
    return {"phase": "R2_RESPOND", "run": run, "side": side, "as_of": cf.as_of, "attacks_on_me": items}


def judge_view(cf: CaseFile, run: int, to_rule: list[Claim], pre: dict[str, Any]) -> dict[str, Any]:
    r = cf.run(run)
    return {
        "phase": "JUDGING",
        "run": run,
        "as_of": cf.as_of,
        "scope": scope_view(cf),
        "verification": cf.verification.model_dump() if cf.verification else None,
        "claims_to_rule": [claim_view(cf, c) for c in to_rule],
        "attacks": [
            {
                "id": a.id,
                "by": a.by,
                "target_claim": a.target_claim,
                "type": a.type,
                "argument": a.argument,
                "evidence": [evidence_view(e) for e in a.evidence],
                "validation": [k.model_dump() for k in r.citation_checks.get(a.id, [])],
            }
            for a in r.attacks
        ],
        "responses": [
            {
                "id": x.id,
                "by": x.by,
                "attack_id": x.attack_id,
                "action": x.action,
                "argument": x.argument,
                "new_evidence": [evidence_view(e) for e in x.new_evidence],
                "revised_bound": x.revised_bound,
                "validation": [k.model_dump() for k in r.citation_checks.get(x.id, [])],
            }
            for x in r.responses
        ],
        "unrebutted_attacks": r.unrebutted,
        "positions": [p.model_dump() for p in r.positions],
        "side_status": {k: v.value for k, v in r.side_status.items()},
        "prior_rulings": prior_rulings(cf, run),
        "checklist_facts": pre,
        "data_quality": cf.data_quality,
        "traps_known": cf.traps,
    }


def research_view(cf: CaseFile, run: int, past_queries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "phase": "RESEARCH",
        "run": run,
        "as_of": cf.as_of,
        "question": cf.question,
        "scope": scope_view(cf),
        "deficiencies": [d.model_dump() for d in cf.deficiencies(run)],
        "pool": pool_summary(cf),
        "past_queries": past_queries,
    }

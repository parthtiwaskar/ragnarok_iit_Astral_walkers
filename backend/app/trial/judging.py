"""Odin's JUDGING turn, deterministic side: ruling coverage + consistency, final ranges, sufficiency merge."""

from __future__ import annotations

import re
from typing import Any

from app.tools.catalog import record_calc
from app.tools.context import CaseContext
from app.tools.exposure import compute, final_params
from app.tools.submit_tools import SubmitRulingsArgs, SubmitSufficiencyArgs
from app.tools.trade_tools import row_stats
from app.trial.casefile import Claim, FinalRange, Parameter, Position, Ruling, Sufficiency
from app.trial.sufficiency import (
    checks_for,
    deterministic_checklist,
    make_deficiencies,
    strong_support,
    tier4_only,
    valid_evidence,
)
from app.trial.validators import Applied

# A deficiency must describe a gap. These shapes are instructions to someone (invariant 2).
INSTRUCTION = re.compile(
    r"^\s*(?:please\s+)?(?:search|find|look|check|investigate|gather|collect|retrieve|obtain|get|query|research|"
    r"re-?search|read|ask|assign|send|go|you|heimdall|thor|loki|odin)\b"
    r"|\b(?:heimdall|thor|loki)\s*(?:,|:|should|must|needs? to|has to)",
    re.IGNORECASE,
)


def claims_to_rule(ctx: CaseContext, run: int) -> list[Claim]:
    """New/amended claims of this run + earlier claims attacked, conceded, or named by a deficiency this run."""
    cf = ctx.cf
    r = cf.run(run)
    ids: list[str] = [c.id for c in r.claims]
    attacked = {a.target_claim for a in r.attacks}
    touched: set[str] = set()
    if run > 1:
        for d in cf.deficiencies(run - 1):
            touched.update(d.related)
    for c in cf.all_claims():
        if c.run < run and (
            c.id in attacked or c.id in r.conceded_prior or (c.id in touched and c.status == "ACTIVE")
        ):
            ids.append(c.id)
    out = []
    for cid in dict.fromkeys(ids):
        cl = cf.claim(cid)
        if cl is not None:
            out.append(cl)
    return out


def _consistency(c: Claim, verdict: str, finding: str, basis: str, ctx: CaseContext) -> list[str]:
    errs: list[str] = []
    chks = checks_for(ctx.cf, c.id)
    has_valid = bool(valid_evidence(c, chks))
    if c.status == "CONCEDED" and verdict != "CONCEDED":
        errs.append(f"{c.id} was conceded by its side: verdict must be CONCEDED")
    if verdict == "CONCEDED" and c.status != "CONCEDED":
        errs.append(f"{c.id} was not conceded by its side: CONCEDED is not available")
    if verdict in ("UPHELD", "PARTIALLY_UPHELD") and not has_valid:
        errs.append(f"{c.id} has no valid citation (validator): it cannot be upheld")
    pairs = {
        "REJECTED": {"REJECTED"},
        "CONCEDED": {"CONCEDED"},
        "UPHELD": {"ESTABLISHED", "CONTESTED"},
        "PARTIALLY_UPHELD": {"ESTABLISHED", "CONTESTED"},
    }
    if finding not in pairs.get(verdict, set()):
        errs.append(f"{c.id}: finding {finding} does not match verdict {verdict}")
    if finding == "ESTABLISHED" and tier4_only(c, chks):
        errs.append(f"{c.id}: Tier 4 alone is never ESTABLISHED")
    if finding == "ESTABLISHED" and c.unchallenged and not _tier1_or_row(c, chks):
        errs.append(f"{c.id} is unchallenged: at most CONTESTED unless backed by Tier 1 or a clean trade row")
    if verdict in ("UPHELD", "PARTIALLY_UPHELD") and basis == "UNSUPPORTED":
        errs.append(f"{c.id}: an upheld claim cannot have basis UNSUPPORTED")
    return errs


def _tier1_or_row(c: Claim, chks: list[Any]) -> bool:
    from app.trial.casefile import DocEvidence, TradeEvidence

    for e in valid_evidence(c, chks):
        if (isinstance(e, TradeEvidence) and e.quality == "OK") or (
            isinstance(e, DocEvidence) and e.tier == 1
        ):
            return True
    return False


def _auto_ruling(c: Claim, run: int, ctx: CaseContext, reason: str) -> Ruling:
    """Deterministic fallback used only on the last attempt for an inconsistent ruling (logged as such)."""
    chks = checks_for(ctx.cf, c.id)
    if c.status == "CONCEDED":
        v, b, f = "CONCEDED", "UNSUPPORTED", "CONCEDED"
    elif not valid_evidence(c, chks):
        v, b, f = "REJECTED", "UNSUPPORTED", "REJECTED"
    else:
        v, b, f = "NO_RULING", "UNSUPPORTED", "REJECTED"
    return Ruling(
        id=f"RUL:{c.id}:R{run}",
        claim_id=c.id,
        run=run,
        verdict=v,
        basis=b,
        finding=f,
        rationale=f"Automatic: {reason}",
        decisive_evidence=[],
        citation_issues=chks,
    )


def apply_rulings(ctx: CaseContext, run: int, a: SubmitRulingsArgs, last: bool) -> Applied:
    cf = ctx.cf
    r = cf.run(run)
    targets = {c.id: c for c in claims_to_rule(ctx, run)}
    errors: list[str] = []
    warnings: list[str] = []
    given: dict[str, Any] = {}
    for x in a.rulings:
        if x.claim_id not in targets:
            (errors if not last else warnings).append(f"{x.claim_id} is not a claim to rule on this run")
            continue
        given[x.claim_id] = x
    missing = [cid for cid in targets if cid not in given]
    if missing:
        (errors if not last else warnings).append(f"rule on every listed claim; missing: {missing}")
    rulings: list[Ruling] = []
    for cid, c in targets.items():
        xr = given.get(cid)
        if xr is None:
            if last:
                rulings.append(
                    _auto_ruling(c, run, ctx, "no ruling given")
                    if c.status == "CONCEDED" or not valid_evidence(c, checks_for(cf, cid))
                    else Ruling(
                        id=f"RUL:{cid}:R{run}",
                        claim_id=cid,
                        run=run,
                        verdict="NO_RULING",
                        basis="UNSUPPORTED",
                        finding="REJECTED",
                        rationale="Odin gave no ruling on this claim.",
                        decisive_evidence=[],
                        citation_issues=checks_for(cf, cid),
                    )
                )
            continue
        errs = _consistency(c, xr.verdict, xr.finding, xr.basis, ctx)
        if errs:
            if not last:
                errors.extend(errs)
                continue
            warnings.extend(errs)
            rulings.append(_auto_ruling(c, run, ctx, "; ".join(errs)))
            continue
        adj = xr.adjusted_bound if xr.verdict == "PARTIALLY_UPHELD" else None
        rulings.append(
            Ruling(
                id=f"RUL:{cid}:R{run}",
                claim_id=cid,
                run=run,
                verdict=xr.verdict,
                basis=xr.basis,
                finding=xr.finding,
                rationale=xr.rationale.strip(),
                decisive_evidence=xr.decisive_evidence,
                citation_issues=checks_for(cf, cid),
                memory_influence=[m.model_dump() for m in a.memory_influence],
                adjusted_bound=adj,
            )
        )
    if errors and not last:
        return Applied(False, errors, done=False)
    r.rulings = rulings
    for rul in rulings:
        ctx.emit("ruling_issued", "odin", rul.model_dump())
    r.final_ranges = compute_final_ranges(ctx, run, a.range_notes)
    for fr in r.final_ranges:
        ctx.emit("final_range", "odin", fr.model_dump())
    s = ctx.services.settings
    checklist, gaps, facts = deterministic_checklist(cf, run, s.MAX_CONFIDENT_WIDTH_PP)
    ctx.pending_checklist = (checklist, gaps)
    for w in warnings:
        ctx.emit("validation_warning", "thing", {"agent": "odin", "warning": w})
    return Applied(
        True,
        warnings=warnings,
        done=False,
        action_ref=",".join(x.id for x in rulings),
        memory_influence=[m.model_dump() for m in a.memory_influence],
        result={
            "accepted": True,
            "final_ranges": [
                {
                    "country": f.country,
                    "sector": f.sector,
                    "low": f.low,
                    "high": f.high,
                    "params": {k: list(v) for k, v in f.params.items()},
                    "contested_params": f.contested_params,
                    "unsupported_params": f.unsupported_params,
                    "observed_anchor": f.observed_anchor,
                    "justification": f.justification,
                }
                for f in r.final_ranges
            ],
            "checklist": checklist,
            "deterministic_gaps": gaps,
            "facts": facts,
            "next": "now call submit_sufficiency",
            "warnings": warnings,
        },
    )


def _latest_position(ctx: CaseContext, side: str, country: str, sector: str, run: int) -> Position | None:
    """The side's most recent position for the row (a side that failed this run keeps its last one)."""
    for rr in sorted((x for x in ctx.cf.runs if x.n <= run), key=lambda x: -x.n):
        for p in rr.positions:
            if p.side == side and p.country == country and p.sector == sector:
                return p
    return None


def compute_final_ranges(ctx: CaseContext, run: int, notes: dict[str, str] | None = None) -> list[FinalRange]:
    """Apply EXPOSURE_MODEL §6 to the effective rulings, then the observed anchor, then compute_exposure."""
    cf = ctx.cf
    s = ctx.services.settings
    H = float(s.HORIZON_MONTHS)
    if not cf.scope:
        return []
    claims = {c.id: c for c in cf.all_claims()}
    eff = cf.effective_rulings()
    out: list[FinalRange] = []
    for row in cf.scope.rows:
        thor = _latest_position(ctx, "thor", row.country, row.sector, run)
        loki = _latest_position(ctx, "loki", row.country, row.sector, run)
        fps = final_params(thor, loki, claims, eff, H)
        stats = row_stats(
            ctx.services.repo, row.trade_tables, cf.scope.event_date, ctx.clock, s.BASELINE_MONTHS
        )
        k_obs = stats["k_obs"]
        res = compute(
            {p: list(fp.interval) for p, fp in fps.items()},
            horizon=H,
            min_width=s.MIN_PARAM_WIDTH,
            k_obs=k_obs,
            anchor_tol=s.ANCHOR_TOL,
        )
        param_claims: dict[Any, list[str]] = {p: fp.rulings for p, fp in fps.items()}
        record_calc(ctx, "odin", res, param_claims, row.country, row.sector)
        contested: list[Parameter] = [p for p, fp in fps.items() if fp.status == "CONTESTED"]
        unsupported: list[Parameter] = [p for p, fp in fps.items() if fp.status in ("UNSUPPORTED", "MISSING")]
        parts = [
            f"{p}: {fp.status.lower()} [{fp.interval[0]:.3g}, {fp.interval[1]:.3g}]" for p, fp in fps.items()
        ]
        if res.anchor_applied:
            parts.append(f"observed anchor k_obs={k_obs} applied")
        elif res.anchor_conflict:
            parts.append(f"observed anchor k_obs={k_obs} conflicts with the surviving bounds (not applied)")
        key = f"{row.country}/{row.sector}"
        note = (notes or {}).get(key) or (notes or {}).get(key.lower())
        if note:
            parts.append(f"Odin: {note.strip()}")
        out.append(
            FinalRange(
                country=row.country,
                sector=row.sector,
                low=res.low,
                high=res.high,
                unit="pct_of_baseline_trade_value",
                params={p: res.params[p] for p in fps},
                supporting_rulings=sorted({x for fp in fps.values() for x in fp.rulings}),
                observed_anchor=stats["observed_change"],
                justification="; ".join(parts),
                calc_id=res.calc_id,
                contested_params=contested,
                unsupported_params=unsupported,
                anchor_conflict=res.anchor_conflict,
            )
        )
    return out


def apply_sufficiency(ctx: CaseContext, run: int, a: SubmitSufficiencyArgs, last: bool) -> Applied:
    cf = ctx.cf
    r = cf.run(run)
    s = ctx.services.settings
    if not r.rulings:
        return Applied(False, ["call submit_rulings first"])
    if ctx.pending_checklist is not None:
        checklist, gaps = ctx.pending_checklist
    else:
        checklist, gaps, _ = deterministic_checklist(cf, run, s.MAX_CONFIDENT_WIDTH_PP)
    errors: list[str] = []
    warnings: list[str] = []
    odin_items: list[dict[str, Any]] = []
    for d in a.deficiencies:
        if INSTRUCTION.search(d.statement):
            msg = (
                f"deficiency '{d.statement[:80]}' reads as an instruction; state what is missing "
                "(e.g. 'No Tier 1-2 source on X dated on or before the as-of date')"
            )
            (errors if not last else warnings).append(msg)
            if last:
                ctx.emit("deficiency_rejected", "thing", {"statement": d.statement, "reason": "instruction"})
            continue
        odin_items.append({"check": d.check, "statement": d.statement, "related": d.related})
    checklist = dict(checklist)
    checklist["S5"] = bool(a.s5_contradictions_resolved)
    width_ok = checklist["S6"] or bool((a.width_reason or "").strip())
    if not a.s5_contradictions_resolved and not any(i["check"] == "S5" for i in odin_items):
        odin_items.append(
            {
                "check": "S5",
                "statement": "Conflicting sources on the same point were neither resolved "
                "nor marked Contested.",
                "related": [],
            }
        )
    det_failed = [k for k, v in checklist.items() if not v and k not in ("S5", "S6")]
    if not width_ok:
        det_failed.append("S6")
    verdict = a.verdict
    overridden = False
    if verdict == "SUFFICIENT" and det_failed:
        overridden = True
        verdict = "INSUFFICIENT"
        warnings.append(f"SUFFICIENT overridden: deterministic checks failed {det_failed}")
    items = [
        g for g in gaps if g["check"] in det_failed or (g["check"] == "S6" and not width_ok)
    ] + odin_items
    if verdict == "INSUFFICIENT" and not items:
        if not last:
            errors.append("INSUFFICIENT needs at least one deficiency (a statement of what is missing)")
        else:
            items.append(
                {
                    "check": "S5",
                    "statement": "Odin found the evidence insufficient without naming a gap.",
                    "related": [],
                }
            )
    if errors and not last:
        return Applied(False, errors, done=True)
    defs = make_deficiencies(run, items) if verdict == "INSUFFICIENT" else []
    if checklist["S6"] is False and width_ok:
        checklist["S6"] = True
    r.sufficiency = Sufficiency(
        verdict=verdict,
        checklist=checklist,
        deficiencies=defs,
        width_reason=a.width_reason,
        overridden=overridden,
    )
    ctx.emit("sufficiency", "odin", r.sufficiency.model_dump())
    for w in warnings:
        ctx.emit("validation_warning", "thing", {"agent": "odin", "warning": w})
    return Applied(
        True,
        warnings=warnings,
        done=True,
        action_ref=f"R{run}-sufficiency",
        memory_influence=[m.model_dump() for m in a.memory_influence],
        result={
            "accepted": True,
            "verdict": verdict,
            "overridden": overridden,
            "deficiencies": [d.model_dump() for d in defs],
        },
    )


def summarize_claim(ctx: CaseContext, c: Claim) -> dict[str, Any]:
    chks = checks_for(ctx.cf, c.id)
    return {
        "strong_support": strong_support(c, chks),
        "tier4_only": tier4_only(c, chks),
        "valid_evidence": len(valid_evidence(c, chks)),
        "total_evidence": len(c.evidence),
    }

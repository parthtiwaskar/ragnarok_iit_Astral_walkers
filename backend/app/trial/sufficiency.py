"""Deterministic part of the sufficiency rubric (TRIAL_PROTOCOL.md §4). Odin writes the verdict."""

from __future__ import annotations

from typing import Any

from app.tools.citation import evidence_ref
from app.trial.casefile import (
    CaseFile,
    CitationCheck,
    Claim,
    Deficiency,
    DocEvidence,
    FinalRange,
    Ruling,
    SideStatus,
    TradeEvidence,
)

CHECKS = ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9")


def valid_evidence(claim: Claim, checks: list[CitationCheck]) -> list[DocEvidence | TradeEvidence]:
    ok = {c.evidence_ref for c in checks if c.valid}
    return [e for e in claim.evidence if evidence_ref(e) in ok]


def checks_for(cf: CaseFile, claim_id: str) -> list[CitationCheck]:
    for r in cf.runs:
        if claim_id in r.citation_checks:
            return r.citation_checks[claim_id]
    return []


def strong_support(claim: Claim, checks: list[CitationCheck]) -> bool:
    """>= 1 valid Tier 1-2 document or a clean trade row."""
    for e in valid_evidence(claim, checks):
        if isinstance(e, TradeEvidence) and e.quality == "OK":
            return True
        if isinstance(e, DocEvidence) and e.tier <= 2:
            return True
    return False


def tier4_only(claim: Claim, checks: list[CitationCheck]) -> bool:
    ev = valid_evidence(claim, checks)
    return bool(ev) and all(isinstance(e, DocEvidence) and e.tier >= 4 for e in ev)


def deterministic_checklist(
    cf: CaseFile, run: int, max_width_pp: float
) -> tuple[dict[str, bool], list[dict[str, Any]], dict[str, Any]]:
    """Returns (checklist, gap statements, facts). S5 and S9 are Odin's; they default to pass here."""
    r = cf.run(run)
    rows = cf.scope.rows if cf.scope else []
    eff = cf.effective_rulings()
    claims = {c.id: c for c in cf.all_claims()}
    gaps: list[dict[str, Any]] = []
    facts: dict[str, Any] = {}
    # S1
    s1 = True
    for row in rows:
        for side in ("thor", "loki"):
            if not any(
                p.side == side and p.country == row.country and p.sector == row.sector for p in r.positions
            ):
                s1 = False
                gaps.append(
                    {
                        "check": "S1",
                        "statement": f"No position from {side.capitalize()} for "
                        f"{row.country}/{row.sector} in run {run}.",
                        "related": [],
                    }
                )
    # S2 / S3
    s2 = s3 = True
    for cid, rul in eff.items():
        c = claims.get(cid)
        if c is None:
            continue
        chks = checks_for(cf, cid)
        if rul.finding == "ESTABLISHED" and not strong_support(c, chks):
            s2 = False
            gaps.append(
                {
                    "check": "S2",
                    "statement": f"Finding {rul.id} is marked Established but has no valid "
                    "Tier 1-2 source or clean trade row.",
                    "related": [cid],
                }
            )
        if rul.verdict in ("UPHELD", "PARTIALLY_UPHELD") and tier4_only(c, chks):
            s3 = False
            gaps.append(
                {
                    "check": "S3",
                    "statement": f"Claim {cid} is upheld on Tier 4 sources only; no corroboration was found.",
                    "related": [cid],
                }
            )
    # S4 / S6 from final ranges
    s4 = s6 = True
    widths: dict[str, float] = {}
    for fr in r.final_ranges:
        key = f"{fr.country}/{fr.sector}"
        widths[key] = round(fr.high - fr.low, 2)
        for p in fr.unsupported_params:
            s4 = False
            gaps.append(
                {
                    "check": "S4",
                    "statement": f"No upheld claim backs {p} for {key}; its interval is unsupported.",
                    "related": [],
                }
            )
        if fr.high - fr.low > max_width_pp:
            s6 = False
            drivers: list[str] = [*fr.contested_params] or [_widest_param(fr)]
            extra = (
                ""
                if fr.observed_anchor is not None
                else (f" No post-event trade evidence for {key} dated on or before {cf.as_of} narrows it.")
            )
            gaps.append(
                {
                    "check": "S6",
                    "statement": f"Final range for {key} is {fr.high - fr.low:.1f} pp wide "
                    f"(limit {max_width_pp:g}); the evidence on "
                    f"{', '.join(drivers)} does not narrow it.{extra}",
                    "related": fr.supporting_rulings,
                }
            )
    if not r.final_ranges and rows:
        s4 = s6 = False
        gaps.append(
            {
                "check": "S4",
                "statement": "No final range could be computed for the scope rows.",
                "related": [],
            }
        )
    # S7
    failed = [s for s, st in r.side_status.items() if st != SideStatus.OK]
    s7 = not failed
    for side in failed:
        other = "loki" if side == "thor" else "thor"
        gaps.append(
            {
                "check": "S7",
                "statement": f"{side.capitalize()} failed this run "
                f"({r.side_status[side].value}); {other.capitalize()}'s claims "
                "went unchallenged.",
                "related": [],
            }
        )
    # S8: data-quality issues touching scope are recorded on the Case File
    scope_tables = {t for row in rows for t in row.trade_tables}
    dq_refs = {str(x.get("ref")) for x in cf.data_quality}
    s8 = True
    for x in r.citation_checks.values():
        for chk in x:
            if "DAMAGED_ROW" in chk.issues and chk.evidence_ref not in dq_refs:
                s8 = False
    facts.update(
        {
            "widths_pp": widths,
            "max_width_pp": max_width_pp,
            "failed_sides": failed,
            "scope_tables": sorted(scope_tables),
        }
    )
    checklist = {"S1": s1, "S2": s2, "S3": s3, "S4": s4, "S5": True, "S6": s6, "S7": s7, "S8": s8, "S9": True}
    return checklist, gaps, facts


def _widest_param(fr: FinalRange) -> str:
    best, w = "duration_months", -1.0
    for p, (lo, hi) in fr.params.items():
        span = (hi - lo) / (3.0 if p == "duration_months" else 1.0)
        if span > w:
            best, w = p, span
    return best


def make_deficiencies(run: int, items: list[dict[str, Any]]) -> list[Deficiency]:
    out: list[Deficiency] = []
    seen: set[str] = set()
    for it in items:
        st = str(it["statement"]).strip()
        if not st or st in seen:
            continue
        seen.add(st)
        out.append(
            Deficiency(
                id=f"R{run}-DEF{len(out) + 1:02d}",
                check=str(it.get("check", "S?")),
                statement=st,
                related=list(it.get("related", [])),
            )
        )
    return out


def pre_judging_facts(cf: CaseFile, run: int) -> dict[str, Any]:
    """Facts known before Odin rules (shown in his turn instruction)."""
    r = cf.run(run)
    rows = cf.scope.rows if cf.scope else []
    s1 = all(
        any(p.side == sd and p.country == row.country and p.sector == row.sector for p in r.positions)
        for row in rows
        for sd in ("thor", "loki")
    )
    return {
        "S1_positions_from_both_sides": s1,
        "S7_side_status": {k: v.value for k, v in r.side_status.items()},
        "S8_data_quality_recorded": len(cf.data_quality),
        "claims_without_valid_support": [c.id for c in r.claims if "NO_VALID_SUPPORT" in c.flags],
        "unrebutted_attacks": r.unrebutted,
        "max_confident_width_pp": None,
    }


def ruling_for(eff: dict[str, Ruling], claim_id: str) -> Ruling | None:
    return eff.get(claim_id)

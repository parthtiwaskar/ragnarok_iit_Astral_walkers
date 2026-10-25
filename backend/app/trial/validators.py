"""Submission validators: they check structure, hydrate evidence, and write accepted submissions to the Case File.

They never judge content (that is Odin's job). On the first failure they return errors for one re-prompt; on
the last attempt they keep what is valid and drop the rest, logging what was dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.tools.asof import as_of_status, doc_visible
from app.tools.catalog import record_calc
from app.tools.citation import EvidenceIn, HydrationError, evidence_ref, hydrate, verify_citation
from app.tools.context import CaseContext
from app.tools.event_tools import event_profile
from app.tools.exposure import compute, param_bounds
from app.tools.submit_tools import (
    ScopeRowIn,
    SubmitAttacksArgs,
    SubmitCaseFileArgs,
    SubmitClaimsArgs,
    SubmitResearchArgs,
    SubmitResponsesArgs,
)
from app.tools.trade_tools import row_stats, tables_for
from app.trial.casefile import (
    EVIDENCE_REQUIRED_ATTACKS,
    PARAMETERS,
    Attack,
    CitationCheck,
    Claim,
    Evidence,
    Parameter,
    PoolItem,
    Position,
    ResearchReport,
    Response,
    Scope,
    ScopeRow,
    Side,
    Verification,
)

HEDGE_ONLY = re.compile(r"^\s*(maybe|perhaps|possibly|it might|it may|could be)\b.*\bfine\b", re.I)


@dataclass
class Applied:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    done: bool = True
    memory_influence: list[dict[str, Any]] = field(default_factory=list)
    action_ref: str | None = None


def _settings(ctx: CaseContext) -> Any:
    return ctx.services.settings


def _manifest_present(ctx: CaseContext) -> bool:
    return ctx.services.repo.get_meta("manifest_present", "1") == "1"


def check_evidence(ctx: CaseContext, ev: Evidence) -> CitationCheck:
    chk = verify_citation(
        ev, ctx.services.repo, ctx.clock, ctx.event_id, _settings(ctx).QUOTE_MATCH_MIN, _manifest_present(ctx)
    )
    return chk


def hydrate_all(
    ctx: CaseContext, items: list[EvidenceIn], label: str, strict: bool
) -> tuple[list[Evidence], list[CitationCheck], list[str]]:
    """Hydrate + verify. `strict`: fixable citation defects (quote mismatch, not in manifest) are errors."""
    out: list[Evidence] = []
    checks: list[CitationCheck] = []
    errors: list[str] = []
    for i, item in enumerate(items, start=1):
        try:
            ev = hydrate(item, ctx.services.repo, ctx.clock)
        except HydrationError as exc:
            errors.append(f"{label} evidence {i}: {exc}")
            continue
        chk = check_evidence(ctx, ev)
        if strict and ("QUOTE_MISMATCH" in chk.issues or "NOT_IN_MANIFEST" in chk.issues):
            why = (
                "quote not found in " + chk.evidence_ref + f" (similarity {chk.quote_similarity})"
                if "QUOTE_MISMATCH" in chk.issues
                else f"{chk.evidence_ref} is not in the manifest"
            )
            errors.append(f"{label} evidence {i}: {why}; copy the quote exactly from read_document")
            continue
        out.append(ev)
        checks.append(chk)
    return out, checks, errors


def _emit_checks(ctx: CaseContext, owner: str, checks: list[CitationCheck], agent: str) -> None:
    for c in checks:
        ctx.emit("citation_check", "tool", {**c.model_dump(), "owner": owner, "agent": agent})


def _mi(args: Any) -> list[dict[str, Any]]:
    return [m.model_dump() for m in getattr(args, "memory_influence", [])]


# ======================================================================== Heimdall: GATHERING
def _horizon(as_of: date, months: int) -> tuple[date, date]:
    start = as_of + timedelta(days=1)
    y, m = start.year, start.month + months
    while m > 12:
        y, m = y + 1, m - 12
    try:
        end = date(y, m, start.day) - timedelta(days=1)
    except ValueError:
        end = date(y, m, 28)
    return start, end


def _pool_item(ctx: CaseContext, ref: str, run: int) -> PoolItem | None:
    repo = ctx.services.repo
    d = repo.get_doc(ref)
    if d is not None:
        if not doc_visible(d, ctx.clock):
            return None
        st = as_of_status(repo, d.doc_id, ctx.clock)
        status: Any = (
            d.status
            if d.status in ("OK", "REPAIRED", "PARTIAL", "DUPLICATE", "QUARANTINED")
            else "QUARANTINED"
        )
        if st["retracted"]:
            status = "RETRACTED_AS_OF"
        elif st["corrected_by"]:
            status = "CORRECTED_AS_OF"
        flags = list(d.flags)
        if d.is_background:
            flags.append("BACKGROUND")
        if ctx.event_id and not d.is_background and (d.event_id or "").lower() != ctx.event_id.lower():
            flags.append("OTHER_EVENT")
        return PoolItem(
            ref=d.doc_id,
            kind="doc",
            added_in_run=run,
            tier=d.tier,
            pub_date=date.fromisoformat(d.pub_date) if d.pub_date else None,
            event_id=d.event_id,
            status=status,
            flags=sorted(set(flags)),
            title=d.title,
        )
    t = repo.get_trade_table(ref)
    if t is not None:
        return PoolItem(
            ref=t.table_id,
            kind="trade_table",
            added_in_run=run,
            tier=None,
            pub_date=None,
            event_id=t.event_id,
            status="OK" if t.status == "OK" else "PARTIAL",
            flags=t.flags,
            title=f"{t.exporter} -> {t.importer} {t.sector}",
        )
    return None


def _norm_name(name: str, known: list[str]) -> str:
    for k in known:
        if k.lower() == name.strip().lower():
            return k
    return name.strip()


def apply_case_file(ctx: CaseContext, a: SubmitCaseFileArgs, last: bool) -> Applied:
    s = _settings(ctx)
    repo = ctx.services.repo
    errors: list[str] = []
    warnings: list[str] = []
    profile: dict[str, Any] = (
        event_profile(repo, a.event_id, ctx.clock, s.VERIFY_MIN_TIER1, s.VERIFY_MIN_TIER2_OUTLETS)
        if a.event_id
        else {"rule_verdict": "NOT_FOUND", "reasons": ["no event id given"], "visible_docs": 0}
    )
    rule = str(profile["rule_verdict"])
    verdict = a.verdict
    if not a.event_id and verdict in ("NOT_FOUND", "NOT_YET_KNOWN"):
        rule = verdict  # no visible event to profile: list_events decides between the two
    if verdict != rule and not (a.disagreement or "").strip():
        errors.append(
            f"your verdict {verdict} differs from the rule verdict {rule}; explain why in 'disagreement' "
            "or use the rule verdict"
        )
    if verdict == "VERIFIED":
        if not a.event_id:
            errors.append("a VERIFIED case needs event_id")
        elif not profile.get("visible_docs"):
            errors.append(f"event {a.event_id} has no documents visible as of {ctx.clock.iso}")
    if errors and not last:
        return Applied(False, errors)
    if errors and last:
        if verdict == "VERIFIED" and (not a.event_id or not profile.get("visible_docs")):
            return Applied(False, errors)
        warnings.extend(errors)
    countries = [str(r.get("name", "")) for r in repo.reference("countries")]
    sectors = [str(r.get("name", "")) for r in repo.reference("sectors")]
    rows: list[ScopeRow] = []
    event_date: date | None = a.event_date
    if event_date is None and profile.get("first_date"):
        event_date = date.fromisoformat(str(profile["first_date"]))
    if event_date and event_date > ctx.clock.as_of:
        warnings.append("event_date after as-of ignored")
        event_date = None
    if verdict == "VERIFIED" and a.event_id:
        given = a.rows
        if not given:
            given = _scope_from_tables(ctx, a.event_id, event_date)
            warnings.append(f"no scope rows given: {len(given)} rows taken from the event's trade tables")
        for r in given[: s.MAX_SCOPE_ROWS]:
            country = _norm_name(r.country, countries)
            sector = _norm_name(r.sector, sectors).lower()
            if countries and country not in countries:
                warnings.append(f"UNKNOWN_COUNTRY: {r.country}")
            tables = [t for t in r.trade_tables if repo.get_trade_table(t) is not None]
            dropped = [t for t in r.trade_tables if t not in tables]
            if dropped:
                (errors if not last else warnings).append(
                    f"unknown trade tables for {country}/{sector}: {dropped}"
                )
            if not tables:
                tables = [
                    t.table_id
                    for t in tables_for(repo, None, country, sector)
                    if (t.event_id or "").lower() == a.event_id.lower()
                ]
            stats = row_stats(repo, tables, event_date, ctx.clock, s.BASELINE_MONTHS)
            unit = next((t.unit for t in (repo.get_trade_table(x) for x in tables) if t and t.unit), "") or ""
            rows.append(
                ScopeRow(
                    country=country,
                    sector=sector,
                    trade_tables=tables,
                    baseline_value=stats["baseline_value"],
                    unit=unit,
                    route_flags={k: max(0.0, min(1.0, v)) for k, v in r.route_flags.items()},
                )
            )
        if len(a.rows) > s.MAX_SCOPE_ROWS:
            warnings.append(f"scope truncated to {s.MAX_SCOPE_ROWS} rows")
    # pool
    pool: list[PoolItem] = []
    unknown: list[str] = []
    doc_refs = [r for r in dict.fromkeys(a.pool) if repo.get_trade_table(r) is None]
    if len(doc_refs) > s.POOL_SIZE_R1:
        (errors if not last else warnings).append(
            f"pool has {len(doc_refs)} documents; the limit is {s.POOL_SIZE_R1}"
        )
        doc_refs = doc_refs[: s.POOL_SIZE_R1]
    for ref in [*doc_refs, *[r for r in a.pool if repo.get_trade_table(r) is not None]]:
        item = _pool_item(ctx, ref, 1)
        if item is None:
            unknown.append(ref)
        elif all(p.ref != item.ref for p in pool):
            pool.append(item)
    for row in rows:
        for t in row.trade_tables:
            if all(p.ref != t for p in pool):
                it = _pool_item(ctx, t, 1)
                if it:
                    pool.append(it)
    if unknown:
        (errors if not last else warnings).append(
            f"pool refs not found or not visible as of the date: {unknown}"
        )
    if errors and not last:
        return Applied(False, errors)
    # ---- write
    retractions = sorted({*a.retractions_applied, *[x["doc_id"] for x in profile.get("retractions", [])]})
    ctx.cf.verification = Verification(
        verdict=verdict,
        rule_verdict=rule,
        reasons=a.reasons or list(profile.get("reasons", [])),
        supporting_docs=a.supporting_docs,
        contradicting_docs=a.contradicting_docs,
        retractions_applied=retractions,
        alternatives=a.alternatives,
        disagreement=(a.disagreement or None) if verdict != rule else None,
    )
    if verdict != rule:
        ctx.emit(
            "verification_disagreement",
            "heimdall",
            {"rule_verdict": rule, "verdict": verdict, "reason": a.disagreement},
        )
    if verdict == "VERIFIED" and a.event_id:
        h0, h1 = _horizon(ctx.clock.as_of, s.HORIZON_MONTHS)
        ctx.cf.scope = Scope(
            event_id=a.event_id,
            event_summary=a.event_summary,
            event_date=event_date,
            chokepoints=a.chokepoints,
            rows=rows,
            horizon_start=h0,
            horizon_end=h1,
        )
        ctx.cf.pool = pool
        _record_quality_and_traps(ctx, profile)
    ctx.emit(
        "verification",
        "heimdall",
        {
            **ctx.cf.verification.model_dump(),
            "profile": {
                k: profile.get(k)
                for k in ("per_tier", "outlets_by_tier", "denials", "retractions", "corrections")
            },
        },
    )
    if ctx.cf.scope:
        ctx.emit(
            "case_file_submitted",
            "heimdall",
            {"scope": ctx.cf.scope.model_dump(), "pool": [p.model_dump() for p in ctx.cf.pool]},
        )
    for w in warnings:
        ctx.emit("validation_warning", "thing", {"agent": "heimdall", "warning": w})
    return Applied(
        True,
        warnings=warnings,
        memory_influence=_mi(a),
        action_ref="case_file",
        result={
            "accepted": True,
            "verdict": verdict,
            "rule_verdict": rule,
            "scope_rows": len(rows),
            "pool": len(ctx.cf.pool),
            "warnings": warnings,
        },
    )


def _scope_from_tables(ctx: CaseContext, event_id: str, event_date: date | None) -> list[ScopeRowIn]:
    repo = ctx.services.repo
    groups: dict[tuple[str, str], list[str]] = {}
    for t in tables_for(repo, event_id):
        if t.importer and t.sector:
            groups.setdefault((t.importer, t.sector), []).append(t.table_id)
    scored = []
    for (c, sct), tabs in groups.items():
        st = row_stats(repo, tabs, event_date, ctx.clock)
        scored.append((st["baseline_value"] or 0.0, c, sct, tabs))
    scored.sort(key=lambda x: (-x[0], x[1], x[2]))
    return [ScopeRowIn(country=c, sector=sct, trade_tables=tabs) for _, c, sct, tabs in scored]


def _record_quality_and_traps(ctx: CaseContext, profile: dict[str, Any]) -> None:
    repo = ctx.services.repo
    cf = ctx.cf
    assert cf.scope is not None
    seen_dq = {str(x.get("ref")) for x in cf.data_quality}
    for row in cf.scope.rows:
        for tid in row.trade_tables:
            t = repo.get_trade_table(tid)
            if t is None:
                continue
            for flag in t.flags:
                ref = f"{tid}:{flag}"
                if ref not in seen_dq:
                    cf.data_quality.append(
                        {"ref": ref, "issue": f"table {tid} {flag}", "kind": "trade_table"}
                    )
                    seen_dq.add(ref)
            for r in repo.trade_rows(tid):
                if r.quality != "OK" and r.ref not in seen_dq:
                    cf.data_quality.append(
                        {
                            "ref": r.ref,
                            "issue": f"{r.quality} excluded",
                            "raw": r.raw_line,
                            "kind": "trade_row",
                        }
                    )
                    seen_dq.add(r.ref)
    for d in repo.docs(
        "lower(event_id)=lower(?) AND status IN ('QUARANTINED','PARTIAL','DUPLICATE','REPAIRED')",
        (cf.scope.event_id,),
    ):
        if d.pub_date and d.pub_date > ctx.clock.iso:
            continue
        if d.doc_id not in seen_dq:
            cf.data_quality.append(
                {"ref": d.doc_id, "issue": f"{d.status}: {d.status_reason or ''}".strip(), "kind": "document"}
            )
            seen_dq.add(d.doc_id)
    known = {(t.get("type"), t.get("doc_id")) for t in cf.traps}

    def trap(kind: str, doc_id: str, **extra: Any) -> None:
        if (kind, doc_id) in known:
            return
        known.add((kind, doc_id))
        item = {"type": kind, "doc_id": doc_id, **extra}
        cf.traps.append(item)
        ctx.emit("trap_detected", "heimdall", item)

    for r in profile.get("retractions", []):
        trap("RETRACTION", r["doc_id"], by=r["retracted_by"])
    for c in profile.get("corrections", []):
        trap("CORRECTION", c["doc_id"], by=c["corrected_by"])
    for d in repo.docs("lower(event_id)=lower(?)", (cf.scope.event_id,)):
        if d.pub_date and d.pub_date <= ctx.clock.iso and "INJECTION_DETECTED" in d.flags:
            spans = [i["span"] for i in repo.injections(d.doc_id)]
            trap("INJECTION", d.doc_id, span=spans[0] if spans else "")
    # look-alikes in the pool (other event or background)
    for p in cf.pool:
        if p.kind == "doc" and ("OTHER_EVENT" in p.flags or "BACKGROUND" in p.flags):
            trap("WRONG_EVENT" if "OTHER_EVENT" in p.flags else "BACKGROUND", p.ref, event=p.event_id)


# ======================================================================== Heimdall: RESEARCH
def apply_research(
    ctx: CaseContext,
    a: SubmitResearchArgs,
    last: bool,
    next_run: int,
    deficiency_ids: list[str],
    queries: list[str],
) -> tuple[Applied, ResearchReport | None]:
    errors: list[str] = []
    warnings: list[str] = []
    existing = ctx.cf.pool_ids()
    added: list[PoolItem] = []
    for ref in dict.fromkeys(a.new_items):
        if ref in existing:
            warnings.append(f"{ref} is already in the pool (not new)")
            continue
        item = _pool_item(ctx, ref, next_run)
        if item is None:
            errors.append(f"{ref} not found or not visible as of {ctx.clock.iso}")
            continue
        added.append(item)
    unknown_defs = [k for k in a.per_deficiency if k not in deficiency_ids]
    if unknown_defs:
        warnings.append(f"unknown deficiency ids: {unknown_defs}")
    missing_defs = [d for d in deficiency_ids if d not in a.per_deficiency]
    if missing_defs:
        (errors if not last else warnings).append(f"report every deficiency; missing: {missing_defs}")
    if errors and not last:
        return Applied(False, errors), None
    ctx.cf.pool.extend(added)
    per = {k: v for k, v in a.per_deficiency.items() if k in deficiency_ids}
    for d in missing_defs:
        per[d] = "NO_EVIDENCE_FOUND"
    report = ResearchReport(
        run=next_run,
        queries=queries,
        per_deficiency=per,
        new_items=[p.ref for p in added],
        memory_influence=_mi(a),
        notes=a.notes,
    )
    if errors:
        warnings.extend(errors)
    return Applied(
        True,
        warnings=warnings,
        memory_influence=_mi(a),
        action_ref="research",
        result={"accepted": True, "new_items": report.new_items, "warnings": warnings},
    ), report


# ======================================================================== Thor / Loki: R1_SUBMIT
def _scope_row(ctx: CaseContext, country: str | None, sector: str | None) -> ScopeRow | None:
    if not ctx.cf.scope or country is None or sector is None:
        return None
    for r in ctx.cf.scope.rows:
        if r.country.lower() == country.strip().lower() and r.sector.lower() == sector.strip().lower():
            return r
    return None


def apply_claims(ctx: CaseContext, side: Side, run: int, a: SubmitClaimsArgs, last: bool) -> Applied:
    s = _settings(ctx)
    cf = ctx.cf
    r = cf.run(run)
    errors: list[str] = []
    warnings: list[str] = []
    H = float(s.HORIZON_MONTHS)
    if run not in ctx.sides_searched.get(side, set()):
        errors.append("you must call search_documents at least once during this turn before submitting")
    if len(a.claims) > s.MAX_CLAIMS_PER_SIDE:
        (errors if not last else warnings).append(f"at most {s.MAX_CLAIMS_PER_SIDE} claims per run")
    own_prior = {c.id: c for c in cf.active_claims(side) if c.run < run}
    local: dict[str, str] = {}
    new_claims: list[Claim] = []
    checks_by: dict[str, list[CitationCheck]] = {}
    letter = "T" if side == "thor" else "L"
    for i, c in enumerate(a.claims[: s.MAX_CLAIMS_PER_SIDE], start=1):
        cid = f"R{run}-{letter}-C{len(new_claims) + 1:02d}"
        label = c.id or f"C{i:02d}"
        errs: list[str] = []
        if not c.statement.strip():
            errs.append(f"{label}: empty statement")
        elif HEDGE_ONLY.search(c.statement) and not c.evidence:
            errs.append(f"{label}: 'maybe it will be fine' is not a claim")
        evs, checks, ev_errs = hydrate_all(ctx, c.evidence, label, strict=not last)
        errs.extend(ev_errs)
        if not evs:
            errs.append(
                f"{label}: needs at least one valid evidence item (doc_id + exact quote, or table_id + row)"
            )
        if (c.parameter is None) != (c.bound_value is None):
            errs.append(f"{label}: give both parameter and bound_value, or neither")
        if c.parameter is not None and c.bound_value is not None:
            lo, hi = param_bounds(c.parameter, H)
            if not lo <= c.bound_value <= hi:
                errs.append(f"{label}: bound_value {c.bound_value} outside {c.parameter} range [{lo}, {hi}]")
        row = _scope_row(ctx, c.country, c.sector)
        if (c.country or c.sector) and row is None:
            errs.append(f"{label}: country/sector {c.country}/{c.sector} is not a scope row")
        if c.supersedes and c.supersedes not in own_prior:
            errs.append(f"{label}: supersedes {c.supersedes} is not one of your active earlier-run claims")
        if errs:
            if not last:
                errors.extend(errs)
                continue
            if not evs:
                warnings.extend([*errs, f"{label}: dropped"])
                continue
            warnings.extend(errs)
        local[label] = cid
        local[cid] = cid
        if c.id:
            local[c.id] = cid
        local[f"C{i:02d}"] = local.get(f"C{i:02d}", cid)
        claim = Claim(
            id=cid,
            run=run,
            side=side,
            statement=c.statement.strip(),
            type=c.type,
            premises=c.premises,
            country=row.country if row else None,
            sector=row.sector if row else None,
            parameter=c.parameter,
            bound_value=c.bound_value,
            evidence=evs,
            supersedes=c.supersedes if c.supersedes in own_prior else None,
            memory_influence=_mi(a),
        )
        if not any(ch.valid for ch in checks):
            claim.flags.append("NO_VALID_SUPPORT")
        new_claims.append(claim)
        checks_by[cid] = checks
    # positions
    positions: list[Position] = []
    rows = cf.scope.rows if cf.scope else []
    given = {(p.country.strip().lower(), p.sector.strip().lower()): p for p in a.positions}
    for row in rows:
        p = given.get((row.country.lower(), row.sector.lower()))
        if p is None:
            (errors if not last else warnings).append(f"missing position for {row.country}/{row.sector}")
            continue
        params: dict[Parameter, tuple[float, float]] = {}
        pclaims: dict[Parameter, list[str]] = {}
        for prm, iv in p.params.items():
            if len(iv) != 2:
                (errors if not last else warnings).append(
                    f"{row.country}/{row.sector} {prm}: give [low, high]"
                )
                continue
            params[prm] = (float(min(iv)), float(max(iv)))
        for prm, ids in p.param_claims.items():
            resolved = []
            for x in ids:
                if x in local:
                    resolved.append(local[x])
                elif x in own_prior and x not in a.concede:
                    resolved.append(x)
                else:
                    (errors if not last else warnings).append(
                        f"{row.country}/{row.sector} {prm}: unknown or conceded claim id {x}"
                    )
            pclaims[prm] = resolved
        res = compute(
            {k: list(v) for k, v in params.items()},
            horizon=float(s.HORIZON_MONTHS),
            min_width=s.MIN_PARAM_WIDTH,
        )
        record_calc(ctx, side, res, {k: v for k, v in pclaims.items()}, row.country, row.sector)
        norm = {k: res.params[k] for k in params}
        positions.append(
            Position(
                side=side,
                country=row.country,
                sector=row.sector,
                params=norm,
                param_claims=pclaims,
                low=res.low,
                high=res.high,
                headline=res.high if side == "thor" else res.low,
                calc_id=res.calc_id,
                assumed_params=[q for q in PARAMETERS if q not in params or not pclaims.get(q)],
            )
        )
    bad_concede = [x for x in a.concede if x not in own_prior]
    if bad_concede:
        warnings.append(f"cannot concede unknown/inactive claims: {bad_concede}")
    if errors and not last:
        return Applied(False, errors)
    if not new_claims and not positions and not (own_prior and a.carry_forward):
        return Applied(False, errors + warnings + ["no valid claims or positions"])
    # ---- write (sealed)
    for nc in new_claims:
        if nc.supersedes and nc.supersedes in own_prior:
            own_prior[nc.supersedes].status = "SUPERSEDED"
    for x in a.concede:
        if x in own_prior:
            own_prior[x].status = "CONCEDED"
            r.conceded_prior.append(x)
    r.claims.extend(new_claims)
    r.positions = [p for p in r.positions if p.side != side] + positions
    r.citation_checks.update(checks_by)
    r.sealed[side] = True
    r.sealed_at[side] = datetime.now(UTC)
    for cid, checks in checks_by.items():
        _emit_checks(ctx, cid, checks, side)
    ctx.emit(
        "sealed_commit",
        side,
        {
            "side": side,
            "n_claims": len(new_claims),
            "n_positions": len(positions),
            "committed_at": r.sealed_at[side],
        },
    )
    for w in warnings:
        ctx.emit("validation_warning", "thing", {"agent": side, "warning": w})
    return Applied(
        True,
        warnings=warnings,
        memory_influence=_mi(a),
        action_ref=f"R{run}-{letter}-claims",
        result={
            "accepted": True,
            "claim_ids": [c.id for c in new_claims],
            "positions": [
                {"country": p.country, "sector": p.sector, "low": p.low, "high": p.high, "calc_id": p.calc_id}
                for p in positions
            ],
            "warnings": warnings,
        },
    )


# ======================================================================== Round 2A
def apply_attacks(ctx: CaseContext, side: Side, run: int, a: SubmitAttacksArgs, last: bool) -> Applied:
    s = _settings(ctx)
    cf = ctx.cf
    r = cf.run(run)
    opponent: Side = "loki" if side == "thor" else "thor"
    targets = {c.id: c for c in cf.active_claims(opponent)}
    errors: list[str] = []
    warnings: list[str] = []
    attacks: list[Attack] = []
    checks_by: dict[str, list[CitationCheck]] = {}
    letter = "T" if side == "thor" else "L"
    if len(a.attacks) > s.MAX_ATTACKS_PER_SIDE:
        (errors if not last else warnings).append(f"at most {s.MAX_ATTACKS_PER_SIDE} attacks")
    for i, at in enumerate(a.attacks[: s.MAX_ATTACKS_PER_SIDE], start=1):
        errs: list[str] = []
        if at.target_claim not in targets:
            errs.append(f"attack {i}: {at.target_claim} is not an active claim of {opponent}")
        if not at.argument.strip():
            errs.append(f"attack {i}: empty argument")
        evs, checks, ev_errs = hydrate_all(ctx, at.evidence, f"attack {i}", strict=not last)
        errs.extend(ev_errs)
        if at.type in EVIDENCE_REQUIRED_ATTACKS and not evs:
            errs.append(f"attack {i}: {at.type} attacks must cite evidence")
        if errs:
            if (
                not last
                or at.target_claim not in targets
                or (at.type in EVIDENCE_REQUIRED_ATTACKS and not evs)
            ):
                (errors if not last else warnings).extend(errs)
                continue
            warnings.extend(errs)
        aid = f"R{run}-{letter}-A{len(attacks) + 1:02d}"
        attacks.append(
            Attack(
                id=aid,
                run=run,
                by=side,
                target_claim=at.target_claim,
                type=at.type,
                argument=at.argument.strip(),
                evidence=evs,
            )
        )
        checks_by[aid] = checks
    if len(attacks) < s.MIN_ATTACKS_PER_SIDE and not errors:
        errors.append(f"file at least {s.MIN_ATTACKS_PER_SIDE} attack(s)")
    if errors and not last:
        return Applied(False, errors)
    if len(attacks) < s.MIN_ATTACKS_PER_SIDE:
        return Applied(False, errors + warnings)
    r.attacks.extend(attacks)
    r.citation_checks.update(checks_by)
    for at2 in attacks:
        ctx.emit("attack_filed", side, at2.model_dump())
        _emit_checks(ctx, at2.id, checks_by[at2.id], side)
    return Applied(
        True,
        warnings=warnings,
        memory_influence=_mi(a),
        action_ref=",".join(x.id for x in attacks),
        result={"accepted": True, "attack_ids": [x.id for x in attacks], "warnings": warnings},
    )


# ======================================================================== Round 2B
def apply_responses(ctx: CaseContext, side: Side, run: int, a: SubmitResponsesArgs, last: bool) -> Applied:
    s = _settings(ctx)
    cf = ctx.cf
    r = cf.run(run)
    H = float(s.HORIZON_MONTHS)
    mine = {c.id for c in cf.all_claims() if c.side == side}
    on_me = {at.id: at for at in r.attacks if at.target_claim in mine}
    errors: list[str] = []
    warnings: list[str] = []
    out: list[Response] = []
    checks_by: dict[str, list[CitationCheck]] = {}
    answered: set[str] = set()
    letter = "T" if side == "thor" else "L"
    for i, x in enumerate(a.responses, start=1):
        errs: list[str] = []
        at = on_me.get(x.attack_id)
        if at is None:
            errs.append(f"response {i}: {x.attack_id} is not an attack on your claims")
        elif x.attack_id in answered:
            errs.append(f"response {i}: {x.attack_id} answered twice")
        evs, checks, ev_errs = hydrate_all(ctx, x.new_evidence, f"response {i}", strict=not last)
        errs.extend(ev_errs)
        claim = cf.claim(at.target_claim) if at else None
        if x.action == "DEFEND" and claim is not None:
            cited = {evidence_ref(e) for e in claim.evidence}
            fresh = [e for e in evs if evidence_ref(e) not in cited]
            if not fresh:
                errs.append(
                    f"response {i}: DEFEND must include at least one evidence item not already cited by "
                    f"{claim.id}"
                )
        if x.action == "REVISE":
            if x.revised_bound is None and not evs:
                errs.append(f"response {i}: REVISE needs a revised_bound or new evidence")
            if x.revised_bound is not None and claim is not None and claim.parameter:
                lo, hi = param_bounds(claim.parameter, H)
                if not lo <= x.revised_bound <= hi:
                    errs.append(f"response {i}: revised_bound outside [{lo}, {hi}]")
        if errs:
            if not last or at is None or x.attack_id in answered:
                (errors if not last else warnings).extend(errs)
                continue
            if x.action == "DEFEND":
                warnings.extend([*errs, f"response {i}: DEFEND without new evidence recorded as unrebutted"])
                continue
            warnings.extend(errs)
        assert at is not None
        rid = f"R{run}-{letter}-D{len(out) + 1:02d}"
        resp = Response(
            id=rid,
            run=run,
            by=side,
            attack_id=x.attack_id,
            action=x.action,
            argument=x.argument.strip(),
            new_evidence=evs,
            revised_bound=x.revised_bound,
        )
        out.append(resp)
        checks_by[rid] = checks
        answered.add(x.attack_id)
    unanswered = [aid for aid in on_me if aid not in answered]
    if unanswered and not last:
        errors.append(f"answer every attack; unanswered: {unanswered}")
    if errors and not last:
        return Applied(False, errors)
    for resp in out:
        if resp.action == "CONCEDE":
            at = on_me[resp.attack_id]
            c = cf.claim(at.target_claim)
            if c is not None:
                c.status = "CONCEDED"
    r.responses.extend(out)
    r.citation_checks.update(checks_by)
    r.unrebutted.extend(aid for aid in unanswered if aid not in r.unrebutted)
    for resp in out:
        ctx.emit("response_filed", side, resp.model_dump())
        _emit_checks(ctx, resp.id, checks_by[resp.id], side)
    for aid in unanswered:
        ctx.emit("attack_unrebutted", "thing", {"attack_id": aid, "side": side})
    return Applied(
        True,
        warnings=warnings,
        memory_influence=_mi(a),
        action_ref=",".join(x.id for x in out),
        result={
            "accepted": True,
            "response_ids": [x.id for x in out],
            "unrebutted": unanswered,
            "warnings": warnings,
        },
    )

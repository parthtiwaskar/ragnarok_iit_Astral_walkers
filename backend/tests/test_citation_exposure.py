from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.tools.asof import Clock
from app.tools.citation import (
    CITE_RX,
    EvidenceIn,
    HydrationError,
    cite_ref,
    hydrate,
    normalize_cite,
    paragraph_of,
    verify_citation,
)
from app.tools.context import Services
from app.tools.exposure import compute, exposure_pct, final_params, normalize_interval
from app.trial.casefile import Claim, DocEvidence, Position, Ruling


def _check(services: Services, clock: Clock, doc_id: str, quote: str, event: str | None = "EVT-A"):  # type: ignore[no-untyped-def]
    d = services.repo.get_doc(doc_id)
    assert d is not None and d.pub_date
    ev = DocEvidence(
        doc_id=doc_id,
        quote=quote,
        pub_date=date.fromisoformat(d.pub_date),
        tier=d.tier or 4,
        event_id=d.event_id or "",
    )
    return verify_citation(ev, services.repo, clock, event)


def test_hydration_fills_metadata_and_rejects_unknown(services: Services, clock: Clock) -> None:
    ev = hydrate(
        EvidenceIn(doc_id="RM-DOC-0101", quote="closure is expected to last at least eight weeks"),
        services.repo,
        clock,
    )
    assert isinstance(ev, DocEvidence) and ev.tier == 1 and ev.event_id == "EVT-A"
    with pytest.raises(HydrationError):
        hydrate(EvidenceIn(doc_id="RM-DOC-9999", quote="x"), services.repo, clock)
    with pytest.raises(HydrationError):  # after as-of: not available
        hydrate(EvidenceIn(doc_id="RM-DOC-0116", quote="x"), services.repo, clock)
    with pytest.raises(HydrationError):  # April row not visible on 2025-04-10
        hydrate(EvidenceIn(table_id="TT-A01", row=10), services.repo, clock)


def test_citation_issues(services: Services, clock: Clock) -> None:
    ok = _check(services, clock, "RM-DOC-0101", "The closure is expected to last at least eight weeks.")
    assert ok.valid and not ok.issues and (ok.quote_similarity or 0) >= 90
    assert (
        "QUOTE_MISMATCH"
        in _check(services, clock, "RM-DOC-0101", "The canal will reopen tomorrow morning").issues
    )
    assert "RETRACTED_AS_OF" in _check(services, clock, "RM-DOC-0105", "collapsed by 40 percent").issues
    corr = _check(
        services, clock, "RM-DOC-0102", "Roughly 70 percent of Brenmark's electronics imports transit"
    )
    assert corr.valid and "CORRECTED_AS_OF" in corr.issues and corr.superseded_by == "RM-DOC-0110"
    assert "WRONG_EVENT" in _check(services, clock, "RM-DOC-0120", "closed the canal for five months").issues
    bg = _check(services, clock, "RM-DOC-0121", "About half of Brenmark's electronics imports")
    assert bg.valid and "BACKGROUND_ONLY" in bg.issues
    assert "NOT_IN_MANIFEST" in _check(services, clock, "RM-DOC-0199", "Unlisted copy").issues
    assert not _check(services, Clock(date(2025, 3, 8)), "RM-DOC-0105", "collapsed by 40 percent").issues


def test_damaged_trade_row_is_never_valid(services: Services) -> None:
    clock = Clock(date(2025, 4, 10))
    bad = hydrate(EvidenceIn(table_id="TT-A03", row=4), services.repo, clock)
    chk = verify_citation(bad, services.repo, clock, "EVT-A")
    assert not chk.valid and "DAMAGED_ROW" in chk.issues
    good = hydrate(EvidenceIn(table_id="TT-A01", row=9), services.repo, clock)
    assert verify_citation(good, services.repo, clock, "EVT-A").valid


def test_briefing_citation_helpers_normalize_rows_and_find_paragraphs() -> None:
    match = CITE_RX.search("[TT-A01 r9, 2025-03]")
    assert match is not None
    assert normalize_cite(match) == "[TT-A01 r09, 2025-03]"
    assert cite_ref("[TT-A01 r09, 2025-03]") == "TT-A01#r09"
    assert cite_ref("[RM-DOC-0101 P2, 2025-03-01]") == "RM-DOC-0101"
    assert (
        paragraph_of("[P1] Unrelated. [P2] The canal is closed to all traffic.", "closed to all traffic")
        == "P2"
    )


def test_formula_corners_and_worked_example() -> None:
    r = compute(
        {
            "route_share": [0.55, 0.60],
            "severity": [0.9, 1.0],
            "duration_months": [0.5, 1.5],
            "reroute_offset": [0.2, 0.2],
        }
    )
    assert r.params["reroute_offset"] == (0.175, 0.225)  # widened to MIN_PARAM_WIDTH
    # EXPOSURE_MODEL.md §7: E_low ~ 6.4 %, E_high ~ 24.8 %
    assert r.low == pytest.approx(6.4, abs=0.1) and r.high == pytest.approx(24.8, abs=0.1)
    assert (
        r.calc_id
        == compute(
            {
                "route_share": [0.55, 0.60],
                "severity": [0.9, 1.0],
                "duration_months": [0.5, 1.5],
                "reroute_offset": [0.2, 0.2],
            }
        ).calc_id
    )


def test_anchor_narrows_or_reports_conflict() -> None:
    p = {
        "route_share": [0.4, 0.8],
        "severity": [0.5, 1.0],
        "duration_months": [1, 2],
        "reroute_offset": [0.1, 0.3],
    }
    base = compute(p)
    anchored = compute(p, k_obs=0.4)
    assert anchored.anchor_applied and base.low <= anchored.low and anchored.high <= base.high
    conflict = compute({**p, "route_share": [0.1, 0.2]}, k_obs=0.99)
    base = compute({**p, "route_share": [0.1, 0.2]})
    assert conflict.anchor_conflict and not conflict.anchor_applied and conflict.low == base.low


unit = st.floats(min_value=0, max_value=1, allow_nan=False)


@settings(max_examples=200, deadline=None)
@given(
    rs=unit,
    sv=unit,
    du=st.floats(min_value=0, max_value=3),
    ro=unit,
    d=st.floats(min_value=0.01, max_value=0.3),
)
def test_monotonic_in_every_parameter(rs: float, sv: float, du: float, ro: float, d: float) -> None:
    e = exposure_pct(rs, sv, du, ro, 3)
    assert exposure_pct(min(1, rs + d), sv, du, ro, 3) >= e - 1e-9
    assert exposure_pct(rs, min(1, sv + d), du, ro, 3) >= e - 1e-9
    assert exposure_pct(rs, sv, min(3, du + d), ro, 3) >= e - 1e-9
    assert exposure_pct(rs, sv, du, min(1, ro + d), 3) <= e + 1e-9
    assert 0 <= e <= 100


@settings(max_examples=100, deadline=None)
@given(a=unit, b=unit)
def test_normalized_intervals_are_ordered_and_wide_enough(a: float, b: float) -> None:
    lo, hi = normalize_interval("severity", [a, b], 3, 0.05)
    assert 0 <= lo <= hi <= 1 and hi - lo >= 0.05 - 1e-9


def _pos(side: str, iv: tuple[float, float], cid: str) -> Position:
    return Position(
        side=side,
        country="A",
        sector="s",
        params={"route_share": iv},  # type: ignore[arg-type]
        param_claims={"route_share": [cid]},
        low=0,
        high=0,
        headline=0,
        calc_id="x",
    )


def _rul(cid: str, verdict: str) -> Ruling:
    return Ruling(
        id=f"RUL:{cid}:R1",
        claim_id=cid,
        run=1,
        verdict=verdict,
        basis="SOURCED_FACT",  # type: ignore[arg-type]
        finding="ESTABLISHED" if verdict == "UPHELD" else "REJECTED",
        rationale="",
        decisive_evidence=[],
    )


def _claim(cid: str, side: str, v: float) -> Claim:
    ev = DocEvidence(doc_id="D", quote="q", pub_date=date(2025, 1, 1), tier=1, event_id="E")
    return Claim(
        id=cid,
        run=1,
        side=side,
        statement="s",
        type="SOURCED_FACT",
        country="A",
        sector="s",  # type: ignore[arg-type]
        parameter="route_share",
        bound_value=v,
        evidence=[ev],
    )


def test_final_range_rules_table() -> None:
    t, lk = _pos("thor", (0.5, 0.7), "T1"), _pos("loki", (0.4, 0.6), "L1")
    claims = {"T1": _claim("T1", "thor", 0.7), "L1": _claim("L1", "loki", 0.4)}
    both = final_params(t, lk, claims, {"T1": _rul("T1", "UPHELD"), "L1": _rul("L1", "UPHELD")}, 3)
    assert both["route_share"].status == "INTERSECTION" and both["route_share"].interval == (0.5, 0.6)
    lk2 = _pos("loki", (0.1, 0.2), "L1")
    disjoint = final_params(t, lk2, claims, {"T1": _rul("T1", "UPHELD"), "L1": _rul("L1", "UPHELD")}, 3)
    assert disjoint["route_share"].status == "CONTESTED" and disjoint["route_share"].interval == (0.1, 0.7)
    thor_only = final_params(t, lk, claims, {"T1": _rul("T1", "UPHELD"), "L1": _rul("L1", "REJECTED")}, 3)
    assert thor_only["route_share"].status == "THOR_ONLY" and thor_only["route_share"].interval == (0.5, 0.7)
    neither = final_params(t, lk, claims, {"T1": _rul("T1", "REJECTED"), "L1": _rul("L1", "CONCEDED")}, 3)
    assert neither["route_share"].status == "UNSUPPORTED" and neither["route_share"].interval == (0.4, 0.7)
    assert neither["severity"].status == "MISSING"

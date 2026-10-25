"""Debate quality: audit helpers for attackers and a deterministic scorecard.

The Thing never judges content; it only measures the debate so Odin (and a reader) can see whether cross-examination
really tested the claims or collapsed into agreement.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .models import Attack, CaseFile, Claim, Evidence, Response

# Specific, checkable ways to attack a claim. "logic" / "other" are not accepted: they say nothing about what is wrong.
ATTACK_KINDS = ("contradiction", "retracted", "stale", "wrong_event", "weak_source", "scope", "arithmetic", "inference_gap")
INTEGRITY_KINDS = ("retracted", "stale", "wrong_event")  # facts about the source itself, which outrank source tier
_STOP = frozenset("a an and are as at be by for from has have in is it its of on or that the to was were with which this these their than".split())
_TOKEN = re.compile(r"[a-z]{3,}")
_NUM = re.compile(r"\d[\d,]*\.?\d*")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOP}


def _numbers(text: str) -> set[int]:
    out = set()
    for m in _NUM.findall(text):
        try:
            out.add(round(float(m.replace(",", ""))))
        except ValueError:
            pass
    return out


def best_tier(ev: list[Evidence]) -> int | None:
    tiers = [e.tier for e in ev if e.tier is not None]
    return min(tiers) if tiers else None


def audit_claim(c: Claim) -> dict[str, Any]:
    """Deterministic facts an attacker should check first. Hints, not verdicts."""
    quoted = set().union(*[_numbers(e.quote) for e in c.evidence]) if c.evidence else set()
    figures = None
    if c.estimate:
        figures = any(abs(round(abs(v)) - n) <= 1 for v in (c.estimate.low, c.estimate.high) for n in quoted)
    tiers = [e.tier for e in c.evidence if e.tier is not None]
    return {
        "best_tier": min(tiers) if tiers else None, "weakest_tier": max(tiers) if tiers else None,
        "distinct_sources": len(c.doc_ids()), "kind": c.kind,
        "estimate_figures_appear_in_quotes": figures,
        "hint": ("the estimate's figures appear in no cited quote: it is derived, so recompute it from the table before accepting"
                 if figures is False else ""),
    }


def is_relevant(counter: list[Evidence], target: Claim) -> bool:
    """Counter-evidence must bear on the claim: share >=2 content words or a number with it. Stops 'cite anything' attacks."""
    claim_words = _tokens(target.text) | ({target.estimate.country.lower(), target.estimate.sector.lower()} if target.estimate else set())
    claim_nums = _numbers(target.text) | {round(abs(v)) for v in ((target.estimate.low, target.estimate.high) if target.estimate else ())}
    for e in counter:
        if len(_tokens(e.quote) & claim_words) >= 2 or (_numbers(e.quote) & claim_nums):
            return True
    return False


def debate_stats(case: CaseFile, attack_ids: list[str], response_ids: list[str]) -> dict[str, Any]:
    attacks: list[Attack] = [case.attacks[i] for i in attack_ids if i in case.attacks]
    responses: dict[str, Response] = {i: case.responses[i] for i in response_ids if i in case.responses}
    attacked = {a.target_claim_id for a in attacks}
    by_action = Counter(r.action for r in responses.values())
    conceded = {a.target_claim_id for a in attacks if responses.get(a.id) and responses[a.id].action == "CONCEDE"}
    rate = len(conceded) / len(attacked) if attacked else 0.0
    tier12_facts_conceded = [
        c for c in conceded if case.claims[c].kind == "fact" and (best_tier(case.claims[c].evidence) or 9) <= 2]
    n = len(attacks)
    return {
        "attacks": n,
        "attack_kinds": dict(Counter(a.kind for a in attacks)),
        "specific_attack_pct": round(100 * sum(1 for a in attacks if a.kind in ATTACK_KINDS) / n) if n else 0,
        "evidence_backed_pct": round(100 * sum(1 for a in attacks if a.counter_evidence) / n) if n else 0,
        "claims_attacked": len(attacked),
        "defended": by_action.get("DEFEND", 0), "revised": by_action.get("REVISE", 0), "conceded": by_action.get("CONCEDE", 0),
        "unanswered": n - len(responses),
        "defence_without_evidence_downgraded": sum(1 for r in responses.values() if "no new evidence" in r.note),
        "concession_rate": round(rate, 2),
        "tier12_facts_conceded": sorted(tier12_facts_conceded),
        # a debate that ends with nearly everything conceded has agreed, not been tested
        "mass_concession": rate >= 0.7 and len(attacked) >= 3,
    }


def stats_from_dict(case: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """Scorecard for a saved case (e.g. one recorded before the scorecard existed), from its dict form."""
    from .models import Estimate

    c = CaseFile(case["case_id"], case["question"], case["as_of"])
    for cid, cl in case.get("claims", {}).items():
        est = Estimate(**cl["estimate"]) if cl.get("estimate") else None
        c.claims[cid] = Claim(cid, cl["side"], cl["run"], cl["text"], cl["kind"], [Evidence(**e) for e in cl["evidence"]], est,
                              cl.get("confidence", 0.5), cl.get("status", "live"))
    for aid, a in case.get("attacks", {}).items():
        c.attacks[aid] = Attack(aid, a["run"], a["by"], a["target_claim_id"], a["text"], a.get("kind", ""),
                                [Evidence(**e) for e in a.get("counter_evidence", [])])
    for aid, r in case.get("responses", {}).items():
        c.responses[aid] = Response(aid, r["by"], r["action"], r.get("text", ""), [], None, r.get("note", ""))
    return debate_stats(c, run.get("attacks", []), run.get("responses", []))

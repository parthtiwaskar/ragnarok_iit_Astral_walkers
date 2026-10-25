"""Debate quality rules, verified offline with the scripted model."""
from __future__ import annotations

from nine_realms import FakeLLM, Thing
from nine_realms.debate import ATTACK_KINDS, audit_claim, debate_stats, is_relevant
from nine_realms.models import Attack, CaseFile, Claim, Estimate, Evidence, Response

from .fixtures import AS_OF, Q, StubToolBox, make_policy


def run(**kw):
    t = Thing(FakeLLM(make_policy(**kw)), StubToolBox())
    return t, t.run_case(Q, AS_OF)


def events(t, name):
    return t.bus.of_type(name)


def test_vague_attacks_are_rejected_and_the_attacker_must_be_specific():
    t, case = run(generic_first=True)
    errors = [e["error"] for e in events(t, "invalid_output") if e["phase"] == "attack"]
    assert any("kind must be one of" in e for e in errors) or any("say exactly what is wrong" in e for e in errors)
    attacks = list(case.attacks.values())
    assert attacks and all(a.kind in ATTACK_KINDS and a.counter_evidence for a in attacks)
    assert all(r.debate["specific_attack_pct"] == 100 and r.debate["evidence_backed_pct"] == 100 for r in case.runs)


def test_counter_evidence_must_bear_on_the_claim():
    claim = Claim("R1-T1", "THOR", 1, "62% of Midgard wheat imports used the corridor", "fact",
                  [Evidence("ORV-0001", "The corridor expired.", tier=1)], Estimate("Midgard", "wheat", 55, 70))
    on_topic = [Evidence("ORV-0002", "Carriers reroute Midgard grain; rail can absorb 30 percent of displaced wheat volume.")]
    off_topic = [Evidence("ORV-0008", "The Ymir canal pilot schedule changed for the summer season.")]
    assert is_relevant(on_topic, claim) and not is_relevant(off_topic, claim)
    assert is_relevant([Evidence("X", "Volumes fell 62 percent in September.")], claim)  # a shared figure counts


def test_audit_flags_estimates_whose_figures_appear_in_no_quote():
    quoted = Claim("c", "THOR", 1, "t", "fact", [Evidence("D", "imports fell 44 percent", tier=1)], Estimate("Midgard", "wheat", 43, 45))
    derived = Claim("c", "THOR", 1, "t", "inference", [Evidence("D", "imports fell sharply", tier=2)], Estimate("Midgard", "wheat", 43, 45))
    assert audit_claim(quoted)["estimate_figures_appear_in_quotes"] is True and audit_claim(quoted)["best_tier"] == 1
    a = audit_claim(derived)
    assert a["estimate_figures_appear_in_quotes"] is False and "recompute" in a["hint"]


def test_a_sourced_fact_is_not_conceded_to_weaker_evidence_without_a_fight():
    t, case = run(loki_targets_tier1=True, thor_response="concede_fact")
    assert events(t, "concession_guard")  # the first concession was bounced with an explanation
    t1 = case.claims["R1-T1"]
    assert t1.status != "conceded"  # defended instead, with a second document
    resp = next(r for r in case.responses.values() if case.attacks[r.attack_id].target_claim_id == "R1-T1")
    assert resp.action == "DEFEND" and {e.doc_id for e in resp.evidence} == {"ORV-0002"}


def test_revise_narrows_a_claim_instead_of_dropping_it():
    t, case = run(thor_response="revise")
    revised = [c for c in case.claims.values() if c.status == "amended"]
    assert revised and revised[0].estimate.low == 50 and revised[0].estimate.high == 60
    assert events(t, "revision") and revised[0].id not in {e["claim_id"] for e in events(t, "concession")}
    assert all(r.debate["revised"] >= 0 for r in case.runs) and case.runs[0].debate["revised"] == 1


def test_a_defence_without_new_evidence_gets_one_retry_then_counts_as_a_concession():
    t, case = run()
    assert events(t, "defence_retry") and events(t, "defence_downgraded")
    assert case.runs[1].debate["defence_without_evidence_downgraded"] == 1


def test_a_collapsed_debate_is_flagged_and_sends_the_case_back():
    t, case = run(attack_all=True, concede_all=True)
    ds = events(t, "debate_stats")[0]
    assert ds["mass_concession"] and ds["concession_rate"] >= 0.7 and ds["claims_attacked"] >= 3
    assert any("S7" in d for d in case.runs[0].deficiencies) and case.runs[0].sufficient is False
    assert events(t, "remand") and len(case.runs) >= 2  # sent back for another run, not accepted as agreement


def test_scorecard_counts_actions_and_flags_conceded_well_sourced_facts():
    case = CaseFile("c", "q", AS_OF)
    case.claims["R1-T1"] = Claim("R1-T1", "THOR", 1, "fact", "fact", [Evidence("A", "x", tier=1)])
    case.claims["R1-L1"] = Claim("R1-L1", "LOKI", 1, "inference", "inference", [Evidence("B", "y", tier=3)])
    case.attacks["a1"] = Attack("a1", 1, "LOKI", "R1-T1", "t", "scope", [Evidence("B", "y")])
    case.attacks["a2"] = Attack("a2", 1, "THOR", "R1-L1", "t", "logic", [])
    case.responses["a1"] = Response("a1", "THOR", "CONCEDE", "ok")
    case.responses["a2"] = Response("a2", "LOKI", "DEFEND", "ok", [Evidence("C", "z")])
    s = debate_stats(case, ["a1", "a2"], ["a1", "a2"])
    assert s["attacks"] == 2 and s["conceded"] == 1 and s["defended"] == 1 and s["concession_rate"] == 0.5
    assert s["specific_attack_pct"] == 50 and s["evidence_backed_pct"] == 50 and s["tier12_facts_conceded"] == ["R1-T1"]
    assert s["mass_concession"] is False

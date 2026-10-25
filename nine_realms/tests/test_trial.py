from __future__ import annotations

from nine_realms import FakeLLM, Limits, Memory, Thing
from nine_realms.tools import ALLOWED_TOOLS

from .fixtures import AS_OF, DOCS, Q, StubToolBox, make_policy


def run(**policy_kw):
    tb = StubToolBox(fail_on=policy_kw.pop("fail_on", None))
    t = Thing(FakeLLM(make_policy(**policy_kw)), tb)
    return t, tb, t.run_case(Q, AS_OF)


def types(t, name):
    return t.bus.of_type(name)


def test_full_two_run_case_completes():
    t, tb, case = run()
    assert case.status == "COMPLETE", case.status_reasons
    assert len(case.runs) == 2 and case.runs[0].sufficient is False and case.runs[1].sufficient is True
    assert case.runs[1].new_doc_ids == ["ORV-0005"]  # run 2 brought new evidence
    # remand happened with a deficiency, not an instruction
    assert types(t, "remand")[0]["deficiencies"]
    # cross-examination, a concession, a rejected/conceded ruling
    assert types(t, "attack_made") and types(t, "concession")
    assert any(r.status in ("conceded", "rejected") for r in case.rulings.values())
    # a defence with no new evidence was downgraded to a concession
    assert types(t, "defence_downgraded")
    # every claim has a ruling
    assert set(case.rulings) == set(case.claims)


def test_briefing_is_rulings_only_with_citations_and_exposure_table():
    _, _, case = run()
    md = case.briefing_md
    assert "Established findings" in md and "Contested findings" in md and "Conceded" in md
    assert "| Midgard | wheat |" in md and "[ORV-0001, 2027-01-10]" in md
    assert "ORV-9999" not in md and "[unverified-id-removed]" in md  # Odin cannot invent a source
    assert "Assumptions" in md and "Rail capacity holds" in md
    cited = {line.split(" ")[1] for line in md.splitlines() if line.startswith("- ORV-")}
    assert cited <= set(DOCS)


def test_odin_range_sits_inside_evidence_span_and_exposure_rows_are_separate():
    from nine_realms.briefing import exposure_rows
    _, _, case = run()
    wheat = next(r for r in exposure_rows(case) if r["sector"] == "wheat")
    assert wheat["thor"] and wheat["loki"] and wheat["odin"]
    # the conceded tier-4 claim (80-90) must not inflate Thor's range
    assert wheat["thor"][1] <= 70
    assert wheat["loki"][0] <= wheat["odin"][0] <= wheat["odin"][1] <= wheat["thor"][1]


def test_unverified_event_stops_before_the_trial():
    t, _, case = run(verified=False)
    assert case.status == "REJECTED" and "rumour" in case.verdict_reason.lower()
    assert not types(t, "claim_submitted")


def test_one_side_fails_other_goes_unchallenged_and_case_is_not_complete():
    t, _, case = run(loki_garbage=True)
    assert case.status == "INCOMPLETE"
    assert case.runs[0].unchallenged_side == "THOR"
    assert "unchallenged" in case.briefing_md.lower()
    assert any("LOKI failed round 1" in f for f in case.runs[0].failures)


def test_clock_firewall_and_dedupe_and_allowlist():
    t, tb, case = run()
    # no agent ever got a document dated after the as-of date
    retrieved = {d for e in types(t, "tool_call") for d in e["retrieved"]}
    assert "ORV-0099" not in retrieved
    # as_of is injected by the guard on every clocked call
    assert all(a.get("as_of") == AS_OF for n, a in tb.log if n in ("search_documents", "verify_citation"))
    # odin has no search tool; nobody has a tool outside their allowlist
    assert "search_documents" not in ALLOWED_TOOLS["ODIN"]
    searches = [(e["agent"], e["args"]["query"]) for e in types(t, "tool_call") if e["tool"] == "search_documents"]
    assert len(searches) == len(set(searches))  # no repeated query per agent
    # every advocate searched at least once
    assert {"THOR", "LOKI"} <= {a for a, _ in searches}


def test_injection_is_flagged_not_obeyed():
    from nine_realms.events import EventBus
    from nine_realms.tools import Budget, Guard
    bus = EventBus()
    g = Guard(StubToolBox(), bus, Budget(Limits()))
    t = Thing(FakeLLM(make_policy()), StubToolBox())
    res = g.call(t.thor, "search_documents", {"query": "maize arrivals fell"}, _case(), 1, "round1")
    assert res["injection_suspected"] and "DATA" in res["warning"]
    assert bus.of_type("injection_flagged")
    denied = g.call(t.odin, "search_documents", {"query": "x"}, _case(), 1, "judge")
    assert denied["error"] == "NOT_ALLOWED"
    first = g.call(t.thor, "search_documents", {"query": "wheat"}, _case(), 1, "round1")
    again = g.call(t.thor, "search_documents", {"query": "  WHEAT "}, _case(), 1, "round1")
    assert again["cached"] and first["untrusted_document_content"] == again["result"]["untrusted_document_content"]


def test_failed_tool_does_not_crash_the_trial():
    t, _, case = run(fail_on="read_document")  # unused by the script; also try a tool the script uses:
    assert case.status == "COMPLETE"
    t2, _, case2 = run(fail_on="list_events")
    assert case2.status == "COMPLETE"
    t3 = Thing(FakeLLM(make_policy()), StubToolBox(fail_on="search_documents"))
    case3 = t3.run_case(Q, AS_OF)  # every search fails -> agents cannot gather -> clean abort, no exception
    assert case3.status in ("ABORTED", "INCOMPLETE") and types(t3, "tool_error")


def test_hard_limits_stop_the_case_cleanly():
    t = Thing(FakeLLM(make_policy()), StubToolBox(), limits=Limits(max_llm_calls=5))
    case = t.run_case(Q, AS_OF)
    assert case.status == "INCOMPLETE" and "max_llm_calls" in " ".join(case.status_reasons)


def test_private_memory_changes_after_case_and_respects_the_clock():
    t, _, case = run()
    assert t.thor.memory is not t.loki.memory and t.thor.memory.all() != t.loki.memory.all()
    assert any("Conceded" in m["text"] for m in t.thor.memory.all())
    assert not any("Conceded" in m["text"] for m in t.odin.memory.all())  # no cross-agent leakage
    m = Memory("X")
    m.add("Document ORV-0100 was retracted", case_id="c", knowledge_date="2027-03-01")
    m.add("Always check retraction status first", case_id="c", knowledge_date="2027-03-01")
    texts = [r["text"] for r in m.recall("retraction retracted document", "2027-01-25")]
    assert texts == ["Always check retraction status first"]  # facts from the future are hidden; lessons are not
    assert m.classify("Document ORV-0100 was retracted") == "fact"


def _case():
    from nine_realms.models import CaseFile
    return CaseFile("c", Q, AS_OF)

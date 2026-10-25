from __future__ import annotations

from nine_realms import EventBus, FakeLLM, Thing
from nine_realms.store import Store

from .fixtures import AS_OF, Q, StubToolBox, make_policy


def _run(store: Store):
    cid = "CASE-TEST"
    bus = EventBus(sink=lambda ev: store.add_event(cid, ev))
    t = Thing(FakeLLM(make_policy()), StubToolBox(), bus=bus, on_update=lambda c: store.save_case(c.to_dict(), "fake"))
    return t.run_case(Q, AS_OF, case_id=cid)


def test_case_round_trips_through_the_database():
    store = Store()
    case = _run(store)
    saved = store.load_case("CASE-TEST")
    original = case.to_dict()
    assert saved["status"] == original["status"] == "COMPLETE"
    assert set(saved["claims"]) == set(original["claims"]) and set(saved["rulings"]) == set(original["rulings"])
    assert saved["claims"]["R1-T1"]["evidence"] == original["claims"]["R1-T1"]["evidence"]
    assert saved["claims"]["R1-T1"]["estimate"] == original["claims"]["R1-T1"]["estimate"]
    assert {(r["country"], r["sector"]) for r in saved["ranges"]} == {(r["country"], r["sector"]) for r in original["ranges"]}
    assert saved["briefing_md"] == original["briefing_md"] and len(saved["runs"]) == 2
    assert saved["scope"] == [list(s) for s in original["scope"]]


def test_events_are_persisted_live_and_filterable():
    store = Store()
    case = _run(store)
    rulings = store.events("CASE-TEST", types=["ruling"])
    assert len(rulings) == len(case.rulings)
    assert all(e["type"] == "ruling" and "claim_id" in e for e in rulings)
    later = store.events("CASE-TEST", since=rulings[0]["seq"])
    assert all(e["seq"] > rulings[0]["seq"] for e in later)
    assert store.list_cases()[0]["claims"] == len(case.claims)


def test_saving_twice_replaces_rows_and_deleting_cleans_up():
    store = Store()
    _run(store)
    n = store.counts()["claims"]
    store.save_case(store.load_case("CASE-TEST"))
    assert store.counts()["claims"] == n and store.counts()["cases"] == 1
    store.delete_case("CASE-TEST")
    assert all(v == 0 for v in store.counts().values())

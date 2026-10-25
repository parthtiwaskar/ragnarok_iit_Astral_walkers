from __future__ import annotations

from nine_realms import EventBus, FakeLLM, Thing
from nine_realms.acceptance import check
from nine_realms.store import Store

from .fixtures import AS_OF, Q, StubToolBox, make_policy


def _case(**policy):
    store, cid = Store(), "CASE-ACC"
    bus = EventBus(sink=lambda ev: store.add_event(cid, ev))
    Thing(FakeLLM(make_policy(**policy)), StubToolBox(), bus=bus, on_update=lambda c: store.save_case(c.to_dict(), "fake")).run_case(Q, AS_OF, case_id=cid)
    return store.load_case(cid), store.events(cid, limit=100000)


def _by_n(res):
    return {r["n"]: r for r in res}


def test_a_complete_two_run_case_passes_the_structural_criteria():
    case, events = _case()
    r = _by_n(check(case, events))
    for n in (3, 4, 5, 8):
        assert r[n]["passed"], (n, r[n]["detail"])
    assert r[7]["passed"] is None  # the dashboard is checked by eye
    assert r[1]["passed"] is False and r[2]["passed"] is False  # the stub never read a trade table or filtered a search, and the checker notices


def test_unruled_claims_or_missing_second_run_fail():
    case, events = _case()
    case["rulings"].pop(next(iter(case["rulings"])))
    assert _by_n(check(case, events))[5]["passed"] is False
    case["runs"] = case["runs"][:1]
    assert _by_n(check(case, [e for e in events if e["type"] != "remand"]))[8]["passed"] is False


def test_a_rejected_case_is_reported_not_crashed():
    case, events = _case(verified=False)
    r = _by_n(check(case, events))
    assert r[3]["passed"] is False and r[4]["passed"] is False


def test_scripted_dry_run_over_the_real_corpus_and_a_live_damaged_pack_meets_every_checkable_criterion(tmp_path, monkeypatch):
    from nine_realms import embed
    monkeypatch.setattr(embed, "_cached", embed.HashEmbedder())  # the light embedder keeps this end-to-end test fast
    from nine_realms.dryrun import run_dry
    from nine_realms.rehearsal import make_pack
    from nine_realms.server import default_pack

    base = default_pack()
    make_pack(tmp_path / "e9", kind="damaged", source_pack=base)
    r = run_dry(base, tmp_path / "dry.sqlite", tmp_path / "out", [str(tmp_path / "e9")])
    assert r["status"] == "COMPLETE" and r["runs"] == 2, r["reasons"]
    failed = [(c["n"], c["detail"]) for c in r["acceptance"] if c["passed"] is False]
    assert not failed, failed
    # Tolan maize has no trade table: Odin says so rather than invent a range
    from nine_realms.store import Store
    ranges = {(g["country"], g["sector"]): g for g in Store(tmp_path / "dry.sqlite").load_case(r["case_id"])["ranges"]}
    assert ranges[("Tolan", "maize")]["low"] is None and "unsupported_no_evidence" in ranges[("Tolan", "maize")]["flags"]

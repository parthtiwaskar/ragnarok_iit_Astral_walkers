from __future__ import annotations

import json

from nine_realms import EventBus, FakeLLM, Thing
from nine_realms.export import export_case
from nine_realms.store import Store

from .fixtures import AS_OF, Q, StubToolBox, make_policy


def test_a_saved_case_exports_as_the_submission_log(tmp_path):
    db = tmp_path / "x.sqlite"
    store = Store(db)
    bus = EventBus(sink=lambda ev: store.add_event("CASE-X", ev))
    Thing(FakeLLM(make_policy()), StubToolBox(), bus=bus, on_update=lambda c: store.save_case(c.to_dict(), "fake")).run_case(Q, AS_OF, case_id="CASE-X")
    dest = export_case(str(db), "CASE-X", str(tmp_path / "runs"))
    assert {p.name for p in dest.iterdir()} == {"case.json", "events.jsonl", "briefing.md", "acceptance.json"}
    assert json.loads((dest / "case.json").read_text())["status"] == "COMPLETE"
    assert len((dest / "events.jsonl").read_text().splitlines()) > 20
    assert "Established findings" in (dest / "briefing.md").read_text()
    assert len(json.loads((dest / "acceptance.json").read_text())) == 10

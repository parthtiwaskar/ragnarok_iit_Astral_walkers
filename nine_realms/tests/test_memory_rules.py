"""Memory that changes behaviour deterministically, and the read-only inspector."""
from __future__ import annotations

from nine_realms import FakeLLM, Memory, Thing

from .fixtures import AS_OF, Q, StubToolBox, make_policy

REPEAT = "Midgard wheat imports will collapse by 90%"


def run(**kw):
    t = Thing(FakeLLM(make_policy(**kw)), StubToolBox())
    return t, t.run_case(Q, AS_OF)


def test_a_conceded_claim_is_remembered_after_the_run_that_conceded_it():
    t, case = run()
    mems = t.thor.memory.items("conceded;")
    run1 = [m for m in mems if m["text"].startswith("Run 1 conceded")]
    assert len(run1) == 1 and REPEAT in run1[0]["text"] and f"case={case.case_id}" in run1[0]["tags"]
    assert "docs=ORV-0003" in run1[0]["tags"] and "key=Midgard/wheat" in run1[0]["tags"]
    assert any(m["text"].startswith("Run 2 conceded") for m in mems)                                # run 2's concession is remembered too
    assert t.loki.memory.items("conceded;") == [] and t.odin.memory.items("conceded;") == []   # private: only the agent that conceded remembers it


def test_memory_stops_an_agent_re_arguing_a_claim_it_conceded_without_new_evidence():
    t, case = run(thor_repeats="plain")
    hits = [e for e in t.bus.of_type("memory_influence") if e.get("kind") == "suppressed_repeat_of_conceded_claim"]
    assert len(hits) == 1 and hits[0]["agent"] == "THOR" and hits[0]["run"] == 2 and hits[0]["suppressed_claim"] == REPEAT
    assert hits[0]["memory_ids"] and REPEAT in hits[0]["memory"]                                  # the log names the memory that changed the action
    assert REPEAT not in [c.text for c in case.claims.values() if c.run == 2]                      # ... and the claim never reached the Case File


def test_the_same_claim_with_new_evidence_is_allowed_through():
    t, case = run(thor_repeats="new_evidence")
    assert not [e for e in t.bus.of_type("memory_influence") if e.get("kind") == "suppressed_repeat_of_conceded_claim"]
    again = [c for c in case.claims.values() if c.run == 2 and c.text == REPEAT]
    assert again and {e.doc_id for e in again[0].evidence} == {"ORV-0003", "ORV-0005"}              # an agent may change its mind when the evidence changes


def test_memory_is_scoped_to_the_case(tmp_path):
    m = Memory("THOR", tmp_path / "thor.sqlite")
    m.add("Run 1 conceded (x) :: some claim", case_id="CASE-A", knowledge_date=AS_OF, tags="conceded;case=CASE-A;key=Midgard/wheat;docs=D1")
    assert m.items("conceded;case=CASE-A;") and not m.items("conceded;case=CASE-B;")


def test_the_memory_inspector_is_read_only_and_per_agent(tmp_path):
    import http.client
    import json
    import threading
    from http.server import ThreadingHTTPServer

    from nine_realms.server import App, Config, make_handler
    from nine_realms.store import Store

    Memory("THOR", tmp_path / "thor.sqlite").add("Lesson: check retraction status first", case_id="C", knowledge_date=AS_OF)
    app = App(None, Store(), Config(token="", memory_dir=str(tmp_path)), "")  # type: ignore[arg-type]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    app.cfg.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", app.cfg.port, timeout=10)
            c.request("GET", path)
            r = c.getresponse()
            return r.status, json.loads(r.read())
        code, body = get("/api/memory/thor")
        assert code == 200 and body["total"] == 1 and "retraction" in body["memories"][0]["text"]
        assert get("/api/memory/loki")[1]["total"] == 0                       # each agent has its own store
        assert get("/api/memory/nobody")[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()

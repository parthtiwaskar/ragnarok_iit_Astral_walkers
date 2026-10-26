from __future__ import annotations

import json

from nine_realms import embed, realrun
from nine_realms.llm import FakeLLM


def _case(status="COMPLETE", runs=2):
    return {"status": status, "status_reasons": ["x"], "runs": [{}] * runs}


def test_a_case_is_a_valid_log_only_if_complete_with_two_runs_and_no_failed_criterion():
    ok = [{"n": 1, "title": "t", "detail": "d", "passed": True}, {"n": 7, "title": "t", "detail": "d", "passed": None}]
    assert realrun.verdict(_case(), ok) == []
    assert "not COMPLETE" in realrun.verdict(_case("INCOMPLETE"), ok)[0]
    assert any("only 1 run" in p for p in realrun.verdict(_case(runs=1), ok))
    bad = ok + [{"n": 5, "title": "Odin rules", "detail": "0 of 3", "passed": False}]
    assert any("criterion 5" in p for p in realrun.verdict(_case(), bad))


def test_realrun_writes_the_log_for_a_good_case_and_nothing_for_a_bad_one(tmp_path, monkeypatch, capsys):
    from nine_realms import preflight, server
    from nine_realms.corpus import LocalCorpus
    from nine_realms.dryrun import DryPolicy

    monkeypatch.setattr(embed, "_cached", embed.HashEmbedder())
    monkeypatch.setattr(realrun, "POLL_SECONDS", 0.05)
    monkeypatch.setenv("NINE_REALMS_DB", str(tmp_path / "r.sqlite"))
    monkeypatch.setenv("NINE_REALMS_MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("LIVE_PACKS_DIR", str(tmp_path / "live"))
    monkeypatch.setattr(preflight, "run", lambda *a, **k: 0)
    monkeypatch.setattr(server, "choose_provider", lambda: ("fake", "test"))
    monkeypatch.setattr(server, "provider_model", lambda p: "scripted")
    question = realrun.default_question(server.default_pack())
    corpus = LocalCorpus(server.default_pack(), str(tmp_path / "policy.sqlite"))
    from nine_realms.dates import extract_as_of
    policy = DryPolicy(corpus, question, extract_as_of(question) or corpus.pack_as_of)
    monkeypatch.setattr(server, "make_llm", lambda provider: FakeLLM(policy))

    assert realrun.main(["--out", str(tmp_path / "runs")]) == 0
    logs = list((tmp_path / "runs").iterdir())
    assert len(logs) == 1 and json.loads((logs[0] / "case.json").read_text())["status"] == "COMPLETE"
    assert "VALID two-run log" in capsys.readouterr().out

    # a case that cannot finish two runs writes nothing
    monkeypatch.setattr(server, "make_llm", lambda provider: FakeLLM(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider down"))))
    assert realrun.main(["--out", str(tmp_path / "runs2")]) == 1
    assert not (tmp_path / "runs2").exists()


def test_realrun_stops_with_a_clear_message_when_there_is_no_key(monkeypatch, capsys):
    from nine_realms import server
    monkeypatch.setattr(server, "choose_provider", lambda: ("", "no LLM key set"))
    monkeypatch.setattr(realrun, "load_env", lambda *a, **k: [])
    assert realrun.main([]) == 2
    assert "NOT READY" in capsys.readouterr().err

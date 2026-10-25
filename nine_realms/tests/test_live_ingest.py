"""Final-round ingestion: a new event pack arrives live and is merged into the corpus. No code or index edits."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nine_realms import embed
from nine_realms.corpus import LocalCorpus
from nine_realms.rehearsal import make_pack
from nine_realms.server import default_pack

BASE = default_pack()
pytestmark = pytest.mark.skipif(not (Path(BASE) / "manifest.json").exists(), reason="Runemark pack not found")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    os.environ["NINE_REALMS_EMBEDDER"] = "hash"
    embed._cached = None
    tmp = tmp_path_factory.mktemp("live")
    corpus = LocalCorpus(BASE, tmp / "c.sqlite")
    before = corpus.stats()
    expect = make_pack(tmp / "pack_e9", kind="damaged", source_pack=BASE)
    report = corpus.ingest_pack(tmp / "pack_e9", "pack-e9")
    return corpus, expect, report, before, tmp


def test_new_documents_are_searchable_alongside_the_base_pack(world):
    corpus, expect, report, before, _ = world
    assert corpus.stats()["documents"] > before["documents"] and corpus.stats()["packs"] == 2
    hits = corpus.call("search_documents", {"query": "Orvan corridor agreement expired", "as_of": expect["as_of"], "event_id": "E9"})["results"]
    assert hits and all(h["event_id"] == "E9" and h["doc_id"].startswith("KET-") for h in hits)
    assert corpus.call("search_documents", {"query": "Orvan corridor agreement expired", "as_of": "2027-01-25", "event_id": "E5"})["results"]  # base intact
    events = {e["event_id"]: e for e in corpus.call("list_events", {"as_of": expect["as_of"]})["results"]}
    assert "E9" in events and "Kethar" in events["E9"]["name"] and "E5" in events


def test_all_three_file_formats_are_read(world):
    corpus, *_ = world
    fmts = {corpus.docs[d]["format"] for d in ("KET-0001", "KET-0002", "KET-0003")}
    assert fmts == {"TXT", "PDF", "DOCX"}
    for d in ("KET-0002", "KET-0003"):
        assert len(corpus.docs[d]["body"]) > 200 and "[P1]" in corpus.docs[d]["body"]


def test_every_kind_of_damaged_document_is_caught_and_reported(world):
    corpus, expect, report, *_ = world
    q = report["quarantined"]
    assert "KET-0013" in q and "empty" in q["KET-0013"]
    assert "KET-0018" in q and "MISSING_FILE" in q["KET-0018"] and report["missing_files"] == ["KET-0018"]
    assert "KET-0014" in report["duplicates"] and report["duplicates"]["KET-0014"] == "KET-0004"          # cut-off copy of a document we hold in full
    assert "TRUNCATED_COPY" in corpus.docs["KET-0014"]["flags"]
    assert "POSSIBLY_TRUNCATED" in corpus.docs["KET-0015"]["flags"]                                        # cut off, no full copy
    assert report["duplicates"].get("KET-0016") == "KET-0001"                                              # exact duplicate
    assert "KET-0005" in report["repaired"] and "mojibake" in report["repaired"]["KET-0005"]
    assert "Résumé" in corpus.docs["KET-0005"]["body"] and "Ã" not in corpus.docs["KET-0005"]["body"]
    assert report["off_manifest"] == ["KET-0017.txt"] and "KET-0017" in corpus.docs                         # a document the manifest forgot


def test_duplicates_and_truncated_copies_do_not_appear_twice_in_search(world):
    corpus, expect, *_ = world
    ids = [h["doc_id"] for h in corpus.call("search_documents", {"query": "corridor agreement was not renewed", "as_of": expect["as_of"],
                                                                 "event_id": "E9", "limit": 20})["results"]]
    assert "KET-0016" not in ids and "KET-0014" not in ids


def test_damaged_trade_rows_are_reported_and_never_used(world):
    corpus, expect, report, *_ = world
    reasons = {(r["doc_id"], r["row"]): r["reason"] for r in report["trade"]["rows_flagged"]}
    for row, why in expect["bad_rows"]["KET-0007"].items():
        assert reasons.get(("KET-0007", row)) == why, (row, why, reasons)
    assert {"doc_id": "KET-0008", "missing_months": ["2026-10", "2026-11", "2026-12", "2027-10", "2027-11", "2027-12"]} in report["trade"]["incomplete_tables"] or \
        any(t["doc_id"] == "KET-0008" for t in report["trade"]["incomplete_tables"])
    t = corpus.call("read_trade_table", {"table_id": "KET-0007", "as_of": expect["as_of"]})
    assert "do NOT use" in t["warning"] and all(r["quality"] != "ok" or r["value_usd_m"] for r in t["rows"])
    stats = corpus.call("trade_stats", {"table_id": "KET-0007", "as_of": expect["as_of"]})
    assert stats["excluded_damaged_rows"] and all(m["row"] not in stats["excluded_damaged_rows"] for m in stats["yoy"])
    cut = corpus.call("read_trade_table", {"table_id": "KET-0008", "as_of": expect["as_of"]})
    assert cut["missing_months"] and "incomplete" in cut["warning"]


def test_injection_and_correction_in_the_new_pack_are_visible_to_the_agents(world):
    corpus, expect, *_ = world
    inj = corpus.call("read_document", {"doc_id": "KET-0012", "as_of": expect["as_of"]})
    assert "ASSISTANT:" in inj["text"]  # present as data; the guard flags it when an agent retrieves it (see test_trial)
    assert corpus.docs["KET-0004"]["source_type"] == "correction"


def test_ingesting_the_same_pack_again_changes_nothing(world):
    corpus, expect, report, _, tmp = world
    before = corpus.stats()
    again = corpus.ingest_pack(tmp / "pack_e9", "pack-e9")
    assert again["ingested"] == 0 and again["skipped_unchanged"] >= report["ingested"]
    assert corpus.stats()["documents"] == before["documents"] and corpus.stats()["embeddings"] == before["embeddings"]


def test_a_failed_document_never_stops_the_rest_of_the_pack(world):
    _, _, report, *_ = world
    assert report["ingested"] >= 15 and len(report["quarantined"]) == 2  # empty + missing file, nothing else


def test_new_pack_survives_a_restart_and_the_base_is_not_rebuilt(world):
    corpus, expect, _, _, tmp = world
    again = LocalCorpus(BASE, tmp / "c.sqlite")
    assert again.reused and "KET-0001" in again.docs and again.stats()["packs"] == 2


def test_a_rumour_only_pack_ingests_with_its_reliability_tiers(tmp_path):
    os.environ["NINE_REALMS_EMBEDDER"] = "hash"
    embed._cached = None
    corpus = LocalCorpus(BASE, tmp / "c.sqlite") if False else LocalCorpus(BASE, tmp_path / "c.sqlite")
    expect = make_pack(tmp_path / "pack_e8", kind="fake-event", source_pack=BASE)
    report = corpus.ingest_pack(tmp_path / "pack_e8", "pack-e8")
    assert report["ingested"] == 4 and not report["quarantined"]
    prof = corpus.call("event_profile", {"event_id": "E8", "as_of": expect["as_of"]})
    assert prof["by_tier"] == {4: 3, 1: 1} and sorted(prof["low_reliability_only"]) == ["RUM-0001", "RUM-0002", "RUM-0003"]

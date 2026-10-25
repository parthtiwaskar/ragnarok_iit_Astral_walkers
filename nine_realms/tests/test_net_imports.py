"""import_summary: the net-exposure anchor. Totals across ALL origins, damaged rows never used, no invented drops."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nine_realms import embed
from nine_realms.corpus import LocalCorpus
from nine_realms.server import default_pack

BASE = default_pack()
AS_OF = "2027-01-25"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    os.environ["NINE_REALMS_EMBEDDER"] = "hash"
    embed._cached = None
    if not (Path(BASE) / "manifest.json").exists():
        pytest.skip("Runemark pack not found")
    return LocalCorpus(BASE, tmp_path_factory.mktemp("n") / "c.sqlite")


def summary(c, country, product, as_of=AS_OF):
    return c.call("import_summary", {"country": country, "product": product, "as_of": as_of})


def test_midgard_wheat_net_exposure_matches_a_hand_calculation(corpus):
    r = summary(corpus, "Midgard", "wheat")
    whole, since, last3 = r["windows"]["whole_period"], r["windows"]["since_break"], r["windows"]["last_3_months"]
    assert [o["origin"] for o in r["origins"]] == ["Orvan", "Vanir"] and r["break_month"] == "2026-09"
    assert whole["net_volume_change_pct"] == -32.2 and whole["net_value_change_pct"] == -28.5     # Jul-Dec 2026 vs 2025, Orvan + Vanir together
    assert whole["lost_volume_t_by_origin"] == {"Orvan": -677000} and whole["gained_volume_t_by_origin"] == {"Vanir": 23000}
    assert whole["substitution_offset_pct_of_loss"] == 3.4                                           # substitutes cover almost none of the loss
    assert since["from"] == "2026-09" and since["net_volume_change_pct"] == -47.1 and last3["net_value_change_pct"] == -39.4


def test_every_month_cites_the_rows_it_came_from(corpus):
    m = summary(corpus, "Midgard", "wheat")["months"][3]  # 2026-10
    assert m["period"] == "2026-10" and m["complete"]
    orvan = m["by_origin"]["Orvan"]
    assert orvan["table_id"] == "ORV-0009" and orvan["row"] == 5 and orvan["prior_row"] == 11   # [P5] Oct 2026 and [P11] Oct 2025 in the document
    assert corpus.call("read_document", {"doc_id": "ORV-0009", "as_of": AS_OF})["text"].count("[P5] In October 2026") == 1


def test_a_single_origin_country_and_a_missing_table_are_handled(corpus):
    assert summary(corpus, "Midgard", "maize")["windows"]["whole_period"]["net_volume_change_pct"] == -29.5
    err = summary(corpus, "Tolan", "maize")
    assert err["error"] == "NO_TABLES"                                                               # no invented number for a flow with no data
    assert summary(corpus, "Midgard", "wheat", as_of="2026-12-01")["error"] == "NO_TABLES"           # the clock: those tables are dated January 2027


def test_a_month_missing_on_one_origin_is_left_out_not_counted_as_a_collapse(tmp_path):
    def note(doc_id, origin, rows):
        lines = [f"[P{i + 2}] In {m} {y}, the value was USD {v}M and the volume was {vol} tonnes." for i, (m, y, v, vol) in enumerate(rows)]
        head = f"DOC_ID: {doc_id}\nEVENT_ID: T1\nTITLE: Trade note: {origin} to Midgard, wheat, July to September 2026\nSOURCE_NAME: Realm Trade Ministry\n" \
               "SOURCE_TYPE: trade_note\nRELIABILITY: 1\nPUB_DATE: 2026-10-20\nPUB_TIME: 09:00\nCOUNTRY: Midgard\nSECTOR: Wheat\n---\n"
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / "docs" / f"{doc_id}.txt").write_text(head + "Trade note\n\n[P1] Route.\n\n" + "\n\n".join(lines) + "\n", encoding="utf-8")
    full = lambda y, k: [(m, y, 25 * k, 100000 * k) for m in ("July", "August", "September")]  # noqa: E731
    note("T-0001", "Orvan", full(2025, 1) + [(m, 2026, 25, 100000) for m in ("July", "August", "September")])
    note("T-0002", "Vanir", full(2025, 1)[:3] + [(m, 2026, 20, 80000) for m in ("July", "August")])  # September 2026 is missing
    (tmp_path / "manifest.json").write_text(json.dumps({"event_id": "T1", "name": "t", "as_of": "2026-11-01", "documents": [
        {"DOC_ID": d, "file": f"{d}.txt", "EVENT_ID": "T1", "TITLE": f"Trade note: {o} to Midgard, wheat, July to September 2026", "SOURCE_NAME": "Realm Trade Ministry",
         "SOURCE_TYPE": "trade_note", "RELIABILITY": "1", "PUB_DATE": "2026-10-20", "COUNTRY": "Midgard", "SECTOR": "Wheat"} for d, o in (("T-0001", "Orvan"), ("T-0002", "Vanir"))]}))
    c = LocalCorpus(tmp_path, tmp_path / "t.sqlite")
    r = c.call("import_summary", {"country": "Midgard", "product": "wheat", "as_of": "2026-11-01"})
    months = {m["period"]: m for m in r["months"]}
    assert months["2026-09"]["complete"] is False and any("Vanir has no usable row" in e for e in r["excluded"])
    assert months["2026-07"]["complete"] and r["windows"]["whole_period"]["months"] == 2          # September is not in the totals
    assert r["windows"]["whole_period"]["net_volume_change_pct"] == -10.0                          # (100k+80k) vs (100k+100k) over Jul+Aug

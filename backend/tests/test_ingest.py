from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.runtime import build_services, ingest
from app.tools.context import Services
from tests.conftest import PACK, make_settings


def _doc(svc: Services, doc_id: str):  # type: ignore[no-untyped-def]
    d = svc.repo.get_doc(doc_id)
    assert d is not None, doc_id
    return d


def test_every_damage_type_is_handled(services: Services) -> None:
    s = services
    assert _doc(s, "RM-DOC-0101").status == "OK" and _doc(s, "RM-DOC-0101").format == "pdf"
    assert _doc(s, "RM-DOC-0103").format == "docx"
    assert _doc(s, "RM-DOC-0190").status == "QUARANTINED"  # empty PDF
    assert "EMPTY" in (_doc(s, "RM-DOC-0190").status_reason or "")
    assert _doc(s, "RM-DOC-0192").status == "REPAIRED"  # latin-1 mojibake
    assert _doc(s, "RM-DOC-0193").status == "DUPLICATE"  # duplicate of 0101
    assert _doc(s, "RM-DOC-0193").canonical_id == "RM-DOC-0101"
    assert _doc(s, "RM-DOC-0194").status == "PARTIAL"  # no header, manifest supplies metadata
    assert _doc(s, "RM-DOC-0195").status == "QUARANTINED"  # garbage with .pdf extension
    assert "UNSUPPORTED" in (_doc(s, "RM-DOC-0195").status_reason or "")
    assert _doc(s, "RM-DOC-0196").status == "PARTIAL" and "TRUNCATED" in _doc(s, "RM-DOC-0196").flags
    assert _doc(s, "RM-DOC-0197").format == "pdf"  # a PDF with a .txt extension, sniffed by magic bytes
    assert _doc(s, "RM-DOC-0198").status == "MISSING"  # listed in the manifest, no file
    assert _doc(s, "RM-DOC-0199").off_manifest  # valid header, not in the manifest
    assert "INJECTION_DETECTED" in _doc(s, "RM-DOC-0111").flags


def test_trade_rows_flagged_never_ok(services: Services) -> None:
    rows = {r.ref: r.quality for r in services.repo.trade_rows("TT-A03")}
    assert rows["TT-A03#r04"] == "BAD_NUMBER" and rows["TT-A03#r07"] == "MISSING_VALUE"
    a04 = [r.quality for r in services.repo.trade_rows("TT-A04")]
    assert "DUPLICATE_ROW" in a04 and a04[-1] == "TRUNCATED"


def test_relations_and_reference(services: Services) -> None:
    rel = {(r.src_doc, r.kind) for r in services.repo.relations_to("RM-DOC-0105")}
    assert ("RM-DOC-0106", "retracts") in rel
    assert ("RM-DOC-0110", "corrects") in {
        (r.src_doc, r.kind) for r in services.repo.relations_to("RM-DOC-0102")
    }
    assert ("RM-DOC-0403", "denies") in {(r.src_doc, r.kind) for r in services.repo.relations_to("EVT-R")}
    outlets = {r["name"]: r["tier"] for r in services.repo.reference("outlets")}
    assert outlets["Serpent Canal Authority"] == "1" and len(outlets) == 12


def test_report_written_and_incremental(tmp_path: Path) -> None:
    s = make_settings(tmp_path)
    svc = build_services(s)
    rep = ingest(svc).to_dict()
    assert rep["totals"]["quarantined"] >= 3 and rep["totals"]["trade_rows_flagged"] == 4
    assert rep["by_format"]["pdf"] >= 5 and rep["by_format"]["docx"] >= 5
    latest = json.loads((s.runs_dir / "_ingest" / "latest.json").read_text(encoding="utf-8"))
    assert latest["index_version"] == rep["index_version"]
    again = ingest(svc).to_dict()  # unchanged files are skipped
    assert again["skipped_unchanged"] >= rep["totals"]["files"] - 1
    assert again["index_version"] == rep["index_version"]


def test_live_pack_zip_ingested_without_crash(services: Services, settings) -> None:  # type: ignore[no-untyped-def]
    live = settings.live_packs_dir
    live.mkdir(parents=True, exist_ok=True)
    shutil.copy(PACK.parent / "live_pack_d.zip", live / "live_pack_d.zip")
    from app.runtime import live_pack_roots

    rep = ingest(services, live_pack_roots(live)).to_dict()
    d = services.repo.get_doc("RM-DOC-0501")
    assert d is not None and d.pack == "live_pack_d" and d.status == "OK"
    assert services.repo.get_trade_table("TT-D01") is not None
    reasons = " ".join(q["reason"] for q in rep["quarantine"])
    assert "EMPTY" in reasons and ("CORRUPT_DOCX" in reasons or "UNSUPPORTED" in reasons)


def test_garbage_folder_never_crashes(tmp_path: Path) -> None:
    root = tmp_path / "junk"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.pdf").write_bytes(b"%PDF-1.4 truncated")
    (root / "docs" / "b.docx").write_bytes(b"PK\x03\x04garbage")
    (root / "docs" / "c.txt").write_bytes(b"\xff\xfe\x00\x00" * 50)
    (root / "manifest.csv").write_text("not,a,real\nmanifest", encoding="utf-8")
    s = make_settings(tmp_path, RUNEMARK_DIR=str(root))
    rep = ingest(build_services(s)).to_dict()
    assert rep["totals"]["quarantined"] >= 2

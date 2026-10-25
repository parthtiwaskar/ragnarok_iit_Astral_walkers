from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.runtime import build_services, ingest
from app.search.filters import SearchFilters
from app.tools.asof import Clock
from app.tools.context import Services
from tests.conftest import AS_OF, PACK, make_settings


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


def test_mixed_encoding_body_is_repaired_not_quarantined(services: Services) -> None:
    d = _doc(services, "RM-DOC-0186")
    assert d.status == "REPAIRED" and "MIXED_ENCODING" in (d.status_reason or "")
    # both halves survive: the header was utf-8, the body utf-16
    assert d.outlet == "Corvenia Courier" and d.tier == 3
    assert "added two sailings a week" in d.body and "\x00" not in d.body


def test_empty_file_named_without_its_folder_is_quarantined_not_missing(services: Services) -> None:
    d = _doc(services, "RM-DOC-0187")
    assert d.status == "QUARANTINED" and "EMPTY" in (d.status_reason or "")
    assert d.title == "Wire that arrived empty"  # the manifest entry was still matched to the file


def test_truncated_copy_is_demoted_to_the_full_document(services: Services) -> None:
    cut, full = _doc(services, "RM-DOC-0189"), _doc(services, "RM-DOC-0188")
    assert full.status == "OK"
    assert cut.status == "DUPLICATE" and cut.canonical_id == "RM-DOC-0188"
    assert "TRUNCATED" in cut.flags
    assert ("RM-DOC-0189", "duplicate_of") in {
        (r.src_doc, r.kind) for r in services.repo.relations_to("RM-DOC-0188")
    }
    # a demoted copy leaves nothing behind in the index
    assert not [r for r in services.repo.all_chunks() if r["chunk_id"].startswith("RM-DOC-0189#")]
    hits = services.searcher.search(
        "rounds the Eskerland cape adding nine days", SearchFilters(), Clock(AS_OF)
    )
    assert all(h["doc_id"] != "RM-DOC-0189" for h in hits["results"])


def test_pack_furniture_is_skipped_rather_than_called_off_manifest(tmp_path: Path) -> None:
    rep = ingest(build_services(make_settings(tmp_path))).to_dict()
    skipped = set(rep["skipped_not_documents"])
    assert {"runemark/README.md", "runemark/runemark_map.png", "runemark/hashes.sha256"} <= skipped
    assert not any("README" in item or ".png" in item for item in rep["off_manifest"])
    assert not any("README" in q["file"] or ".png" in q["file"] for q in rep["quarantine"])


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

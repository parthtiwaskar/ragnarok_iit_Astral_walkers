"""Rehearsal packs: synthetic final-round event packs built from the real E5 pack, for practising live ingestion.

    python -m nine_realms.rehearsal --out /tmp/pack_e9 --kind damaged        # a new event with every kind of damage
    python -m nine_realms.rehearsal --out /tmp/pack_e8 --kind fake-event     # a rumour with no real event behind it
    python -m nine_realms.rehearsal --out /tmp/pack_e9 --zip                 # also write /tmp/pack_e9.zip, ready to upload

The new pack uses the same header, countries, outlets and file formats as Runemark but a new event, new document IDs (KET-/RUM-)
and a timeline shifted one year later. The damage mirrors section 4.5 of the problem statement. `make_pack` returns a dict
naming which documents carry which damage, so tests can check that every one was caught.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import textwrap
import zipfile
from pathlib import Path
from typing import Any

from .corpus import LocalCorpus

HEADER_ORDER = ["DOC_ID", "EVENT_ID", "TITLE", "SOURCE_NAME", "SOURCE_TYPE", "RELIABILITY", "PUB_DATE", "PUB_TIME", "COUNTRY", "SECTOR", "LANGUAGE"]
# real E5 documents the damaged pack is built from: (source doc id, new doc id, file format)
PLAN = [("ORV-0016", "KET-0001", "txt"), ("ORV-0034", "KET-0002", "pdf"), ("ORV-0032", "KET-0003", "docx"), ("ORV-0013", "KET-0004", "txt"),
        ("ORV-0028", "KET-0005", "txt"), ("ORV-0007", "KET-0006", "txt"), ("ORV-0009", "KET-0007", "txt"), ("ORV-0030", "KET-0008", "txt"),
        ("ORV-0044", "KET-0009", "txt"), ("ORV-0046", "KET-0010", "pdf"), ("ORV-0027", "KET-0011", "docx"), ("ORV-0020", "KET-0012", "txt")]


def _year_up(text: str) -> str:
    return re.sub(r"\b(2025|2026|2027)\b", lambda m: str(int(m.group(1)) + 1), text)


def _pdf_bytes(lines: list[str]) -> bytes:
    """A minimal valid one-page text PDF (Courier), the same shape as the PDFs in the Runemark pack."""
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")  # noqa: E731
    stream = "BT\n/F1 10 Tf\n13.5 TL\n56 780 Td\n" + "\nT*\n".join(f"({esc(line[:110])}) Tj" for line in lines) + "\nET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [5 0 R] /Count 1 >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 3 0 R >> >> >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1", "replace")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def _write_doc(docs_dir: Path, meta: dict[str, str], body: str, fmt: str) -> str:
    text = "\n".join(f"{k}: {meta[k]}" for k in HEADER_ORDER if k in meta) + "\n---\n" + body
    name = f"{meta['DOC_ID']}.{fmt}"
    path = docs_dir / name
    if fmt == "txt":
        path.write_text(text, encoding="utf-8")
    elif fmt == "pdf":
        path.write_bytes(_pdf_bytes([seg for line in text.split("\n") for seg in (textwrap.wrap(line, 100) or [" "])]))
    else:
        import docx

        d = docx.Document()
        for line in text.split("\n"):
            d.add_paragraph(line)
        d.save(str(path))
    return name


def _trade_body(title: str, rows: list[tuple[str, int, str, str]]) -> str:
    """rows: (month name, year, value text, volume text)"""
    lines = [title, "", f"[P1] Route: {title.split(':', 1)[1].split(',')[0].strip()}, {title.split(',')[1].strip()}."]
    for i, (m, y, v, vol) in enumerate(rows, 2):
        lines.append(f"[P{i}] In {m} {y}, the value was USD {v}M and the volume was {vol} tonnes.")
    return "\n\n".join(lines)


def make_pack(out: str | Path, *, kind: str = "damaged", event_id: str | None = None, source_pack: str | Path = "data/runemark") -> dict[str, Any]:
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    (out / "docs").mkdir(parents=True)
    src = LocalCorpus(source_pack, ":memory:")
    if kind == "fake-event":
        return _fake_event_pack(out, event_id or "E8")
    event_id = event_id or "E9"
    manifest_docs: list[dict[str, str]] = []
    expect: dict[str, Any] = {"event_id": event_id, "empty": [], "truncated_copy": [], "possibly_truncated": [], "duplicate": [], "mojibake": [],
                              "bad_rows": {}, "incomplete_tables": [], "off_manifest": [], "missing_file": [], "injection": [], "retracted": []}
    latest = ""
    bodies: dict[str, str] = {}
    for old, new, fmt in PLAN:
        d = src.docs[old]
        meta = {"DOC_ID": new, "EVENT_ID": event_id, "TITLE": _year_up(d["title"]), "SOURCE_NAME": d["outlet"], "SOURCE_TYPE": d["source_type"],
                "RELIABILITY": str(d["tier"]), "PUB_DATE": _year_up(d["date"]), "PUB_TIME": d["time"], "COUNTRY": d["country"], "SECTOR": d["sector"],
                "LANGUAGE": "en"}
        body = _year_up("\n\n".join(d["paras"])).replace("[P1] ", f"[P1] ({event_id} rehearsal pack) ", 1)  # distinct from the E5 original
        bodies[new] = body
        latest = max(latest, meta["PUB_DATE"])
        if "ASSISTANT:" in body:
            expect["injection"].append(new)
        if d["source_type"] == "correction":
            expect["retracted"].append(new)
        name = _write_doc(out / "docs", meta, body, fmt)
        manifest_docs.append({**meta, "file": name, "FORMAT": fmt.upper()})
    by = {m["DOC_ID"]: m for m in manifest_docs}

    def rewrite(doc_id: str, fn) -> None:  # re-write a TXT document after changing its body
        m = by[doc_id]
        path = out / "docs" / m["file"]
        head, body = path.read_text(encoding="utf-8").split("\n---\n", 1)
        path.write_text(head + "\n---\n" + fn(body), encoding="utf-8")

    # ---- damaged documents -------------------------------------------------------------
    (out / "docs" / "KET-0013.txt").write_bytes(b"")  # empty
    manifest_docs.append({"DOC_ID": "KET-0013", "EVENT_ID": event_id, "TITLE": "Empty notice", "SOURCE_NAME": "Raven Wire", "SOURCE_TYPE": "news",
                          "RELIABILITY": "2", "PUB_DATE": latest, "PUB_TIME": "08:00", "COUNTRY": "Orvan", "SECTOR": "Wheat", "file": "KET-0013.txt"})
    expect["empty"].append("KET-0013")
    m3 = by["KET-0004"]  # a correction, copied and cut mid-sentence: a truncated copy of a document we hold in full
    head, body = (out / "docs" / m3["file"]).read_text(encoding="utf-8").split("\n---\n", 1)
    cut = body[: int(len(body) * 0.62)].rsplit(" ", 1)[0]
    meta14 = {**{k: m3[k] for k in HEADER_ORDER if k in m3}, "DOC_ID": "KET-0014", "PUB_DATE": m3["PUB_DATE"]}
    manifest_docs.append({**meta14, "file": _write_doc(out / "docs", meta14, cut, "txt"), "FORMAT": "TXT"})
    expect["truncated_copy"].append("KET-0014")
    meta15 = {**{k: m3[k] for k in HEADER_ORDER if k in m3}, "DOC_ID": "KET-0015", "TITLE": "Strait notice, second edition"}
    body15 = "[P1] The Ymir Canal Authority reports that the canal reopened to all traffic after a two-day suspension and the first convoy of"
    manifest_docs.append({**meta15, "file": _write_doc(out / "docs", meta15, body15, "txt"), "FORMAT": "TXT"})  # cut off with no full copy
    expect["possibly_truncated"] += ["KET-0015"]
    meta16 = {**{k: by["KET-0001"][k] for k in HEADER_ORDER if k in by["KET-0001"]}, "DOC_ID": "KET-0016"}  # exact duplicate of KET-0001
    manifest_docs.append({**meta16, "file": _write_doc(out / "docs", meta16, bodies["KET-0001"], "txt"), "FORMAT": "TXT"})
    expect["duplicate"].append("KET-0016")
    rewrite("KET-0005", lambda b: b + "\n\n[P9] Résumé of the café traders' naïve forecast: déjà vu in Midgård, according to the Ymir pilots.")
    raw = (out / "docs" / by["KET-0005"]["file"]).read_text(encoding="utf-8")
    (out / "docs" / by["KET-0005"]["file"]).write_bytes(raw.encode("utf-8").decode("latin-1").encode("utf-8"))  # UTF-8 read as Latin-1, then saved
    expect["mojibake"].append("KET-0005")
    # ---- damaged trade tables ------------------------------------------------------------
    t7 = (out / "docs" / by["KET-0007"]["file"]).read_text(encoding="utf-8")
    t7 = re.sub(r"(\[P4\] In September 2027, the value was USD )[\d.]+(M)", r"\g<1>\2", t7)                       # missing value
    t7 = re.sub(r"(\[P5\] In October 2027, the value was USD )[\d.]+M", r"\g<1>3x.0M", t7)                       # bad number
    t7 = re.sub(r"(\[P6\] In November 2027, the value was USD )([\d.]+)M( and the volume was )([\d,]+)", lambda m: f"{m.group(1)}{float(m.group(2)) * 10:.1f}M{m.group(3)}{m.group(4)}", t7)  # 10x digit slip
    t7 += "\n\n[P15] In July 2027, the value was USD 12.0M and the volume was 31,000 tonnes."                     # repeated month, different numbers
    (out / "docs" / by["KET-0007"]["file"]).write_text(t7, encoding="utf-8")
    expect["bad_rows"]["KET-0007"] = {4: "UNPARSEABLE_ROW", 5: "UNPARSEABLE_ROW", 6: "PRICE_OUTLIER", 15: "CONFLICTING_ROW", 2: "CONFLICTING_ROW"}
    rewrite("KET-0008", lambda b: re.sub(r"\n\n\[P(1[0-4])\] In (October|November|December) 2026.*", "", b))      # cut-off table: months missing
    expect["incomplete_tables"].append("KET-0008")
    # ---- manifest problems ---------------------------------------------------------------
    meta17 = {"DOC_ID": "KET-0017", "EVENT_ID": event_id, "TITLE": "Port bulletin the manifest forgot", "SOURCE_NAME": "Cargo Weekly", "SOURCE_TYPE": "shipping_update",
              "RELIABILITY": "2", "PUB_DATE": latest, "PUB_TIME": "07:30", "COUNTRY": "Midgard", "SECTOR": "Wheat", "LANGUAGE": "en"}
    _write_doc(out / "docs", meta17, "[P1] Midgard Harbour reports longer waits for bulk wheat berths after the corridor ended.", "txt")
    expect["off_manifest"].append("KET-0017.txt")
    manifest_docs.append({"DOC_ID": "KET-0018", "EVENT_ID": event_id, "TITLE": "Listed but absent", "SOURCE_NAME": "Raven Wire", "SOURCE_TYPE": "news", "RELIABILITY": "2",
                          "PUB_DATE": latest, "file": "KET-0018.txt"})
    expect["missing_file"].append("KET-0018")
    from datetime import date, timedelta

    as_of = (date.fromisoformat(latest) + timedelta(days=5)).isoformat()
    (out / "manifest.json").write_text(json.dumps({"pack": out.name, "event_id": event_id, "name": "Kethar corridor strike (rehearsal)", "as_of": as_of,
                                                   "count": len(manifest_docs), "documents": manifest_docs}, indent=2), encoding="utf-8")
    expect["as_of"] = as_of
    expect["question"] = f"As of {as_of}, how exposed are Midgard and Tolan to the end of the Orvan corridor agreement? Give a low and high range for wheat and maize imports."
    return expect


def _fake_event_pack(out: Path, event_id: str) -> dict[str, Any]:
    docs = [("RUM-0001", "tabloid", 4, "Daily Roar", "STRAIT SEALED: ASGARD CUT OFF FROM THE WORLD", "2027-03-02", "[P1] The Asgard strait has been sealed and no ship can pass, the Daily Roar can reveal.\n\n[P2] A source who works at the port says every vessel was turned back."),
            ("RUM-0002", "social_post", 4, "Saga Chat", "Strait closed?? ships stuck", "2027-03-02", "[P1] Hearing the Asgard strait is closed. Share before they delete this."),
            ("RUM-0003", "blog", 4, "Trade Whisper", "Insider: Asgard strait shut for months", "2027-03-03", "[P1] An insider claims the strait will stay shut for months and LNG will vanish."),
            ("RUM-0004", "gov_notice", 1, "Realm Trade Ministry", "Asgard strait operating normally", "2027-03-04", "[P1] The Realm Trade Ministry confirms the Asgard strait is open and operating normally.\n\n[P2] Reports of a closure are false. Transit volumes are in line with the old normal.")]
    manifest = []
    for did, typ, tier, outlet, title, d, body in docs:
        meta = {"DOC_ID": did, "EVENT_ID": event_id, "TITLE": title, "SOURCE_NAME": outlet, "SOURCE_TYPE": typ, "RELIABILITY": str(tier), "PUB_DATE": d,
                "PUB_TIME": "09:00", "COUNTRY": "Asgard", "SECTOR": "LNG", "LANGUAGE": "en"}
        manifest.append({**meta, "file": _write_doc(out / "docs", meta, title + "\n\n" + body, "txt"), "FORMAT": "TXT"})
    (out / "manifest.json").write_text(json.dumps({"pack": out.name, "event_id": event_id, "name": "Asgard strait closure (rumour, rehearsal)", "as_of": "2027-03-10",
                                                   "count": len(manifest), "documents": manifest}, indent=2), encoding="utf-8")
    return {"event_id": event_id, "as_of": "2027-03-10", "fake": True, "question": "As of 2027-03-10, how exposed is Midgard to the closure of the Asgard strait? Give a low and high range for LNG imports."}


def zip_pack(pack: str | Path) -> Path:
    pack = Path(pack)
    target = pack.with_suffix(".zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(pack.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(pack.parent))
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--kind", choices=["damaged", "fake-event"], default="damaged")
    ap.add_argument("--source", default="data/runemark")
    ap.add_argument("--zip", action="store_true")
    a = ap.parse_args()
    expect = make_pack(a.out, kind=a.kind, source_pack=a.source)
    print(json.dumps(expect, indent=2)[:1500])
    if a.zip:
        print("wrote", zip_pack(a.out))


if __name__ == "__main__":
    main()

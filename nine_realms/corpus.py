"""LocalCorpus: the Runemark pack ingested into SQLite, exposed as the agents' ToolBox.

Ingestion (once per pack fingerprint; re-runs reuse the database):
  * PDF / TXT / DOCX -> text. Mixed encodings are repaired (e.g. UTF-8 header + UTF-16 body).
  * Header metadata cross-checked against the manifest; empty/unreadable files are quarantined with a reason.
  * Paragraphs go into an FTS5 (BM25, porter stemming) index.
  * Retractions/corrections become relation edges (named, or inferred from the date the correction states).
  * Exact duplicates are detected and collapsed in search.
  * Trade notes are parsed into structured monthly rows with per-row quality flags; damaged rows are never used.

Tables: meta, docs, paras, paras_fts, relations, quarantine, trade_rows.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections import Counter
from datetime import date
from difflib import SequenceMatcher
from array import array
from pathlib import Path
from typing import Any

from .embed import dot, get_embedder

_TOKEN = re.compile(r"[a-z0-9]{2,}")
_ANCHOR = re.compile(r"\[P\d+\]\s*")
_DOC_ID = re.compile(r"\b[A-Z]{3}-\d{4}\b")
_HEADER = re.compile(r"^([A-Z_]+):\s*(.*)$")
_RETRACT = re.compile(r"retract|withdrawn|withdraw(s|al)|correction|corrected|erratum", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
_TITLE_ROUTE = re.compile(r"Trade note:\s*(\w+) to (\w+),\s*(\w+)", re.I)
_ROW = re.compile(r"In (\w+) (\d{4}), the value was USD ([\d,.]+)M and the volume was ([\d,]+) tonnes", re.I)
_ROW_START = re.compile(r"^(?:\[P\d+\]\s*)?In (\w+) (\d{4})\b", re.I)

PARSER_VERSION = "4"  # bump when parsing changes so stale databases are rebuilt

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS docs(doc_id TEXT PRIMARY KEY, event_id TEXT, title TEXT, outlet TEXT, source_type TEXT,
  tier INTEGER, date TEXT, time TEXT, country TEXT, sector TEXT, format TEXT, file TEXT, body TEXT,
  notes TEXT, duplicate_of TEXT, pack TEXT DEFAULT 'base', src_hash TEXT DEFAULT '', flags TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS paras(doc_id TEXT, idx INTEGER, text TEXT, PRIMARY KEY(doc_id, idx));
CREATE VIRTUAL TABLE IF NOT EXISTS paras_fts USING fts5(text, title, doc_id UNINDEXED, idx UNINDEXED, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS relations(target TEXT, kind TEXT, by_doc TEXT, by_date TEXT, inferred INTEGER);
CREATE TABLE IF NOT EXISTS packs(pack_id TEXT PRIMARY KEY, path TEXT, event_id TEXT, name TEXT, as_of TEXT, ingested_at REAL,
  fingerprint TEXT, report TEXT);
CREATE TABLE IF NOT EXISTS quarantine(doc_id TEXT PRIMARY KEY, file TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS para_vecs(doc_id TEXT, idx INTEGER, vec BLOB, PRIMARY KEY(doc_id, idx));
CREATE TABLE IF NOT EXISTS trade_rows(doc_id TEXT, para INTEGER, route_from TEXT, route_to TEXT, product TEXT,
  year INTEGER, month INTEGER, value_usd_m REAL, volume_t REAL, quality TEXT);
"""


_STOP = frozenset("a an and are as at be been but by do does did for from had has have how i if in into is it its of on or so than that the their them then there these they this those to was were what when where which who why will with would about after before over under can could should may might not no".split())


def _tok(s: str) -> list[str]:
    return _TOKEN.findall(s.lower())


def _days(iso: str) -> int:
    return date.fromisoformat(iso).toordinal()


def _anchored(i: int, p: str) -> str:
    return p if _ANCHOR.match(p) else f"[P{i + 1}] {p}"


def _norm(s: str) -> str:
    return " ".join(_ANCHOR.sub("", s).lower().split())


def _decode_txt(raw: bytes) -> tuple[str, str]:
    """Return (text, note). Handles BOMs and files whose body switches to UTF-16 after a UTF-8 header."""
    for bom in (b"\xff\xfe", b"\xfe\xff"):
        i = raw.find(bom)
        if i == 0:
            return raw.decode("utf-16", errors="replace"), "decoded as UTF-16 (encoding repaired)"
        if i > 0:
            return (raw[:i].decode("utf-8", errors="replace") + raw[i:].decode("utf-16", errors="replace"),
                    "repaired mixed encoding: UTF-8 header + UTF-16 body")
    if b"\x00" in raw[:2000]:
        return raw.decode("utf-16-le", errors="replace"), "decoded as UTF-16LE without BOM (encoding repaired)"
    text = raw.decode("utf-8-sig", errors="replace")
    return text, ("contains undecodable bytes (encoding damaged)" if "�" in text else "")


def _split_paras(body: str) -> list[str]:
    """Paragraphs carry [P#] anchors. PDFs separate them with single newlines, TXT/DOCX with blank lines,
    so split on the anchors when present (the title line before [P1] stays its own paragraph)."""
    if re.search(r"^\[P\d+\]", body, re.M):
        parts = re.split(r"(?m)^(?=\[P\d+\])", body)
    else:
        parts = re.split(r"\n\s*\n", body)
    return [" ".join(p.split()) for p in parts if p.strip()]


SUPPORTED = {".txt", ".pdf", ".docx"}
HEADER_KEYS = {"DOC_ID", "EVENT_ID", "TITLE", "SOURCE_NAME", "SOURCE_TYPE", "RELIABILITY", "PUB_DATE", "PUB_TIME", "COUNTRY", "SECTOR", "LANGUAGE"}
_MOJIBAKE = re.compile(r"Ã[\x80-\xbf]|â€[™œ\x9d\x93\x94¦“”]|Â[\xa0-\xbf]")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "pack"


def _valid_iso(s: str) -> bool:
    try:
        date.fromisoformat(s)
        return len(s) == 10
    except ValueError:
        return False


def _split_header(text: str) -> tuple[dict[str, str], str]:
    """The metadata block at the top of every document (KEY: value lines, ending at '---' or the first prose line)."""
    header: dict[str, str] = {}
    lines, start = text.splitlines(), 0
    for i, line in enumerate(lines):
        if line.strip() == "---":
            start = i + 1
            break
        hm = _HEADER.match(line.strip())
        if hm:
            header[hm.group(1)] = hm.group(2)
        elif header and line.strip():
            start = i
            break
    return header, "\n".join(lines[start:]).strip()


def _repair_text(text: str, note: str) -> tuple[str, str]:
    """UTF-8 that was read as Windows-1252/Latin-1 ("Ã©" for "é") is repaired when the round trip is clean."""
    if len(_MOJIBAKE.findall(text)) >= 3:
        for enc in ("cp1252", "latin-1"):
            try:
                fixed = text.encode(enc).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if len(_MOJIBAKE.findall(fixed)) < len(_MOJIBAKE.findall(text)):
                return fixed, (note + "; " if note else "") + "repaired mojibake (UTF-8 read as Windows-1252)"
    return text, note


def _expected_months(title: str) -> list[tuple[int, int]]:
    """Months a trade note's title promises: 'July to December 2026' or 'January 2027', plus the same months a year earlier."""
    m = re.search(r"(\w+) to (\w+) (\d{4})\s*$", title) or re.search(r"()(\w+) (\d{4})\s*$", title)
    if not m:
        return []
    first, last, year = m.group(1).lower() or m.group(2).lower(), m.group(2).lower(), int(m.group(3))
    if first not in _MONTHS or last not in _MONTHS:
        return []
    months = range(_MONTHS[first], _MONTHS[last] + 1)
    return [(y, mo) for y in (year - 1, year) for mo in months]


def _extract(path: Path) -> tuple[str, str]:
    suf = path.suffix.lower()
    if suf == ".txt":
        return _decode_txt(path.read_bytes())
    if suf == ".pdf":
        from pypdf import PdfReader

        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages), ""
    if suf == ".docx":
        import docx

        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs), ""
    raise ValueError(f"unsupported format {suf}")


def _said_date(body: str, fixed_on: str) -> str | None:
    m = re.search(r"\bOn (\d{1,2}) (%s)\b" % "|".join(_MONTHS), body, re.I)
    if not m:
        return None
    year, month = int(fixed_on[:4]), _MONTHS[m.group(2).lower()]
    if month > int(fixed_on[5:7]):
        year -= 1
    return f"{year:04d}-{month:02d}-{int(m.group(1)):02d}"


class LocalCorpus:
    def __init__(self, pack_dir: str | Path, db_path: str | Path = ":memory:") -> None:
        self.dir = Path(pack_dir)
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.embedder = get_embedder()
        fp = self._fingerprint(self.dir)
        row = self.db.execute("SELECT fingerprint FROM packs WHERE pack_id='base'").fetchone()
        self.reused = bool(row and row["fingerprint"] == fp)
        if self.reused:
            self._load()
            self._ensure_vectors()
        else:
            self.ingest_pack(self.dir, "base", replace=True)  # loads and embeds when done

    def _migrate(self) -> None:
        """Databases written by earlier versions get the new columns in place."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(docs)")}
        for name, ddl in (("pack", "TEXT DEFAULT 'base'"), ("src_hash", "TEXT DEFAULT ''"), ("flags", "TEXT DEFAULT ''")):
            if name not in cols:
                self.db.execute(f"ALTER TABLE docs ADD COLUMN {name} {ddl}")
        self.db.commit()

    # -- ingestion --------------------------------------------------------------------
    @staticmethod
    def _pack_files(pack_dir: Path) -> dict[str, Path]:
        """Every readable document file in the pack, by file name. README/checksums/images are pack furniture, not documents."""
        root = pack_dir / "docs" if (pack_dir / "docs").is_dir() else pack_dir
        found: dict[str, Path] = {}
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix.lower() in SUPPORTED and not re.match(r"(readme|sample_question|manifest|hashes)", p.name, re.I):
                found.setdefault(p.name, p)
        return found

    def _fingerprint(self, pack_dir: Path) -> str:
        h = hashlib.sha256(PARSER_VERSION.encode())
        mf = pack_dir / "manifest.json"
        if mf.exists():
            h.update(mf.read_bytes())
        for name, p in sorted(self._pack_files(pack_dir).items()):
            h.update(name.encode() + p.read_bytes())
        return h.hexdigest()

    @staticmethod
    def _read_manifest(pack_dir: Path, report: dict[str, Any]) -> dict[str, Any]:
        mf = pack_dir / "manifest.json"
        if not mf.exists():
            report["notes"].append("no manifest.json: documents were read from their own headers")
            return {}
        try:
            data = json.loads(mf.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError) as exc:
            report["notes"].append(f"manifest.json unreadable ({type(exc).__name__}); documents were read from their own headers")
            return {}
        return {"documents": data} if isinstance(data, list) else data

    def _reconcile(self, manifest: dict[str, Any], files: dict[str, Path], report: dict[str, Any]) -> list[tuple[dict[str, Any], Path]]:
        """Pair manifest rows with files (tolerating a missing sub-folder) and pick up files the manifest forgot."""
        entries: list[tuple[dict[str, Any], Path]] = []
        used: set[str] = set()
        for m in manifest.get("documents", []):
            name = Path(str(m.get("file", ""))).name
            if name in files:
                used.add(name)
                entries.append((dict(m), files[name]))
            else:
                report["missing_files"].append(m.get("DOC_ID", name))
                report["quarantined"][m.get("DOC_ID", name)] = f"MISSING_FILE: {m.get('file', '?')} is listed in the manifest but not in the pack"
        for name, path in files.items():
            if name not in used:
                entries.append(({"file": name}, path))  # metadata comes from the document's own header
                report["off_manifest"].append(name)
        return entries

    def ingest_pack(self, pack_dir: str | Path, pack_id: str | None = None, replace: bool = False) -> dict[str, Any]:
        """Add a pack's documents to the corpus WITHOUT touching other packs. Idempotent: unchanged files are skipped.
        Returns a report of what was ingested, repaired, quarantined, flagged or skipped. Never raises on a bad document."""
        pack_dir = Path(pack_dir)
        pack_id = pack_id or _slug(pack_dir.name)
        report: dict[str, Any] = {"pack_id": pack_id, "path": str(pack_dir), "ingested": 0, "skipped_unchanged": 0, "quarantined": {},
                                  "repaired": {}, "duplicates": {}, "truncated": {}, "off_manifest": [], "missing_files": [],
                                  "id_collisions": [], "notes": []}
        started = time.time()
        manifest = self._read_manifest(pack_dir, report)
        files = self._pack_files(pack_dir)
        entries = self._reconcile(manifest, files, report)
        report["documents_in_pack"] = len(entries) + len(report["missing_files"])
        with self._lock, self.db:
            if replace:
                for r in self.db.execute("SELECT doc_id FROM docs WHERE pack=?", (pack_id,)).fetchall():
                    self._delete_doc(r["doc_id"])
            for meta, path in entries:
                try:
                    self._ingest_doc(meta, path, pack_id, report)
                except Exception as exc:  # one damaged file must never stop ingestion
                    doc_id = meta.get("DOC_ID") or path.stem
                    self.db.execute("INSERT OR REPLACE INTO quarantine VALUES (?,?,?)", (doc_id, path.name, f"{type(exc).__name__}: {exc}"))
                    report["quarantined"][doc_id] = f"{type(exc).__name__}: {exc}"
            for doc_id, reason in report["quarantined"].items():
                self.db.execute("INSERT OR REPLACE INTO quarantine VALUES (?,?,?)", (doc_id, "", reason))
            self._post_ingest()
            as_of = manifest.get("as_of") or ""
            if as_of:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('pack_as_of', ?)", (as_of,))
            self.db.execute("INSERT OR REPLACE INTO packs VALUES (?,?,?,?,?,?,?,?)",
                            (pack_id, str(pack_dir), manifest.get("event_id", ""), manifest.get("name", ""), as_of, time.time(),
                             self._fingerprint(pack_dir), "{}"))
        self._load()
        self._ensure_vectors()
        report["trade"] = self.trade_quality(pack_id)
        report["seconds"] = round(time.time() - started, 2)
        for d in self.docs.values():
            if d["pack"] == pack_id:
                if d["duplicate_of"]:
                    report["duplicates"][d["doc_id"]] = d["duplicate_of"]
                if "TRUNCATED" in (d["flags"] or ""):
                    report["truncated"][d["doc_id"]] = d["flags"]
                if d["notes"] and "repaired" in d["notes"]:
                    report["repaired"][d["doc_id"]] = d["notes"]
        with self._lock, self.db:
            self.db.execute("UPDATE packs SET report=? WHERE pack_id=?", (json.dumps(report), pack_id))
        return report

    def _delete_doc(self, doc_id: str) -> None:
        for t in ("docs", "paras", "para_vecs", "trade_rows", "quarantine"):
            self.db.execute(f"DELETE FROM {t} WHERE doc_id=?", (doc_id,))
        self.db.execute("DELETE FROM paras_fts WHERE doc_id=?", (doc_id,))

    def _ingest_doc(self, m: dict[str, Any], path: Path, pack_id: str, report: dict[str, Any]) -> None:
        raw_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        text, note = _extract(path)
        text, note = _repair_text(text, note)
        if len(text.strip()) < 40:
            raise ValueError("empty or unreadable document")
        header, body = _split_header(text)
        meta = {**{k: v for k, v in header.items() if k in HEADER_KEYS}, **{k: v for k, v in m.items() if v not in (None, "")}}
        conflicts = [k for k in ("EVENT_ID", "PUB_DATE", "RELIABILITY") if header.get(k) and str(m.get(k, header[k])) != header[k]]
        if conflicts:
            note = (note + "; " if note else "") + f"header/manifest conflict on {','.join(conflicts)}"
        doc_id = meta.get("DOC_ID")
        if not doc_id:
            raise ValueError("no document ID in the manifest or the document header")
        if not _valid_iso(str(meta.get("PUB_DATE", ""))):
            raise ValueError(f"missing or invalid publication date {meta.get('PUB_DATE')!r} (must be YYYY-MM-DD)")
        existing = self.db.execute("SELECT src_hash, pack FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
        if existing and existing["src_hash"] == raw_hash:
            report["skipped_unchanged"] += 1
            return
        if existing:
            if existing["pack"] != pack_id:
                report["id_collisions"].append(f"{doc_id}: already in pack {existing['pack']}; replaced by this pack's different content")
            self._delete_doc(doc_id)
        paras = _split_paras(body)
        fmt = meta.get("FORMAT") or path.suffix.lstrip(".").upper()
        title = meta.get("TITLE", "")
        self.db.execute(
            "INSERT INTO docs(doc_id,event_id,title,outlet,source_type,tier,date,time,country,sector,format,file,body,notes,duplicate_of,pack,src_hash,flags) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, meta.get("EVENT_ID", "NONE"), title, meta.get("SOURCE_NAME", ""), meta.get("SOURCE_TYPE", ""),
             int(meta.get("RELIABILITY") or 4), meta["PUB_DATE"], meta.get("PUB_TIME", ""), meta.get("COUNTRY", ""), meta.get("SECTOR", ""),
             fmt, path.name, body, note, None, pack_id, raw_hash, ""))
        self.db.execute("DELETE FROM quarantine WHERE doc_id=?", (doc_id,))
        for i, p in enumerate(paras):
            self.db.execute("INSERT INTO paras VALUES (?,?,?)", (doc_id, i, p))
            self.db.execute("INSERT INTO paras_fts(text,title,doc_id,idx) VALUES (?,?,?,?)", (p, title, doc_id, i))
        if meta.get("SOURCE_TYPE") == "trade_note":
            self._trade_rows(doc_id, title, paras)
        report["ingested"] += 1

    def _trade_rows(self, doc_id: str, title: str, paras: list[str]) -> None:
        """One row per month line. A row that cannot be trusted is stored WITH its reason and is never used for numbers."""
        rm = _TITLE_ROUTE.search(title)
        src, dst, product = (rm.group(1), rm.group(2), rm.group(3).lower()) if rm else ("?", "?", "?")
        seen: dict[tuple[int, int], tuple[int, float | None, float | None]] = {}
        rows: list[list[Any]] = []
        for i, p in enumerate(paras):
            ym = _ROW_START.match(p)
            if not ym:
                continue
            anchor = re.match(r"\[P(\d+)\]", p)
            row_no = int(anchor.group(1)) if anchor else i + 1  # the row number is the [P#] anchor printed in the document
            mm = _ROW.search(p)
            if not mm or mm.group(1).lower() not in _MONTHS:
                rows.append([doc_id, row_no, src, dst, product, int(ym.group(2)), _MONTHS.get(ym.group(1).lower(), 0), None, None, "UNPARSEABLE_ROW"])
                continue
            month, year = _MONTHS[mm.group(1).lower()], int(mm.group(2))
            try:
                value, vol, quality = float(mm.group(3).replace(",", "")), float(mm.group(4).replace(",", "")), "ok"
                if value <= 0 or vol <= 0:
                    quality = "BAD_NUMBER"
            except ValueError:
                value = vol = None  # type: ignore[assignment]
                quality = "BAD_NUMBER"
            if (year, month) in seen:
                quality = "DUPLICATE_ROW"
                first = seen[(year, month)]
                if (first[1], first[2]) != (value, vol):  # a repeated month with different numbers: neither can be trusted
                    rows[first[0]][9] = "CONFLICTING_ROW"
                    quality = "CONFLICTING_ROW"
            else:
                seen[(year, month)] = (len(rows), value, vol)
            rows.append([doc_id, row_no, src, dst, product, year, month, value, vol, quality])
        # a mistyped digit shows up as an implausible unit price (USD per tonne) against the table's own median
        prices = sorted(r[7] * 1e6 / r[8] for r in rows if r[9] == "ok" and r[7] and r[8])
        if len(prices) >= 4:
            med = prices[len(prices) // 2]
            for r in rows:
                if r[9] == "ok" and r[7] and r[8] and not (med / 3 <= r[7] * 1e6 / r[8] <= med * 3):
                    r[9] = "PRICE_OUTLIER"
        self.db.executemany("INSERT INTO trade_rows VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    def _post_ingest(self) -> None:
        """Whole-corpus passes after any ingest: duplicates, truncated copies, damage flags, retraction links."""
        rows = self.db.execute("SELECT doc_id,date,body,notes FROM docs").fetchall()
        norm = {r["doc_id"]: _norm(r["body"]) for r in rows}
        date_of = {r["doc_id"]: r["date"] for r in rows}
        self.db.execute("UPDATE docs SET duplicate_of=NULL, flags=''")
        flags: dict[str, set[str]] = {r["doc_id"]: set() for r in rows}
        dup: dict[str, str] = {}
        canon: dict[str, str] = {}
        for r in sorted(rows, key=lambda r: (r["date"], r["doc_id"])):
            h = hashlib.sha256(norm[r["doc_id"]].encode()).hexdigest()
            if h in canon:
                dup[r["doc_id"]] = canon[h]
            else:
                canon[h] = r["doc_id"]
        for a in rows:
            ida, na = a["doc_id"], norm[a["doc_id"]]
            if ida in dup:
                continue
            core = na.rsplit(" ", 1)[0] if len(na) >= 120 else ""
            for b in rows:
                idb = b["doc_id"]
                if idb != ida and core and len(norm[idb]) > len(na) and norm[idb].startswith(core):
                    flags[ida].add("TRUNCATED_COPY")
                    if date_of[idb] <= date_of[ida]:
                        dup[ida] = idb  # a cut-off copy of a document we already have in full
                    break
            if not re.search(r"[.!?\"'”’)\]]\s*$", a["body"].strip()):
                flags[ida].add("POSSIBLY_TRUNCATED")
            if a["body"].count("�") > 0.005 * max(len(a["body"]), 1):
                flags[ida].add("ENCODING_DAMAGED")
        for doc_id, f in flags.items():
            self.db.execute("UPDATE docs SET flags=?, duplicate_of=? WHERE doc_id=?", (",".join(sorted(f)), dup.get(doc_id), doc_id))
        self._relations()

    def trade_quality(self, pack_id: str | None = None) -> dict[str, Any]:
        """Per-table damage report: rows excluded (with why) and months the table should have but does not (a cut-off table)."""
        with self._lock:
            docs = self.db.execute("SELECT doc_id,title FROM docs WHERE source_type='trade_note'" + (" AND pack=?" if pack_id else ""),
                                   (pack_id,) if pack_id else ()).fetchall()
            out: dict[str, Any] = {"tables": len(docs), "rows_ok": 0, "rows_flagged": [], "incomplete_tables": []}
            for d in docs:
                rows = self.db.execute("SELECT para,year,month,quality FROM trade_rows WHERE doc_id=?", (d["doc_id"],)).fetchall()
                out["rows_ok"] += sum(1 for r in rows if r["quality"] == "ok")
                out["rows_flagged"] += [{"doc_id": d["doc_id"], "row": r["para"], "period": f"{r['year']}-{r['month']:02d}", "reason": r["quality"]}
                                        for r in rows if r["quality"] != "ok"]
                have = {(r["year"], r["month"]) for r in rows if r["quality"] == "ok"}
                missing = [f"{y}-{m:02d}" for y, m in _expected_months(d["title"]) if (y, m) not in have]
                if missing:
                    out["incomplete_tables"].append({"doc_id": d["doc_id"], "missing_months": missing})
            return out

    def is_current(self, pack_dir: str | Path, pack_id: str) -> bool:
        """True if this pack was already ingested and its files have not changed since."""
        with self._lock:
            row = self.db.execute("SELECT fingerprint FROM packs WHERE pack_id=?", (pack_id,)).fetchone()
        return bool(row and row["fingerprint"] == self._fingerprint(Path(pack_dir)))

    def list_packs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM packs ORDER BY ingested_at").fetchall()
            counts = {r["pack"]: r["n"] for r in self.db.execute("SELECT pack, COUNT(*) AS n FROM docs GROUP BY pack")}
        return [{"pack_id": r["pack_id"], "event_id": r["event_id"], "name": r["name"], "as_of": r["as_of"], "ingested_at": r["ingested_at"],
                 "documents": counts.get(r["pack_id"], 0), "report": json.loads(r["report"] or "{}")} for r in rows]

    def _relations(self) -> None:
        """A correction/retraction names its targets by doc ID or (as in E5) only by the date it states.
        Unnamed targets are inferred (same event, up to 3 days earlier, shared title words) and labelled as inferred."""
        stop = {"correction", "the", "was", "not", "for", "and", "retraction", "retracted", "of", "to", "a", "in"}
        rows = self.db.execute("SELECT doc_id,event_id,title,source_type,date,body FROM docs").fetchall()
        for d in rows:
            if not (d["source_type"] == "correction" or _RETRACT.search(d["title"])):
                continue
            kind = "retracted_by" if re.search(r"retract|withdraw|removed|should not be relied", d["body"], re.I) else "corrected_by"
            named = set(_DOC_ID.findall(d["body"])) - {d["doc_id"]}
            inferred = 0
            if not named:
                words = set(_tok(d["title"])) - stop
                said = _said_date(d["body"], d["date"])
                named = {t["doc_id"] for t in rows
                         if t["doc_id"] != d["doc_id"] and t["event_id"] == d["event_id"] and t["source_type"] != "correction"
                         and 0 <= _days(d["date"]) - _days(t["date"]) <= 3
                         and len(words & (set(_tok(t["title"])) - stop)) >= (2 if said else 3)
                         and (said is None or t["date"] == said)}
                inferred = 1
            for target in named:
                self.db.execute("INSERT INTO relations VALUES (?,?,?,?,?)", (target, kind, d["doc_id"], d["date"], inferred))

    def _load(self) -> None:
        with self._lock:
            self.docs: dict[str, dict[str, Any]] = {}
            for r in self.db.execute("SELECT * FROM docs"):
                d = dict(r)
                d.update(tier=int(d["tier"]), retracted_by=[], corrected_by=[],
                         paras=[x["text"] for x in self.db.execute("SELECT text FROM paras WHERE doc_id=? ORDER BY idx", (d["doc_id"],))])
                self.docs[d["doc_id"]] = d
            for r in self.db.execute("SELECT * FROM relations"):
                if r["target"] in self.docs:
                    self.docs[r["target"]][r["kind"]].append({"doc_id": r["by_doc"], "date": r["by_date"], "inferred": bool(r["inferred"])})
            self.quarantine = {r["doc_id"]: r["reason"] for r in self.db.execute("SELECT * FROM quarantine")}
            row = self.db.execute("SELECT v FROM meta WHERE k='pack_as_of'").fetchone()
            self.pack_as_of = row["v"] if row else ""

    # -- semantic vectors --------------------------------------------------------------
    def _ensure_vectors(self) -> None:
        """Embed each paragraph once (title + text). Only paragraphs without a vector are embedded, so ingesting a live pack
        costs seconds, not a full re-embed. Everything is re-embedded only if the embedder itself changed."""
        with self._lock:
            row = self.db.execute("SELECT v FROM meta WHERE k='embed_backend'").fetchone()
            if not (row and row["v"] == self.embedder.name):
                self.db.execute("DELETE FROM para_vecs")
            have = {(r["doc_id"], r["idx"]) for r in self.db.execute("SELECT doc_id, idx FROM para_vecs")}
            want = {(d["doc_id"], i) for d in self.docs.values() for i in range(len(d["paras"]))}
            keys = sorted(want - have)
            if keys:
                texts = [f"{self.docs[k[0]]['title']}. {_ANCHOR.sub('', self.docs[k[0]]['paras'][k[1]])}"[:1500] for k in keys]
                vecs = self.embedder.encode_docs(texts)
                with self.db:
                    self.db.executemany("INSERT OR REPLACE INTO para_vecs VALUES (?,?,?)", [(k[0], k[1], v.tobytes()) for k, v in zip(keys, vecs)])
            with self.db:
                for k in have - want:  # paragraphs of documents that were replaced or removed
                    self.db.execute("DELETE FROM para_vecs WHERE doc_id=? AND idx=?", k)
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('embed_backend', ?)", (self.embedder.name,))
            self._vecs: list[tuple[str, int, array]] = []
            for r in self.db.execute("SELECT doc_id, idx, vec FROM para_vecs"):
                a = array("f")
                a.frombytes(r["vec"])
                self._vecs.append((r["doc_id"], r["idx"], a))

    def stats(self) -> dict[str, Any]:
        with self._lock:
            q = lambda s: self.db.execute(s).fetchone()[0]  # noqa: E731
            return {"documents": q("SELECT COUNT(*) FROM docs"), "paragraphs": q("SELECT COUNT(*) FROM paras"),
                    "trade_rows_ok": q("SELECT COUNT(*) FROM trade_rows WHERE quality='ok'"),
                    "trade_rows_flagged": q("SELECT COUNT(*) FROM trade_rows WHERE quality!='ok'"),
                    "relations": q("SELECT COUNT(*) FROM relations"), "quarantined": len(self.quarantine),
                    "duplicates": q("SELECT COUNT(*) FROM docs WHERE duplicate_of IS NOT NULL"),
                    "encoding_repairs": q("SELECT COUNT(*) FROM docs WHERE notes LIKE '%repaired%' OR notes LIKE '%encoding%'"),
                    "truncated": q("SELECT COUNT(*) FROM docs WHERE flags LIKE '%TRUNCATED%'"), "packs": q("SELECT COUNT(*) FROM packs"),
                    "embeddings": len(self._vecs), "embed_backend": self.embedder.name, "reused_existing_db": self.reused}

    # -- helpers ------------------------------------------------------------------------
    def _status(self, d: dict[str, Any], as_of: str) -> dict[str, Any]:
        ret = [r for r in d["retracted_by"] if r["date"] <= as_of]
        cor = [r for r in d["corrected_by"] if r["date"] <= as_of]
        flags = (["RETRACTED"] if ret else []) + (["CORRECTED"] if cor else []) + (["LOW_RELIABILITY_TIER4"] if d["tier"] >= 4 else []) \
            + ([f"DUPLICATE_OF_{d['duplicate_of']}"] if d["duplicate_of"] else []) \
            + (["ENCODING_REPAIRED"] if "repaired" in (d["notes"] or "") or "encoding" in (d["notes"] or "") else []) \
            + [f for f in (d.get("flags") or "").split(",") if f]
        return {"flags": flags, "retracted_by": ret, "corrected_by": cor}

    def _meta(self, d: dict[str, Any], as_of: str) -> dict[str, Any]:
        return {"doc_id": d["doc_id"], "title": d["title"], "outlet": d["outlet"], "source_type": d["source_type"], "tier": d["tier"],
                "date": d["date"], "time": d["time"], "event_id": d["event_id"], "country": d["country"], "sector": d["sector"], "pack": d.get("pack", "base"),
                **self._status(d, as_of)}

    # -- tool dispatch ----------------------------------------------------------------
    def call(self, name: str, args: dict[str, Any]) -> Any:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            raise ValueError(f"unknown tool {name}")
        with self._lock:
            return fn(**args)

    # -- API helpers (not agent tools: they do not apply the as-of firewall, the UI shows every record) ----------
    def list_documents(self, country: str = "", sector: str = "", source_type: str = "", tier: int | None = None,
                       prefix: str = "", text: str = "", as_of: str = "", limit: int = 200, offset: int = 0,
                       date_from: str = "", date_to: str = "", exclude_type: str = "") -> dict[str, Any]:
        as_of = as_of or self.pack_as_of
        hits: dict[str, float] | None = None
        matched: dict[str, list[str]] = {}
        if text.strip():
            r = self.t_search_documents(text, "9999-12-31", limit=40)
            hits = {x["doc_id"]: x["score"] for x in r["results"]}
            matched = {x["doc_id"]: x["matched_by"] for x in r["results"]}
        out = []
        for d in sorted(self.docs.values(), key=lambda d: d["doc_id"]):
            if country and d["country"].lower() != country.lower() or sector and d["sector"].lower() != sector.lower():
                continue
            if source_type and d["source_type"] != source_type or tier is not None and d["tier"] != tier or prefix and not d["doc_id"].startswith(prefix):
                continue
            if hits is not None and d["doc_id"] not in hits:
                continue
            if date_from and d["date"] < date_from or date_to and d["date"] > date_to or exclude_type and d["source_type"] == exclude_type:
                continue
            out.append({**self._meta(d, as_of), "format": d["format"], "after_as_of": d["date"] > as_of, "duplicate_of": d["duplicate_of"],
                        "notes": d["notes"], "matched_by": matched.get(d["doc_id"], [])})
        if hits is not None:
            out.sort(key=lambda d: -hits[d["doc_id"]])
        return {"total": len(out), "documents": out[offset: offset + max(1, min(limit, 500))], "quarantined": self.quarantine}

    def trade_series(self, as_of: str = "") -> list[dict[str, Any]]:
        """Monthly trade rows per trade note (damaged rows excluded), for the charts. Same numbers the agents see."""
        as_of = as_of or self.pack_as_of
        out = []
        for d in sorted(self.docs.values(), key=lambda d: d["doc_id"]):
            if d["source_type"] != "trade_note" or d["date"] > as_of or d["duplicate_of"]:
                continue
            t = self.t_read_trade_table(d["doc_id"], as_of)
            if "error" in t:
                continue
            rows = [r for r in t["rows"] if r["quality"] == "ok"]
            m = _TITLE_ROUTE.search(d["title"])
            out.append({"doc_id": d["doc_id"], "title": d["title"], "from": m.group(1) if m else "?", "to": m.group(2) if m else "?",
                        "product": m.group(3).lower() if m else "?", "tier": d["tier"], "date": d["date"], "outlet": d["outlet"],
                        "rows": rows, "damaged_rows": len(t["rows"]) - len(rows), "flags": self._status(d, as_of)["flags"]})
        return out

    def get_document(self, doc_id: str) -> dict[str, Any] | None:
        d = self.docs.get(doc_id)
        if not d:
            return None
        return {**self._meta(d, self.pack_as_of), "format": d["format"], "after_as_of": d["date"] > self.pack_as_of, "notes": d["notes"],
                "duplicate_of": d["duplicate_of"], "paragraphs": [_anchored(i, p) for i, p in enumerate(d["paras"])],
                "trade_rows": [dict(r) for r in self.db.execute("SELECT para AS row, year, month, value_usd_m, volume_t, quality FROM trade_rows WHERE doc_id=? ORDER BY year, month", (doc_id,))]}

    def feed(self, as_of: str = "", limit: int = 60) -> list[dict[str, Any]]:
        as_of = as_of or self.pack_as_of
        docs = [d for d in self.docs.values() if d["date"] <= as_of and d["source_type"] != "atlas" and not d["duplicate_of"]]
        docs.sort(key=lambda d: (d["date"], d["time"]))
        return [{**self._meta(d, as_of), "time": d["time"], "headline": d["title"]} for d in docs[-limit:]]

    def t_search_documents(self, query: str, as_of: str, country: str = "", sector: str = "", event_id: str = "",
                           source_type: str = "", tier_max: int | None = None, date_from: str = "", date_to: str = "",
                           limit: int = 6, mode: str = "hybrid") -> dict[str, Any]:
        """Hybrid search: BM25 (FTS5, stemmed) and dense cosine similarity, merged with reciprocal-rank fusion.
        Filters (as-of, country, sector, event, type, tier, dates) apply to both sides. mode: hybrid | lexical | semantic."""
        memo: dict[str, bool] = {}

        def ok(doc_id: str) -> bool:
            if doc_id not in memo:
                d = self.docs[doc_id]
                memo[doc_id] = not (
                    d["date"] > as_of or d["duplicate_of"]
                    or (country and d["country"].lower() != country.lower()) or (sector and d["sector"].lower() != sector.lower())
                    or (event_id and d["event_id"].lower() != event_id.lower()) or (source_type and d["source_type"].lower() != source_type.lower())
                    or (tier_max is not None and d["tier"] > int(tier_max)) or (date_from and d["date"] < date_from) or (date_to and d["date"] > date_to))
            return memo[doc_id]

        toks = [t for t in dict.fromkeys(_tok(query)) if t not in _STOP] or list(dict.fromkeys(_tok(query)))  # drop filler words
        if not toks:
            return {"results": [], "note": "empty query"}
        lex: dict[str, tuple[float, int]] = {}
        if mode in ("hybrid", "lexical"):
            match = " OR ".join(f'"{t}"' for t in toks)
            for r in self.db.execute("SELECT doc_id, idx, bm25(paras_fts) AS s FROM paras_fts WHERE paras_fts MATCH ? ORDER BY s LIMIT 400", (match,)):
                if ok(r["doc_id"]) and -r["s"] > lex.get(r["doc_id"], (-1e9, 0))[0]:
                    lex[r["doc_id"]] = (-r["s"], r["idx"])
        sem: dict[str, tuple[float, int]] = {}
        if mode in ("hybrid", "semantic") and self._vecs:
            q = self.embedder.encode_query(query)
            for doc_id, idx, vec in self._vecs:
                if ok(doc_id):
                    c = dot(q, vec)
                    if c >= self.embedder.min_score and c > sem.get(doc_id, (-1.0, 0))[0]:
                        sem[doc_id] = (c, idx)
            if sem:  # keep only hits close to the best one: in a single-topic corpus the absolute floor alone lets almost everything through
                top = max(v[0] for v in sem.values())
                sem = {d: v for d, v in sem.items() if v[0] >= top - self.embedder.margin}
            sem = dict(sorted(sem.items(), key=lambda kv: -kv[1][0])[:30])
        lex_rank = {d: i for i, (d, _) in enumerate(sorted(lex.items(), key=lambda kv: -kv[1][0]))}
        sem_rank = {d: i for i, (d, _) in enumerate(sorted(sem.items(), key=lambda kv: -kv[1][0]))}
        fused = {d: (1 / (60 + lex_rank[d]) if d in lex_rank else 0) + (1 / (60 + sem_rank[d]) if d in sem_rank else 0) for d in set(lex) | set(sem)}
        ranked = sorted(fused.items(), key=lambda kv: -kv[1])[: max(1, min(int(limit or 6), 40))]
        out = []
        for doc_id, score in ranked:
            idx = lex[doc_id][1] if doc_id in lex else sem[doc_id][1]
            out.append({**self._meta(self.docs[doc_id], as_of), "score": round(score * 100, 2),
                        "matched_by": [n for n, hit in (("lexical", doc_id in lex), ("semantic", doc_id in sem)) if hit],
                        "semantic": round(sem[doc_id][0], 3) if doc_id in sem else None,
                        "snippet": _anchored(idx, self.docs[doc_id]["paras"][idx])[:320]})
        return {"results": out, "total_matches": len(fused), "search": self.embedder.name if sem or mode != "lexical" else "lexical"}

    def t_read_document(self, doc_id: str, as_of: str) -> dict[str, Any]:
        d = self.docs.get(doc_id)
        if not d:
            return {"error": "NOT_FOUND", "detail": self.quarantine.get(doc_id, "no such document in the manifest")}
        if d["date"] > as_of:
            return {"error": "AFTER_AS_OF", "detail": f"{doc_id} is dated after the as-of date and cannot be used"}
        text = "\n\n".join(_anchored(i, p) for i, p in enumerate(d["paras"]))
        return {**self._meta(d, as_of), "text": text[:6000]}

    def _route(self, d: dict[str, Any]) -> dict[str, str]:
        m = _TITLE_ROUTE.search(d["title"])
        return {"route": f"{m.group(1)} -> {m.group(2)}", "product": m.group(3).lower()} if m else {}

    def t_list_trade_tables(self, as_of: str, country: str = "", sector: str = "", event_id: str = "") -> dict[str, Any]:
        out = []
        for d in self.docs.values():
            if d["source_type"] != "trade_note" or d["date"] > as_of or d["duplicate_of"]:
                continue
            hay = (d["title"] + " " + d["country"]).lower()
            if country and country.lower() not in hay or sector and sector.lower() not in hay:
                continue
            if event_id and d["event_id"].lower() != event_id.lower():
                continue
            out.append({**self._meta(d, as_of), **self._route(d)})
        return {"results": out}

    def t_read_trade_table(self, table_id: str, as_of: str) -> dict[str, Any]:
        d = self.docs.get(table_id)
        if not d or d["source_type"] != "trade_note":
            return {"error": "NOT_FOUND", "detail": "no trade note with that ID"}
        if d["date"] > as_of:
            return {"error": "AFTER_AS_OF", "detail": f"{table_id} is dated after the as-of date and cannot be used"}
        rows = [{"row": r["para"], "period": f"{r['year']}-{r['month']:02d}", "value_usd_m": r["value_usd_m"],
                 "volume_t": r["volume_t"], "quality": r["quality"]}
                for r in self.db.execute("SELECT * FROM trade_rows WHERE doc_id=? ORDER BY year, month", (table_id,))]
        bad = [r for r in rows if r["quality"] != "ok"]
        have = {(int(r["period"][:4]), int(r["period"][5:])) for r in rows if r["quality"] == "ok"}
        missing = [f"{y}-{m:02d}" for y, m in _expected_months(d["title"]) if (y, m) not in have]
        warn = ([f"{len(bad)} damaged row(s) flagged ({', '.join(sorted({r['quality'] for r in bad}))}); do NOT use them"] if bad else []) \
            + ([f"table is incomplete: no usable row for {', '.join(missing)}; do not extrapolate them"] if missing else [])
        return {**self._meta(d, as_of), **self._route(d), "rows": rows, "missing_months": missing,
                "warning": "; ".join(warn) or None,
                "cite_as": "table ID + row number (the [P#] anchor of the row)"}

    def t_trade_stats(self, table_id: str, as_of: str) -> dict[str, Any]:
        """Year-on-year change per month from the table's own rows (same month, previous year). Damaged rows excluded."""
        t = self.t_read_trade_table(table_id, as_of)
        if "error" in t:
            return t
        ok = {tuple(map(int, r["period"].split("-"))): r for r in t["rows"] if r["quality"] == "ok"}
        months = []
        for (y, m), r in sorted(ok.items()):
            prev = ok.get((y - 1, m))
            if prev and prev["volume_t"] and prev["value_usd_m"]:
                months.append({"period": r["period"], "row": r["row"], "prior_row": prev["row"],
                               "volume_t": r["volume_t"], "prior_volume_t": prev["volume_t"],
                               "volume_change_pct": round(100 * (r["volume_t"] / prev["volume_t"] - 1), 1),
                               "value_change_pct": round(100 * (r["value_usd_m"] / prev["value_usd_m"] - 1), 1)})
        last3 = months[-3:]
        avg = lambda k: round(sum(x[k] for x in last3) / len(last3), 1) if last3 else None  # noqa: E731
        return {"table_id": table_id, "route": t.get("route"), "product": t.get("product"), "yoy": months,
                "last3_avg_volume_change_pct": avg("volume_change_pct"), "last3_avg_value_change_pct": avg("value_change_pct"),
                "excluded_damaged_rows": [r["row"] for r in t["rows"] if r["quality"] != "ok"]}

    def t_import_summary(self, country: str, product: str, as_of: str) -> dict[str, Any]:
        """Net exposure anchor: a country's TOTAL imports of a product from every origin in the trade tables, month by month and
        year on year, with the loss on affected routes and the gain on substitute routes broken out. Damaged rows are excluded and a
        month missing a usable row on any origin is left out of the totals (marked incomplete) instead of faking a drop."""
        tables = []
        for d in self.docs.values():
            if d["source_type"] != "trade_note" or d["date"] > as_of or d["duplicate_of"]:
                continue
            m = _TITLE_ROUTE.search(d["title"])
            if m and m.group(2).lower() == country.lower() and m.group(3).lower() == product.lower():
                tables.append((d, m.group(1)))
        if not tables:
            return {"error": "NO_TABLES", "detail": f"no trade table into {country} for {product} dated on or before {as_of}"}
        cells: dict[str, dict[tuple[int, int], dict[str, Any]]] = {}
        sources: dict[str, dict[str, Any]] = {}
        for d, origin in sorted(tables, key=lambda t: (t[0]["date"], t[0]["doc_id"])):  # a later table for the same origin wins
            sources[origin] = {"origin": origin, "table_id": d["doc_id"], "tier": d["tier"], "date": d["date"]}
            for r in self.db.execute("SELECT para,year,month,value_usd_m,volume_t FROM trade_rows WHERE doc_id=? AND quality='ok'", (d["doc_id"],)):
                cells.setdefault(origin, {})[(r["year"], r["month"])] = {"row": r["para"], "vol": r["volume_t"], "val": r["value_usd_m"], "table": d["doc_id"]}
        origins = sorted(cells)
        latest_year = max(y for o in cells.values() for y, _ in o)
        months = sorted({(y, m) for o in cells.values() for y, m in o if y == latest_year})
        out_months, excluded = [], []
        for y, m in months:
            per, ok = {}, True
            for o in origins:
                cur, prev = cells[o].get((y, m)), cells[o].get((y - 1, m))
                if not cur or not prev:
                    ok = False
                    excluded.append(f"{y}-{m:02d}: {o} has no usable row for {'this month' if not cur else 'the same month last year'}")
                    break
                per[o] = {"volume_t": cur["vol"], "prior_volume_t": prev["vol"], "value_usd_m": cur["val"], "prior_value_usd_m": prev["val"],
                          "change_pct": round(100 * (cur["vol"] / prev["vol"] - 1), 1), "value_change_pct": round(100 * (cur["val"] / prev["val"] - 1), 1),
                          "row": cur["row"], "prior_row": prev["row"], "table_id": cur["table"]}
            if not ok:
                out_months.append({"period": f"{y}-{m:02d}", "complete": False})
                continue
            tv, pv = sum(x["volume_t"] for x in per.values()), sum(x["prior_volume_t"] for x in per.values())
            tval, pval = sum(x["value_usd_m"] for x in per.values()), sum(x["prior_value_usd_m"] for x in per.values())
            out_months.append({"period": f"{y}-{m:02d}", "complete": True, "total_volume_t": tv, "prior_volume_t": pv,
                               "volume_change_pct": round(100 * (tv / pv - 1), 1), "total_value_usd_m": round(tval, 1), "prior_value_usd_m": round(pval, 1),
                               "value_change_pct": round(100 * (tval / pval - 1), 1), "by_origin": per})
        full = [m for m in out_months if m["complete"]]
        # the break: the first month in which any origin's volume is 25% or more below last year
        brk = next((m for m in full if any(v["change_pct"] <= -25 for v in m["by_origin"].values())), None)

        def window(ms: list[dict[str, Any]]) -> dict[str, Any] | None:
            if not ms:
                return None
            tv, pv = sum(m["total_volume_t"] for m in ms), sum(m["prior_volume_t"] for m in ms)
            tval, pval = sum(m["total_value_usd_m"] for m in ms), sum(m["prior_value_usd_m"] for m in ms)
            lost: dict[str, float] = {}
            gained: dict[str, float] = {}
            for o in origins:
                delta = sum(m["by_origin"][o]["volume_t"] - m["by_origin"][o]["prior_volume_t"] for m in ms)
                (lost if delta < 0 else gained)[o] = round(delta)
            gross = -sum(lost.values())
            return {"from": ms[0]["period"], "to": ms[-1]["period"], "months": len(ms), "net_volume_change_pct": round(100 * (tv / pv - 1), 1),
                    "net_value_change_pct": round(100 * (tval / pval - 1), 1), "net_volume_change_t": round(tv - pv),
                    "lost_volume_t_by_origin": lost, "gained_volume_t_by_origin": gained,
                    "substitution_offset_pct_of_loss": round(100 * sum(gained.values()) / gross, 1) if gross else None}

        return {"country": country, "product": product.lower(), "latest_year": latest_year, "origins": list(sources.values()), "months": out_months,
                "windows": {"whole_period": window(full), "since_break": window([m for m in full if brk and m["period"] >= brk["period"]]),
                            "last_3_months": window(full[-3:])},
                "break_month": brk["period"] if brk else None, "excluded": excluded,
                "how_to_cite": "cite each origin's table ID and row number from by_origin (row, prior_row)",
                "note": "net = change in TOTAL imports from all origins; the loss on affected routes and the substitution on others are in lost/gained"}

    def t_get_reference(self, kind: str, name: str = "") -> dict[str, Any]:
        q = _tok(f"{kind} {name}")
        hits = [d for d in self.docs.values() if d["source_type"] == "atlas"
                and sum(t in _tok(d["title"] + " " + d["body"][:400]) for t in q) >= max(1, len(q) // 2)][:3]
        return {"results": [{"doc_id": d["doc_id"], "title": d["title"], "tier": d["tier"], "text": d["body"][:3000]} for d in hits]}

    def t_list_events(self, as_of: str) -> dict[str, Any]:
        ev = Counter(d["event_id"] for d in self.docs.values() if d["date"] <= as_of)
        names = {r["event_id"]: r["name"] for r in self.db.execute("SELECT event_id, name FROM packs WHERE event_id != ''")}
        return {"results": [{"event_id": e, "name": names.get(e, ""), "documents": n,
                             "note": "background, no event" if e == "NONE" else ""} for e, n in ev.items()]}

    def t_event_profile(self, event_id: str, as_of: str) -> dict[str, Any]:
        docs = [d for d in self.docs.values() if d["event_id"].lower() == event_id.lower()]
        vis = [d for d in docs if d["date"] <= as_of]
        fixed = [{"doc_id": d["doc_id"], **self._status(d, as_of)} for d in vis
                 if self._status(d, as_of)["retracted_by"] or self._status(d, as_of)["corrected_by"]]
        return {"event_id": event_id, "documents_visible": len(vis), "documents_after_as_of": len(docs) - len(vis),
                "by_tier": dict(Counter(d["tier"] for d in vis)), "by_type": dict(Counter(d["source_type"] for d in vis)),
                "tier1_or_2_sources": [d["doc_id"] for d in vis if d["tier"] <= 2][:30], "retracted_or_corrected": fixed,
                "low_reliability_only": [d["doc_id"] for d in vis if d["tier"] == 4],
                "duplicates": [d["doc_id"] for d in vis if d["duplicate_of"]],
                "encoding_repaired": [d["doc_id"] for d in vis if "encoding" in (d["notes"] or "")],
                "quarantined": self.quarantine}

    def t_verify_citation(self, doc_id: str, as_of: str, quote: str = "", event_id: str = "") -> dict[str, Any]:
        d = self.docs.get(doc_id)
        if not d:
            return {"valid": False, "reason": "doc_id not in manifest" + (f" (quarantined: {self.quarantine[doc_id]})" if doc_id in self.quarantine else "")}
        if d["date"] > as_of:
            return {"valid": False, "reason": f"published {d['date']}, after as-of {as_of}"}
        st = self._status(d, as_of)
        if st["retracted_by"]:
            return {"valid": False, "reason": f"retracted by {st['retracted_by'][0]['doc_id']} on {st['retracted_by'][0]['date']}"}
        if event_id and d["event_id"] not in (event_id, "NONE"):
            return {"valid": False, "reason": f"document belongs to event {d['event_id']}, not {event_id}"}
        if not quote.strip():
            return {"valid": False, "reason": "an exact quote is required"}
        nq, nb = _norm(quote), _norm(d["body"] + " " + d["title"])
        if nq not in nb:
            qt, bt = nq.split(), nb.split()
            matched = sum(b.size for b in SequenceMatcher(None, qt, bt, autojunk=False).get_matching_blocks())
            if not qt or matched / len(qt) < 0.9:
                return {"valid": False, "reason": "quote not found in document"}
        return {"valid": True, "date": d["date"], "time": d["time"], "tier": d["tier"], "flags": st["flags"]}

    def t_compute_exposure(self, route_share: float, severity: float, duration_months: float, reroute_offset: float) -> dict[str, Any]:
        """exposure % = 100 x route_share x severity x min(duration/3, 1) x (1 - reroute_offset); inputs are fractions in [0,1]."""
        for n, v in (("route_share", route_share), ("severity", severity), ("reroute_offset", reroute_offset)):
            if not 0 <= v <= 1:
                return {"error": "BAD_INPUT", "detail": f"{n} must be between 0 and 1"}
        pct = 100 * route_share * severity * min(max(duration_months, 0) / 3, 1) * (1 - reroute_offset)
        return {"exposure_pct": round(pct, 2), "formula": "100*route_share*severity*min(months/3,1)*(1-reroute_offset)"}

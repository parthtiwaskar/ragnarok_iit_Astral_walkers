"""Typed accessors over the Runemark store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.store.db import Database


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def _l(v: str | None) -> list[Any]:
    if not v:
        return []
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else []
    except ValueError:
        return []


def _d(v: str | None) -> dict[str, Any]:
    if not v:
        return {}
    try:
        out = json.loads(v)
        return out if isinstance(out, dict) else {}
    except ValueError:
        return {}


@dataclass
class DocRecord:
    doc_id: str
    title: str | None = None
    outlet: str | None = None
    tier: int | None = None
    pub_date: str | None = None
    source_type: str | None = None
    event_id: str | None = None
    is_background: bool = False
    countries: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    format: str | None = None
    status: str = "OK"  # OK | REPAIRED | PARTIAL | DUPLICATE | QUARANTINED | MISSING
    status_reason: str | None = None
    flags: list[str] = field(default_factory=list)
    header: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    content_hash: str | None = None
    file_path: str | None = None
    pack: str | None = None
    off_manifest: bool = False
    canonical_id: str | None = None
    ingest_seq: int = 0
    ingested_at: str | None = None

    @property
    def usable(self) -> bool:
        return self.status not in ("QUARANTINED", "MISSING")

    @staticmethod
    def from_row(r: sqlite3.Row) -> DocRecord:
        return DocRecord(
            doc_id=r["doc_id"],
            title=r["title"],
            outlet=r["outlet"],
            tier=r["tier"],
            pub_date=r["pub_date"],
            source_type=r["source_type"],
            event_id=r["event_id"],
            is_background=bool(r["is_background"]),
            countries=_l(r["countries"]),
            sectors=_l(r["sectors"]),
            format=r["format"],
            status=r["status"],
            status_reason=r["status_reason"],
            flags=_l(r["flags"]),
            header=_d(r["header"]),
            body=r["body"] or "",
            content_hash=r["content_hash"],
            file_path=r["file_path"],
            pack=r["pack"],
            off_manifest=bool(r["off_manifest"]),
            canonical_id=r["canonical_id"],
            ingest_seq=r["ingest_seq"] or 0,
            ingested_at=r["ingested_at"],
        )

    def summary(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("body")
        d.pop("header")
        return d


@dataclass
class TradeTableRecord:
    table_id: str
    exporter: str | None
    importer: str | None
    sector: str | None
    unit: str | None
    period_start: str | None
    period_end: str | None
    event_id: str | None
    is_background: bool
    columns: list[str]
    header: dict[str, Any]
    file_path: str | None
    status: str
    flags: list[str]
    pack: str | None
    ingest_seq: int
    published: str | None = None  # publication date of the source document (trade notes)
    source_doc: str | None = None  # the document the table was read from, if any

    @staticmethod
    def from_row(r: sqlite3.Row) -> TradeTableRecord:
        return TradeTableRecord(
            table_id=r["table_id"],
            exporter=r["exporter"],
            importer=r["importer"],
            sector=r["sector"],
            unit=r["unit"],
            period_start=r["period_start"],
            period_end=r["period_end"],
            event_id=r["event_id"],
            is_background=bool(r["is_background"]),
            columns=_l(r["columns"]),
            header=_d(r["header"]),
            file_path=r["file_path"],
            status=r["status"],
            flags=_l(r["flags"]),
            pack=r["pack"],
            ingest_seq=r["ingest_seq"] or 0,
            published=r["published"],
            source_doc=r["source_doc"],
        )


@dataclass
class TradeRowRecord:
    table_id: str
    row: int | None
    month: str | None
    value: float | None
    volume: float | None
    quality: str
    raw_line: str
    reason: str

    @property
    def ref(self) -> str:
        return f"{self.table_id}#r{(self.row or 0):02d}"


@dataclass
class RelationRecord:
    src_doc: str
    dst: str
    kind: str
    pub_date: str | None


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------- meta ----------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        r = self.db.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        with self.db.write_lock, self.db.connect() as c:
            c.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def current_ingest_seq(self) -> int:
        return int(self.get_meta("ingest_seq", "0") or 0)

    def next_ingest_seq(self) -> int:
        with self.db.write_lock:
            n = self.current_ingest_seq() + 1
            self.set_meta("ingest_seq", str(n))
            return n

    # ---------- files ----------
    def get_file(self, path: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM files WHERE path=?", (path,))

    def upsert_file(
        self,
        path: str,
        content_hash: str,
        kind: str,
        fmt: str | None,
        status: str,
        reason: str | None,
        ref: str | None,
        pack: str | None,
        size: int,
        ingest_seq: int,
    ) -> None:
        with self.db.write_lock, self.db.connect() as c:
            c.execute(
                "INSERT INTO files(path,content_hash,kind,format,status,reason,ref,pack,size,ingested_at,ingest_seq) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET content_hash=excluded.content_hash,"
                "kind=excluded.kind,format=excluded.format,status=excluded.status,reason=excluded.reason,"
                "ref=excluded.ref,pack=excluded.pack,size=excluded.size,ingested_at=excluded.ingested_at,"
                "ingest_seq=excluded.ingest_seq",
                (path, content_hash, kind, fmt, status, reason, ref, pack, size, _now(), ingest_seq),
            )

    def files(self) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM files ORDER BY path")

    # ---------- manifest ----------
    def upsert_manifest(self, entries: list[dict[str, Any]]) -> None:
        with self.db.write_lock, self.db.connect() as c:
            for e in entries:
                c.execute(
                    "INSERT INTO manifest(doc_id,file,title,outlet,pub_date,tier,event_id,format,word_count,size,pack,raw)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(doc_id) DO UPDATE SET file=excluded.file,"
                    "title=excluded.title,outlet=excluded.outlet,pub_date=excluded.pub_date,tier=excluded.tier,"
                    "event_id=excluded.event_id,format=excluded.format,word_count=excluded.word_count,"
                    "size=excluded.size,pack=excluded.pack,raw=excluded.raw",
                    (
                        e["doc_id"],
                        e.get("file"),
                        e.get("title"),
                        e.get("outlet"),
                        e.get("pub_date"),
                        e.get("tier"),
                        e.get("event_id"),
                        e.get("format"),
                        e.get("word_count"),
                        e.get("size"),
                        e.get("pack"),
                        _j(e.get("raw", {})),
                    ),
                )

    def manifest_get(self, doc_id: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM manifest WHERE doc_id=?", (doc_id,))

    def manifest_all(self) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM manifest ORDER BY doc_id")

    def has_manifest(self) -> bool:
        r = self.db.one("SELECT COUNT(*) AS n FROM manifest")
        return bool(r and r["n"])

    # ---------- docs ----------
    def upsert_doc(self, d: DocRecord) -> None:
        with self.db.write_lock, self.db.connect() as c:
            c.execute(
                "INSERT INTO docs(doc_id,title,outlet,tier,pub_date,source_type,event_id,is_background,countries,"
                "sectors,format,status,status_reason,flags,header,body,content_hash,file_path,pack,off_manifest,"
                "canonical_id,ingest_seq,ingested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(doc_id) DO UPDATE SET title=excluded.title,outlet=excluded.outlet,tier=excluded.tier,"
                "pub_date=excluded.pub_date,source_type=excluded.source_type,event_id=excluded.event_id,"
                "is_background=excluded.is_background,countries=excluded.countries,sectors=excluded.sectors,"
                "format=excluded.format,status=excluded.status,status_reason=excluded.status_reason,"
                "flags=excluded.flags,header=excluded.header,body=excluded.body,content_hash=excluded.content_hash,"
                "file_path=excluded.file_path,pack=excluded.pack,off_manifest=excluded.off_manifest,"
                "canonical_id=excluded.canonical_id,ingest_seq=excluded.ingest_seq,ingested_at=excluded.ingested_at",
                (
                    d.doc_id,
                    d.title,
                    d.outlet,
                    d.tier,
                    d.pub_date,
                    d.source_type,
                    d.event_id,
                    int(d.is_background),
                    _j(d.countries),
                    _j(d.sectors),
                    d.format,
                    d.status,
                    d.status_reason,
                    _j(d.flags),
                    _j(d.header),
                    d.body,
                    d.content_hash,
                    d.file_path,
                    d.pack,
                    int(d.off_manifest),
                    d.canonical_id,
                    d.ingest_seq,
                    d.ingested_at or _now(),
                ),
            )

    def get_doc(self, doc_id: str) -> DocRecord | None:
        r = self.db.one("SELECT * FROM docs WHERE doc_id=?", (doc_id,))
        return DocRecord.from_row(r) if r else None

    def docs(self, where: str = "1=1", params: tuple[object, ...] = ()) -> list[DocRecord]:
        return [
            DocRecord.from_row(r)
            for r in self.db.query(f"SELECT * FROM docs WHERE {where} ORDER BY doc_id", params)
        ]

    def doc_by_hash(self, content_hash: str, exclude: str) -> DocRecord | None:
        r = self.db.one(
            "SELECT * FROM docs WHERE content_hash=? AND doc_id<>? AND status NOT IN ('DUPLICATE','QUARANTINED','MISSING') "
            "ORDER BY pub_date, doc_id LIMIT 1",
            (content_hash, exclude),
        )
        return DocRecord.from_row(r) if r else None

    def set_doc_flags(self, doc_id: str, flags: list[str]) -> None:
        with self.db.write_lock, self.db.connect() as c:
            c.execute("UPDATE docs SET flags=? WHERE doc_id=?", (_j(sorted(set(flags))), doc_id))

    # ---------- chunks ----------
    def replace_chunks(
        self, doc_id: str, chunks: list[tuple[str, int, str, int, int]], fts_texts: list[str]
    ) -> None:
        with self.db.write_lock, self.db.connect() as c:
            old = [r["chunk_id"] for r in c.execute("SELECT chunk_id FROM chunks WHERE doc_id=?", (doc_id,))]
            for cid in old:
                c.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (cid,))
            c.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            for (cid, seq, text, start, end), fts in zip(chunks, fts_texts, strict=True):
                c.execute(
                    'INSERT INTO chunks(chunk_id,doc_id,seq,text,start,"end") VALUES(?,?,?,?,?,?)',
                    (cid, doc_id, seq, text, start, end),
                )
                c.execute("INSERT INTO chunks_fts(text,chunk_id) VALUES(?,?)", (fts, cid))

    def chunk(self, chunk_id: str) -> sqlite3.Row | None:
        return self.db.one("SELECT * FROM chunks WHERE chunk_id=?", (chunk_id,))

    def all_chunks(self) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT c.chunk_id, c.text, d.title FROM chunks c JOIN docs d ON d.doc_id=c.doc_id "
            "ORDER BY c.chunk_id"
        )

    # ---------- trade ----------
    def upsert_trade_table(self, t: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        with self.db.write_lock, self.db.connect() as c:
            c.execute(
                "INSERT INTO trade_tables(table_id,exporter,importer,sector,unit,period_start,period_end,event_id,"
                "is_background,columns,header,file_path,status,flags,pack,ingest_seq,published,source_doc) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(table_id) DO UPDATE SET "
                "exporter=excluded.exporter,importer=excluded.importer,sector=excluded.sector,unit=excluded.unit,"
                "period_start=excluded.period_start,period_end=excluded.period_end,event_id=excluded.event_id,"
                "is_background=excluded.is_background,columns=excluded.columns,header=excluded.header,"
                "file_path=excluded.file_path,status=excluded.status,flags=excluded.flags,pack=excluded.pack,"
                "ingest_seq=excluded.ingest_seq,published=excluded.published,source_doc=excluded.source_doc",
                (
                    t["table_id"],
                    t.get("exporter"),
                    t.get("importer"),
                    t.get("sector"),
                    t.get("unit"),
                    t.get("period_start"),
                    t.get("period_end"),
                    t.get("event_id"),
                    int(bool(t.get("is_background"))),
                    _j(t.get("columns", [])),
                    _j(t.get("header", {})),
                    t.get("file_path"),
                    t.get("status", "OK"),
                    _j(t.get("flags", [])),
                    t.get("pack"),
                    t.get("ingest_seq", 0),
                    t.get("published"),
                    t.get("source_doc"),
                ),
            )
            c.execute("DELETE FROM trade_rows WHERE table_id=?", (t["table_id"],))
            for r in rows:
                c.execute(
                    "INSERT INTO trade_rows(table_id,row,month,value,volume,quality,raw_line,reason) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        t["table_id"],
                        r.get("row"),
                        r.get("month"),
                        r.get("value"),
                        r.get("volume"),
                        r["quality"],
                        r.get("raw_line", ""),
                        r.get("reason", ""),
                    ),
                )

    def delete_trade_table(self, table_id: str) -> None:
        with self.db.write_lock, self.db.connect() as c:
            c.execute("DELETE FROM trade_rows WHERE table_id=?", (table_id,))
            c.execute("DELETE FROM trade_tables WHERE table_id=?", (table_id,))

    def get_trade_table(self, table_id: str) -> TradeTableRecord | None:
        r = self.db.one("SELECT * FROM trade_tables WHERE table_id=?", (table_id,))
        return TradeTableRecord.from_row(r) if r else None

    def trade_tables(self) -> list[TradeTableRecord]:
        return [
            TradeTableRecord.from_row(r)
            for r in self.db.query("SELECT * FROM trade_tables ORDER BY table_id")
        ]

    def trade_rows(self, table_id: str) -> list[TradeRowRecord]:
        return [
            TradeRowRecord(
                r["table_id"],
                r["row"],
                r["month"],
                r["value"],
                r["volume"],
                r["quality"],
                r["raw_line"] or "",
                r["reason"] or "",
            )
            for r in self.db.query("SELECT * FROM trade_rows WHERE table_id=? ORDER BY id", (table_id,))
        ]

    def trade_row(self, table_id: str, row: int) -> TradeRowRecord | None:
        """The citable row: the first valid occurrence of that row number, else the first occurrence."""
        rows = [r for r in self.trade_rows(table_id) if r.row == row]
        ok = [r for r in rows if r.quality == "OK"]
        return (ok or rows)[0] if rows else None

    # ---------- relations ----------
    def add_relation(self, src: str, dst: str, kind: str, pub_date: str | None) -> None:
        with self.db.write_lock, self.db.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO relations(src_doc,dst,kind,pub_date) VALUES(?,?,?,?)",
                (src, dst, kind, pub_date),
            )

    def relations_to(self, dst: str) -> list[RelationRecord]:
        return [
            RelationRecord(r["src_doc"], r["dst"], r["kind"], r["pub_date"])
            for r in self.db.query("SELECT * FROM relations WHERE dst=?", (dst,))
        ]

    def relations_from(self, src: str) -> list[RelationRecord]:
        return [
            RelationRecord(r["src_doc"], r["dst"], r["kind"], r["pub_date"])
            for r in self.db.query("SELECT * FROM relations WHERE src_doc=?", (src,))
        ]

    def clear_relations_from(self, src: str) -> None:
        with self.db.write_lock, self.db.connect() as c:
            c.execute("DELETE FROM relations WHERE src_doc=?", (src,))

    # ---------- reference ----------
    def upsert_reference(self, sheet: str, rows: list[dict[str, str]]) -> None:
        with self.db.write_lock, self.db.connect() as c:
            for i, row in enumerate(rows):
                key = row.get("name") or row.get("code") or row.get(next(iter(row), "")) or str(i)
                c.execute(
                    "INSERT OR REPLACE INTO reference(sheet,key,data) VALUES(?,?,?)", (sheet, key, _j(row))
                )

    def reference(self, sheet: str | None = None, key: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT sheet, key, data FROM reference WHERE 1=1"
        params: list[object] = []
        if sheet:
            sql += " AND sheet=?"
            params.append(sheet)
        if key:
            sql += " AND lower(key)=lower(?)"
            params.append(key)
        return [
            dict(_d(r["data"]), _sheet=r["sheet"])
            for r in self.db.query(sql + " ORDER BY sheet, key", params)
        ]

    def reference_sheets(self) -> list[str]:
        return [r["sheet"] for r in self.db.query("SELECT DISTINCT sheet FROM reference ORDER BY sheet")]

    # ---------- injections ----------
    def set_injections(self, doc_id: str, hits: list[tuple[int, str]]) -> None:
        with self.db.write_lock, self.db.connect() as c:
            c.execute("DELETE FROM injections WHERE doc_id=?", (doc_id,))
            for start, span in hits:
                c.execute(
                    "INSERT OR REPLACE INTO injections(doc_id,start,span) VALUES(?,?,?)",
                    (doc_id, start, span),
                )

    def injections(self, doc_id: str | None = None) -> list[dict[str, Any]]:
        if doc_id:
            rows = self.db.query("SELECT * FROM injections WHERE doc_id=? ORDER BY start", (doc_id,))
        else:
            rows = self.db.query("SELECT * FROM injections ORDER BY doc_id, start")
        return [{"doc_id": r["doc_id"], "start": r["start"], "span": r["span"]} for r in rows]

    # ---------- issues ----------
    def add_issue(self, file: str, ref: str | None, status: str, reason: str) -> None:
        with self.db.write_lock, self.db.connect() as c:
            c.execute(
                "INSERT INTO ingest_issues(file,ref,status,reason,ts) VALUES(?,?,?,?,?)",
                (file, ref, status, reason, _now()),
            )

    def issues(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM ingest_issues ORDER BY id")]

    # ---------- index version ----------
    def index_version(self) -> str:
        rows = self.db.query("SELECT doc_id, content_hash FROM docs ORDER BY doc_id")
        h = hashlib.sha256()
        for r in rows:
            h.update(f"{r['doc_id']}:{r['content_hash'] or ''}\n".encode())
        for t in self.db.query("SELECT table_id, file_path FROM trade_tables ORDER BY table_id"):
            h.update(f"T:{t['table_id']}\n".encode())
        return h.hexdigest()[:16]

    def counts(self) -> dict[str, int]:
        def n(sql: str) -> int:
            r = self.db.one(sql)
            return int(r[0]) if r else 0

        return {
            "docs": n("SELECT COUNT(*) FROM docs WHERE status NOT IN ('QUARANTINED','MISSING')"),
            "docs_total": n("SELECT COUNT(*) FROM docs"),
            "chunks": n("SELECT COUNT(*) FROM chunks"),
            "trade_tables": n("SELECT COUNT(*) FROM trade_tables"),
            "trade_rows": n("SELECT COUNT(*) FROM trade_rows"),
            "manifest": n("SELECT COUNT(*) FROM manifest"),
        }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")

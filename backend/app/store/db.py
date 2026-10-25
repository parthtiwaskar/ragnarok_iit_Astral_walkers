"""SQLite schema and connection management (one connection per thread, WAL mode)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY, content_hash TEXT, kind TEXT, format TEXT, status TEXT, reason TEXT,
  ref TEXT, pack TEXT, size INTEGER, ingested_at TEXT, ingest_seq INTEGER);
CREATE TABLE IF NOT EXISTS manifest(
  doc_id TEXT PRIMARY KEY, file TEXT, title TEXT, outlet TEXT, pub_date TEXT, tier INTEGER,
  event_id TEXT, format TEXT, word_count INTEGER, size INTEGER, pack TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS docs(
  doc_id TEXT PRIMARY KEY, title TEXT, outlet TEXT, tier INTEGER, pub_date TEXT, source_type TEXT,
  event_id TEXT, is_background INTEGER DEFAULT 0, countries TEXT DEFAULT '[]', sectors TEXT DEFAULT '[]',
  format TEXT, status TEXT, status_reason TEXT, flags TEXT DEFAULT '[]', header TEXT DEFAULT '{}',
  body TEXT DEFAULT '', content_hash TEXT, file_path TEXT, pack TEXT, off_manifest INTEGER DEFAULT 0,
  canonical_id TEXT, ingest_seq INTEGER DEFAULT 0, ingested_at TEXT);
CREATE INDEX IF NOT EXISTS docs_event ON docs(event_id);
CREATE INDEX IF NOT EXISTS docs_date ON docs(pub_date);
CREATE TABLE IF NOT EXISTS chunks(
  chunk_id TEXT PRIMARY KEY, doc_id TEXT, seq INTEGER, text TEXT, start INTEGER, "end" INTEGER);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, chunk_id UNINDEXED, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS trade_tables(
  table_id TEXT PRIMARY KEY, exporter TEXT, importer TEXT, sector TEXT, unit TEXT, period_start TEXT,
  period_end TEXT, event_id TEXT, is_background INTEGER DEFAULT 0, columns TEXT, header TEXT,
  file_path TEXT, status TEXT, flags TEXT DEFAULT '[]', pack TEXT, ingest_seq INTEGER DEFAULT 0,
  published TEXT, source_doc TEXT);
CREATE TABLE IF NOT EXISTS trade_rows(
  id INTEGER PRIMARY KEY AUTOINCREMENT, table_id TEXT, row INTEGER, month TEXT, value REAL, volume REAL,
  quality TEXT, raw_line TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS trade_rows_tbl ON trade_rows(table_id, row);
CREATE TABLE IF NOT EXISTS relations(src_doc TEXT, dst TEXT, kind TEXT, pub_date TEXT,
  PRIMARY KEY(src_doc, dst, kind));
CREATE TABLE IF NOT EXISTS reference(sheet TEXT, key TEXT, data TEXT, PRIMARY KEY(sheet, key));
CREATE TABLE IF NOT EXISTS injections(doc_id TEXT, start INTEGER, span TEXT, PRIMARY KEY(doc_id, start));
CREATE TABLE IF NOT EXISTS ingest_issues(
  id INTEGER PRIMARY KEY AUTOINCREMENT, file TEXT, ref TEXT, status TEXT, reason TEXT, ts TEXT);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.write_lock = threading.RLock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_tables)")}
            for col in ("published", "source_doc"):  # migrate indexes built before trade notes existed
                if col not in cols:
                    conn.execute(f"ALTER TABLE trade_tables ADD COLUMN {col} TEXT")

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=OFF")
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def query(self, sql: str, params: tuple[object, ...] | list[object] = ()) -> list[sqlite3.Row]:
        return list(self._conn().execute(sql, params).fetchall())

    def one(self, sql: str, params: tuple[object, ...] | list[object] = ()) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn().execute(sql, params).fetchone()
        return row

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

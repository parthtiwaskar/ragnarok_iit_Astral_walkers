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

PARSER_VERSION = "2"  # bump when parsing changes so stale databases are rebuilt

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS docs(doc_id TEXT PRIMARY KEY, event_id TEXT, title TEXT, outlet TEXT, source_type TEXT,
  tier INTEGER, date TEXT, time TEXT, country TEXT, sector TEXT, format TEXT, file TEXT, body TEXT,
  notes TEXT, duplicate_of TEXT);
CREATE TABLE IF NOT EXISTS paras(doc_id TEXT, idx INTEGER, text TEXT, PRIMARY KEY(doc_id, idx));
CREATE VIRTUAL TABLE IF NOT EXISTS paras_fts USING fts5(text, title, doc_id UNINDEXED, idx UNINDEXED, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS relations(target TEXT, kind TEXT, by_doc TEXT, by_date TEXT, inferred INTEGER);
CREATE TABLE IF NOT EXISTS quarantine(doc_id TEXT PRIMARY KEY, file TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS para_vecs(doc_id TEXT, idx INTEGER, vec BLOB, PRIMARY KEY(doc_id, idx));
CREATE TABLE IF NOT EXISTS trade_rows(doc_id TEXT, para INTEGER, route_from TEXT, route_to TEXT, product TEXT,
  year INTEGER, month INTEGER, value_usd_m REAL, volume_t REAL, quality TEXT);
"""


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
        fp = self._fingerprint()
        row = self.db.execute("SELECT v FROM meta WHERE k='fingerprint'").fetchone()
        self.reused = bool(row and row["v"] == fp)
        if not self.reused:
            self._ingest(fp)
        self._load()
        self.embedder = get_embedder()
        self._ensure_vectors()

    # -- ingestion --------------------------------------------------------------------
    def _fingerprint(self) -> str:
        h = hashlib.sha256(PARSER_VERSION.encode() + (self.dir / "manifest.json").read_bytes())
        for p in sorted((self.dir / "docs").glob("*")):
            h.update(p.name.encode() + p.read_bytes())
        return h.hexdigest()

    def _ingest(self, fp: str) -> None:
        manifest = json.loads((self.dir / "manifest.json").read_text(encoding="utf-8"))
        with self._lock, self.db:
            for t in ("docs", "paras", "paras_fts", "relations", "quarantine", "trade_rows", "meta"):
                self.db.execute(f"DELETE FROM {t}")
            seen_body: dict[str, str] = {}
            for m in manifest["documents"]:
                try:
                    self._ingest_doc(m, seen_body)
                except Exception as exc:  # one damaged file must never stop ingestion
                    self.db.execute("INSERT OR REPLACE INTO quarantine VALUES (?,?,?)",
                                    (m.get("DOC_ID", "?"), m.get("file", ""), f"{type(exc).__name__}: {exc}"))
            self._relations()
            self.db.execute("INSERT INTO meta VALUES ('fingerprint', ?)", (fp,))
            self.db.execute("INSERT INTO meta VALUES ('pack_as_of', ?)", (manifest.get("as_of", ""),))

    def _ingest_doc(self, m: dict[str, Any], seen_body: dict[str, str]) -> None:
        path = self.dir / "docs" / m["file"]
        text, note = _extract(path)
        if len(text.strip()) < 40:
            raise ValueError("empty or unreadable document")
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
        body = "\n".join(lines[start:]).strip()
        conflicts = [k for k in ("EVENT_ID", "PUB_DATE", "RELIABILITY") if header.get(k) and str(m.get(k)) != header[k]]
        if conflicts:
            note = (note + "; " if note else "") + f"header/manifest conflict on {','.join(conflicts)}"
        paras = _split_paras(body)
        digest = hashlib.sha256(_norm(body).encode()).hexdigest()
        dup = seen_body.get(digest)
        seen_body.setdefault(digest, m["DOC_ID"])
        fmt = m.get("FORMAT") or path.suffix.lstrip(".").upper()
        self.db.execute(
            "INSERT INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m["DOC_ID"], m.get("EVENT_ID", "NONE"), m.get("TITLE", ""), m.get("SOURCE_NAME", ""), m.get("SOURCE_TYPE", ""),
             int(m.get("RELIABILITY") or 4), m["PUB_DATE"], m.get("PUB_TIME", ""), m.get("COUNTRY", ""), m.get("SECTOR", ""),
             fmt, m["file"], body, note, dup))
        for i, p in enumerate(paras):
            self.db.execute("INSERT INTO paras VALUES (?,?,?)", (m["DOC_ID"], i, p))
            self.db.execute("INSERT INTO paras_fts(text,title,doc_id,idx) VALUES (?,?,?,?)", (p, m.get("TITLE", ""), m["DOC_ID"], i))
        if m.get("SOURCE_TYPE") == "trade_note":
            self._trade_rows(m["DOC_ID"], m.get("TITLE", ""), paras)

    def _trade_rows(self, doc_id: str, title: str, paras: list[str]) -> None:
        rm = _TITLE_ROUTE.search(title)
        src, dst, product = (rm.group(1), rm.group(2), rm.group(3).lower()) if rm else ("?", "?", "?")
        seen: set[tuple[int, int]] = set()
        for i, p in enumerate(paras):
            ym = _ROW_START.match(p)
            if not ym:
                continue
            mm = _ROW.search(p)
            if not mm or mm.group(1).lower() not in _MONTHS:
                self.db.execute("INSERT INTO trade_rows VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (doc_id, i + 1, src, dst, product, int(ym.group(2)), _MONTHS.get(ym.group(1).lower(), 0),
                                 None, None, "UNPARSEABLE_ROW"))
                continue
            month, year = _MONTHS[mm.group(1).lower()], int(mm.group(2))
            try:
                value, vol = float(mm.group(3).replace(",", "")), float(mm.group(4).replace(",", ""))
                quality = "ok"
            except ValueError:
                value = vol = None  # type: ignore[assignment]
                quality = "BAD_NUMBER"
            if (year, month) in seen:
                quality = "DUPLICATE_ROW"
            seen.add((year, month))
            self.db.execute("INSERT INTO trade_rows VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (doc_id, i + 1, src, dst, product, year, month, value, vol, quality))

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
        """Embed every paragraph once (title + text). Re-embeds if the embedder changed or the pack was re-ingested."""
        with self._lock:
            row = self.db.execute("SELECT v FROM meta WHERE k='embed_backend'").fetchone()
            have = self.db.execute("SELECT COUNT(*) FROM para_vecs").fetchone()[0]
            total = self.db.execute("SELECT COUNT(*) FROM paras").fetchone()[0]
            if not (row and row["v"] == self.embedder.name and have == total and total):
                keys, texts = [], []
                for d in self.docs.values():
                    for i, p in enumerate(d["paras"]):
                        keys.append((d["doc_id"], i))
                        texts.append(f"{d['title']}. {_ANCHOR.sub('', p)}"[:1500])
                vecs = self.embedder.encode_docs(texts) if texts else []
                with self.db:
                    self.db.execute("DELETE FROM para_vecs")
                    self.db.executemany("INSERT INTO para_vecs VALUES (?,?,?)", [(k[0], k[1], v.tobytes()) for k, v in zip(keys, vecs)])
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
                    "encoding_repairs": q("SELECT COUNT(*) FROM docs WHERE notes LIKE '%encoding%'"),
                    "embeddings": len(self._vecs), "embed_backend": self.embedder.name, "reused_existing_db": self.reused}

    # -- helpers ------------------------------------------------------------------------
    def _status(self, d: dict[str, Any], as_of: str) -> dict[str, Any]:
        ret = [r for r in d["retracted_by"] if r["date"] <= as_of]
        cor = [r for r in d["corrected_by"] if r["date"] <= as_of]
        flags = (["RETRACTED"] if ret else []) + (["CORRECTED"] if cor else []) + (["LOW_RELIABILITY_TIER4"] if d["tier"] >= 4 else []) \
            + ([f"DUPLICATE_OF_{d['duplicate_of']}"] if d["duplicate_of"] else []) \
            + (["ENCODING_REPAIRED"] if "encoding" in (d["notes"] or "") else [])
        return {"flags": flags, "retracted_by": ret, "corrected_by": cor}

    def _meta(self, d: dict[str, Any], as_of: str) -> dict[str, Any]:
        return {"doc_id": d["doc_id"], "title": d["title"], "outlet": d["outlet"], "source_type": d["source_type"], "tier": d["tier"],
                "date": d["date"], "event_id": d["event_id"], "country": d["country"], "sector": d["sector"], **self._status(d, as_of)}

    # -- tool dispatch ----------------------------------------------------------------
    def call(self, name: str, args: dict[str, Any]) -> Any:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            raise ValueError(f"unknown tool {name}")
        with self._lock:
            return fn(**args)

    # -- API helpers (not agent tools: they do not apply the as-of firewall, the UI shows every record) ----------
    def list_documents(self, country: str = "", sector: str = "", source_type: str = "", tier: int | None = None,
                       prefix: str = "", text: str = "", as_of: str = "", limit: int = 200, offset: int = 0) -> dict[str, Any]:
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
            out.append({**self._meta(d, as_of), "format": d["format"], "after_as_of": d["date"] > as_of, "duplicate_of": d["duplicate_of"],
                        "notes": d["notes"], "matched_by": matched.get(d["doc_id"], [])})
        if hits is not None:
            out.sort(key=lambda d: -hits[d["doc_id"]])
        return {"total": len(out), "documents": out[offset: offset + max(1, min(limit, 500))], "quarantined": self.quarantine}

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

        toks = list(dict.fromkeys(_tok(query)))
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
        return {**self._meta(d, as_of), **self._route(d), "rows": rows,
                "warning": f"{len(bad)} damaged row(s) flagged; do NOT use them" if bad else None,
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

    def t_get_reference(self, kind: str, name: str = "") -> dict[str, Any]:
        q = _tok(f"{kind} {name}")
        hits = [d for d in self.docs.values() if d["source_type"] == "atlas"
                and sum(t in _tok(d["title"] + " " + d["body"][:400]) for t in q) >= max(1, len(q) // 2)][:3]
        return {"results": [{"doc_id": d["doc_id"], "title": d["title"], "tier": d["tier"], "text": d["body"][:3000]} for d in hits]}

    def t_list_events(self, as_of: str) -> dict[str, Any]:
        ev = Counter(d["event_id"] for d in self.docs.values() if d["date"] <= as_of)
        return {"results": [{"event_id": e, "documents": n, "note": "background, no event" if e == "NONE" else ""} for e, n in ev.items()]}

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
        return {"valid": True, "date": d["date"], "tier": d["tier"], "flags": st["flags"]}

    def t_compute_exposure(self, route_share: float, severity: float, duration_months: float, reroute_offset: float) -> dict[str, Any]:
        """exposure % = 100 x route_share x severity x min(duration/3, 1) x (1 - reroute_offset); inputs are fractions in [0,1]."""
        for n, v in (("route_share", route_share), ("severity", severity), ("reroute_offset", reroute_offset)):
            if not 0 <= v <= 1:
                return {"error": "BAD_INPUT", "detail": f"{n} must be between 0 and 1"}
        pct = 100 * route_share * severity * min(max(duration_months, 0) / 3, 1) * (1 - reroute_offset)
        return {"exposure_pct": round(pct, 2), "formula": "100*route_share*severity*min(months/3,1)*(1-reroute_offset)"}

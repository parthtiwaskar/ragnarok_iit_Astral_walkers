"""Store: cases, runs, claims, rulings and the event log, in the same SQLite file as the corpus.

One database, two groups of tables:
  corpus  (written by corpus.py): meta, docs, paras, paras_fts, relations, quarantine, trade_rows
  cases   (written here):         cases, runs, claims, evidence, attacks, responses, rulings, ranges, events

`save_case` takes a CaseFile.to_dict() and rewrites that case's rows in one transaction; `load_case` returns the
same shape, so the API, the dashboard and case.json all speak one format. Per-agent memory stays in its own private
files (by design: agents share only the Case File).
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases(
  case_id TEXT PRIMARY KEY, question TEXT NOT NULL, as_of TEXT NOT NULL, event_id TEXT, event_summary TEXT,
  verified INTEGER, verdict_reason TEXT, unit TEXT, status TEXT NOT NULL, status_reasons TEXT,
  scope TEXT, pool TEXT, red_flags TEXT, briefing TEXT, briefing_md TEXT, provider TEXT,
  created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS runs(case_id TEXT, run INTEGER, data TEXT, PRIMARY KEY(case_id, run));
CREATE TABLE IF NOT EXISTS claims(
  case_id TEXT, claim_id TEXT, run INTEGER, side TEXT, kind TEXT, text TEXT, confidence REAL, status TEXT,
  est_country TEXT, est_sector TEXT, est_low REAL, est_high REAL, est_unit TEXT, PRIMARY KEY(case_id, claim_id));
CREATE TABLE IF NOT EXISTS evidence(
  case_id TEXT, claim_id TEXT, doc_id TEXT, quote TEXT, locator TEXT, date TEXT, tier INTEGER);
CREATE TABLE IF NOT EXISTS attacks(
  case_id TEXT, attack_id TEXT, run INTEGER, by_side TEXT, target_claim_id TEXT, kind TEXT, text TEXT,
  counter_evidence TEXT, PRIMARY KEY(case_id, attack_id));
CREATE TABLE IF NOT EXISTS responses(
  case_id TEXT, attack_id TEXT, by_side TEXT, action TEXT, text TEXT, evidence TEXT, revised_estimate TEXT, note TEXT,
  PRIMARY KEY(case_id, attack_id));
CREATE TABLE IF NOT EXISTS rulings(
  case_id TEXT, claim_id TEXT, run INTEGER, basis TEXT, status TEXT, rationale TEXT, PRIMARY KEY(case_id, claim_id));
CREATE TABLE IF NOT EXISTS ranges(
  case_id TEXT, country TEXT, sector TEXT, low REAL, high REAL, rationale TEXT, claim_ids TEXT, flags TEXT,
  PRIMARY KEY(case_id, country, sector));
CREATE TABLE IF NOT EXISTS events(
  case_id TEXT, seq INTEGER, ts REAL, type TEXT, agent TEXT, run INTEGER, phase TEXT, data TEXT, PRIMARY KEY(case_id, seq));
CREATE INDEX IF NOT EXISTS idx_evidence_case ON evidence(case_id, claim_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(case_id, type);
"""

_J = lambda v: json.dumps(v, default=str)  # noqa: E731


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # -- writes -----------------------------------------------------------------------
    def save_case(self, c: dict[str, Any], provider: str = "") -> None:
        cid, now = c["case_id"], time.time()
        with self._lock, self.db:
            old = self.db.execute("SELECT created_at, provider FROM cases WHERE case_id=?", (cid,)).fetchone()
            for t in ("runs", "claims", "evidence", "attacks", "responses", "rulings", "ranges"):
                self.db.execute(f"DELETE FROM {t} WHERE case_id=?", (cid,))
            self.db.execute(
                "INSERT OR REPLACE INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, c["question"], c["as_of"], c.get("event_id", ""), c.get("event_summary", ""),
                 None if c.get("verified") is None else int(bool(c["verified"])), c.get("verdict_reason", ""), c.get("unit", ""),
                 c.get("status", "OPEN"), _J(c.get("status_reasons", [])), _J(c.get("scope", [])), _J(c.get("pool", [])),
                 _J(c.get("red_flags", [])), _J(c.get("briefing", {})), c.get("briefing_md", ""),
                 provider or (old["provider"] if old else ""), old["created_at"] if old else now, now))
            for r in c.get("runs", []):
                self.db.execute("INSERT INTO runs VALUES (?,?,?)", (cid, r["run"], _J(r)))
            for cl in c.get("claims", {}).values():
                est = cl.get("estimate") or {}
                self.db.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (cid, cl["id"], cl["run"], cl["side"], cl["kind"], cl["text"], cl.get("confidence"), cl.get("status", "live"),
                                 est.get("country"), est.get("sector"), est.get("low"), est.get("high"), est.get("unit")))
                for e in cl.get("evidence", []):
                    self.db.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?)",
                                    (cid, cl["id"], e["doc_id"], e.get("quote", ""), e.get("locator", ""), e.get("date", ""), e.get("tier")))
            for a in c.get("attacks", {}).values():
                self.db.execute("INSERT INTO attacks VALUES (?,?,?,?,?,?,?,?)",
                                (cid, a["id"], a["run"], a["by"], a["target_claim_id"], a.get("kind", ""), a["text"], _J(a.get("counter_evidence", []))))
            for r in c.get("responses", {}).values():
                self.db.execute("INSERT INTO responses VALUES (?,?,?,?,?,?,?,?)",
                                (cid, r["attack_id"], r["by"], r["action"], r.get("text", ""), _J(r.get("evidence", [])),
                                 _J(r.get("revised_estimate")), r.get("note", "")))
            for r in c.get("rulings", {}).values():
                self.db.execute("INSERT INTO rulings VALUES (?,?,?,?,?,?)",
                                (cid, r["claim_id"], r["run"], r["basis"], r["status"], r["rationale"]))
            for g in c.get("ranges", []):
                self.db.execute("INSERT OR REPLACE INTO ranges VALUES (?,?,?,?,?,?,?,?)",
                                (cid, g["country"], g["sector"], g.get("low"), g.get("high"), g.get("rationale", ""),
                                 _J(g.get("claim_ids", [])), _J(g.get("flags", []))))

    def add_event(self, case_id: str, ev: dict[str, Any]) -> None:
        extra = {k: v for k, v in ev.items() if k not in ("seq", "ts", "type", "agent", "run", "phase")}
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?)",
                            (case_id, ev["seq"], ev["ts"], ev["type"], ev.get("agent"), ev.get("run"), ev.get("phase"), _J(extra)))

    def delete_case(self, case_id: str) -> None:
        with self._lock, self.db:
            for t in ("cases", "runs", "claims", "evidence", "attacks", "responses", "rulings", "ranges", "events"):
                self.db.execute(f"DELETE FROM {t} WHERE case_id=?", (case_id,))

    def import_run_dir(self, path: str | Path, provider: str = "imported") -> str | None:
        """Load runs/<case>/case.json (+ events.jsonl) into the database. Idempotent."""
        p = Path(path)
        if not (p / "case.json").exists():
            return None
        case = json.loads((p / "case.json").read_text(encoding="utf-8"))
        if not case.get("claims"):
            return None  # aborted before any evidence was gathered: not worth keeping
        case["question"] = re.sub(r"^\s*E\d+ \(as of [\d-]+\)\s*", "", case["question"]).strip()
        self.save_case(case, provider)
        if (p / "events.jsonl").exists():
            with self._lock, self.db:
                self.db.execute("DELETE FROM events WHERE case_id=?", (case["case_id"],))
            for line in (p / "events.jsonl").read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.add_event(case["case_id"], json.loads(line))
        return case["case_id"]

    # -- reads ------------------------------------------------------------------------
    def list_cases(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT c.case_id, c.question, c.as_of, c.status, c.event_id, c.provider, c.created_at, c.updated_at, "
                "(SELECT COUNT(*) FROM runs r WHERE r.case_id=c.case_id) AS runs, "
                "(SELECT COUNT(*) FROM claims k WHERE k.case_id=c.case_id) AS claims, "
                "(SELECT COUNT(*) FROM rulings g WHERE g.case_id=c.case_id) AS rulings, c.scope "
                "FROM cases c ORDER BY c.updated_at DESC").fetchall()
        return [{**dict(r), "scope": json.loads(r["scope"] or "[]")} for r in rows]

    def load_case(self, case_id: str) -> dict[str, Any] | None:
        with self._lock:
            c = self.db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
            if not c:
                return None
            ev: dict[str, list[dict[str, Any]]] = {}
            for e in self.db.execute("SELECT * FROM evidence WHERE case_id=?", (case_id,)):
                ev.setdefault(e["claim_id"], []).append({"doc_id": e["doc_id"], "quote": e["quote"], "locator": e["locator"], "date": e["date"], "tier": e["tier"]})
            claims = {}
            for k in self.db.execute("SELECT * FROM claims WHERE case_id=? ORDER BY run, claim_id", (case_id,)):
                est = ({"country": k["est_country"], "sector": k["est_sector"], "low": k["est_low"], "high": k["est_high"], "unit": k["est_unit"]}
                       if k["est_country"] else None)
                claims[k["claim_id"]] = {"id": k["claim_id"], "side": k["side"], "run": k["run"], "text": k["text"], "kind": k["kind"],
                                         "evidence": ev.get(k["claim_id"], []), "estimate": est, "confidence": k["confidence"], "status": k["status"]}
            attacks = {a["attack_id"]: {"id": a["attack_id"], "run": a["run"], "by": a["by_side"], "target_claim_id": a["target_claim_id"],
                                        "text": a["text"], "kind": a["kind"], "counter_evidence": json.loads(a["counter_evidence"] or "[]")}
                       for a in self.db.execute("SELECT * FROM attacks WHERE case_id=?", (case_id,))}
            responses = {r["attack_id"]: {"attack_id": r["attack_id"], "by": r["by_side"], "action": r["action"], "text": r["text"],
                                          "evidence": json.loads(r["evidence"] or "[]"), "revised_estimate": json.loads(r["revised_estimate"] or "null"),
                                          "note": r["note"]} for r in self.db.execute("SELECT * FROM responses WHERE case_id=?", (case_id,))}
            rulings = {r["claim_id"]: {"claim_id": r["claim_id"], "run": r["run"], "basis": r["basis"], "status": r["status"], "rationale": r["rationale"]}
                       for r in self.db.execute("SELECT * FROM rulings WHERE case_id=?", (case_id,))}
            ranges = [{"country": g["country"], "sector": g["sector"], "low": g["low"], "high": g["high"], "rationale": g["rationale"],
                       "claim_ids": json.loads(g["claim_ids"] or "[]"), "flags": json.loads(g["flags"] or "[]")}
                      for g in self.db.execute("SELECT * FROM ranges WHERE case_id=?", (case_id,))]
            runs = [json.loads(r["data"]) for r in self.db.execute("SELECT data FROM runs WHERE case_id=? ORDER BY run", (case_id,))]
        return {"case_id": c["case_id"], "question": c["question"], "as_of": c["as_of"], "event_id": c["event_id"],
                "event_summary": c["event_summary"], "verified": None if c["verified"] is None else bool(c["verified"]),
                "verdict_reason": c["verdict_reason"], "unit": c["unit"], "scope": json.loads(c["scope"] or "[]"),
                "pool": json.loads(c["pool"] or "[]"), "red_flags": json.loads(c["red_flags"] or "[]"), "claims": claims,
                "attacks": attacks, "responses": responses, "rulings": rulings, "ranges": ranges, "runs": runs, "status": c["status"],
                "status_reasons": json.loads(c["status_reasons"] or "[]"), "briefing": json.loads(c["briefing"] or "{}"),
                "briefing_md": c["briefing_md"] or "", "provider": c["provider"], "created_at": c["created_at"], "updated_at": c["updated_at"]}

    def events(self, case_id: str, since: int = -1, limit: int = 500, types: list[str] | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM events WHERE case_id=? AND seq>?", [case_id, since]
        if types:
            q += f" AND type IN ({','.join('?' * len(types))})"
            args += types
        with self._lock:
            rows = self.db.execute(q + " ORDER BY seq LIMIT ?", (*args, limit)).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "type": r["type"], "agent": r["agent"], "run": r["run"], "phase": r["phase"],
                 **json.loads(r["data"] or "{}")} for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {t: self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("cases", "runs", "claims", "evidence", "attacks", "responses", "rulings", "ranges", "events")}

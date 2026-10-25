"""A full two-run case with NO language model: a scripted court over the REAL corpus and the REAL tool layer.

    python -m nine_realms.dryrun [--pack PATH] [--extra-pack DIR ...] [--out runs/dry]

Every agent turn is scripted, but everything else is the real system: the tool calls hit the real hybrid search (with metadata filters)
and the trade tables, evidence is real paragraphs verified by the real citation checker, and the Thing, guards, limits, memory, store,
briefing and acceptance checker all run for real. It rehearses the pipeline and the final-round procedure (live pack in, case out,
acceptance report) without spending a token or needing a key.

THIS IS NOT A MODEL RUN. Its ranges come from a fixed rule over the trade tables, so they say nothing about how well an LLM argues. The
saved case is labelled provider="dryrun" and must never be submitted as the "two-run case" log: that one needs a real provider.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .llm import LLMResponse, ToolCall

_STOP = {"the", "and", "how", "are", "exposed", "give", "low", "high", "range", "imports", "end", "agreement", "corridor", "what", "for"}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{4,}", text.lower())) - _STOP


class DryPolicy:
    """Scripted turns grounded in the corpus. Reads the corpus directly to choose WHAT to say; every action still goes through the tools."""

    def __init__(self, corpus: Any, question: str, as_of: str) -> None:
        self.c, self.as_of, self.question = corpus, as_of, question
        rows = corpus.db.execute("SELECT DISTINCT country, sector FROM docs WHERE country!='' AND sector!=''").fetchall()
        qw = _words(question)
        titles = [r[0] for r in corpus.db.execute("SELECT title FROM docs WHERE source_type='trade_note'").fetchall()]
        into = {m.group(1) for t in titles for m in [re.search(r" to (\w+),", t)] if m}  # importing countries: the ones the question is about
        self.countries = sorted({r[0] for r in rows if r[0].lower() in qw and (r[0] in into or not into)}) or sorted({r[0] for r in rows})[:2]
        self.sectors = sorted({r[1].lower() for r in rows if r[1].lower() in qw}) or sorted({r[1].lower() for r in rows})[:2]
        self.pairs = [(cy, sec) for cy in self.countries for sec in self.sectors]
        events = corpus.t_list_events(as_of)["results"]
        named = [e for e in events if e["event_id"] != "NONE" and _words(e["name"]) & qw]
        self.event_id = (named or sorted((e for e in events if e["event_id"] != "NONE"), key=lambda e: -e["documents"]) or [{"event_id": "E5"}])[0]["event_id"]
        self.summaries = {p: self.c.call("import_summary", {"country": p[0], "product": p[1], "as_of": as_of}) for p in self.pairs}
        self.summaries = {p: s for p, s in self.summaries.items() if "error" not in s}
        self.tier4 = next((d["doc_id"] for d in corpus.docs.values() if d["tier"] == 4 and d["date"] <= as_of and d["event_id"] == self.event_id), "")
        self.used: set[str] = set()
        self.pool1: set[str] = set()

    # -- helpers ------------------------------------------------------------------------------------------------
    def para(self, doc_id: str, anchor: int | None = None, contains: str = "") -> dict[str, str]:
        paras = self.c.get_document(doc_id)["paragraphs"]
        pick = next((p for p in paras if anchor and p.startswith(f"[P{anchor}]")), None) or next((p for p in paras if contains and contains in p), None) or paras[-1]
        m = re.match(r"\[(P\d+)\]\s*(.*)", pick, re.S)
        return {"doc_id": doc_id, "quote": (m.group(2) if m else pick).strip(), "locator": m.group(1) if m else "P1"}

    def row_ev(self, pair: tuple[str, str], which: str = "last") -> dict[str, str]:
        s = self.summaries[pair]
        months = [m for m in s["months"] if m["complete"]]
        o = max(months[-1]["by_origin"].items(), key=lambda kv: abs(kv[1]["volume_t"] - kv[1]["prior_volume_t"]))[1]
        return self.para(o["table_id"], o["row"])

    def window(self, pair: tuple[str, str], name: str) -> dict[str, Any]:
        return self.summaries[pair]["windows"][name]

    def search_docs(self, pair: tuple[str, str], query: str, **kw: Any) -> list[dict[str, Any]]:
        return self.c.call("search_documents", {"query": query, "country": pair[0], "sector": pair[1], "as_of": self.as_of, "limit": 6, **kw})["results"]

    # -- the policy ---------------------------------------------------------------------------------------------
    def __call__(self, system: str, messages: list[dict[str, Any]], tools: list[Any]) -> LLMResponse:
        role = re.match(r"ROLE: (\w+)", system).group(1)  # type: ignore[union-attr]
        first = messages[0]["content"]
        task = first.split("\n", 1)[0]
        payload = json.loads(first.split("CASE FILE VIEW:\n", 1)[1].split("\nYOUR PRIVATE MEMORY", 1)[0])
        n_tools = sum(1 for m in messages if m["role"] == "tool")
        call = lambda name, **a: LLMResponse(None, [ToolCall(f"c{len(messages)}", name, a)])  # noqa: E731
        out = lambda d: LLMResponse(json.dumps(d))  # noqa: E731
        pair0 = self.pairs[0]
        if role == "HEIMDALL":
            return self._heimdall(task, payload, n_tools, call, out)
        if role in ("THOR", "LOKI"):
            return self._advocate(role == "THOR", task, payload, n_tools, call, out)
        if role == "ODIN":
            return self._odin(task, payload, out)
        raise AssertionError(f"unscripted turn {role}/{task} {pair0}")

    def _heimdall(self, task: str, p: dict[str, Any], n: int, call: Any, out: Any) -> LLMResponse:
        pair = next(iter(self.summaries), self.pairs[0])
        if "Open the case" in task:
            if n == 0:
                return call("list_events")
            if n == 1:
                return call("event_profile", event_id=self.event_id)
            if n == 2:
                return call("import_summary", country=pair[0], product=pair[1])
            if n == 3:
                return call("search_documents", query=f"{pair[0]} {pair[1]} imports corridor", event_id=self.event_id, country=pair[0])
            prof = self.c.call("event_profile", {"event_id": self.event_id, "as_of": self.as_of})
            hits = self.search_docs(pair, f"{pair[1]} imports corridor", event_id=self.event_id)
            pool = list(dict.fromkeys([h["doc_id"] for h in hits[:3]] + [s["origins"][0]["table_id"] for s in self.summaries.values()]))
            self.pool1 = set(pool)
            flags = ([{"doc_id": d, "type": "rumour", "note": "Tier 4, not a primary source"} for d in prof["low_reliability_only"][:3]]
                     + [{"doc_id": r["doc_id"], "type": "retraction", "note": "retracted or corrected by a later notice"} for r in prof["retracted_or_corrected"][:3]])
            self.used |= set(pool)
            return out({"verified": True, "verdict_reason": f"{prof['documents_visible']} dated documents and Tier 1 trade tables confirm {self.event_id}.",
                        "event_id": self.event_id, "event_summary": f"Event {self.event_id}", "unit": "% fall in total import value",
                        "scope": [{"country": c, "sector": s} for c, s in self.pairs], "pool": pool, "red_flags": flags})
        # remand: look for what the court said was missing
        q = {"query": "rail reroute alternative supply reserves", "country": self.countries[-1], "event_id": self.event_id}
        if n == 0:
            return call("search_documents", **q)
        res = self.c.call("search_documents", {**q, "as_of": self.as_of, "limit": 6})["results"]
        fresh = [h["doc_id"] for h in res if h["doc_id"] not in self.pool1 and h["tier"] <= 2]
        self.used |= set(fresh[:2])
        return out({"new_doc_ids": fresh[:2], "nothing_new": not fresh})

    def _advocate(self, thor: bool, task: str, p: dict[str, Any], n: int, call: Any, out: Any) -> LLMResponse:
        keys = list(self.summaries)
        if "Submit claims now" in task:
            run = p["run"]
            if run == 1:
                steps = [("search_documents", {"query": f"{keys[0][0]} {keys[0][1]} imports", "country": keys[0][0], "sector": keys[0][1].capitalize(),
                                               "event_id": self.event_id})] + [("import_summary", {"country": c, "product": s}) for c, s in keys]
                if thor and self.tier4:
                    steps.append(("read_document", {"doc_id": self.tier4}))
                if n < len(steps):
                    return call(steps[n][0], **steps[n][1])
                return out({"claims": self._claims_run1(thor, keys)})
            fresh = [d for d in p["pool"] if d not in self.pool1]
            if n == 0:
                return call("search_documents", query="rail reroute alternative supply reserves", country=self.countries[-1], event_id=self.event_id)
            if n == 1 and fresh:
                return call("read_document", doc_id=fresh[0])
            return out({"claims": self._claims_run2(thor, fresh, keys, p)})
        if "Cross-examination" in task:
            cl = p["opponent_claims"]
            run = max(c["run"] for c in cl)
            target = next((c for c in cl if c["run"] == run and any(e["tier"] >= 3 for e in c["evidence"])), [c for c in cl if c["run"] == run][0])
            est = target.get("estimate") or {}
            args = {"query": f"{est.get('country') or self.countries[0]} {est.get('sector') or self.sectors[0]} imports corridor", "country": est.get("country") or self.countries[0],
                    "event_id": self.event_id, "limit": 8}
            if n == 0:
                return call("search_documents", **args)
            hits = self.c.call("search_documents", {**args, "as_of": self.as_of})["results"]
            return out({"attacks": self._attacks(thor, p, hits)})
        if "You were attacked" in task:
            mine = p["attacks_against_you"][0]["your_claim"]
            est = mine.get("estimate") or {}
            args = {"query": f"{est.get('country') or self.countries[0]} {est.get('sector') or self.sectors[0]} imports corridor", "country": est.get("country") or self.countries[0],
                    "event_id": self.event_id, "limit": 8}
            if n == 0:
                return call("search_documents", **args)
            return out({"responses": self._responses(thor, p, self.c.call("search_documents", {**args, "as_of": self.as_of})["results"])})
        raise AssertionError(f"unscripted advocate turn: {task}")

    # -- claims -------------------------------------------------------------------------------------------------
    def _claims_run1(self, thor: bool, keys: list[tuple[str, str]]) -> list[dict[str, Any]]:
        claims = []
        for pair in keys:
            sb, wp, l3 = (self.window(pair, w) for w in ("since_break", "whole_period", "last_3_months"))
            lost = abs(sb["net_value_change_pct"]) if thor else abs(wp["net_value_change_pct"])
            gain = abs(l3["net_volume_change_pct"]) if thor else abs(sb["net_value_change_pct"])
            low, high = sorted((round(lost, 1), round(gain, 1)))
            text = (f"Total {pair[1]} imports into {pair[0]} fell {abs(sb['net_value_change_pct'])}% by value since {sb['from']}, "
                    f"against the same months a year earlier; {pair[0]} {pair[1]} exposure is therefore material.") if thor else \
                   (f"Net of other suppliers, total {pair[1]} imports into {pair[0]} fell {abs(wp['net_value_change_pct'])}% by value over "
                    f"{wp['from']} to {wp['to']}, and other origins offset {wp['substitution_offset_pct_of_loss']}% of the loss in {pair[0]}.")
            claims.append({"text": text, "kind": "fact", "evidence": [self.row_ev(pair)], "estimate": {"country": pair[0], "sector": pair[1], "low": low, "high": high}})
        if thor:  # one weak claim that will not survive cross-examination: a Tier 4 source
            if self.tier4:
                claims.append({"text": f"Unconfirmed reports say {keys[0][0]} {keys[0][1]} imports will collapse far beyond the official tables.",
                               "kind": "inference", "evidence": [self.para(self.tier4, 1)],
                               "estimate": {"country": keys[0][0], "sector": keys[0][1], "low": 60.0, "high": 90.0}})
        return claims

    def _claims_run2(self, thor: bool, fresh: list[str], keys: list[tuple[str, str]], p: dict[str, Any]) -> list[dict[str, Any]]:
        pair = keys[0]
        doc = (fresh or [self.summaries[pair]["origins"][-1]["table_id"]])[0]
        ev = self.para(doc, 2)
        sb = self.window(pair, "since_break")
        text = (f"New evidence ({doc}) shows {pair[0]} {pair[1]} supply routes remain exposed after the corridor ended: total imports are still {abs(sb['net_value_change_pct'])}% below last year by value."
                if thor else f"New evidence ({doc}) shows other routes carry some {pair[0]} {pair[1]} supply, which limits the net exposure to {abs(sb['net_value_change_pct'])}% by value.")
        claims = [{"text": text, "kind": "inference", "evidence": [ev], "estimate": {"country": pair[0], "sector": pair[1],
                  "low": round(abs(sb["net_value_change_pct"]) - 4, 1), "high": round(abs(sb["net_value_change_pct"]) + (6 if thor else 0), 1)}}]
        conceded = next((c for c in p.get("prior_claims", []) if c["status"] == "conceded" and c["id"].startswith("R1-T")), None) if thor else None
        if conceded:  # Thor tries to re-argue what he gave up, with no new document: his own memory must stop him
            claims.insert(0, {"text": conceded["text"], "kind": "inference", "evidence": [{"doc_id": e["doc_id"], "quote": e["quote"], "locator": "P1"} for e in conceded["evidence"]],
                              "estimate": conceded["estimate"]})
        return claims

    # -- cross-examination ----------------------------------------------------------------------------------------
    def _counter(self, claim: dict[str, Any], hits: list[dict[str, Any]]) -> dict[str, str] | None:
        """The paragraph, from documents this agent just retrieved, that shares the most words with the claim."""
        quoted = {e["quote"] for e in claim["evidence"]}
        want = _words(claim["text"])
        best: tuple[int, dict[str, str]] | None = None
        for h in hits:
            if h["tier"] > 2:
                continue
            for para in self.c.get_document(h["doc_id"])["paragraphs"]:
                m = re.match(r"\[(P\d+)\]\s*(.*)", para, re.S)
                score = len(_words(para) & want)
                if m and m.group(2).strip() not in quoted and score >= 2 and (best is None or score > best[0]):
                    best = (score, {"doc_id": h["doc_id"], "quote": m.group(2).strip(), "locator": m.group(1)})
        return best[1] if best else None

    def _attacks(self, thor: bool, p: dict[str, Any], hits: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        cl = p["opponent_claims"]
        run = max(c["run"] for c in cl)
        this = [c for c in cl if c["run"] == run]
        weak = next((c for c in this if any(e["tier"] >= 3 for e in c["evidence"])), None)
        target = weak or this[0]
        est = target.get("estimate") or {}
        pair = (est.get("country"), est.get("sector")) if est.get("country") else self.pairs[0]
        pair = pair if pair in self.summaries else next(iter(self.summaries))
        counter = self._counter(target, hits or []) or self.row_ev(pair)
        kind = "weak_source" if weak else "scope"
        words = f"{pair[0]} {pair[1]} imports"
        text = (f"This claim rests on one source of low reliability and no official table backs the {words} figure it gives; the trade table says otherwise."
                if weak else f"The claim about {words} generalises one window; the table shows a different net change for the months it leaves out.")
        return [{"target_claim_id": target["id"], "kind": kind, "counter_evidence": [counter], "text": text}]

    def _responses(self, thor: bool, p: dict[str, Any], hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for a in p["attacks_against_you"]:
            claim = a["your_claim"]
            if any(e["tier"] >= 3 for e in claim["evidence"]) and claim["kind"] != "fact":
                out.append({"attack_id": a["attack_id"], "action": "CONCEDE", "text": "The only source is Tier 3-4 and no official table supports it; the claim is withdrawn."})
                continue
            est = claim.get("estimate") or {}
            pair = (est.get("country"), est.get("sector"))
            pair = pair if pair in self.summaries else next(iter(self.summaries))
            cited = {e["doc_id"] for e in claim["evidence"]}
            others = [h for h in hits if h["doc_id"] not in cited]
            extra = self._counter(claim, others)  # a paragraph from a document the claim does not cite yet, retrieved just now
            if not extra:
                s = self.summaries[pair]
                extra = next((self.para(o["table_id"], m["by_origin"][o["origin"]]["row"]) for m in reversed(s["months"]) if m["complete"]
                              for o in s["origins"] if o["table_id"] not in cited and o["origin"] in m["by_origin"]), None)
            out.append({"attack_id": a["attack_id"], "action": "DEFEND", "text": "A further table row for the same flow supports the figure and covers the months the attack leaves out.",
                        "evidence": [extra] if extra else []})
        return out

    # -- judge --------------------------------------------------------------------------------------------------------
    def _odin(self, task: str, p: dict[str, Any], out: Any) -> LLMResponse:
        if "Rule on EVERY claim" in task:
            claims = p["claims_to_rule"]
            run = max(c["run"] for c in claims)
            rulings = []
            for c in claims:
                status = ("conceded" if c["status"] == "conceded" else "rejected" if any(e["tier"] >= 4 for e in c["evidence"])
                          else "contested" if c["side"] == "LOKI" and c["run"] == 1 else "established")
                rulings.append({"claim_id": c["id"], "basis": "sourced_fact" if c["kind"] == "fact" else "inference", "status": status,
                                "rationale": f"Ruled {status} on source tier and the attack record."})
            ranges = []
            for cy, sec in self.pairs:
                if (cy, sec) in self.summaries:
                    wp, sb = self.window((cy, sec), "whole_period"), self.window((cy, sec), "since_break")
                    lo, hi = sorted((abs(wp["net_value_change_pct"]), abs(sb["net_value_change_pct"])))
                    ranges.append({"country": cy, "sector": sec, "low": round(lo, 1), "high": round(hi, 1),
                                   "rationale": f"Net value change of total {sec} imports, whole period to since the break ({sb['from']})."})
                else:
                    ranges.append({"country": cy, "sector": sec, "unsupported": True,
                                   "rationale": "No trade table for this flow in the corpus as of the date; no number is given rather than a guess."})
            return out({"rulings": rulings, "ranges": ranges, "sufficiency": {
                "sufficient": run >= 2, "deficiencies": [] if run >= 2 else ["No Tier 1-2 evidence on alternative routes or reserves for the affected country"]}})
        f = p["digest"]["findings"]
        ids = lambda *st: [x["claim_id"] for x in f if x["status"] in st]  # noqa: E731
        sec = lambda *st: [{"text": f"{'/'.join(st)} findings", "claim_ids": ids(*st)}] if ids(*st) else []  # noqa: E731
        return out({"title": "Dry run: scripted trial over the real corpus", "executive_summary": "Scripted rehearsal, not a model run.",
                    "established": sec("established"), "contested": sec("contested"), "conceded": sec("conceded", "rejected"),
                    "outlook": {"text": "Exposure is expected to persist while alternative routes stay partial.",
                                "assumptions": ["Alternative routes stay at current capacity", "No new closures"]}})


def run_dry(pack: str | Path, db: str | Path, out: str | Path, extra_packs: list[str] | None = None, question: str | None = None) -> dict[str, Any]:
    from .acceptance import check
    from .corpus import LocalCorpus
    from .dates import extract_as_of
    from .events import EventBus
    from .llm import FakeLLM
    from .store import Store
    from .thing import Thing

    corpus = LocalCorpus(pack, db)
    for i, extra in enumerate(extra_packs or []):
        corpus.ingest_pack(Path(extra), f"dry-extra-{i + 1}", replace=True)
    pack_dir = Path(pack)
    if question is None:
        sample = pack_dir / "sample_question.txt"
        lines = [ln.strip() for ln in sample.read_text(encoding="utf-8").splitlines() if ln.strip()] if sample.exists() else []
        question = " ".join(ln for ln in lines if not ln.upper().startswith("QUESTION")) or "How exposed are Midgard and Tolan to the end of the Orvan corridor agreement?"
    as_of = extract_as_of(question) or corpus.pack_as_of
    store = Store(db)
    case_id = f"DRY-{abs(hash((question, as_of))) % 100000:05d}"
    store.delete_case(case_id)
    bus = EventBus(Path(out) / case_id / "events.jsonl", sink=lambda ev: store.add_event(case_id, ev))
    thing = Thing(FakeLLM(DryPolicy(corpus, question, as_of)), corpus, bus=bus, memory_dir=Path(out) / "memory",
                  on_update=lambda c: store.save_case(c.to_dict(), "dryrun"))
    case = thing.run_case(question, as_of, case_id=case_id)
    (Path(out) / case_id / "case.json").write_text(json.dumps(case.to_dict(), indent=2, default=str), encoding="utf-8")
    (Path(out) / case_id / "briefing.md").write_text(case.briefing_md, encoding="utf-8")
    saved = store.load_case(case_id)
    report = check(saved, store.events(case_id, limit=100000), corpus.stats(), corpus.list_packs())
    return {"case_id": case_id, "status": case.status, "reasons": case.status_reasons, "runs": len(case.runs), "acceptance": report, "events": len(bus.events)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", default=None)
    ap.add_argument("--extra-pack", action="append", default=[], help="extra pack folders ingested live before the trial (repeatable)")
    ap.add_argument("--db", default="data/dryrun.sqlite")
    ap.add_argument("--out", default="runs/dry")
    ap.add_argument("--question", default=None)
    a = ap.parse_args()
    from .server import default_pack

    r = run_dry(a.pack or default_pack(), a.db, a.out, a.extra_pack, a.question)
    print(f"{r['case_id']}: {r['status']} {r['reasons']} · runs {r['runs']} · events {r['events']}  (SCRIPTED: not a model run)")
    for c in r["acceptance"]:
        print(f"  [{ {True: 'PASS', False: 'FAIL', None: 'MANUAL'}[c['passed']]:6}] {c['n']:2}. {c['title'][:90]}\n           {c['detail']}")
    return 0 if r["status"] == "COMPLETE" and not any(c["passed"] is False for c in r["acceptance"]) else 1


if __name__ == "__main__":
    sys.exit(main())

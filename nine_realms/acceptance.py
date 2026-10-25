"""Check a saved case against the ten minimum acceptance criteria of the problem statement (section 3.3).

    python -m nine_realms.acceptance <case_id>          # reads data/ragnarisk.sqlite
    GET /api/cases/<id>/acceptance                       # same, for the dashboard

Each criterion returns passed (True/False/None) and the evidence found. None means it cannot be checked from the data (the dashboard
itself). A case can pass every checkable item and still need a human look at the briefing; this only proves the structure is there.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

FILTER_KEYS = ("country", "sector", "event_id", "source_type", "tier_max", "date_from", "date_to")


def check(case: dict[str, Any], events: list[dict[str, Any]], corpus: dict[str, Any] | None = None, packs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    corpus, packs = corpus or {}, packs or []
    tool = [e for e in events if e["type"] == "tool_call"]
    searches = [e for e in tool if e.get("tool") == "search_documents"]
    claims, attacks, rulings = case.get("claims", {}), case.get("attacks", {}), case.get("rulings", {})
    runs, brief = case.get("runs", []), case.get("briefing", {})
    by_side = lambda side: [c for c in claims.values() if c["side"] == side and c.get("evidence")]  # noqa: E731
    results: list[dict[str, Any]] = []

    def add(n: int, title: str, passed: bool | None, detail: str) -> None:
        results.append({"n": n, "title": title, "passed": passed, "detail": detail})

    # 1. Heimdall ingests the feed, builds the Case File, reads the trade tables, checks the event is real
    read_tables = [e for e in tool if e.get("tool") in ("read_trade_table", "trade_stats", "import_summary", "list_trade_tables")]
    quarantined = sum(len(p.get("report", {}).get("quarantined", {})) for p in packs)
    add(1, "Heimdall ingests the feed, opens the Case File, reads the trade tables, verifies the event",
        bool(case.get("verified") is not None and case.get("verdict_reason") and case.get("scope") and read_tables),
        f"event {case.get('event_id') or '?'} verified={case.get('verified')}; {len(read_tables)} trade-table reads; {corpus.get('documents', '?')} documents in {len(packs) or '?'} pack(s)"
        f"{'; live packs ingested' if len(packs) > 1 else '; NOTE: only one pack ingested, so live ingestion is not shown by this case'}")
    # 2. hybrid search with at least two metadata filters
    two = [e for e in searches if sum(1 for k in FILTER_KEYS if (e.get("args") or {}).get(k) not in (None, "")) >= 2]
    add(2, "Hybrid search works with at least two metadata filters", bool(two),
        f"{len(two)} of {len(searches)} searches used 2+ explicit filters (the as-of date is applied on top of those); search backend: {corpus.get('embed_backend', 'n/a')}")
    # 3. Thor and Loki each submit evidence-backed claims from their own queries and each searches at least once
    agents_searched = {e["agent"] for e in searches}
    add(3, "Thor and Loki each submit evidence-backed claims and each uses search at least once",
        bool(by_side("THOR") and by_side("LOKI") and {"THOR", "LOKI"} <= agents_searched),
        f"Thor {len(by_side('THOR'))} claims, Loki {len(by_side('LOKI'))}; searches by {sorted(agents_searched)}")
    # 4. at least one cross-examination
    add(4, "At least one cross-examination happens", len(attacks) >= 1, f"{len(attacks)} attacks, {len(case.get('responses', {}))} responses")
    # 5. Odin rules on every claim; at least one claim is rejected or conceded
    unruled = [c for c in claims if c not in rulings]
    dropped = [r for r in rulings.values() if r["status"] in ("rejected", "conceded")]
    add(5, "Odin rules on every claim, and at least one claim is rejected or conceded", bool(claims) and not unruled and bool(dropped),
        f"{len(rulings)} of {len(claims)} claims ruled; {len(dropped)} rejected or conceded" + (f"; UNRULED: {unruled[:5]}" if unruled else ""))
    # 6. the briefing
    ev = [e for c in claims.values() for e in c.get("evidence", [])]
    timed = sum(1 for e in ev if " " in (e.get("date") or ""))
    ranges = case.get("ranges", [])
    has_fallback = "fallback" in (brief.get("title") or "").lower() or not brief.get("outlook", {}).get("assumptions") or "unavailable" in json.dumps(brief.get("outlook", {})).lower()
    sections = all(k in brief for k in ("established", "contested", "conceded"))
    add(6, "The briefing separates Established / Contested / Conceded, has an exposure table with low and high bounds, timestamped citations and an outlook with assumptions",
        bool(sections and not has_fallback and ranges and ev and timed == len(ev)),
        f"sections={'yes' if sections else 'no'}; ranges={len(ranges)}; citations with a timestamp {timed}/{len(ev)}; outlook with assumptions={'no (fallback briefing)' if has_fallback else 'yes'}")
    # 7. the dashboard
    add(7, "The dashboard shows the question box, live feed, trial view, exposure table, and click-through works", None, "visual: check in the browser (Overview, Council, Briefing)")
    # 8. remand and a second run with new evidence
    remands = [e for e in events if e["type"] == "remand"]
    second = runs[1] if len(runs) > 1 else None
    add(8, "Odin sends a case back at least once, and a second run happens with new evidence",
        bool(remands and second and second.get("new_doc_ids")),
        f"{len(remands)} remand(s); runs={len(runs)}; run 2 new documents: {len(second.get('new_doc_ids', [])) if second else 0}")
    # 9. private lasting memory, and a logged action shows memory changing what an agent did
    infl = [e for e in events if e["type"] == "memory_influence"]
    writes = [e for e in events if e["type"] == "memory_write"]
    add(9, "Every agent has its own private, lasting memory, and a logged action shows memory changing what an agent did",
        bool(infl and writes), f"{len(infl)} memory_influence event(s) ({sum(1 for e in infl if e.get('kind'))} deterministic), {len(writes)} memory write(s) across {len({e['agent'] for e in writes})} agents")
    # 10. a failure case is handled, and rounds / runs / retries have hard limits
    started = next((e for e in events if e["type"] == "case_started"), {})
    limits = started.get("limits", {})
    handled = quarantined or [e for e in events if e["type"] in ("tool_error", "invalid_output", "llm_error", "injection_flagged", "evidence_dropped")]
    add(10, "At least one failure case is handled without crashing, and rounds, runs and retries have hard limits",
        bool(handled and limits.get("max_runs") and limits.get("max_llm_calls") and case.get("status") != "OPEN"),
        f"{quarantined} document(s) quarantined; {len([e for e in events if e['type'] in ('tool_error', 'invalid_output', 'llm_error', 'injection_flagged', 'evidence_dropped')])} handled runtime problem(s); "
        f"limits: runs<={limits.get('max_runs')}, LLM calls<={limits.get('max_llm_calls')}, tool calls/turn<={limits.get('max_tool_calls_per_turn')}; final status {case.get('status')}")
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case_id")
    ap.add_argument("--db", default="data/ragnarisk.sqlite")
    a = ap.parse_args()
    from .store import Store

    store = Store(a.db)
    case = store.load_case(a.case_id)
    if not case:
        print(f"no such case: {a.case_id}", file=sys.stderr)
        return 2
    res = check(case, store.events(a.case_id, limit=100000))
    for r in res:
        mark = {True: "PASS", False: "FAIL", None: "MANUAL"}[r["passed"]]
        print(f"[{mark:6}] {r['n']:2}. {r['title']}\n          {r['detail']}")
    failed = [r for r in res if r["passed"] is False]
    print(f"\n{sum(1 for r in res if r['passed'])} passed, {len(failed)} failed, {sum(1 for r in res if r['passed'] is None)} manual")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

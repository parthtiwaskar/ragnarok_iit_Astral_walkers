"""The submission log in one command: check the key, run the sample question as a FULL trial, check it, export it.

    python -m nine_realms.realrun [--question "..."] [--max-usd 6] [--timeout 1800] [--out runs]

1. Checks the configured provider with one tiny request (same as `python -m nine_realms.preflight`).
2. Runs the question as a full trial (up to the usual 3 runs, no Fast-answer limits) against the real corpus and database.
3. Checks the saved case against the 10 minimum criteria and the submission rules (COMPLETE, at least two runs, a remand
   with new evidence, nothing the checker marks as failed).
4. Only if all of that holds, writes runs/<case_id>/ (case.json, events.jsonl, briefing.md, acceptance.json).

Exit code 0 means a valid two-run log was written. Anything else says what is missing and writes nothing, so a half-finished
case can never be mistaken for the submission log.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from .env import load_env

POLL_SECONDS = 3


def verdict(case: dict[str, Any], checks: list[dict[str, Any]]) -> list[str]:
    """Why this case is NOT a valid two-run submission log (an empty list means it is)."""
    problems = []
    if case.get("status") != "COMPLETE":
        problems.append(f"status is {case.get('status')}, not COMPLETE: {'; '.join(case.get('status_reasons') or [])[:200]}")
    if len(case.get("runs", [])) < 2:
        problems.append(f"only {len(case.get('runs', []))} run(s); the log must show two runs (a remand, then a second run with new evidence)")
    problems += [f"criterion {c['n']} failed: {c['title'][:80]}. {c['detail'][:140]}" for c in checks if c["passed"] is False]
    return problems


def default_question(pack_dir: str) -> str:
    sample = Path(pack_dir) / "sample_question.txt"
    if sample.exists():
        lines = [ln.strip() for ln in sample.read_text(encoding="utf-8").splitlines() if ln.strip()]
        text = " ".join(ln for ln in lines if not ln.upper().startswith("QUESTION"))
        if text:
            return text
    return ("As of 25 January 2027, how exposed are Midgard and Tolan to the end of the Orvan corridor agreement? "
            "Give a low and high range for wheat and maize imports.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--question", default=None)
    ap.add_argument("--max-usd", type=float, default=None, help="per-case spend cap where the provider reports cost (default: MAX_CASE_USD, 1.50)")
    ap.add_argument("--timeout", type=int, default=1800, help="give up waiting after this many seconds")
    ap.add_argument("--out", default="runs")
    ap.add_argument("--skip-preflight", action="store_true")
    a = ap.parse_args(argv)
    load_env()

    from . import preflight, server
    from .acceptance import check
    from .dates import extract_as_of
    from .export import export_case

    provider, why = server.choose_provider()
    if not provider:
        print(f"NOT READY: no LLM key. {why}\nPut ONE key in .env (DEEPSEEK_API_KEY, ANTHROPIC_API_KEY or OPENAI_API_KEY), then run this again.", file=sys.stderr)
        return 2
    if provider == "groq":
        print("WARNING: Groq's free tier cannot finish a full two-run case (daily token cap, weak quoting); expect INCOMPLETE.", file=sys.stderr)
    if not a.skip_preflight and preflight.run() != 0:
        return 2

    cfg = server.Config.from_env()
    cfg.max_concurrent = 1
    if a.max_usd is not None:
        cfg.max_case_usd = a.max_usd
    app = server.build_app(cfg)
    question = a.question or default_question(cfg.pack or server.default_pack())
    as_of = extract_as_of(question) or app.corpus.pack_as_of
    print(f"\nrunning a FULL trial via {provider} ({server.provider_model(provider)}), as of {as_of}, spend cap ${cfg.max_case_usd:.2f}/case\n{question}\n")
    case_id = app.start_case(question, as_of, {}, "realrun")
    started, seen, last = time.time(), 0, ""
    while case_id in app.running_ids:
        if time.time() - started > a.timeout:
            print(f"\nTIMED OUT after {a.timeout}s; the case keeps running in this process only until it exits.", file=sys.stderr)
            return 1
        events = app.store.events(case_id, since=seen - 1, limit=500)
        for e in events:
            seen = max(seen, e["seq"] + 1)
            line = f"{e.get('agent') or '-':9} run {e.get('run')} {e.get('phase') or ''}: {e['type']}"
            if e["type"] in ("claim_submitted", "attack_made", "response_made", "ruling", "sufficiency", "remand", "research_done", "invalid_output", "llm_error", "evidence_dropped"):
                if line != last:
                    print(f"  {int(time.time() - started):4d}s  {line}")
                last = line
        time.sleep(POLL_SECONDS)

    case = app.store.load_case(case_id)
    events = app.store.events(case_id, limit=1_000_000)
    checks = check(case, events, app.corpus.stats(), app.corpus.list_packs())
    print(f"\ncase {case_id}: {case['status']} after {len(case['runs'])} run(s), {len(events)} events")
    for c in checks:
        print(f"  [{ {True: 'PASS', False: 'FAIL', None: 'MANUAL'}[c['passed']]:6}] {c['n']:2}. {c['title'][:88]}")
    problems = verdict(case, checks)
    if problems:
        print("\nNOT a valid submission log:")
        for p in problems:
            print(f"  - {p}")
        print("Nothing was written to runs/. Fix the cause (key, budget, model strength) and run again; the case stays in the database for inspection.")
        return 1
    dest = export_case(cfg.db, case_id, a.out)
    print(f"\nVALID two-run log written to {dest}/\nCommit that folder; it contains no keys.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

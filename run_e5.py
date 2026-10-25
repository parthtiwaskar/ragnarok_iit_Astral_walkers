"""Run the Trial of the Nine Realms on the E5 pack with a real LLM.

    ANTHROPIC_API_KEY=... python run_e5.py [--pack PATH] [--model ID] [--out runs]

Saves events.jsonl, case.json, briefing.md and per-agent memory under <out>/<case_id>/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from nine_realms import AnthropicClient, EventBus, Limits, Thing
from nine_realms.llm import OpenAICompatClient
from nine_realms.corpus import LocalCorpus

PACK = "03_Ragnarok Trial of the Nine Realms/dev_pack_E5"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default=PACK)
    ap.add_argument("--model", default=None)
    ap.add_argument("--provider", choices=["anthropic", "groq"], default="anthropic")
    ap.add_argument("--out", default="runs")
    ap.add_argument("--db", default="data/runemark.sqlite", help="SQLite database holding the ingested corpus")
    ap.add_argument("--memory-dir", default="data/memory", help="persistent per-agent memory, reused across cases")
    ap.add_argument("--max-llm-calls", type=int, default=150)
    args = ap.parse_args()

    key_var = "GROQ_API_KEY" if args.provider == "groq" else "ANTHROPIC_API_KEY"
    if not os.environ.get(key_var):
        print(f"{key_var} is not set. Export it in your shell, then rerun.", file=sys.stderr)
        return 2
    pack = Path(args.pack)
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    question = (pack / "sample_question.txt").read_text(encoding="utf-8").split("-", 1)[-1]
    question = question.split("\n", 1)[-1].strip() if question.lstrip().startswith("QUESTION") else question.strip()
    as_of = manifest["as_of"]

    corpus = LocalCorpus(pack, args.db)
    print(f"Corpus database {args.db}: {corpus.stats()}\nQuarantined: {corpus.quarantine or 'none'}")
    case_id = f"E5-{os.getpid()}"
    out = Path(args.out) / case_id
    bus = EventBus(out / "events.jsonl")
    llm = (OpenAICompatClient(os.environ["GROQ_API_KEY"], args.model or "llama-3.3-70b-versatile", max_tokens=2500)
           if args.provider == "groq" else AnthropicClient(model=args.model, max_tokens=8192))
    thing = Thing(llm, corpus,
                  limits=Limits(max_llm_calls=args.max_llm_calls, **({"max_tool_result_chars": 3500, "max_context_chars": 13000} if args.provider == "groq" else {})), bus=bus, memory_dir=args.memory_dir)
    case = thing.run_case(question, as_of, case_id=case_id)

    (out / "case.json").write_text(json.dumps(case.to_dict(), indent=2, default=str), encoding="utf-8")
    (out / "briefing.md").write_text(case.briefing_md, encoding="utf-8")
    print(f"\nStatus: {case.status} {case.status_reasons}\nRuns: {len(case.runs)} · LLM calls: {thing.budget.llm_calls} · tokens: {thing.budget.tokens}")
    print(f"Saved to {out}/\n")
    print(case.briefing_md)
    return 0 if case.status in ("COMPLETE", "INCOMPLETE") else 1


if __name__ == "__main__":
    raise SystemExit(main())

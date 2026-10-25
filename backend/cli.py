"""Command line: ingest | ask | replay | score | export | memory-reset | watch.

python cli.py ingest [--path data/live_packs/pack1]
python cli.py ask "How exposed is <country>'s <sector> to <event>?" --as-of 2025-03-20
python cli.py replay <case_id>
python cli.py score  <case_id> [--truth truth.json]
python cli.py export <case_id> --zip
python cli.py memory-reset --agent all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import AGENTS, Settings, get_settings


def _out(obj: object) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _settings(args: argparse.Namespace) -> Settings:
    s = get_settings()
    overrides = {}
    if getattr(args, "llm", None):
        overrides["LLM_PROVIDER"] = args.llm
    if getattr(args, "embeddings", None):
        overrides["EMBEDDINGS"] = args.embeddings
    return s.model_copy(update=overrides) if overrides else s


def cmd_ingest(args: argparse.Namespace) -> int:
    from app.runtime import build_services, ingest

    s = _settings(args)
    svc = build_services(s)
    paths = None
    if args.path:
        p = Path(args.path).resolve()
        paths = [(p, args.pack or (None if p == s.runemark_dir else p.name))]
    t0 = time.monotonic()
    rep = ingest(svc, paths)
    d = rep.to_dict()
    _out(
        {
            "index_version": d["index_version"],
            "seconds": round(time.monotonic() - t0, 1),
            "totals": d["totals"],
            "by_format": d["by_format"],
            "quarantine": d["quarantine"],
            "partial": d["partial"],
            "duplicates": d["duplicates"],
            "missing": d["missing"],
            "off_manifest": d["off_manifest"],
            "flagged_rows": d["flagged_rows"],
            "injections": len(d["injections"]),
            "notes": d["notes"],
            "embedder": d["embedder"],
        }
    )
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from app.runtime import build_services, ingest, run_case

    if not args.as_of:
        print("error: --as-of YYYY-MM-DD is required (the simulated clock)", file=sys.stderr)
        return 2
    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        print("error: --as-of must be YYYY-MM-DD", file=sys.stderr)
        return 2
    s = _settings(args)
    svc = build_services(s)
    if svc.repo.counts()["docs_total"] == 0 or args.ingest:
        ingest(svc)
    t0 = time.monotonic()
    cf = asyncio.run(run_case(svc, args.question, as_of))
    case_dir = s.runs_dir / cf.case_id
    _out(
        {
            "case_id": cf.case_id,
            "status": cf.status,
            "status_reasons": cf.status_reasons,
            "runs": len(cf.runs),
            "seconds": round(time.monotonic() - t0, 1),
            "case_dir": str(case_dir),
            "briefing": str(case_dir / "briefing.md"),
        }
    )
    if args.print:
        print((case_dir / "briefing.md").read_text(encoding="utf-8"))
    return 0


def _case_dir(s: Settings, case_id: str) -> Path:
    p = s.runs_dir / case_id
    if not p.exists():
        raise SystemExit(f"no such case: {p}")
    return p


def cmd_replay(args: argparse.Namespace) -> int:
    from app.obs.replay import replay
    from app.runtime import build_services

    s = _settings(args)
    svc = build_services(s)
    rep = replay(_case_dir(s, args.case_id), svc.repo, s.TRADE_ROW_VISIBILITY)
    _out(rep)
    return 0 if rep.get("briefing_identical") in (True, None) and rep.get("exposure_mismatches") == 0 else 1


def cmd_score(args: argparse.Namespace) -> int:
    from app.obs.replay import score
    from app.runtime import build_services

    s = _settings(args)
    svc = build_services(s)
    truth = Path(args.truth) if args.truth else None
    _out(score(_case_dir(s, args.case_id), svc.repo, truth, s.TRADE_ROW_VISIBILITY))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from app.obs.replay import export_zip

    s = _settings(args)
    target = export_zip(_case_dir(s, args.case_id))
    _out({"zip": str(target)})
    return 0


def cmd_memory_reset(args: argparse.Namespace) -> int:
    from app.memory.store import MemoryStore

    s = _settings(args)
    agents = list(AGENTS) if args.agent == "all" else [args.agent]
    for a in agents:
        store = MemoryStore(a, s.memory_dir / f"{a}.sqlite")
        store.reset()
        store.close()
    _out({"reset": agents})
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    from app.ingest.watcher import LivePackWatcher
    from app.runtime import build_services

    s = _settings(args)
    svc = build_services(s)
    w = LivePackWatcher(svc)
    w.start()
    print(f"watching {s.live_packs_dir} (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        w.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="cli.py", description="Trial of the Nine Realms")
    ap.add_argument("--llm", choices=["openai", "anthropic", "fake"], help="override LLM_PROVIDER")
    ap.add_argument("--embeddings", choices=["fastembed", "openai", "hash"], help="override EMBEDDINGS")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("--path")
    p.add_argument("--pack")
    p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("ask")
    p.add_argument("question")
    p.add_argument("--as-of", dest="as_of")
    p.add_argument("--ingest", action="store_true", help="(re)scan the corpus first")
    p.add_argument("--print", action="store_true", help="print the briefing")
    p.set_defaults(fn=cmd_ask)
    for name, fn in (("replay", cmd_replay), ("export", cmd_export)):
        p = sub.add_parser(name)
        p.add_argument("case_id")
        if name == "export":
            p.add_argument("--zip", action="store_true", default=True)
        p.set_defaults(fn=fn)
    p = sub.add_parser("score")
    p.add_argument("case_id")
    p.add_argument("--truth")
    p.set_defaults(fn=cmd_score)
    p = sub.add_parser("memory-reset")
    p.add_argument("--agent", default="all", choices=["all", *AGENTS])
    p.set_defaults(fn=cmd_memory_reset)
    p = sub.add_parser("watch")
    p.set_defaults(fn=cmd_watch)
    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())

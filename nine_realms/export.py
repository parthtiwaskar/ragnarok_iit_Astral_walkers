"""Write one saved case as the submission log: runs/<case_id>/{case.json, events.jsonl, briefing.md, acceptance.json}.

    python -m nine_realms.export <case_id> [--db data/ragnarisk.sqlite] [--out runs]

Works for a case started from the dashboard or from run_e5.py: both save into the database.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def export_case(db: str, case_id: str, out: str = "runs") -> Path:
    from .acceptance import check
    from .store import Store

    store = Store(db)
    case = store.load_case(case_id)
    if not case:
        raise SystemExit(f"no such case: {case_id}")
    events = store.events(case_id, limit=1_000_000)
    dest = Path(out) / case_id
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "case.json").write_text(json.dumps(case, indent=2, default=str), encoding="utf-8")
    (dest / "events.jsonl").write_text("".join(json.dumps(e, default=str) + "\n" for e in events), encoding="utf-8")
    (dest / "briefing.md").write_text(case["briefing_md"], encoding="utf-8")
    (dest / "acceptance.json").write_text(json.dumps(check(case, events), indent=2), encoding="utf-8")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case_id")
    ap.add_argument("--db", default="data/ragnarisk.sqlite")
    ap.add_argument("--out", default="runs")
    a = ap.parse_args()
    dest = export_case(a.db, a.case_id, a.out)
    print(f"wrote {dest}/ (case.json, events.jsonl, briefing.md, acceptance.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Per-case artifacts under runs/<case_id>/ (OBSERVABILITY.md §2)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from pydantic_core import to_jsonable_python

from app.config import REPO_ROOT
from app.store.repo import Repo
from app.tools.asof import Clock, doc_visible
from app.trial.casefile import CaseFile


def dump(obj: Any) -> str:
    return json.dumps(to_jsonable_python(obj), indent=2, ensure_ascii=False)


def git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


class Artifacts:
    def __init__(self, case_dir: Path) -> None:
        self.dir = case_dir
        case_dir.mkdir(parents=True, exist_ok=True)

    def write(self, rel: str, obj: Any) -> Path:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(obj if isinstance(obj, str) else dump(obj), encoding="utf-8")
        return p

    def save_case_file(self, cf: CaseFile) -> None:
        cf.bump()
        self.write("case_file.json", cf.model_dump())
        if cf.runs:
            self.write(f"run_{cf.runs[-1].n}/case_file.json", cf.model_dump())

    def save_run(self, cf: CaseFile, n: int) -> None:
        r = cf.run(n)
        base = f"run_{n}/"
        self.write(base + "claims.json", [c.model_dump() for c in r.claims])
        self.write(base + "positions.json", [p.model_dump() for p in r.positions])
        self.write(base + "attacks.json", [a.model_dump() for a in r.attacks])
        self.write(base + "responses.json", [x.model_dump() for x in r.responses])
        self.write(
            base + "citation_checks.json",
            {k: [c.model_dump() for c in v] for k, v in r.citation_checks.items()},
        )
        self.write(base + "rulings.json", [x.model_dump() for x in r.rulings])
        self.write(base + "final_ranges.json", [f.model_dump() for f in r.final_ranges])
        if r.sufficiency is not None:
            self.write(base + "sufficiency.json", r.sufficiency.model_dump())
        if r.research is not None:
            self.write(base + "research_report.json", r.research.model_dump())
        self.write(base + "case_file.json", cf.model_dump())

    def save_remand(self, cf: CaseFile, n: int) -> None:
        self.write(
            f"run_{n}/remand.json", {"run": n, "deficiencies": [d.model_dump() for d in cf.deficiencies(n)]}
        )

    def save_manifest_snapshot(self, repo: Repo, clock: Clock) -> None:
        docs = [
            {
                "doc_id": d.doc_id,
                "content_hash": d.content_hash,
                "pub_date": d.pub_date,
                "status": d.status,
                "tier": d.tier,
                "event_id": d.event_id,
            }
            for d in repo.docs()
            if doc_visible(d, clock) and not d.off_manifest
        ]
        self.write("manifest_snapshot.json", {"as_of": clock.iso, "pin": clock.pin, "docs": docs})

    def save_memory_diff(self, before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> None:
        for agent, snap in after.items():
            old = {m["id"]: m for m in before.get(agent, {}).get("memories", [])}
            new = {m["id"]: m for m in snap.get("memories", [])}
            added = [m for k, m in new.items() if k not in old]
            changed = [
                {"id": k, "before": old[k], "after": m}
                for k, m in new.items()
                if k in old
                and (old[k].get("confidence") != m.get("confidence") or old[k].get("text") != m.get("text"))
            ]
            self.write(
                f"memory_diff/{agent}.json",
                {
                    "agent": agent,
                    "added": added,
                    "changed": changed,
                    "before_count": len(old),
                    "after_count": len(new),
                },
            )

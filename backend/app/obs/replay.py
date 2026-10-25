"""Deterministic replay and self-scoring from saved artifacts (OBSERVABILITY.md §4). No LLM calls."""

from __future__ import annotations

import difflib
import json
import zipfile
from pathlib import Path
from typing import Any

from app.briefing.render import render_markdown
from app.obs.events import read_events_file
from app.store.repo import Repo
from app.tools.asof import Clock
from app.tools.citation import verify_cite
from app.tools.exposure import compute
from app.trial.casefile import CaseFile


def load_case(case_dir: Path) -> CaseFile:
    return CaseFile.model_validate_json((case_dir / "case_file.json").read_text(encoding="utf-8"))


def replay(case_dir: Path, repo: Repo, trade_rule: str = "completed_month") -> dict[str, Any]:
    cf = load_case(case_dir)
    clock = Clock(as_of=cf.as_of, pin=cf.pinned_ingest_seq or None, trade_rule=trade_rule)  # type: ignore[arg-type]
    out: dict[str, Any] = {"case_id": cf.case_id, "status": cf.status}
    # 1. recompute every exposure from its saved inputs
    events = read_events_file(case_dir / "events.jsonl")
    checked = mismatched = 0
    for e in events:
        if e.type != "exposure_computed":
            continue
        inp = e.payload.get("inputs", {})
        res = compute(
            inp.get("params", {}),
            horizon=float(inp.get("horizon", 3)),
            min_width=float(inp.get("min_width", 0.05)),
            k_obs=inp.get("k_obs"),
            anchor_tol=float(inp.get("anchor_tol", 0.3)),
        )
        checked += 1
        outp = e.payload.get("outputs", {})
        if (
            res.calc_id != e.payload.get("calc_id")
            or abs(res.low - outp.get("low", -1)) > 1e-6
            or abs(res.high - outp.get("high", -1)) > 1e-6
        ):
            mismatched += 1
    out["exposure_recomputed"] = checked
    out["exposure_mismatches"] = mismatched
    # 2. re-render the briefing from saved rulings and sections, diff with the saved one
    bj_path = case_dir / "briefing.json"
    md_path = case_dir / "briefing.md"
    if bj_path.exists() and md_path.exists() and cf.status not in ("REJECTED", "ABORTED"):
        bj = json.loads(bj_path.read_text(encoding="utf-8"))
        ri = bj.get("render_inputs", {})
        if ri.get("case_file_version") is not None:
            cf.version = int(ri["case_file_version"])
        md = render_markdown(
            cf,
            bj["digest"],
            bj["sections"],
            repo,
            bj["status"],
            bj["status_reasons"],
            ri.get("limits_hit", []),
            ri.get("prompt_hashes", {}),
            set(ri.get("cited_refs", [])),
        )
        saved = md_path.read_text(encoding="utf-8")
        diff = list(
            difflib.unified_diff(saved.splitlines(), md.splitlines(), "saved", "replayed", lineterm="")
        )
        out["briefing_identical"] = not diff
        out["briefing_diff"] = diff[:40]
        cites = bj.get("citations", [])
        valid = [c for c in cites if verify_cite(c, repo, clock, cf.scope.event_id if cf.scope else None)]
        out["citations_checked"] = len(cites)
        out["citations_valid"] = len(valid)
        out["citation_validity_pct"] = round(100.0 * len(valid) / len(cites), 1) if cites else None
        out["invalid_citations"] = sorted(set(cites) - set(valid))
    else:
        out["briefing_identical"] = None
    return out


def score(
    case_dir: Path, repo: Repo, truth_path: Path | None = None, trade_rule: str = "completed_month"
) -> dict[str, Any]:
    cf = load_case(case_dir)
    events = read_events_file(case_dir / "events.jsonl")
    rep = replay(case_dir, repo, trade_rule)
    fps = [
        e.payload.get("fingerprint")
        for e in events
        if e.type == "tool_call"
        and e.payload.get("fingerprint")
        and e.payload.get("status") in ("ok", "error")
    ]
    traps: dict[str, int] = {}
    for e in events:
        if e.type == "trap_detected":
            k = str(e.payload.get("type"))
            traps[k] = traps.get(k, 0) + 1
    attacks_by = {"thor": 0, "loki": 0}
    for a in cf.all_attacks():
        attacks_by[a.by] += 1
    defends_new = sum(1 for x in cf.all_responses() if x.action == "DEFEND" and x.new_evidence)
    rulings = [r for run in cf.runs for r in run.rulings]
    filtered_searches = sum(
        1
        for e in events
        if e.type == "tool_call"
        and e.payload.get("tool") == "search_documents"
        and len(
            [
                k
                for k, v in (e.payload.get("args", {}).get("filters") or {}).items()
                if v not in (None, [], False)
            ]
        )
        >= 2
    )
    out: dict[str, Any] = {
        "case_id": cf.case_id,
        "status": cf.status,
        "status_reasons": cf.status_reasons,
        "runs": len(cf.runs),
        "citation_validity_pct": rep.get("citation_validity_pct"),
        "invalid_citations": rep.get("invalid_citations"),
        "duplicate_queries_executed": len(fps) - len(set(fps)),
        "duplicates_blocked": sum(1 for e in events if e.type == "duplicate_blocked"),
        "limits_hit": sorted({str(e.payload.get("limit")) for e in events if e.type == "limit_hit"}),
        "trap_counts": traps,
        "attacks_per_side": attacks_by,
        "defences_with_new_evidence": defends_new,
        "rejected_or_conceded": sum(1 for r in rulings if r.verdict in ("REJECTED", "CONCEDED")),
        "memory_influence": sum(1 for e in events if e.type == "memory_influence"),
        "searches_with_2plus_filters": filtered_searches,
        "remands": sum(1 for e in events if e.type == "remand"),
        "reproducible": rep.get("briefing_identical") and rep.get("exposure_mismatches") == 0,
        "replay": {k: v for k, v in rep.items() if k != "briefing_diff"},
    }
    if truth_path is not None and truth_path.exists() and cf.scope:
        truth = json.loads(truth_path.read_text(encoding="utf-8")).get(cf.scope.event_id, {})
        rows = []
        last = cf.runs[-1] if cf.runs else None
        for fr in last.final_ranges if last else []:
            key = f"{fr.country}/{fr.sector}"
            t = truth.get(key)
            if not t:
                continue
            tv = float(t["exposure_pct"])
            pos = {
                p.side: p
                for run in cf.runs
                for p in run.positions
                if p.country == fr.country and p.sector == fr.sector
            }
            rows.append(
                {
                    "row": key,
                    "truth": tv,
                    "odin": [fr.low, fr.high],
                    "odin_covers": fr.low <= tv <= fr.high,
                    "odin_width_pp": round(fr.high - fr.low, 2),
                    "thor": [pos["thor"].low, pos["thor"].high] if "thor" in pos else None,
                    "thor_covers": "thor" in pos and pos["thor"].low <= tv <= pos["thor"].high,
                    "loki": [pos["loki"].low, pos["loki"].high] if "loki" in pos else None,
                    "loki_covers": "loki" in pos and pos["loki"].low <= tv <= pos["loki"].high,
                }
            )
        out["range_coverage"] = rows
    return out


def export_zip(case_dir: Path, target: Path | None = None) -> Path:
    target = target or case_dir.with_suffix(".zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(case_dir.rglob("*")):
            if p.is_file():
                zf.write(p, Path(case_dir.name) / p.relative_to(case_dir))
    return target

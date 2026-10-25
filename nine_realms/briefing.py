"""Rulings digest, exposure rows and the deterministic Markdown renderer.

Odin writes the prose; this module attaches citations (doc ID + publication date) from the
claims he references, so every citation resolves to something a ruling already rested on.
"""
from __future__ import annotations

from typing import Any

from .models import CaseFile, Claim

DEAD = {"conceded", "rejected"}


def _surviving(case: CaseFile, c: Claim) -> bool:
    r = case.rulings.get(c.id)
    return c.status != "conceded" and not (r and r.status in DEAD)


def exposure_rows(case: CaseFile) -> list[dict[str, Any]]:
    rows = []
    for key in case.scope:
        row: dict[str, Any] = {"country": key[0], "sector": key[1], "unit": case.unit}
        for side in ("THOR", "LOKI"):
            ests = [c.estimate for c in case.claims.values()
                    if c.side == side and c.estimate and c.estimate.key == key and _surviving(case, c)]
            row[side.lower()] = (min(e.low for e in ests), max(e.high for e in ests)) if ests else None
        odin = next((r for r in case.ranges if (r.country, r.sector) == key), None)
        row["odin"] = (odin.low, odin.high) if odin and odin.low is not None else None
        row["odin_flags"] = odin.flags if odin else ["no_range"]
        rows.append(row)
    return rows


def digest(case: CaseFile) -> dict[str, Any]:
    findings = []
    for cid, r in case.rulings.items():
        c = case.claims[cid]
        findings.append({
            "claim_id": cid, "side": c.side, "text": c.text, "basis": r.basis, "status": r.status,
            "rationale": r.rationale,
            "evidence": [{"doc_id": e.doc_id, "quote": e.quote, "date": e.date, "tier": e.tier} for e in c.evidence],
            "estimate": c.estimate.__dict__ if c.estimate else None,
        })
    return {"question": case.question, "as_of": case.as_of, "event": case.event_summary, "unit": case.unit,
            "case_status": case.status, "status_reasons": case.status_reasons,
            "runs": len(case.runs), "findings": findings, "exposure": exposure_rows(case)}


def fallback_brief(d: dict[str, Any]) -> dict[str, Any]:
    """Used when Odin cannot write the briefing: still rulings-only, just terse."""
    by = {"established": [], "contested": [], "conceded": []}
    for f in d["findings"]:
        key = "conceded" if f["status"] in ("conceded", "rejected") else f["status"]
        by[key].append({"text": f["text"], "claim_ids": [f["claim_id"]]})
    return {"title": "Briefing (rulings-only fallback)", "executive_summary":
            "Odin could not draft prose; findings below are listed exactly as ruled.", **by,
            "outlook": {"text": "No outlook could be drafted.", "assumptions": ["Outlook unavailable: Odin failed to draft."]}}


def _fmt(r: tuple[float, float] | None) -> str:
    return "n/a" if not r else f"{r[0]:g}–{r[1]:g}"


def render(case: CaseFile, brief: dict[str, Any], d: dict[str, Any]) -> str:
    by_id = {f["claim_id"]: f for f in d["findings"]}
    cited: dict[str, dict[str, Any]] = {}

    def cites(ids: list[str]) -> str:
        parts = []
        for cid in ids:
            for e in by_id[cid]["evidence"]:
                cited.setdefault(e["doc_id"], e)
                parts.append(f"[{e['doc_id']}, {e['date'] or 'undated'}]")
        return " ".join(dict.fromkeys(parts))

    out = [f"# {brief.get('title') or 'Briefing'} — {case.question}",
           f"Case `{case.case_id}` · As of **{case.as_of}** · Runs: {len(case.runs)} · Status: **{case.status}**"]
    if case.status != "COMPLETE":
        out.append("> ⚠️ This case is **not complete**: " + "; ".join(case.status_reasons or ["see trace"]))
    if any(r.unchallenged_side for r in case.runs):
        out.append("> ⚠️ One side went **unchallenged** in at least one run: " +
                   ", ".join(sorted({r.unchallenged_side for r in case.runs if r.unchallenged_side})))
    out += ["", "## Executive summary", brief["executive_summary"], ""]
    for sec, title in (("established", "Established findings"), ("contested", "Contested findings"),
                       ("conceded", "Conceded / rejected findings")):
        out.append(f"## {title}")
        items = brief.get(sec, [])
        if not items:
            out.append("_None._")
        for it in items:
            kinds = sorted({by_id[c]["basis"].replace("_", " ") for c in it["claim_ids"]})
            out.append(f"- {it['text']} _({', '.join(kinds)})_ {cites(it['claim_ids'])}")
        out.append("")
    out += ["## Exposure table", f"Unit: {case.unit or 'n/a'}", "",
            "| Country | Sector | Loki range | Thor range | Odin range |", "|---|---|---|---|---|"]
    for r in d["exposure"]:
        flag = f" ⚑{','.join(r['odin_flags'])}" if r["odin_flags"] else ""
        out.append(f"| {r['country']} | {r['sector']} | {_fmt(r['loki'])} | {_fmt(r['thor'])} | {_fmt(r['odin'])}{flag} |")
    out += ["", "## Next-quarter outlook", brief["outlook"]["text"], "", "**Assumptions**"]
    out += [f"- {a}" for a in brief["outlook"]["assumptions"]]
    out += ["", "## Sources"]
    for doc_id, e in sorted(cited.items()):
        out.append(f"- {doc_id} · published {e['date'] or 'undated'} · Tier {e['tier'] if e['tier'] is not None else '?'}")
    return "\n".join(out) + "\n"

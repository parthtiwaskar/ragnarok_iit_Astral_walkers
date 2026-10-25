"""Deterministic briefing renderer. Odin's prose fills five sections; everything else is rendered by code."""

from __future__ import annotations

from typing import Any

from app.store.repo import Repo
from app.trial.casefile import CaseFile


def _range(lo: float | None, hi: float | None) -> str:
    if lo is None or hi is None:
        return "n/a"
    return f"{lo:.1f}–{hi:.1f}"


def exposure_table(cf: CaseFile, digest: dict[str, Any]) -> str:
    pos = digest.get("positions", {})
    finals = {(f["country"], f["sector"]): f for f in digest.get("final_ranges", [])}
    rows = cf.scope.rows if cf.scope else []
    lines = [
        "| Country | Sector | Loki range | Thor range | Odin range | Unit | Value at risk (Odin) | Basis rulings |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lk = next(
            (p for p in pos.get("loki", []) if p["country"] == r.country and p["sector"] == r.sector), None
        )
        th = next(
            (p for p in pos.get("thor", []) if p["country"] == r.country and p["sector"] == r.sector), None
        )
        od = finals.get((r.country, r.sector))
        var = "n/a"
        if od and r.baseline_value:
            h = 3
            var = f"{od['low'] / 100 * r.baseline_value * h:,.0f}–{od['high'] / 100 * r.baseline_value * h:,.0f} {r.unit}"
        basis = ", ".join(od["rulings"][:6]) if od else ""
        flags = []
        if od and od.get("contested_params"):
            flags.append("contested: " + ", ".join(od["contested_params"]))
        if od and od.get("unsupported_params"):
            flags.append("unsupported: " + ", ".join(od["unsupported_params"]))
        if flags:
            basis = f"{basis} ({'; '.join(flags)})" if basis else "; ".join(flags)
        lines.append(
            f"| {r.country} | {r.sector} | {_range(lk['low'], lk['high']) if lk else 'n/a'} | "
            f"{_range(th['low'], th['high']) if th else 'n/a'} | "
            f"{_range(od['low'], od['high']) if od else 'n/a'} | % of baseline trade value, next quarter | "
            f"{var} | {basis} |"
        )
    return "\n".join(lines)


def data_quality_section(cf: CaseFile, digest: dict[str, Any], limits_hit: list[str]) -> str:
    out = []
    for x in digest.get("data_quality", []):
        out.append(f"- {x}")
    for t in digest.get("traps", []):
        out.append(f"- Trap handled: {t}")
    for f in digest.get("failures", []):
        out.append(f"- {f}")
    for lim in limits_hit:
        out.append(f"- Limit hit: {lim}")
    for r in cf.runs:
        for side, st in r.side_status.items():
            if st.value != "OK":
                other = "Loki" if side == "thor" else "Thor"
                out.append(f"- Run {r.n}: {side.capitalize()} {st.value}; {other}'s case went unchallenged.")
    return "\n".join(out) or "- No data-quality issues touched the scope."


def rejected_section(digest: dict[str, Any]) -> str:
    rows = digest.get("rejected", [])
    if not rows:
        return "None."
    out = []
    for e in rows:
        issues = sorted({i for chk in e.get("citation_issues", []) for i in chk.get("issues", [])})
        extra = f" — citation issues: {', '.join(issues)}" if issues else ""
        out.append(f"- {e['ruling']} ({e['side']}): {e['statement']} — {e['why']}{extra}")
    return "\n".join(out)


def sources_section(cf: CaseFile, repo: Repo, cited: set[str]) -> str:
    lines = ["| ID | Title | Outlet | Tier | Date | Status |", "|---|---|---|---|---|---|"]
    for ref in sorted(cited):
        if "#r" in ref or " r" in ref:
            continue
        d = repo.get_doc(ref)
        if d is None:
            continue
        pool = next((p for p in cf.pool if p.ref == ref), None)
        status = pool.status if pool else d.status
        lines.append(
            f"| {d.doc_id} | {d.title or ''} | {d.outlet or ''} | {d.tier} | {d.pub_date} | {status} |"
        )
    tables = sorted({r.split("#")[0] for r in cited if "#r" in r})
    for t in tables:
        tt = repo.get_trade_table(t)
        if tt is not None:
            lines.append(
                f"| {t} | trade table {tt.exporter} → {tt.importer} {tt.sector} | trade data | – | "
                f"{tt.period_start}..{tt.period_end} | {tt.status} |"
            )
    return "\n".join(lines)


def trace_section(cf: CaseFile, prompt_hashes: dict[str, str]) -> str:
    hashes = ", ".join(f"{k}={v}" for k, v in sorted(prompt_hashes.items()))
    return "\n".join(
        [
            f"- Case File version: {cf.version} (`run_<n>/case_file.json`)",
            "- Rulings: `run_<n>/rulings.json`; citation checks: `run_<n>/citation_checks.json`",
            "- Event log: `events.jsonl`; config and prompt hashes: `config.json`",
            f"- index_version: {cf.index_version}",
            f"- prompt hashes: {hashes or 'n/a'}",
        ]
    )


def render_markdown(
    cf: CaseFile,
    digest: dict[str, Any],
    sections: dict[str, Any],
    repo: Repo,
    status: str,
    reasons: list[str],
    limits_hit: list[str],
    prompt_hashes: dict[str, str],
    cited_refs: set[str],
) -> str:
    head = f"Case {cf.case_id} · As of {cf.as_of.isoformat()} · Runs: {len(cf.runs)} · Status: {status}"
    if reasons:
        head += f" ({', '.join(reasons)})"
    ver = digest.get("verification", {})
    ver_lines = [f"Verdict: **{ver.get('verdict')}**"]
    ver_lines += [f"- {x}" for x in ver.get("reasons", [])]
    if ver.get("retractions_applied"):
        ver_lines.append(f"- Retractions applied as of the date: {', '.join(ver['retractions_applied'])}")
    if sections.get("verification_note"):
        ver_lines.append(f"\n{sections['verification_note']}")
    assumptions = sections.get("assumptions") or []
    outlook = str(sections.get("outlook") or "")
    if assumptions:
        outlook += "\n\nAssumptions:\n" + "\n".join(
            f"- **{a['id']}** {a['text']} (rulings: {', '.join(a.get('ruling_ids', []))})"
            for a in assumptions
        )
    parts = [
        f"# Briefing — {cf.question}",
        head,
        "## Executive summary",
        str(sections.get("executive_summary") or "(not written)"),
        "## Event verification",
        "\n".join(ver_lines),
        "## Established findings",
        str(sections.get("established") or "None."),
        "## Contested findings",
        str(sections.get("contested") or "None."),
        "## Conceded findings",
        str(sections.get("conceded") or "None."),
        "## Exposure table",
        exposure_table(cf, digest),
        "## Next-quarter outlook",
        outlook or "(not written)",
        "## Data quality & limitations",
        data_quality_section(cf, digest, limits_hit),
        "## Rejected claims (appendix)",
        rejected_section(digest),
        "## Sources",
        sources_section(cf, repo, cited_refs),
        "## Trace",
        trace_section(cf, prompt_hashes),
    ]
    return "\n\n".join(parts) + "\n"


def deterministic_sections(digest: dict[str, Any], why: str) -> dict[str, Any]:
    """Fallback when Odin cannot write: only digest facts, each with its citation."""

    def lines(key: str) -> str:
        out = []
        for e in digest.get(key, []):
            cites = " ".join(c["cite"] for c in e["citations"][:2])
            out.append(f"- {e['ruling']}: {e['statement']} {cites}".strip())
        return "\n".join(out) or "None."

    return {
        "executive_summary": f"Odin's briefing text is unavailable ({why}); this summary lists ruled findings "
        "only and the case is INCOMPLETE.",
        "established": lines("established"),
        "contested": lines("contested"),
        "conceded": lines("conceded"),
        "outlook": "No outlook was written because the judge's briefing turn did not complete.",
        "assumptions": [],
        "verification_note": "",
    }

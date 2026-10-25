from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

from app.config import AGENTS
from app.memory.store import MemoryStore, reclassify
from app.obs.events import open_bus
from app.runtime import AgentPool, open_case
from app.search.dense import HashEmbedder
from app.tools.asof import Clock
from app.tools.context import CaseContext, Services
from app.tools.guards import run_tool
from app.tools.registry import AGENT_TOOLS
from app.trial.casefile import Claim, DocEvidence, Phase
from app.trial.limits import Budget, TurnBudget
from tests.conftest import AS_OF, QUESTION


def _ctx(services: Services, **limit_over: Any) -> CaseContext:
    ctx = open_case(services, QUESTION, AS_OF)
    if limit_over:
        ctx.limits = ctx.limits.model_copy(update=limit_over)
        ctx.budget = Budget(ctx.limits)
    return ctx


def _call(
    ctx: CaseContext, agent: str, tool: str, args: dict[str, Any], turn: TurnBudget | None = None
) -> dict[str, Any]:
    turn = turn or TurnBudget(agent=agent, phase="R1_SUBMIT", limits=ctx.limits)
    return asyncio.run(run_tool(ctx, agent, "R1_SUBMIT", 1, turn, tool, args, None, set()))


def test_allowlist_is_enforced_by_code(services: Services) -> None:
    ctx = _ctx(services)
    assert "search_documents" not in AGENT_TOOLS["odin"]  # Odin never searches
    assert not {"submit_claims", "submit_attacks", "submit_responses"} & AGENT_TOOLS["heimdall"]
    r = _call(ctx, "odin", "search_documents", {"query": "canal"})
    assert "not permitted" in r["error"]
    assert any(e.type == "tool_denied" for e in ctx.bus.events())


def test_dedupe_canonicalization(services: Services) -> None:
    ctx = _ctx(services)
    a = _call(
        ctx,
        "thor",
        "search_documents",
        {"query": "Canal   Closure", "filters": {"tier_max": 2, "event_id": "EVT-A"}, "k": 3},
    )
    assert "error" not in a
    # same query: case, whitespace, filter order and k differ -> duplicate, not executed
    b = _call(
        ctx,
        "loki",
        "search_documents",
        {"query": " canal closure ", "filters": {"event_id": ["evt-a"], "tier_max": 2}, "k": 7},
    )
    assert b["duplicate_of"] == a["call_id"] and "cached_result" in b
    assert ctx.budget.duplicates_blocked == 1
    executed = [e for e in ctx.bus.events() if e.type == "tool_call" and e.payload.get("status") == "ok"]
    assert len(executed) == 1


def test_budget_exhaustion_never_raises(services: Services) -> None:
    ctx = _ctx(services, MAX_TOOL_CALLS_PER_TURN=2)
    turn = TurnBudget(agent="thor", phase="R1_SUBMIT", limits=ctx.limits)
    for q in ("one", "two"):
        assert "error" not in _call(ctx, "thor", "get_reference", {"sheet": "countries", "key": q}, turn)
    r = _call(ctx, "thor", "get_reference", {"sheet": "sectors"}, turn)
    assert r["error"] == "limit_hit" and r["limit"] == "MAX_TOOL_CALLS_PER_TURN"
    assert any(e.type == "limit_hit" for e in ctx.bus.events())


def test_invalid_args_and_tool_errors_are_structured(services: Services) -> None:
    ctx = _ctx(services)
    r = _call(ctx, "thor", "read_document", {"offset": -1})
    assert r["retryable"] and "invalid arguments" in r["error"]
    r = _call(ctx, "thor", "read_document", {"doc_id": "RM-DOC-0116"})
    assert r["error"] == "NOT_AVAILABLE_AS_OF" and "document" not in r
    r = _call(ctx, "thor", "read_document", {"doc_id": "RM-DOC-0111"})
    assert 'untrusted="true"' in r["document"] and r["injection_warnings"]


def test_view_for_hides_sealed_opponent_claims(services: Services) -> None:
    ctx = _ctx(services)
    cf = ctx.cf
    run = cf.start_run(1)
    ev = DocEvidence(doc_id="RM-DOC-0101", quote="q", pub_date=date(2025, 3, 2), tier=1, event_id="EVT-A")
    for side in ("thor", "loki"):
        run.claims.append(
            Claim(
                id=f"R1-{side[0].upper()}-C01",
                run=1,
                side=side,
                statement="s",  # type: ignore[arg-type]
                type="SOURCED_FACT",
                country=None,
                sector=None,
                parameter=None,
                bound_value=None,
                evidence=[ev],
            )
        )
        run.sealed[side] = True
    thor_view = cf.view_for("thor", Phase.R1_SUBMIT)
    assert [c.side for c in thor_view.runs[-1].claims] == ["thor"]
    assert len(cf.view_for("thor", Phase.R2_ATTACK).runs[-1].claims) == 2
    assert len(cf.runs[-1].claims) == 2  # the original is untouched


def test_memory_isolation_and_hygiene(tmp_path: Path, services: Services) -> None:
    pool = AgentPool(services)
    try:
        paths = {a: pool.memories[a].path for a in AGENTS}
        assert len(set(paths.values())) == 4  # one SQLite file per agent
        pool.memories["thor"].add("procedural", "corroborate duration claims", ["duration"])
        clock = Clock(AS_OF)
        assert pool.memories["thor"].recall("duration claims", 5, clock)
        assert pool.memories["loki"].recall("duration claims", 5, clock) == []
        for a in AGENTS:  # each agent object holds only its own store
            agent = getattr(pool.set, a)
            assert agent.memory is pool.memories[a]
    finally:
        pool.close()
    assert reclassify("procedural", "doc RM-DOC-0101 was retracted") == "fact"
    m = MemoryStore("odin", tmp_path / "odin.sqlite", HashEmbedder())
    a, merged_a = m.add("procedural", "Tier 4 alone is never established")
    b, merged_b = m.add("procedural", "Tier 4 alone is never established")
    assert a == b and merged_b and not merged_a
    m.log_query("fp1", "q", {}, 0, "C1", 1, "2025-04-10", "C1:0001")
    assert m.zero_result_queries(Clock(date(2025, 4, 10))) and not m.zero_result_queries(
        Clock(date(2025, 1, 1))
    )


def test_event_bus_is_append_only_jsonl(tmp_path: Path) -> None:
    bus = open_bus("CASE-TEST-0001", tmp_path / "events.jsonl")
    bus.emit("phase_changed", actor="thing", payload={"from": "A", "to": "B"})
    bus.emit("tool_call", actor="thor", payload={"tool": "x"})
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and '"seq":1' in lines[0] and '"seq":2' in lines[1]

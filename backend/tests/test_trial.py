"""Integration tests: full cases through the real agent loop, guard chain and Thing, with the FakeLLM."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

from app.config import Settings
from app.llm.client import LLMResponse, ToolCall, Usage
from app.llm.fake import ScriptedLLM
from app.obs.events import read_events_file
from app.obs.replay import replay, score
from app.runtime import AgentPool, build_services, ingest, live_pack_roots, run_case
from app.tools.context import Services
from app.trial.casefile import CaseFile
from tests.conftest import AS_OF, MINI, PACK, QUESTION


def _run(
    services: Services, question: str = QUESTION, as_of: date = AS_OF, pool: AgentPool | None = None
) -> tuple[CaseFile, Path, list[Any]]:
    cf = asyncio.run(run_case(services, question, as_of, pool=pool))
    case_dir = services.settings.runs_dir / cf.case_id
    return cf, case_dir, read_events_file(case_dir / "events.jsonl")


def test_full_case_two_runs_remand_and_artifacts(services: Services) -> None:
    cf, case_dir, events = _run(services)
    types = [e.type for e in events]
    assert cf.status == "COMPLETE", cf.status_reasons
    assert len(cf.runs) >= 2 and "remand" in types
    assert cf.runs[1].research is not None and cf.new_evidence(2)  # run 2 brought new evidence
    # sealed Round 1, and each side searched itself
    assert types.count("sealed_commit") >= 2 * len(cf.runs)
    r1_searchers = {
        e.actor
        for e in events
        if e.type == "tool_call"
        and e.payload.get("tool") == "search_documents"
        and e.phase == "R1_SUBMIT"
        and e.run == 1
    }
    assert {"thor", "loki"} <= r1_searchers
    # >= 2 metadata filters on at least one search
    assert any(
        len([k for k, v in e.payload["args"]["filters"].items() if v not in (None, [], False)]) >= 2
        for e in events
        if e.type == "tool_call"
        and e.payload.get("tool") == "search_documents"
        and e.payload.get("status") == "ok"
    )
    # cross-examination on both sides, every claim ruled, >= 1 rejected/conceded
    for r in cf.runs:
        assert {a.by for a in r.attacks} == {"thor", "loki"}
        assert {x.claim_id for x in r.rulings} >= {c.id for c in r.claims}
    assert any(x.verdict in ("REJECTED", "CONCEDED") for r in cf.runs for x in r.rulings)
    # memory changed an action (Heimdall's deterministic query skip)
    assert any(e.type == "memory_influence" and e.payload.get("deterministic") for e in events)
    # Odin never searched; no duplicate query executed
    assert not any(
        e.type == "tool_call" and e.actor == "odin" and e.payload.get("tool") == "search_documents"
        for e in events
    )
    fps = [e.payload["fingerprint"] for e in events if e.type == "tool_call" and e.payload.get("fingerprint")]
    assert len(fps) == len(set(fps))
    for rel in (
        "config.json",
        "manifest_snapshot.json",
        "briefing.md",
        "briefing.json",
        "summary.json",
        "run_1/remand.json",
        "run_1/rulings.json",
        "run_2/research_report.json",
        "memory_diff/thor.json",
    ):
        assert (case_dir / rel).exists(), rel
    md = (case_dir / "briefing.md").read_text(encoding="utf-8")
    for section in (
        "## Established findings",
        "## Contested findings",
        "## Conceded findings",
        "## Exposure table",
        "## Next-quarter outlook",
        "## Sources",
        "Assumptions:",
    ):
        assert section in md
    rep = replay(case_dir, services.repo)
    assert rep["briefing_identical"] and rep["exposure_mismatches"] == 0, rep.get("briefing_diff")
    sc = score(case_dir, services.repo, MINI / "truth.json")
    assert sc["citation_validity_pct"] == 100.0 and sc["duplicate_queries_executed"] == 0
    assert sc["range_coverage"] and sc["range_coverage"][0]["odin_covers"]


def test_rumour_is_rejected_with_reasons(services: Services) -> None:
    cf, case_dir, events = _run(
        services, "How exposed is Eskerland's chemicals sector to the Ravenmouth Strait closure?"
    )
    assert cf.status == "REJECTED" and cf.verification is not None and cf.verification.verdict == "REFUTED"
    assert not cf.runs and any(
        e.type == "trap_detected" and e.payload.get("type") == "RUMOUR" for e in events
    )
    assert "REFUTED" in (case_dir / "briefing.md").read_text(encoding="utf-8")


def test_event_known_only_after_as_of_is_not_revealed(services: Services) -> None:
    cf, _, _ = _run(
        services,
        "How exposed is Eskerland's chemicals sector to the Eskhaven port strike?",
        date(2025, 4, 10),
    )
    assert cf.status == "REJECTED" and cf.verification is not None
    assert cf.verification.verdict in ("NOT_YET_KNOWN", "NOT_FOUND")


def test_side_failure_marks_other_side_unchallenged(settings: Settings) -> None:
    svc = build_services(settings.model_copy(update={"LLM_FAULT_INJECT": "thor", "LLM_MAX_RETRIES": 0}))
    cf, case_dir, events = _run(svc)
    assert cf.status == "INCOMPLETE"
    assert cf.runs[0].side_status["thor"].value == "FAILED"
    assert all(c.unchallenged for c in cf.runs[0].claims if c.side == "loki")
    assert any(e.type == "agent_failed" and e.actor == "thor" for e in events)
    assert "Loki's case went unchallenged" in (case_dir / "briefing.md").read_text(encoding="utf-8")


def test_budget_exhaustion_ends_cleanly(settings: Settings) -> None:
    svc = build_services(settings.model_copy(update={"MAX_TOOL_CALLS_PER_CASE": 22}))
    cf, _, events = _run(svc)
    assert cf.status in ("INCOMPLETE", "ABORTED")
    assert any(e.type == "limit_hit" for e in events)
    assert cf.status != "COMPLETE"


def test_heimdall_failure_aborts_honestly(services: Services) -> None:
    bad = LLMResponse(
        tool_calls=[ToolCall(id="1", name="submit_case_file", args={"verdict": "MAYBE"})], usage=Usage(10, 10)
    )
    pool = AgentPool(services, llm_override={"heimdall": ScriptedLLM([bad, bad, bad])})
    try:
        cf, case_dir, _ = _run(services, pool=pool)
    finally:
        pool.close()
    assert cf.status == "ABORTED" and "could not be built" in (case_dir / "briefing.md").read_text(
        encoding="utf-8"
    )


def test_memory_changes_behaviour_across_cases(services: Services) -> None:
    pool = AgentPool(services)
    try:
        _run(services, pool=pool)  # case A writes lessons
        cf, _, events = _run(
            services,
            "How exposed is Eskerland's chemicals sector to the Eskhaven port strike?",
            date(2025, 6, 20),
            pool=pool,
        )
    finally:
        pool.close()
    assert cf.status in ("COMPLETE", "INCOMPLETE")
    declared = [e for e in events if e.type == "memory_influence" and e.payload.get("declared")]
    assert declared, "a recalled lesson should change what an agent did in case B"
    reads = {mid for e in events if e.type == "memory_read" for mid in e.payload.get("memory_ids", [])}
    assert all(e.payload["memory_id"] in reads for e in declared)


def test_live_pack_case_cites_pack_documents(services: Services, settings: Settings) -> None:
    live = settings.live_packs_dir
    live.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PACK, live / "live_pack_d")
    ingest(services, live_pack_roots(live))
    cf, case_dir, _ = _run(
        services,
        "How exposed is Brenmark's automotive sector to the Ironway Rail Link derailment?",
        date(2025, 5, 10),
    )
    assert cf.verification is not None and cf.verification.verdict == "VERIFIED"
    cites = json.loads((case_dir / "briefing.json").read_text(encoding="utf-8"))["citations"]
    assert any("RM-DOC-05" in c for c in cites)

"""Heimdall, the Gatekeeper: builds and verifies the Case File, re-searches on remand. Never debates."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
from pydantic import ValidationError

from app.agents import prompt_lib
from app.agents.base import Agent, TurnOutcome
from app.agents.views import pool_summary, research_view
from app.obs.events import to_json
from app.tools.catalog import SearchArgs
from app.tools.context import CaseContext, fingerprint
from app.tools.submit_tools import SubmitCaseFileArgs, SubmitResearchArgs
from app.trial.casefile import ResearchReport
from app.trial.validators import Applied, apply_case_file, apply_research

NEAR_DUPLICATE = 0.92


class Heimdall(Agent):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.last_report: ResearchReport | None = None
        self._research_searches = 0

    # ------------------------------------------------------------------ GATHERING
    async def gather(self, ctx: CaseContext) -> TurnOutcome:
        s = self.settings
        cf = ctx.cf
        view = {
            "phase": "GATHERING",
            "question": cf.question,
            "as_of": cf.as_of,
            "horizon_months": s.HORIZON_MONTHS,
            "pool_size": s.POOL_SIZE_R1,
            "max_scope_rows": s.MAX_SCOPE_ROWS,
        }
        instruction = prompt_lib.render(
            prompt_lib.load("turn_gather"),
            {
                "QUESTION": cf.question,
                "AS_OF": cf.as_of.isoformat(),
                "POOL_SIZE": s.POOL_SIZE_R1,
                "HORIZON": f"the {s.HORIZON_MONTHS} months after {cf.as_of.isoformat()}",
            },
        )

        def handle(a: Any, last: bool) -> Applied:
            assert isinstance(a, SubmitCaseFileArgs)
            return apply_case_file(ctx, a, last)

        return await self.turn(
            ctx,
            "GATHERING",
            None,
            instruction,
            view,
            {"submit_case_file": handle},
            system_extra={"RULE_VERDICT": "given by event_profile", "DEFICIENCIES": "(none yet)"},
            recall_context=f"gather verify event {cf.question}",
        )

    # ------------------------------------------------------------------ RESEARCH
    async def research(self, ctx: CaseContext, run: int) -> TurnOutcome:
        """Answer the remand of `run`; new items join the pool for run + 1."""
        cf = ctx.cf
        self.last_report = None
        self._research_searches = 0
        defs = cf.deficiencies(run)
        past = ctx.ledger.queries_by("heimdall")
        view = research_view(cf, run, past)
        instruction = prompt_lib.render(
            prompt_lib.load("turn_research"),
            {
                "RUN": run,
                "DEFICIENCIES": to_json([d.model_dump() for d in defs]),
                "POOL_SUMMARY": f"{len(cf.pool)} items (see case_view.pool)",
                "PAST_QUERIES": to_json(past),
                "MAX_REMAND_QUERIES": ctx.limits.MAX_REMAND_QUERIES,
            },
        )
        def_ids = [d.id for d in defs]

        def handle(a: Any, last: bool) -> Applied:
            assert isinstance(a, SubmitResearchArgs)
            queries = [
                str(e.args.get("query", ""))
                for e in ctx.ledger.entries()
                if e.agent == "heimdall"
                and e.tool == "search_documents"
                and e.phase == "RESEARCH"
                and e.run == run
            ]
            applied, report = apply_research(ctx, a, last, run + 1, def_ids, queries)
            if report is not None:
                self.last_report = report
            return applied

        return await self.turn(
            ctx,
            "RESEARCH",
            run,
            instruction,
            view,
            {"submit_case_file": handle},
            system_extra={
                "RULE_VERDICT": ctx.cf.verification.rule_verdict if ctx.cf.verification else "n/a",
                "DEFICIENCIES": "; ".join(d.statement for d in defs),
            },
            recall_context="research remand queries " + " ".join(d.statement for d in defs),
        )

    # ------------------------------------------------------------------ deterministic memory skip
    def before_tool(
        self, ctx: CaseContext, phase: str, name: str, args: dict[str, Any], recalled: set[str]
    ) -> dict[str, Any] | None:
        if phase != "RESEARCH" or name != "search_documents":
            return None
        try:
            parsed = SearchArgs.model_validate(args or {})
        except ValidationError:
            return None
        if self._research_searches >= ctx.limits.MAX_REMAND_QUERIES:
            ctx.budget.record_limit("MAX_REMAND_QUERIES")
            ctx.emit(
                "limit_hit",
                "thing",
                {
                    "limit": "MAX_REMAND_QUERIES",
                    "agent": "heimdall",
                    "phase": phase,
                    "value": self._research_searches,
                    "action_taken": "search refused",
                },
            )
            return {
                "error": "limit_hit",
                "limit": "MAX_REMAND_QUERIES",
                "retryable": False,
                "message": "Remand search budget used. Submit your research now.",
            }
        canon: dict[str, Any] = {
            "query": " ".join(parsed.query.lower().split()),
            "filters": parsed.filters.canonical(),
        }
        fp = fingerprint("search_documents", canon)
        match = self._zero_result_match(ctx, fp, canon)
        if match is None:
            self._research_searches += 1
            return None
        mem_id = self._query_memory(ctx, match)
        recalled.add(mem_id)
        effect = (
            f"skipped query '{canon['query']}' ({_filters_text(canon['filters'])}): it returned 0 results in "
            f"{match.get('case_id')} run {match.get('run')}; reformulate with different wording or filters"
        )
        ctx.emit("memory_read", "heimdall", {"agent": "heimdall", "memory_ids": [mem_id], "via": "query_log"})
        ctx.emit(
            "memory_influence",
            "heimdall",
            {
                "agent": "heimdall",
                "memory_id": mem_id,
                "effect": effect,
                "action_ref": f"skip:{fp[:12]}",
                "declared": False,
                "deterministic": True,
            },
        )
        return {
            "skipped_by_memory": mem_id,
            "executed": False,
            "note": f"Your memory {mem_id} says this query returned 0 results before. It was not run again. "
            "Reformulate: different wording, filters, tier, source_type or date window.",
        }

    def _zero_result_match(self, ctx: CaseContext, fp: str, canon: dict[str, Any]) -> dict[str, Any] | None:
        rows = self.memory.zero_result_queries(ctx.clock)
        for r in rows:
            if r["fingerprint"] == fp:
                return r
        emb = self.memory.embedder
        if emb is None:
            return None
        q = emb.embed_query(canon["query"])
        want_filters = json.dumps(canon["filters"], sort_keys=True)
        for r in rows:
            if r["filters"] != want_filters:
                continue
            v = emb.embed_query(r["query"])
            if float(np.dot(q, v)) > NEAR_DUPLICATE:
                return r
        return None

    def _query_memory(self, ctx: CaseContext, row: dict[str, Any]) -> str:
        tag = f"fp:{row['fingerprint'][:16]}"
        for m in self.memory.all():
            if m.kind == "query" and tag in m.tags:
                return m.id
        mid, _ = self.memory.add(
            "query",
            _query_text(row),
            ["zero_results", tag],
            row.get("case_id"),
            row.get("run"),
            row.get("knowledge_date"),
            0.7,
        )
        ctx.emit("memory_write", "heimdall", {"agent": "heimdall", "memory_id": mid, "kind": "query"})
        return mid

    def after_turn(
        self, ctx: CaseContext, phase: str, run: int | None, outcome: TurnOutcome, calls: list[dict[str, Any]]
    ) -> None:
        """Zero-result searches become `query` memories (knowledge_date = this case's as-of)."""
        for c in calls:
            if c["tool"] != "search_documents":
                continue
            res = c["result"]
            if res.get("error") or res.get("skipped_by_memory") or res.get("duplicate_of"):
                continue
            if res.get("results"):
                continue
            try:
                parsed = SearchArgs.model_validate(c["args"] or {})
            except ValidationError:
                continue
            canon: dict[str, Any] = {
                "query": " ".join(parsed.query.lower().split()),
                "filters": parsed.filters.canonical(),
            }
            fp = fingerprint("search_documents", canon)
            row = {
                "fingerprint": fp,
                "query": canon["query"],
                "filters": json.dumps(canon["filters"], sort_keys=True),
                "case_id": ctx.case_id,
                "run": run,
                "knowledge_date": ctx.clock.iso,
            }
            self._query_memory(ctx, row)


def _filters_text(f: dict[str, Any]) -> str:
    active = {k: v for k, v in f.items() if v not in (None, [], False)}
    return ", ".join(f"{k}={v}" for k, v in active.items()) or "no filters"


def _query_text(row: dict[str, Any]) -> str:
    try:
        f = json.loads(row.get("filters") or "{}")
    except ValueError:
        f = {}
    return f"search_documents '{row.get('query')}' ({_filters_text(f)}) -> 0 results"


def pool_brief(ctx: CaseContext) -> list[dict[str, Any]]:
    return pool_summary(ctx.cf)

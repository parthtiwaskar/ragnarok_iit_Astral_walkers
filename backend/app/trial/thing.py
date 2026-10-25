"""The Thing: the deterministic trial protocol (TRIAL_PROTOCOL.md). It is NOT an agent and contains no LLM.

It knows whose turn it is, which structural rules a submission must satisfy, and how much budget is left.
It never reads claim content to make decisions and never assigns work.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.agents.base import TurnOutcome
from app.agents.heimdall import Heimdall
from app.agents.odin import Odin
from app.agents.sides import SideAgent
from app.briefing.digest import all_citable_refs, build_digest
from app.briefing.render import deterministic_sections, render_markdown
from app.briefing.validate import sanitize, validate_sections
from app.memory.reflect import reflect
from app.obs.artifacts import Artifacts, git_commit
from app.tools.asof import Clock
from app.tools.citation import CITE_RX as CITATION
from app.tools.citation import cite_ref, normalize_cite, verify_cite
from app.tools.context import CaseContext
from app.trial.casefile import (
    SIDES,
    CaseFile,
    Phase,
    Ruling,
    SideStatus,
    Sufficiency,
)
from app.trial.judging import claims_to_rule, compute_final_ranges
from app.trial.sufficiency import checks_for, deterministic_checklist, make_deficiencies

REJECT_VERDICTS = {"NOT_FOUND", "UNCONFIRMED", "REFUTED", "RETRACTED", "NOT_YET_KNOWN"}
CASE_LIMITS = {"MAX_TOOL_CALLS_PER_CASE", "MAX_TOKENS_PER_CASE", "CASE_TIMEOUT_S"}


@dataclass
class AgentSet:
    heimdall: Heimdall
    thor: SideAgent
    loki: SideAgent
    odin: Odin

    def all(self) -> list[Any]:
        return [self.heimdall, self.thor, self.loki, self.odin]

    def side(self, name: str) -> SideAgent:
        return self.thor if name == "thor" else self.loki


class Thing:
    def __init__(
        self, ctx: CaseContext, agents: AgentSet, artifacts: Artifacts, models: dict[str, Any]
    ) -> None:
        self.ctx = ctx
        self.agents = agents
        self.art = artifacts
        self.models = models
        self.reasons: list[str] = []
        self.failures: list[str] = []
        self.pending_research: Any = None

    # ------------------------------------------------------------------ plumbing
    @property
    def cf(self) -> CaseFile:
        return self.ctx.cf

    def reason(self, r: str) -> None:
        if r not in self.reasons:
            self.reasons.append(r)

    def transition(self, to: Phase, reason: str = "") -> None:
        frm = self.cf.phase
        self.cf.phase = to
        run = self.cf.current_run.n if self.cf.current_run else None
        self.ctx.bus.context.update({"phase": to.value, "run": run})
        self.ctx.emit(
            "phase_changed", "thing", {"from": frm.value, "to": to.value, "run": run, "reason": reason}
        )
        self.ctx.bus.sync()
        self.art.save_case_file(self.cf)

    async def turn(
        self, agent: str, phase: str, factory: Callable[[], Awaitable[TurnOutcome]]
    ) -> TurnOutcome:
        """Timeout, exception capture and logging around every agent turn. Never raises into the Thing."""
        left = self.ctx.budget.time_left()
        if left <= 0:
            self.ctx.budget.record_limit("CASE_TIMEOUT_S")
            self.ctx.emit(
                "limit_hit",
                "thing",
                {"limit": "CASE_TIMEOUT_S", "agent": agent, "phase": phase, "action_taken": "turn skipped"},
            )
            return TurnOutcome(ok=False, status="LIMIT", reason="CASE_TIMEOUT_S")
        timeout = max(1.0, min(self.ctx.limits.TURN_TIMEOUT_S, left))
        self.ctx.emit(
            "turn_started", "thing", {"agent": agent, "phase": phase, "timeout_s": round(timeout, 1)}
        )
        try:
            out = await asyncio.wait_for(factory(), timeout=timeout)
        except TimeoutError:
            out = TurnOutcome(ok=False, status="FAILED", reason="TURN_TIMEOUT")
            self.ctx.budget.record_limit("TURN_TIMEOUT_S")
        except Exception as exc:
            out = TurnOutcome(ok=False, status="FAILED", reason=f"{type(exc).__name__}: {str(exc)[:300]}")
        if not out.ok:
            self.ctx.emit(
                "agent_failed",
                agent,
                {
                    "agent": agent,
                    "phase": phase,
                    "error": out.reason,
                    "retries": self.ctx.limits.LLM_MAX_RETRIES,
                },
            )
        self.ctx.emit(
            "turn_finished",
            "thing",
            {
                "agent": agent,
                "phase": phase,
                "status": out.status,
                "reason": out.reason,
                "tool_calls": out.tool_calls,
                "tokens": out.tokens,
                "reprompts": out.reprompts,
            },
        )
        return out

    # ------------------------------------------------------------------ the protocol
    async def run(self) -> CaseFile:
        try:
            await self._run()
        except Exception as exc:  # last resort: never crash, never pretend
            self.ctx.emit(
                "agent_failed",
                "thing",
                {"agent": "thing", "phase": self.cf.phase.value, "error": f"{type(exc).__name__}: {exc}"},
            )
            self.cf.status = "ABORTED"
            self.cf.status_reasons = [*self.reasons, f"INTERNAL_ERROR: {type(exc).__name__}"]
            self.transition(Phase.ABORTED, "internal error")
            self.transition(Phase.CLOSED, "aborted")
        finally:
            self.ctx.bus.sync()
        return self.cf

    async def _run(self) -> None:
        ctx, cf, s = self.ctx, self.cf, self.ctx.services.settings
        from app.agents import prompt_lib

        hashes = prompt_lib.file_hashes()
        ctx.emit(
            "case_opened",
            "thing",
            {
                "question": cf.question,
                "as_of": cf.as_of,
                "index_version": cf.index_version,
                "limits": ctx.limits.model_dump(),
                "models": self.models,
                "prompt_hashes": hashes,
                "pinned_ingest_seq": cf.pinned_ingest_seq,
            },
        )
        self.art.write(
            "config.json",
            {
                "case_id": cf.case_id,
                "as_of": cf.as_of,
                "question": cf.question,
                "limits": ctx.limits.model_dump(),
                "models": self.models,
                "prompt_file_hashes": hashes,
                "index_version": cf.index_version,
                "git_commit": git_commit(),
                "settings": s.public_dict(),
            },
        )
        self.art.save_manifest_snapshot(ctx.services.repo, ctx.clock)
        self.mem_before = {a.name: a.memory.snapshot() for a in self.agents.all()}
        if ctx.services.repo.get_meta("manifest_present", "1") != "1":
            self.reason("NO_MANIFEST_CITATIONS_UNVERIFIABLE")

        # ---------------- GATHERING
        self.transition(Phase.GATHERING, "Heimdall's turn")
        out = await self.turn("heimdall", "GATHERING", functools.partial(self.agents.heimdall.gather, ctx))
        if not out.ok or cf.verification is None:
            cf.status = "ABORTED"
            cf.status_reasons = [*self.reasons, "HEIMDALL_FAILED: could not build the case"]
            self.transition(Phase.ABORTED, "Heimdall could not build the case")
            self._write_rejection(
                "The case could not be built: Heimdall failed in GATHERING "
                f"({out.reason or 'no valid Case File'})."
            )
            self.transition(Phase.CLOSED, "aborted")
            return
        if cf.verification.verdict in REJECT_VERDICTS:
            v = cf.verification
            if v.verdict in ("UNCONFIRMED", "REFUTED"):
                trap = {
                    "type": "RUMOUR",
                    "doc_id": ",".join(v.contradicting_docs or v.supporting_docs),
                    "detail": f"verdict {v.verdict}",
                    "reasons": v.reasons,
                }
                cf.traps.append(trap)
                ctx.emit("trap_detected", "heimdall", trap)
            cf.status = "REJECTED"
            cf.status_reasons = [f"EVENT_{v.verdict}"]
            self.transition(Phase.CASE_REJECTED, f"verdict {v.verdict}")
            self._write_rejection(None)
            await self._reflect_all()
            self.transition(Phase.CLOSED, "user told why")
            return

        # ---------------- runs
        run = 1
        while True:
            r = cf.start_run(run)
            if self.pending_research is not None:
                r.research = self.pending_research
                self.pending_research = None
            self.transition(Phase.R1_SUBMIT, f"run {run} round 1 (sealed)")
            t_out, l_out = await asyncio.gather(
                self.turn("thor", "R1_SUBMIT", functools.partial(self.agents.thor.submit, ctx, run)),
                self.turn("loki", "R1_SUBMIT", functools.partial(self.agents.loki.submit, ctx, run)),
            )
            for side, o in (("thor", t_out), ("loki", l_out)):
                if not o.ok:
                    r.side_status[side] = SideStatus.FAILED
                    self.failures.append(f"Run {run}: {side.capitalize()} failed in Round 1 ({o.reason})")
            self.transition(Phase.R1_REVEAL, "both committed or failed")
            self._reveal(run)
            failed = [sd for sd in SIDES if r.side_status[sd] == SideStatus.FAILED]
            if len(failed) == 2:
                self.reason("BOTH_SIDES_FAILED")
                break
            # ---- Round 2A
            self.transition(Phase.R2_ATTACK, "cross-examination")
            attackers = [
                sd
                for sd in SIDES
                if r.side_status[sd] == SideStatus.OK and cf.active_claims("loki" if sd == "thor" else "thor")
            ]
            outs = await asyncio.gather(
                *[
                    self.turn(sd, "R2_ATTACK", functools.partial(self.agents.side(sd).attack, ctx, run))
                    for sd in attackers
                ]
            )
            for sd, o in zip(attackers, outs, strict=True):
                if not o.ok:
                    r.side_status[sd] = SideStatus.FAILED_TO_ATTACK
                    self.failures.append(f"Run {run}: {sd.capitalize()} filed no valid attack ({o.reason})")
                    ctx.emit("side_failed_to_attack", "thing", {"side": sd, "reason": o.reason})
            # ---- Round 2B
            self.transition(Phase.R2_RESPOND, "responses")
            responders = [
                sd
                for sd in SIDES
                if r.side_status[sd] != SideStatus.FAILED
                and any((c := cf.claim(a.target_claim)) is not None and c.side == sd for a in r.attacks)
            ]
            outs = await asyncio.gather(
                *[
                    self.turn(sd, "R2_RESPOND", functools.partial(self.agents.side(sd).respond, ctx, run))
                    for sd in responders
                ]
            )
            for sd, o in zip(responders, outs, strict=True):
                if not o.ok:
                    mine = {c.id for c in cf.all_claims() if c.side == sd}
                    for a in r.attacks:
                        if (
                            a.target_claim in mine
                            and a.id not in r.unrebutted
                            and not any(x.attack_id == a.id for x in r.responses)
                        ):
                            r.unrebutted.append(a.id)
                    self.failures.append(f"Run {run}: {sd.capitalize()} did not answer attacks ({o.reason})")
            self._precheck(run)
            # ---- JUDGING
            self.transition(Phase.JUDGING, "Odin rules")
            o_out = await self.turn("odin", "JUDGING", functools.partial(self.agents.odin.judge, ctx, run))
            if not o_out.ok or r.sufficiency is None:
                self._odin_failed(run, o_out.reason)
            self.art.save_run(cf, run)
            suff = r.sufficiency
            assert suff is not None
            if suff.verdict == "SUFFICIENT":
                break
            if run >= ctx.limits.MAX_RUNS:
                self.reason("MAX_RUNS_REACHED_INSUFFICIENT")
                ctx.budget.record_limit("MAX_RUNS")
                ctx.emit(
                    "limit_hit", "thing", {"limit": "MAX_RUNS", "value": run, "action_taken": "briefing"}
                )
                break
            if ctx.budget.exhausted:
                lim = ctx.budget.exhausted_reason() or "BUDGET"
                ctx.emit("limit_hit", "thing", {"limit": lim, "action_taken": "briefing (incomplete)"})
                self.reason(f"LIMIT_{lim}")
                break
            # ---- REMAND -> RESEARCH
            self.transition(Phase.REMANDED, "insufficient; deficiencies only")
            ctx.emit(
                "remand", "odin", {"run": run, "deficiencies": [d.model_dump() for d in suff.deficiencies]}
            )
            self.art.save_remand(cf, run)
            self._refresh_pin()
            self.transition(Phase.RESEARCH, "Heimdall's turn")
            h_out = await self.turn(
                "heimdall", "RESEARCH", functools.partial(self.agents.heimdall.research, ctx, run)
            )
            report = self.agents.heimdall.last_report
            if not h_out.ok or report is None:
                self.reason("RESEARCH_FAILED")
                self.failures.append(f"Run {run}: Heimdall's re-search failed ({h_out.reason})")
                break
            new = sorted(cf.new_evidence(run + 1))
            ctx.emit("research_report", "heimdall", {**report.model_dump(), "new_evidence": new})
            if not new:
                self.reason("REMAND_YIELDED_NO_NEW_EVIDENCE")
                self.art.write(f"run_{run + 1}/research_report.json", report.model_dump())
                break
            self.pending_research = report
            run += 1
        await self._briefing()
        await self._reflect_all()
        self._summary()
        self.transition(Phase.CLOSED, "briefing delivered")

    # ------------------------------------------------------------------ steps
    def _reveal(self, run: int) -> None:
        r = self.cf.run(run)
        for side in SIDES:
            r.sealed[side] = False
        failed = [sd for sd in SIDES if r.side_status[sd] == SideStatus.FAILED]
        if len(failed) == 1:
            other = "loki" if failed[0] == "thor" else "thor"
            for c in r.claims:
                if c.side == other:
                    c.unchallenged = True
            self.ctx.emit(
                "side_unchallenged", "thing", {"failed": failed[0], "unchallenged": other, "run": run}
            )
        for c in r.claims:
            self.ctx.emit("claim_submitted", c.side, {**c.model_dump(), "sealed_at": r.sealed_at.get(c.side)})
        for p in r.positions:
            self.ctx.emit("position_submitted", p.side, p.model_dump())
        if r.conceded_prior:
            self.ctx.emit("claims_conceded", "thing", {"run": run, "claims": r.conceded_prior})

    def _precheck(self, run: int) -> None:
        """Deterministic citation pre-check over every evidence item of the run, before Odin rules."""
        r = self.cf.run(run)
        totals = {"items": 0, "valid": 0}
        for checks in r.citation_checks.values():
            for c in checks:
                totals["items"] += 1
                totals["valid"] += int(c.valid)
        for cl in claims_to_rule(self.ctx, run):
            if cl.run < run:
                for chk in checks_for(self.cf, cl.id):
                    totals["items"] += 1
                    totals["valid"] += int(chk.valid)
        self.ctx.emit("citation_precheck", "thing", {"run": run, **totals})

    def _odin_failed(self, run: int, why: str) -> None:
        ctx, cf = self.ctx, self.cf
        r = cf.run(run)
        self.reason("ODIN_FAILED")
        self.failures.append(f"Run {run}: Odin failed to rule ({why}); rulings recorded as NO_RULING")
        if not r.rulings:
            r.rulings = [
                Ruling(
                    id=f"RUL:{c.id}:R{run}",
                    claim_id=c.id,
                    run=run,
                    verdict="NO_RULING",
                    basis="UNSUPPORTED",
                    finding="REJECTED",
                    rationale="Odin failed; no ruling.",
                    decisive_evidence=[],
                    citation_issues=checks_for(cf, c.id),
                )
                for c in claims_to_rule(ctx, run)
            ]
            for rul in r.rulings:
                ctx.emit("ruling_issued", "thing", rul.model_dump())
        if not r.final_ranges:
            r.final_ranges = compute_final_ranges(ctx, run)
            for fr in r.final_ranges:
                fr.justification = "WARNING: Odin failed; hull of both sides' intervals. " + fr.justification
                ctx.emit("final_range", "thing", fr.model_dump())
        checklist, gaps, _ = deterministic_checklist(cf, run, ctx.services.settings.MAX_CONFIDENT_WIDTH_PP)
        gaps.append(
            {"check": "S5", "statement": "No judicial ruling is available for this run.", "related": []}
        )
        r.sufficiency = Sufficiency(
            verdict="INSUFFICIENT", checklist=checklist, deficiencies=make_deficiencies(run, gaps)
        )
        ctx.emit("sufficiency", "thing", r.sufficiency.model_dump())

    def _refresh_pin(self) -> None:
        """Live-ingested docs become visible to a remand re-search (logged)."""
        ctx = self.ctx
        current = ctx.services.repo.current_ingest_seq()
        if ctx.clock.pin is not None and current > ctx.clock.pin:
            ctx.emit(
                "index_refreshed",
                "thing",
                {
                    "from_seq": ctx.clock.pin,
                    "to_seq": current,
                    "reason": "remand re-search sees live-ingested packs",
                },
            )
            ctx.clock = Clock(as_of=ctx.clock.as_of, pin=current, trade_rule=ctx.clock.trade_rule)
            self.cf.pinned_ingest_seq = current

    # ------------------------------------------------------------------ status + briefing
    def _status(self) -> tuple[str, list[str]]:
        cf, ctx = self.cf, self.ctx
        reasons = list(self.reasons)
        last = cf.runs[-1] if cf.runs else None
        insufficient = last is None or last.sufficiency is None or last.sufficiency.verdict != "SUFFICIENT"
        specific = any(r.startswith(("MAX_RUNS", "REMAND", "RESEARCH", "BOTH", "LIMIT")) for r in reasons)
        if insufficient and not specific and "INSUFFICIENT_LAST_RUN" not in reasons:
            reasons.append("INSUFFICIENT_LAST_RUN")
        for r in cf.runs:
            for side, st in r.side_status.items():
                if st != SideStatus.OK:
                    tag = f"{side.upper()}_{st.value}_RUN{r.n}"
                    if tag not in reasons:
                        reasons.append(tag)
            if any(x.verdict == "NO_RULING" for x in r.rulings) and "NO_RULING" not in reasons:
                reasons.append("NO_RULING")
        for lim in ctx.budget.limits_hit:
            if lim in CASE_LIMITS and f"LIMIT_{lim}" not in reasons:
                reasons.append(f"LIMIT_{lim}")
        return ("COMPLETE" if not reasons else "INCOMPLETE"), reasons

    def _verify_cite(self, c: str) -> bool:
        return verify_cite(c, self.ctx.services.repo, self.ctx.clock, self.ctx.event_id)

    async def _briefing(self) -> None:
        ctx, cf = self.ctx, self.cf
        self.transition(Phase.BRIEFING, "Odin writes from rulings only")
        status, reasons = self._status()
        digest = build_digest(cf, self.failures, status, reasons)

        def validate(sections: dict[str, Any]) -> list[str]:
            return validate_sections(sections, digest, self._verify_cite).errors

        sections: dict[str, Any] | None = None
        if ctx.budget.time_left() > 0 and not ctx.budget.exhausted:
            out = await self.turn(
                "odin",
                "BRIEFING",
                functools.partial(self.agents.odin.brief, ctx, digest, status, reasons, validate),
            )
            sections = self.agents.odin.briefing_sections if out.ok else None
            if sections is None:
                self.reason("ODIN_BRIEFING_FAILED")
        else:
            self.reason("ODIN_BRIEFING_SKIPPED_LIMIT")
        if sections is None:
            sections = deterministic_sections(digest, "judge's briefing turn did not complete")
        chk = validate_sections(sections, digest, self._verify_cite)
        sanitized = False
        if not chk.ok:
            sections = sanitize(sections, digest, chk)
            sanitized = True
            self.reason("BRIEFING_SANITIZED")
        status, reasons = self._status()
        cf.status = status  # type: ignore[assignment]
        cf.status_reasons = reasons
        digest["status"], digest["status_reasons"] = status, reasons
        cited = {
            normalize_cite(m)
            for name in ("executive_summary", "established", "contested", "conceded", "outlook")
            for m in CITATION.finditer(str(sections.get(name) or ""))
        }
        cited_refs = {r for r in (cite_ref(c) for c in cited) if r}
        cited_refs |= all_citable_refs(cf)
        md = render_markdown(
            cf,
            digest,
            sections,
            ctx.services.repo,
            status,
            reasons,
            ctx.budget.limits_hit,
            ctx.prompt_hashes,
            cited_refs,
        )
        final_chk = validate_sections(sections, digest, self._verify_cite)
        path = self.art.write("briefing.md", md)
        self.art.write(
            "briefing.json",
            {
                "status": status,
                "status_reasons": reasons,
                "sections": sections,
                "digest": digest,
                "validator": final_chk.as_dict(),
                "sanitized": sanitized,
                "citations": sorted(cited),
                "render_inputs": {
                    "case_file_version": cf.version,
                    "limits_hit": list(ctx.budget.limits_hit),
                    "prompt_hashes": dict(ctx.prompt_hashes),
                    "cited_refs": sorted(cited_refs),
                },
            },
        )
        cf.briefing_path = str(path)
        ctx.emit(
            "briefing_ready",
            "odin",
            {
                "path": str(path),
                "status": status,
                "status_reasons": reasons,
                "validator_result": {
                    "ok": final_chk.ok,
                    "errors": final_chk.errors[:20],
                    "sanitized": sanitized,
                    "citations": len(final_chk.citations),
                },
            },
        )

    def _write_rejection(self, message: str | None) -> None:
        cf = self.cf
        v = cf.verification
        lines = [
            f"# Case {cf.status.lower()} — {cf.question}",
            f"Case {cf.case_id} · As of {cf.as_of.isoformat()} · Status: {cf.status}",
            "## Why there is no trial",
        ]
        if message:
            lines.append(message)
        if v is not None:
            lines.append(f"Heimdall's verdict: **{v.verdict}** (rule-based: {v.rule_verdict})")
            lines += [f"- {x}" for x in v.reasons]
            if v.supporting_docs:
                lines.append(f"- Reports considered: {', '.join(v.supporting_docs)}")
            if v.contradicting_docs:
                lines.append(f"- Contradicting / denying documents: {', '.join(v.contradicting_docs)}")
            if v.retractions_applied:
                lines.append(f"- Retractions applied as of the date: {', '.join(v.retractions_applied)}")
            if v.disagreement:
                lines.append(f"- Heimdall departed from the rule verdict: {v.disagreement}")
        path = self.art.write("briefing.md", "\n\n".join(lines) + "\n")
        self.art.write(
            "briefing.json",
            {
                "status": cf.status,
                "status_reasons": cf.status_reasons,
                "verification": v.model_dump() if v else None,
                "message": message,
            },
        )
        cf.briefing_path = str(path)
        self.ctx.emit(
            "briefing_ready",
            "thing",
            {
                "path": str(path),
                "status": cf.status,
                "status_reasons": cf.status_reasons,
                "validator_result": None,
            },
        )

    async def _reflect_all(self) -> None:
        for a in self.agents.all():
            if self.ctx.budget.time_left() <= 0:
                break
            await self.turn(a.name, "REFLECT", functools.partial(reflect, a, self.ctx))

    def _summary(self) -> None:
        ctx, cf = self.ctx, self.cf
        after = {a.name: a.memory.snapshot() for a in self.agents.all()}
        self.art.save_memory_diff(getattr(self, "mem_before", {}), after)
        events = ctx.bus.events()
        traps: dict[str, int] = {}
        for e in events:
            if e.type == "trap_detected":
                k = str(e.payload.get("type"))
                traps[k] = traps.get(k, 0) + 1
        traps["trade_rows_excluded"] = sum(1 for x in cf.data_quality if x.get("kind") == "trade_row")
        fps = [
            e.payload.get("fingerprint")
            for e in events
            if e.type == "tool_call"
            and e.payload.get("fingerprint")
            and e.payload.get("status") in ("ok", "error")
        ]
        dup_exec = len(fps) - len(set(fps))
        bj = self.art.dir / "briefing.json"
        validity = None
        if bj.exists():
            import json

            data = json.loads(bj.read_text(encoding="utf-8"))
            cites = data.get("citations", [])
            ok = sum(1 for c in cites if self._verify_cite(c))
            validity = round(100.0 * ok / len(cites), 1) if cites else None
        self.art.write(
            "summary.json",
            {
                "case_id": cf.case_id,
                "status": cf.status,
                "status_reasons": cf.status_reasons,
                "runs": len(cf.runs),
                "claims": len(cf.all_claims()),
                "attacks": len(cf.all_attacks()),
                "responses": len(cf.all_responses()),
                "rulings": sum(len(r.rulings) for r in cf.runs),
                "rejected_or_conceded": sum(
                    1 for r in cf.runs for x in r.rulings if x.verdict in ("REJECTED", "CONCEDED")
                ),
                "budget": ctx.budget.snapshot(),
                "duplicates_blocked": ctx.budget.duplicates_blocked,
                "duplicate_queries_executed": dup_exec,
                "trap_counts": traps,
                "memory_influence": sum(1 for e in events if e.type == "memory_influence"),
                "citation_validity_pct": validity,
                "final_ranges": [f.model_dump() for f in (cf.runs[-1].final_ranges if cf.runs else [])],
            },
        )

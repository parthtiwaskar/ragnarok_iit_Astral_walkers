"""The Thing: court procedure, not a planner. It has no LLM and decides nothing about content.

It fixes turn order (sealed Round 1, cross-examination, judgment), enforces hard limits,
records failures, and carries a remanded case through the next run. Agents decide everything else.

  USER -> HEIMDALL -> THOR || LOKI (sealed) -> cross-exam -> ODIN -> briefing | remand -> HEIMDALL -> next run
"""
from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import briefing as B
from .agents.advocates import Loki, Thor
from .agents.base import AgentFailure
from .agents.heimdall import Heimdall
from .agents.odin import JudgeResult, Odin
from .debate import debate_stats
from .events import EventBus
from .llm import LLMClient
from .memory import Memory
from .models import Attack, CaseFile, Claim, Response, RunRecord
from .tools import Budget, Guard, LimitHit, Limits, ToolBox

OTHER = {"THOR": "LOKI", "LOKI": "THOR"}


class Thing:
    def __init__(self, llm: LLMClient, toolbox: ToolBox, *, limits: Limits | None = None,
                 bus: EventBus | None = None, memory_dir: str | Path | None = None,
                 llms: dict[str, LLMClient] | None = None,
                 on_update: Callable[[CaseFile], None] | None = None) -> None:
        self.on_update = on_update  # called after each phase so a UI can show progress
        self.limits = limits or Limits()
        self.bus = bus or EventBus()
        self.budget = Budget(self.limits)
        self.guard = Guard(toolbox, self.bus, self.budget)
        llms = llms or {}

        def mem(name: str) -> Memory:
            if memory_dir is None:
                return Memory(name)
            Path(memory_dir).mkdir(parents=True, exist_ok=True)
            return Memory(name, Path(memory_dir) / f"{name.lower()}.sqlite")  # private store per agent

        def make(cls: Any) -> Any:
            return cls(llm=llms.get(cls.name, llm), guard=self.guard, budget=self.budget,
                       memory=mem(cls.name), bus=self.bus)

        self.heimdall: Heimdall = make(Heimdall)
        self.thor: Thor = make(Thor)
        self.loki: Loki = make(Loki)
        self.odin: Odin = make(Odin)
        self.advocates = {"THOR": self.thor, "LOKI": self.loki}

    # ------------------------------------------------------------------------------
    def run_case(self, question: str, as_of: str, case_id: str | None = None) -> CaseFile:
        case = CaseFile(case_id or f"CASE-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}", question, as_of)
        self._t0 = time.time()
        self.bus.emit("case_started", case_id=case.case_id, question=question, as_of=as_of, limits=self.limits.__dict__)
        self._notify(case)
        try:
            self._run(case)
        except LimitHit as exc:
            self._finish(case, "INCOMPLETE", f"hard limit reached: {exc}")
        except Exception as exc:  # last line of defence: a bug must never lose the trace
            self.bus.emit("unexpected_error", error=f"{type(exc).__name__}: {exc}")
            self._finish(case, "ABORTED", f"unexpected error: {type(exc).__name__}: {exc}")
        if case.rulings and not case.briefing_md:
            self._brief(case)
        self._reflect(case)
        self.bus.emit("case_finished", case_id=case.case_id, status=case.status, reasons=case.status_reasons)
        self._notify(case)
        return case

    def _notify(self, case: CaseFile) -> None:
        if self.on_update:
            try:
                self.on_update(case)
            except Exception as exc:
                self.bus.emit("persist_error", error=str(exc)[:200])

    def _finish(self, case: CaseFile, status: str, reason: str) -> None:
        case.status = status
        case.status_reasons.append(reason)

    def _clock(self) -> None:
        if time.time() - self._t0 > self.limits.case_timeout_s:
            raise LimitHit(f"case_timeout_s={self.limits.case_timeout_s}")

    def _parallel(self, jobs: dict[str, Callable[[], Any]]) -> dict[str, tuple[bool, Any]]:
        """Run one job per side at the same time. A failed agent returns (False, error), never raises,
        except hard limits which must stop the case."""
        def guarded(fn: Callable[[], Any]) -> tuple[bool, Any]:
            try:
                return True, fn()
            except LimitHit:
                raise
            except Exception as exc:
                return False, exc

        with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
            futs = {k: pool.submit(guarded, fn) for k, fn in jobs.items()}
            return {k: f.result() for k, f in futs.items()}

    # ------------------------------------------------------------------------------
    def _run(self, case: CaseFile) -> None:
        try:
            self.heimdall.gather(case)
        except AgentFailure as exc:
            return self._finish(case, "ABORTED", f"Heimdall failed to build the case: {exc}")
        self._notify(case)
        if not case.verified:
            return self._finish(case, "REJECTED", f"event not verified: {case.verdict_reason}")

        deficiencies: list[str] = []
        for run in range(1, self.limits.max_runs + 1):
            self._clock()
            rec = RunRecord(run)
            case.runs.append(rec)
            if run > 1:
                try:
                    rec.new_doc_ids = self.heimdall.research(case, run, deficiencies)
                except AgentFailure as exc:
                    rec.failures.append(f"Heimdall research failed: {exc}")
                if not rec.new_doc_ids:
                    return self._finish(case, "INCOMPLETE", f"run {run} could not bring new evidence from Runemark")
            judged = self._trial(case, run, rec, deficiencies)
            self._notify(case)
            if judged is None:
                return  # status already set by the failure path
            checklist = self._checklist(case, rec, judged)
            sufficient = judged.sufficient and not checklist
            rec.sufficient, rec.deficiencies = sufficient, judged.deficiencies + checklist
            if sufficient and run >= self.limits.min_runs:
                return self._finish(case, "COMPLETE", f"Odin ruled the evidence sufficient after run {run}")
            if sufficient:
                deficiencies = [f"Protocol requires at least {self.limits.min_runs} runs. Odin must test the weakest "
                                "Established finding: disconfirming or Tier 1-2 corroborating evidence is missing."]
            else:
                deficiencies = rec.deficiencies
            self.bus.emit("remand", run=run, deficiencies=deficiencies)
        self._finish(case, "INCOMPLETE", f"still insufficient after max_runs={self.limits.max_runs}: "
                     + "; ".join(case.runs[-1].deficiencies))

    # ------------------------------------------------------------------------------
    def _trial(self, case: CaseFile, run: int, rec: RunRecord, deficiencies: list[str]) -> JudgeResult | None:
        # Round 1: sealed, simultaneous. Neither side sees the other's claims until both are in.
        r1 = self._parallel({s: (lambda a=a: a.submit(case, run, deficiencies)) for s, a in self.advocates.items()})
        failed: set[str] = set()
        for side, (ok, val) in r1.items():
            if ok:
                for c in val:
                    case.claims[c.id] = c
                    rec.claims.append(c.id)
            else:
                failed.add(side)
                rec.failures.append(f"{side} failed round 1: {val}")
        if len(failed) == 2:
            self._finish(case, "ABORTED", "both Thor and Loki failed to submit evidence-backed claims")
            return None

        # Round 2: each side must attack the other. A failed side cannot attack, so the survivor is unchallenged.
        attackers = [s for s in self.advocates if s not in failed and
                     any(c.side == OTHER[s] and c.status != "conceded" for c in case.claims.values())]
        r2 = self._parallel({s: (lambda a=self.advocates[s]: a.attack(case, run)) for s in attackers})
        attacks: list[Attack] = []
        attacked_sides: set[str] = set()
        for side, (ok, val) in r2.items():
            if ok:
                attacks += val
                attacked_sides.add(OTHER[side])
            else:
                rec.failures.append(f"{side} failed cross-examination: {val}")
        for a in attacks:
            case.attacks[a.id] = a
            rec.attacks.append(a.id)
        survivors_unchallenged = sorted(
            s for s in self.advocates if any(c.side == s for c in case.claims.values()) and s not in attacked_sides)
        rec.unchallenged_side = ",".join(survivors_unchallenged) or None
        if rec.unchallenged_side:
            self.bus.emit("unchallenged", run=run, sides=survivors_unchallenged)

        # Responses: every attack must be answered with new evidence or a concession.
        responses: list[Response] = []
        r3 = self._parallel({s: (lambda a=self.advocates[s], s=s: a.respond(case, run, [x for x in attacks if case.claims[x.target_claim_id].side == s]))
                             for s in attacked_sides if s not in failed})
        for side, (ok, val) in r3.items():
            if ok:
                responses += val
            else:
                rec.failures.append(f"{side} failed to respond: {val}; its attacked claims count as unanswered")
        for r in responses:
            case.responses[r.attack_id] = r
            rec.responses.append(r.attack_id)
            claim = case.claims[case.attacks[r.attack_id].target_claim_id]
            if r.action == "REVISE" and r.revised_estimate:
                claim.estimate, claim.status = r.revised_estimate, "amended"  # partly conceded, claim survives in narrower form
                self.bus.emit("revision", run=run, claim_id=claim.id, by=r.by, attack_id=r.attack_id, note=r.note)
            elif r.action == "CONCEDE":
                claim.status = "conceded"
                self.bus.emit("concession", run=run, claim_id=claim.id, by=r.by, attack_id=r.attack_id, note=r.note)
        rec.debate = debate_stats(case, rec.attacks, rec.responses)
        self.bus.emit("debate_stats", run=run, **rec.debate)

        # Judgment: new claims plus any older claim attacked in this run.
        to_rule = {c.id: c for c in case.claims.values() if c.run == run}
        for a in attacks:
            to_rule[a.target_claim_id] = case.claims[a.target_claim_id]
        try:
            judged = self.odin.judge(case, run, list(to_rule.values()), attacks, responses, rec.unchallenged_side, rec.debate)
        except AgentFailure as exc:
            rec.failures.append(f"Odin failed to judge: {exc}")
            self._finish(case, "INCOMPLETE", f"Odin failed to rule on run {run}: {exc}")
            return None
        for r in judged.rulings:
            case.rulings[r.claim_id] = r  # latest ruling is the effective one
        rec.rulings = judged.rulings
        case.ranges = self._normalise_ranges(case, judged)
        return judged

    # ------------------------------------------------------------------------------
    def _normalise_ranges(self, case: CaseFile, judged: JudgeResult) -> list:
        """Odin's range must lie inside the span of the evidence the two sides actually put forward."""
        for rng in judged.ranges:
            if rng.low is None or rng.high is None:
                continue
            ests = [c.estimate for c in case.claims.values() if c.estimate and c.estimate.key == (rng.country, rng.sector)]
            if not ests:
                rng.flags.append("no_claim_estimates")
                continue
            lo, hi = min(e.low for e in ests), max(e.high for e in ests)
            if rng.low < lo or rng.high > hi:
                self.bus.emit("range_clamped", country=rng.country, sector=rng.sector, asked=(rng.low, rng.high), allowed=(lo, hi))
                rng.low, rng.high = max(rng.low, lo), min(rng.high, hi)
                rng.flags.append("clamped_to_evidence_span")
                if rng.low > rng.high:
                    rng.low, rng.high = lo, hi
        return judged.ranges

    def _checklist(self, case: CaseFile, rec: RunRecord, judged: JudgeResult) -> list[str]:
        """Deterministic sufficiency checks. These can only make Odin stricter, never looser."""
        gaps = []
        sides = {c.side for c in case.claims.values()}
        if sides != {"THOR", "LOKI"}:
            gaps.append("S1: only one side has evidence-backed claims")
        if rec.unchallenged_side:
            gaps.append(f"S2: {rec.unchallenged_side} went unchallenged")
        if len(rec.responses) < len(rec.attacks):
            gaps.append("S3: some attacks were never answered")
        covered = {(r.country, r.sector) for r in judged.ranges}
        gaps += [f"S4: no range for {c}/{s}" for c, s in case.scope if (c, s) not in covered]
        if rec.debate.get("mass_concession"):
            gaps.append(f"S7: {int(rec.debate['concession_rate'] * 100)}% of attacked claims were conceded; the debate may have collapsed into agreement "
                        "instead of testing the claims")
        if not any(r.status == "established" for r in case.rulings.values()):
            gaps.append("S5: nothing was established")
        for r in case.rulings.values():
            if r.status == "established":
                tiers = [e.tier for e in case.claims[r.claim_id].evidence if e.tier is not None]
                if tiers and min(tiers) > 3:
                    gaps.append(f"S6: established claim {r.claim_id} rests only on Tier 4 sources")
        return gaps

    # ------------------------------------------------------------------------------
    def _brief(self, case: CaseFile) -> None:
        d = B.digest(case)
        try:
            brief = self.odin.brief(case, d)
        except (AgentFailure, LimitHit) as exc:
            self.bus.emit("brief_fallback", error=str(exc))
            brief = B.fallback_brief(d)
            case.status_reasons.append(f"Odin could not draft the briefing: {exc}")
            if case.status == "COMPLETE":
                case.status = "INCOMPLETE"
        case.briefing, case.briefing_md = brief, B.render(case, brief, d)

    def _reflect(self, case: CaseFile) -> None:
        """End-of-case memory writes (deterministic): each agent keeps only its own lessons."""
        def write(agent: Any, text: str) -> None:
            mid = agent.memory.add(text, case_id=case.case_id, knowledge_date=case.as_of)
            self.bus.emit("memory_write", agent=agent.name, memory_id=mid, text=text)

        write(self.heimdall, f"Event {case.event_id or '?'} verified={case.verified}: {case.verdict_reason}")
        for f in case.red_flags:
            write(self.heimdall, f"Document {f.get('doc_id')} flagged {f.get('type')}: {f.get('note', '')}")
        for a in case.attacks.values():
            r = case.responses.get(a.id)
            if r and r.action == "CONCEDE":
                claim = case.claims[a.target_claim_id]
                write(self.advocates[claim.side], f"Conceded '{claim.text[:80]}' to a {a.kind} attack: {a.text[:120]}")
                if a.kind in ("retracted", "stale"):
                    write(self.advocates[claim.side], f"Always check retraction and date status of a source before citing it ({a.kind} attacks win).")
        for rl in case.rulings.values():
            if rl.status in ("rejected", "conceded"):
                write(self.odin, f"Ruled {rl.status} on '{case.claims[rl.claim_id].text[:80]}': {rl.rationale[:120]}")
        if case.rulings:
            write(self.odin, f"When a side concedes, never restore the claim; case {case.case_id} ended {case.status}.")

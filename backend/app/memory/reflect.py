"""End-of-case reflection: each agent writes up to 3 lessons into ITS OWN memory (MEMORY.md §6)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.agents import prompt_lib
from app.memory.store import reclassify
from app.obs.events import to_json
from app.tools.context import CaseContext
from app.tools.submit_tools import SubmitLessonsArgs
from app.trial.validators import Applied

if TYPE_CHECKING:
    from app.agents.base import Agent, TurnOutcome


def outcomes_for(agent: str, ctx: CaseContext) -> list[dict[str, Any]]:
    cf = ctx.cf
    eff = cf.effective_rulings()
    out: list[dict[str, Any]] = []
    if agent in ("thor", "loki"):
        for c in cf.all_claims():
            if c.side != agent:
                continue
            r = eff.get(c.id)
            tiers = sorted({getattr(e, "tier", 0) for e in c.evidence if getattr(e, "kind", "") == "doc"})
            attacked_by = sorted({a.type for a in cf.all_attacks() if a.target_claim == c.id})
            out.append(
                {
                    "claim": c.id,
                    "parameter": c.parameter,
                    "type": c.type,
                    "evidence_tiers": tiers,
                    "n_evidence": len(c.evidence),
                    "status": c.status,
                    "attacked_by": attacked_by,
                    "verdict": r.verdict if r else None,
                    "finding": r.finding if r else None,
                    "basis": r.basis if r else None,
                    "rationale": r.rationale if r else None,
                }
            )
        for rr in cf.runs:
            for a in rr.attacks:
                if a.by != agent:
                    continue
                t = eff.get(a.target_claim)
                out.append(
                    {
                        "attack": a.id,
                        "type": a.type,
                        "target": a.target_claim,
                        "target_verdict": t.verdict if t else None,
                    }
                )
    elif agent == "odin":
        for cid, r in eff.items():
            cl = cf.claim(cid)
            out.append(
                {
                    "ruling": r.id,
                    "verdict": r.verdict,
                    "finding": r.finding,
                    "basis": r.basis,
                    "claim_type": cl.type if cl else None,
                    "parameter": cl.parameter if cl else None,
                    "evidence_tiers": sorted(
                        {
                            getattr(e, "tier", 0)
                            for e in (cl.evidence if cl else [])
                            if getattr(e, "kind", "") == "doc"
                        }
                    ),
                    "rationale": r.rationale,
                }
            )
    else:
        v = cf.verification
        out.append(
            {
                "verdict": v.verdict if v else None,
                "rule_verdict": v.rule_verdict if v else None,
                "reasons": v.reasons if v else [],
            }
        )
        for rr in cf.runs:
            if rr.research:
                out.append(
                    {
                        "research_for_run": rr.n,
                        "queries": rr.research.queries,
                        "per_deficiency": rr.research.per_deficiency,
                        "new_items": rr.research.new_items,
                    }
                )
    return out


async def reflect(agent: Agent, ctx: CaseContext) -> TurnOutcome:
    s = ctx.services.settings
    outcomes = outcomes_for(agent.name, ctx)
    instruction = prompt_lib.render(prompt_lib.load("reflect"), {"MY_OUTCOMES": to_json(outcomes)})
    view = {"phase": "REFLECT", "agent": agent.name, "case_status": ctx.cf.status, "my_outcomes": outcomes}

    def handle(a: Any, last: bool) -> Applied:
        assert isinstance(a, SubmitLessonsArgs)
        written = []
        for lesson in a.lessons[: s.MEMORY_MAX_LESSONS_PER_CASE]:
            kind: str = "precedent" if agent.name == "odin" and lesson.kind != "procedural" else lesson.kind
            kind = reclassify(kind, lesson.text)
            kd = None if kind == "procedural" else ctx.clock.iso
            mid, merged = agent.memory.add(
                kind, lesson.text, lesson.tags, ctx.case_id, None, kd, lesson.confidence
            )
            ctx.emit(
                "memory_write",
                agent.name,
                {
                    "agent": agent.name,
                    "memory_id": mid,
                    "kind": kind,
                    "merged": merged,
                    "via": "reflection",
                    "text": lesson.text,
                },
            )
            written.append(mid)
        return Applied(True, result={"accepted": True, "memory_ids": written}, action_ref="reflection")

    return await agent.turn(
        ctx,
        "REFLECT",
        None,
        instruction,
        view,
        {"submit_lessons": handle},
        recall_context="lessons " + agent.name,
    )

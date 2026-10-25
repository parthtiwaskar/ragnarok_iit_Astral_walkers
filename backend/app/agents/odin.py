"""Odin, the Allfather: rules on every claim, decides sufficiency, writes the briefing from rulings only.

He never plans, assigns or searches: no search tool and no tool that addresses another agent.
"""

from __future__ import annotations

from typing import Any

from app.agents import prompt_lib
from app.agents.base import Agent, TurnOutcome
from app.agents.views import judge_view
from app.obs.events import to_json
from app.tools.context import CaseContext
from app.tools.submit_tools import SubmitBriefingArgs, SubmitRulingsArgs, SubmitSufficiencyArgs
from app.trial.judging import apply_rulings, apply_sufficiency, claims_to_rule
from app.trial.sufficiency import pre_judging_facts
from app.trial.validators import Applied


class Odin(Agent):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.briefing_sections: dict[str, Any] | None = None

    async def judge(self, ctx: CaseContext, run: int) -> TurnOutcome:
        cf = ctx.cf
        r = cf.run(run)
        to_rule = claims_to_rule(ctx, run)
        pre = pre_judging_facts(cf, run)
        pre["max_confident_width_pp"] = self.settings.MAX_CONFIDENT_WIDTH_PP
        ctx.pending_checklist = None
        view = judge_view(cf, run, to_rule, pre)
        instruction = prompt_lib.render(
            prompt_lib.load("turn_judge"),
            {
                "RUN": run,
                "CLAIMS": f"{len(to_rule)} claims (see case_view.claims_to_rule)",
                "ATTACKS": f"{len(r.attacks)} (see case_view.attacks)",
                "RESPONSES": f"{len(r.responses)} (see case_view.responses)",
                "VALIDATION": "attached to each claim, attack and response as 'validation'",
                "THOR_STATUS": r.side_status["thor"].value,
                "LOKI_STATUS": r.side_status["loki"].value,
                "PRIOR_RULINGS": f"{len(view['prior_rulings'])} (see case_view.prior_rulings)",
                "SUFFICIENCY_CHECKLIST": to_json(pre),
                "CLAIM_IDS": to_json([c.id for c in to_rule]),
            },
        )

        def h_rulings(a: Any, last: bool) -> Applied:
            assert isinstance(a, SubmitRulingsArgs)
            return apply_rulings(ctx, run, a, last)

        def h_suff(a: Any, last: bool) -> Applied:
            assert isinstance(a, SubmitSufficiencyArgs)
            return apply_sufficiency(ctx, run, a, last)

        return await self.turn(
            ctx,
            "JUDGING",
            run,
            instruction,
            view,
            {"submit_rulings": h_rulings, "submit_sufficiency": h_suff},
            system_extra={"SUFFICIENCY_CHECKLIST": "given in the turn message"},
            recall_context="precedent ruling tier corroboration duration claims "
            + " ".join(c.statement[:60] for c in to_rule[:6]),
        )

    async def brief(
        self, ctx: CaseContext, digest: dict[str, Any], status: str, reasons: list[str], validate: Any
    ) -> TurnOutcome:
        """`validate(sections) -> list[str]` runs the briefing validator; errors trigger the one re-prompt."""
        self.briefing_sections = None
        # Odin sees ONLY the rulings digest in this turn (CASE_FILE_SCHEMA §3)
        view = {
            "phase": "BRIEFING",
            "digest": {k: v for k, v in digest.items() if k != "allowed_citations"},
            "allowed_citations": sorted(digest.get("allowed_citations", {})),
        }
        instruction = prompt_lib.render(
            prompt_lib.load("turn_brief"),
            {
                "DIGEST": "see case_view.digest",
                "STATUS": status,
                "STATUS_REASONS": ", ".join(reasons) or "none",
            },
        )

        def h_brief(a: Any, last: bool) -> Applied:
            assert isinstance(a, SubmitBriefingArgs)
            sections = a.model_dump()
            errors = validate(sections)
            if errors and not last:
                return Applied(False, errors)
            self.briefing_sections = sections
            return Applied(
                True,
                warnings=errors,
                result={"accepted": True, "validator_errors": errors},
                action_ref="briefing",
            )

        return await self.turn(
            ctx,
            "BRIEFING",
            None,
            instruction,
            view,
            {"submit_briefing": h_brief},
            recall_context="briefing outlook assumptions",
        )

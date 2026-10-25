"""Thor (case for HIGH exposure) and Loki (case for LIMITED exposure). Same runtime, opposite prompts."""

from __future__ import annotations

from typing import Any

from app.agents import prompt_lib
from app.agents.base import Agent, TurnOutcome
from app.agents.views import attack_view, r1_view, respond_view, scope_view
from app.obs.events import to_json
from app.tools.context import CaseContext
from app.tools.submit_tools import SubmitAttacksArgs, SubmitClaimsArgs, SubmitResponsesArgs
from app.trial.casefile import Side
from app.trial.validators import Applied, apply_attacks, apply_claims, apply_responses


class SideAgent(Agent):
    @property
    def side(self) -> Side:
        return "thor" if self.name == "thor" else "loki"

    @property
    def opponent(self) -> Side:
        return "loki" if self.side == "thor" else "thor"

    async def submit(self, ctx: CaseContext, run: int) -> TurnOutcome:
        s = self.settings
        view = r1_view(ctx.cf, self.side, run)
        instruction = prompt_lib.render(
            prompt_lib.load("turn_r1_submit"),
            {
                "RUN": run,
                "SCOPE_ROWS": to_json((scope_view(ctx.cf) or {}).get("rows", [])),
                "POOL_SUMMARY": f"{len(view['pool'])} items (see case_view.pool)",
                "PRIOR_RULINGS": to_json(view["prior_rulings"]) if view["prior_rulings"] else "none",
                "DEFICIENCIES": to_json(view["deficiencies"]) if view["deficiencies"] else "none",
                "NEW_EVIDENCE": to_json(view["new_evidence"]) if view["new_evidence"] else "none",
                "MAX_CLAIMS": s.MAX_CLAIMS_PER_SIDE,
            },
        )

        def handle(a: Any, last: bool) -> Applied:
            assert isinstance(a, SubmitClaimsArgs)
            return apply_claims(ctx, self.side, run, a, last)

        return await self.turn(
            ctx,
            "R1_SUBMIT",
            run,
            instruction,
            view,
            {"submit_claims": handle},
            recall_context=f"round 1 claims {ctx.cf.question} duration route share severity "
            "reroute evidence tier corroborate",
        )

    async def attack(self, ctx: CaseContext, run: int) -> TurnOutcome:
        s = self.settings
        view = attack_view(ctx.cf, self.side, run)
        instruction = prompt_lib.render(
            prompt_lib.load("turn_r2_attack"),
            {
                "RUN": run,
                "OPPONENT_CLAIMS": f"{len(view['opponent_claims'])} claims (see case_view.opponent_claims)",
                "VALIDATION": "attached to each claim as 'validation'",
                "MIN_ATTACKS": s.MIN_ATTACKS_PER_SIDE,
                "MAX_ATTACKS": s.MAX_ATTACKS_PER_SIDE,
            },
        )

        def handle(a: Any, last: bool) -> Applied:
            assert isinstance(a, SubmitAttacksArgs)
            return apply_attacks(ctx, self.side, run, a, last)

        return await self.turn(
            ctx,
            "R2_ATTACK",
            run,
            instruction,
            view,
            {"submit_attacks": handle},
            recall_context="attack opponent claims retracted wrong event tier counter evidence",
        )

    async def respond(self, ctx: CaseContext, run: int) -> TurnOutcome:
        view = respond_view(ctx.cf, self.side, run)
        instruction = prompt_lib.render(
            prompt_lib.load("turn_r2_respond"),
            {
                "RUN": run,
                "ATTACKS_ON_ME": f"{len(view['attacks_on_me'])} attacks (see case_view.attacks_on_me)",
            },
        )

        def handle(a: Any, last: bool) -> Applied:
            assert isinstance(a, SubmitResponsesArgs)
            return apply_responses(ctx, self.side, run, a, last)

        return await self.turn(
            ctx,
            "R2_RESPOND",
            run,
            instruction,
            view,
            {"submit_responses": handle},
            recall_context="respond defend concede revise new evidence",
        )


class Thor(SideAgent):
    pass


class Loki(SideAgent):
    pass

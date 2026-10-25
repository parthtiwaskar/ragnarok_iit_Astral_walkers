"""Shared agent runtime: one turn = view -> recall -> LLM tool loop -> validate -> re-prompt once -> memory.

Each Agent owns its prompt, its tool allowlist and its private MemoryStore. Agents talk to each other only
through the Case File (the submit handlers write to it).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from app.agents import prompt_lib
from app.config import Settings
from app.llm.client import LLMClient, LLMResponse, Msg, ToolSpec
from app.memory.store import MemoryStore
from app.obs.events import to_json
from app.tools.catalog import TOOLS
from app.tools.context import CaseContext
from app.tools.guards import run_tool
from app.tools.registry import AGENT_TOOLS, TERMINAL_BY_PHASE
from app.tools.submit_tools import submit_def
from app.trial.limits import TurnBudget
from app.trial.validators import Applied

Handler = Callable[[BaseModel, bool], Applied]


@dataclass
class TurnOutcome:
    ok: bool
    status: str  # OK | FAILED | LIMIT
    reason: str = ""
    applied: dict[str, Applied] = field(default_factory=dict)
    tool_calls: int = 0
    tokens: int = 0
    reprompts: int = 0
    recalled: list[str] = field(default_factory=list)


class Agent:
    def __init__(self, name: str, llm: LLMClient, memory: MemoryStore, settings: Settings) -> None:
        self.name = name
        self.llm = llm
        self.memory = memory
        self.settings = settings

    # ------------------------------------------------------------------ hooks
    def before_tool(
        self, ctx: CaseContext, phase: str, name: str, args: dict[str, Any], recalled: set[str]
    ) -> dict[str, Any] | None:
        """Return a result to short-circuit a tool call (Heimdall's deterministic memory skip)."""
        return None

    def after_turn(
        self, ctx: CaseContext, phase: str, run: int | None, outcome: TurnOutcome, calls: list[dict[str, Any]]
    ) -> None:
        return None

    # ------------------------------------------------------------------ prompt
    def system(self, ctx: CaseContext, extra: dict[str, Any]) -> str:
        s = self.settings
        values = {
            "AS_OF": ctx.clock.iso,
            "MAX_TOOL_CALLS": ctx.limits.MAX_TOOL_CALLS_PER_TURN,
            "HORIZON": f"the {s.HORIZON_MONTHS} months after {ctx.clock.iso}",
            "POOL_SIZE": s.POOL_SIZE_R1,
            "MAX_SCOPE_ROWS": s.MAX_SCOPE_ROWS,
            "MAX_REMAND_QUERIES": ctx.limits.MAX_REMAND_QUERIES,
            **extra,
        }
        return prompt_lib.system_prompt(self.name, values)

    def tools_for(self, phase: str, terminal: tuple[str, ...]) -> list[ToolSpec]:
        allowed = AGENT_TOOLS[self.name]
        specs = [TOOLS[n].spec() for n in sorted(allowed) if n in TOOLS]
        for t in terminal:
            if t in allowed:
                specs.append(submit_def(self.name, phase, t).spec())
        return specs

    def recall(self, ctx: CaseContext, context: str) -> list[Any]:
        items = self.memory.recall(context, self.settings.MEMORY_TOP_K, ctx.clock)
        ctx.emit(
            "memory_read",
            self.name,
            {
                "agent": self.name,
                "memory_ids": [m.id for m in items],
                "scores": [m.score for m in items],
                "via": "turn_start",
            },
        )
        return items

    # ------------------------------------------------------------------ the turn
    async def turn(
        self,
        ctx: CaseContext,
        phase: str,
        run: int | None,
        instruction: str,
        view: dict[str, Any],
        handlers: dict[str, Handler],
        system_extra: dict[str, Any] | None = None,
        recall_context: str | None = None,
    ) -> TurnOutcome:
        terminal = TERMINAL_BY_PHASE[(self.name, phase)]
        budget = TurnBudget(agent=self.name, phase=phase, limits=ctx.limits)
        system = self.system(ctx, system_extra or {})
        p_hash = prompt_lib.sha(system)
        ctx.prompt_hashes[f"{self.name}:{phase}"] = p_hash
        mems = self.recall(ctx, recall_context or f"{phase} {instruction[:400]}")
        recalled: set[str] = {m.id for m in mems}
        past = ctx.ledger.queries_by(self.name)
        first = (
            f"{instruction}\n\n<case_view>\n{to_json(view)}\n</case_view>\n\n<memories>\n"
            + ("\n".join(f"<memory>{m.render()}</memory>" for m in mems) or "(none)")
            + "\n</memories>\n\n<past_queries>\n"
            + (to_json(past) if past else "(none)")
            + "\n</past_queries>"
        )
        msgs: list[Msg] = [Msg(role="user", content=first)]
        specs = self.tools_for(phase, terminal)
        terminal_specs = [s for s in specs if s.name in terminal]
        outcome = TurnOutcome(ok=False, status="FAILED", recalled=sorted(recalled))
        calls: list[dict[str, Any]] = []
        reprompts = 0
        nudged = False
        final_only = False
        done = False
        pending_terminal = list(terminal)
        while not done:
            blocked = ctx.budget.can_call_llm(budget)
            if blocked:
                ctx.budget.record_limit(blocked)
                ctx.emit(
                    "limit_hit",
                    "thing",
                    {
                        "limit": blocked,
                        "agent": self.name,
                        "phase": phase,
                        "value": budget.tokens,
                        "action_taken": "turn ended",
                    },
                )
                outcome.status, outcome.reason = "LIMIT", blocked
                break
            resp = await self._chat(
                ctx, system, msgs, terminal_specs if final_only else specs, budget, p_hash, phase
            )
            if resp is None:
                outcome.reason = "LLM_FAILED"
                break
            msgs.append(Msg(role="assistant", content=resp.text, tool_calls=resp.tool_calls, raw=resp.raw))
            notices: list[str] = []  # appended after all tool results of this response
            if not final_only and budget.tokens >= 0.75 * ctx.limits.MAX_TOKENS_PER_TURN:
                # soft edge of the hard token limit: leave room for the submission
                final_only = True
                ctx.emit(
                    "limit_near",
                    "thing",
                    {
                        "limit": "MAX_TOKENS_PER_TURN",
                        "agent": self.name,
                        "value": budget.tokens,
                        "action_taken": "submit-only mode",
                    },
                )
                notices.append(
                    "Your token budget for this turn is nearly used. Call "
                    f"{' then '.join(pending_terminal)} now with what you have."
                )
            if not resp.tool_calls:
                if nudged:
                    outcome.reason = "NO_SUBMISSION"
                    break
                nudged = True
                msgs.append(
                    Msg(
                        role="user",
                        content=f"You must use your tools. Finish by calling "
                        f"{' then '.join(pending_terminal)}.",
                    )
                )
                continue
            for tc in resp.tool_calls:
                if tc.name in terminal:
                    content, accepted, finished = self._handle_terminal(
                        ctx, phase, tc.name, tc.args, handlers, reprompts, outcome
                    )
                    if not accepted:
                        if reprompts < ctx.limits.MAX_REPROMPTS:
                            reprompts += 1
                            ctx.emit(
                                "validation_failed",
                                "thing",
                                {
                                    "agent": self.name,
                                    "phase": phase,
                                    "tool": tc.name,
                                    "errors": content["errors"],
                                    "reprompt": reprompts,
                                },
                            )
                        else:
                            ctx.emit(
                                "validation_failed",
                                "thing",
                                {
                                    "agent": self.name,
                                    "phase": phase,
                                    "tool": tc.name,
                                    "errors": content["errors"],
                                    "final": True,
                                },
                            )
                            outcome.reason = "VALIDATION_FAILED"
                            done = True
                    else:
                        if tc.name in pending_terminal:
                            pending_terminal.remove(tc.name)
                        if finished:
                            done = True
                    msgs.append(Msg(role="tool", content=to_json(content), tool_call_id=tc.id, name=tc.name))
                    if done:
                        break
                    continue
                short = self.before_tool(ctx, phase, tc.name, tc.args, recalled)
                if short is not None:
                    result = short
                else:
                    result = await run_tool(
                        ctx, self.name, phase, run, budget, tc.name, tc.args, self.memory, recalled
                    )
                calls.append({"tool": tc.name, "args": tc.args, "result": result})
                msgs.append(Msg(role="tool", content=to_json(result), tool_call_id=tc.id, name=tc.name))
                if result.get("error") == "limit_hit" and not final_only:
                    final_only = True
                    notices.append(
                        "Your tool budget for this turn is exhausted. Call "
                        f"{' then '.join(pending_terminal)} now."
                    )
            if notices and not done:
                msgs.append(Msg(role="user", content="\n".join(notices)))
        if done and not outcome.reason:
            outcome.ok, outcome.status = True, "OK"
        outcome.tool_calls = budget.tool_calls
        outcome.tokens = budget.tokens
        outcome.reprompts = reprompts
        outcome.recalled = sorted(recalled)
        self._influence(ctx, outcome, recalled)
        self._turn_note(ctx, phase, run, outcome)
        self.after_turn(ctx, phase, run, outcome, calls)
        return outcome

    def _handle_terminal(
        self,
        ctx: CaseContext,
        phase: str,
        name: str,
        args: dict[str, Any],
        handlers: dict[str, Handler],
        reprompts: int,
        outcome: TurnOutcome,
    ) -> tuple[dict[str, Any], bool, bool]:
        model = submit_def(self.name, phase, name).args_model
        last = reprompts >= self.settings.MAX_REPROMPTS
        try:
            parsed = model.model_validate(args or {})
        except ValidationError as exc:
            errs = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:10]]
            return {"accepted": False, "errors": errs}, False, False
        handler = handlers.get(name)
        if handler is None:
            return {"accepted": False, "errors": [f"{name} is not expected in phase {phase}"]}, False, False
        try:
            applied = handler(parsed, last)
        except Exception as exc:  # a validator bug must not crash the trial
            ctx.emit(
                "agent_failed",
                "thing",
                {
                    "agent": self.name,
                    "phase": phase,
                    "error": f"handler {name}: {type(exc).__name__}: {exc}",
                    "retries": 0,
                },
            )
            return (
                {"accepted": False, "errors": [f"internal validation error: {type(exc).__name__}"]},
                False,
                False,
            )
        if not applied.ok:
            return {"accepted": False, "errors": applied.errors}, False, False
        outcome.applied[name] = applied
        return applied.result or {"accepted": True}, True, applied.done

    async def _chat(
        self,
        ctx: CaseContext,
        system: str,
        msgs: list[Msg],
        specs: list[ToolSpec],
        budget: TurnBudget,
        p_hash: str,
        phase: str,
    ) -> LLMResponse | None:
        attempts = 0
        while True:
            attempts += 1
            t0 = time.monotonic()
            try:
                max_out = min(self.settings.LLM_MAX_OUTPUT_TOKENS, max(256, budget.tokens_left))
                resp = await self.llm.chat(system, msgs, specs, max_out, self.settings.LLM_TEMPERATURE)
            except Exception as exc:
                retryable = getattr(exc, "retryable", True)
                ctx.emit(
                    "llm_error",
                    self.name,
                    {
                        "agent": self.name,
                        "phase": phase,
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                        "attempt": attempts,
                        "retryable": retryable,
                    },
                )
                if retryable and attempts <= self.settings.LLM_MAX_RETRIES:
                    await asyncio.sleep(min(8.0, 0.5 * (2 ** (attempts - 1))))
                    continue
                return None
            # Budget = context growth + output: each call re-sends the whole turn so far, so the raw input count
            # would be charged again and again. Raw usage is still logged on the llm_call event.
            new_input = max(0, resp.usage.input_tokens - budget.last_input)
            budget.last_input = max(budget.last_input, resp.usage.input_tokens)
            ctx.budget.charge_tokens(budget, new_input, resp.usage.output_tokens)
            ctx.emit(
                "llm_call",
                self.name,
                {
                    "agent": self.name,
                    "model": resp.model or self.llm.model,
                    "provider": self.llm.provider,
                    "prompt_hash": p_hash,
                    "n_messages": len(msgs),
                    "tool_names": [s.name for s in specs],
                    "stop_reason": resp.stop_reason,
                    "tool_calls": [tc.name for tc in resp.tool_calls],
                    "text": resp.text[:500] if resp.text else None,
                },
                usage={"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
            ctx.emit("budget", "thing", ctx.budget.snapshot())
            return resp

    # ------------------------------------------------------------------ memory
    def _influence(self, ctx: CaseContext, outcome: TurnOutcome, recalled: set[str]) -> None:
        for name, ap in outcome.applied.items():
            for mi in ap.memory_influence:
                mid = str(mi.get("memory_id", ""))
                if mid in recalled:
                    ctx.emit(
                        "memory_influence",
                        self.name,
                        {
                            "agent": self.name,
                            "memory_id": mid,
                            "effect": mi.get("effect", ""),
                            "action_ref": ap.action_ref or name,
                            "declared": True,
                        },
                    )
                else:
                    ctx.emit(
                        "memory_influence_rejected",
                        "thing",
                        {"agent": self.name, "memory_id": mid, "reason": "not recalled this turn"},
                    )

    def _turn_note(self, ctx: CaseContext, phase: str, run: int | None, outcome: TurnOutcome) -> None:
        if phase == "REFLECT":
            return
        summary = {k: (v.action_ref or "") for k, v in outcome.applied.items()}
        text = (
            f"{phase} run {run}: {outcome.status}{' (' + outcome.reason + ')' if outcome.reason else ''}; "
            f"{outcome.tool_calls} tool calls; submitted {json.dumps(summary)[:300]}"
        )
        mid, _ = self.memory.add("turn_note", text, [phase.lower()], ctx.case_id, run, ctx.clock.iso, 0.3)
        ctx.emit("memory_write", self.name, {"agent": self.name, "memory_id": mid, "kind": "turn_note"})

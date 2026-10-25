"""Shared turn loop: recall memory -> LLM/tool loop -> validate -> re-prompt once."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, TypeVar

from ..events import EventBus
from ..llm import LLMClient
from ..memory import Memory
from ..models import CaseFile, Evidence
from ..tools import ALLOWED_TOOLS, Budget, Guard, LimitHit

T = TypeVar("T")
PROMPT_DIR = Path(__file__).parent / "prompts"


class AgentFailure(Exception):
    """An agent could not produce a valid answer. The Thing handles this, never crashes."""


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name.lower()}.md").read_text(encoding="utf-8")


def extract_json(text: str) -> Any:
    """Pull the first JSON object out of a model reply (tolerates code fences / chatter)."""
    if not text:
        raise ValueError("empty reply; reply with a JSON object")
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    if not candidate:
        raise ValueError("no JSON object found in reply")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc


def parse_evidence(items: Any) -> list[Evidence]:
    if not isinstance(items, list):
        raise ValueError("evidence must be a list")
    return [Evidence.from_dict(i) for i in items]


class Agent:
    name = "AGENT"

    def __init__(self, *, llm: LLMClient, guard: Guard, budget: Budget, memory: Memory, bus: EventBus,
                 prompt: str | None = None) -> None:
        self.llm, self.guard, self.budget, self.memory, self.bus = llm, guard, budget, memory, bus
        self.system = prompt if prompt is not None else load_prompt(self.name)
        self.tools = ALLOWED_TOOLS[self.name]
        self.seen: set[str] = set()  # doc/table IDs this agent actually retrieved at run time
        self.queries: list[str] = []

    def turn(self, *, case: CaseFile, run: int, phase: str, task: str, payload: dict[str, Any],
             validate: Callable[[dict[str, Any]], T]) -> T:
        recalled = self.memory.recall(f"{case.question} {task}", case.as_of)
        self.bus.emit("memory_recall", agent=self.name, run=run, phase=phase, ids=[m["id"] for m in recalled])
        user = (
            f"TASK: {task}\nAS-OF DATE: {case.as_of} (nothing dated later may be used)\n"
            f"CASE FILE VIEW:\n{json.dumps(payload, default=str, indent=1)}\n"
            f"YOUR PRIVATE MEMORY (may be stale; verify before relying):\n{json.dumps(recalled, indent=1)}\n"
            "Reply with ONE JSON object matching your output schema. Include "
            '"memory_used": [ids of memories that changed what you did].'
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        specs = self.guard.specs_for(self.name)
        limits = self.budget.limits
        retried = False
        for step in range(limits.max_steps_per_turn + 3):
            force_final = step >= limits.max_steps_per_turn
            self._compact(messages)
            self.budget.charge_llm()
            if step == limits.max_steps_per_turn:
                messages.append({"role": "user", "content": "Tool budget exhausted. Do NOT call tools. Reply now with the final JSON object only."})
            try:
                resp = self.llm.complete(system=self.system, messages=messages, tools=[] if force_final else specs)
            except LimitHit:
                raise
            except Exception as exc:  # provider error: fail this agent's turn, not the whole case
                self.bus.emit("llm_error", agent=self.name, run=run, phase=phase, error=str(exc)[:300])
                raise AgentFailure(f"{self.name} {phase}: LLM error: {str(exc)[:200]}") from exc
            self.budget.add_tokens(resp.tokens)
            self.bus.emit("llm_call", agent=self.name, run=run, phase=phase, step=step, tokens=resp.tokens, context_chars=sum(len(str(m.get('content') or '')) for m in messages))
            if resp.tool_calls and not force_final:
                messages.append({"role": "assistant", "content": resp.text,
                                 "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in resp.tool_calls]})
                for c in resp.tool_calls:
                    if c.name == "search_documents":
                        self.queries.append(str(c.args.get("query", "")))
                    out = self.guard.call(self, c.name, c.args, case, run, phase)
                    messages.append({"role": "tool", "tool_call_id": c.id, "name": c.name,
                                     "content": json.dumps(out, default=str)[:limits.max_tool_result_chars]})
                continue
            try:
                data = extract_json(resp.text or "")
                result = validate(data)
            except (ValueError, KeyError, TypeError) as exc:
                self.bus.emit("invalid_output", agent=self.name, run=run, phase=phase, error=str(exc), retried=retried)
                if retried:
                    raise AgentFailure(f"{self.name} {phase}: {exc}") from exc
                retried = True
                messages.append({"role": "assistant", "content": resp.text})
                messages.append({"role": "user", "content": f"Your reply was invalid: {exc}. Reply with corrected JSON only."})
                continue
            used = [i for i in data.get("memory_used", []) if isinstance(i, int)]
            valid_used = [i for i in used if i in {m["id"] for m in recalled}]
            if valid_used:
                self.bus.emit("memory_influence", agent=self.name, run=run, phase=phase, memory_ids=valid_used)
            return result
        raise AgentFailure(f"{self.name} {phase}: no valid answer within step limit")

    def _compact(self, messages: list[dict[str, Any]]) -> None:
        """Keep the prompt inside the provider's per-minute token budget by shrinking the oldest tool results."""
        cap = self.budget.limits.max_context_chars
        if not cap:
            return
        total = lambda: sum(len(str(m.get("content") or "")) + len(str(m.get("tool_calls") or "")) for m in messages)  # noqa: E731
        for m in messages[1:]:
            if total() <= cap:
                return
            if m["role"] == "tool" and len(m["content"]) > 500:
                m["content"] = m["content"][:500] + " ...[older result truncated to save context]"

    # -- helpers shared by Thor/Loki ------------------------------------------------
    def check_evidence(self, case: CaseFile, run: int, ev: list[Evidence], allow_pool: bool = True) -> list[Evidence]:
        """Keep only evidence that resolves, is in time, and was actually retrieved by this agent."""
        good: list[Evidence] = []
        for e in ev:
            if e.doc_id not in self.seen and not (allow_pool and e.doc_id in case.pool):
                self.bus.emit("evidence_dropped", agent=self.name, run=run, doc_id=e.doc_id, reason="never retrieved")
                continue
            try:
                v = self.guard.toolbox.call(
                    "verify_citation", {"doc_id": e.doc_id, "quote": e.quote, "event_id": case.event_id, "as_of": case.as_of})
            except Exception as exc:
                self.bus.emit("evidence_dropped", agent=self.name, run=run, doc_id=e.doc_id, reason=f"verify failed: {exc}")
                continue
            if not v.get("valid"):
                self.bus.emit("evidence_dropped", agent=self.name, run=run, doc_id=e.doc_id, reason=v.get("reason", "invalid"))
                continue
            e.date, e.tier = f"{v.get('date', '')} {v.get('time', '')}".strip(), v.get("tier")  # a timestamp, not just a day
            good.append(e)
        return good

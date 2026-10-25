"""FakeLLM: deterministic stand-ins for a real model, used by tests and the offline demo.

- `ScriptedLLM` replays a fixed list of responses (unit tests).
- `PolicyLLM` plays one agent with an event-agnostic rule-based policy (`fake_policies`). It goes through
  exactly the same path as a real model: LLMClient -> agent loop -> guard chain -> validators.
- `FaultyLLM` always fails (chaos tests, LLM_FAULT_INJECT).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Generator
from typing import Any

from app.llm.client import LLMError, LLMResponse, Msg, ToolCall, ToolSpec, Usage, estimate_tokens

CASE_VIEW = re.compile(r"<case_view>\s*(.*?)\s*</case_view>", re.S)
MEMORY_LINE = re.compile(r"<memory>\[([^\]]+)\] \((\w+), conf ([\d.]+)\) (.*?)</memory>", re.S)


class ScriptedLLM:
    provider = "fake"

    def __init__(
        self, script: list[LLMResponse | Callable[[list[Msg]], LLMResponse]], model: str = "scripted"
    ) -> None:
        self.script = list(script)
        self.model = model
        self.calls: list[list[Msg]] = []

    async def chat(
        self, system: str, messages: list[Msg], tools: list[ToolSpec], max_tokens: int, temperature: float
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if not self.script:
            return LLMResponse(text="(script exhausted)", model=self.model)
        item = self.script.pop(0)
        return item(messages) if callable(item) else item


class FaultyLLM:
    provider = "fake"

    def __init__(self, model: str = "faulty", message: str = "injected fault") -> None:
        self.model = model
        self.message = message

    async def chat(
        self, system: str, messages: list[Msg], tools: list[ToolSpec], max_tokens: int, temperature: float
    ) -> LLMResponse:
        raise LLMError(self.message, retryable=True)


Action = list[tuple[str, dict[str, Any]]]
Policy = Generator[Action, Any, None]


def parse_first_message(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    m = CASE_VIEW.search(text)
    view: dict[str, Any] = json.loads(m.group(1)) if m else {}
    mems = [
        {"id": a, "kind": b, "confidence": float(c), "text": d.strip()}
        for a, b, c, d in MEMORY_LINE.findall(text)
    ]
    return view, mems


class PolicyLLM:
    provider = "fake"

    def __init__(self, agent: str, settings: Any, model: str = "policy-v1") -> None:
        self.agent = agent
        self.settings = settings
        self.model = f"{model}:{agent}"
        self._key: str | None = None
        self._gen: Policy | None = None
        self._started = False
        self._n = 0

    async def chat(
        self, system: str, messages: list[Msg], tools: list[ToolSpec], max_tokens: int, temperature: float
    ) -> LLMResponse:
        from app.llm.fake_policies import make_policy

        first = messages[0].content if messages else ""
        key = hashlib.sha256(first.encode()).hexdigest()
        if key != self._key or self._gen is None:
            view, mems = parse_first_message(first)
            self._gen = make_policy(self.agent, view, mems, self.settings)
            self._key = key
            self._started = False
        obs: Any = None
        if self._started:
            obs = _observation(messages)
        try:
            action = next(self._gen) if not self._started else self._gen.send(obs)
            self._started = True
        except StopIteration:
            return self._resp("(policy finished)", [], messages)
        calls = []
        for name, args in action:
            self._n += 1
            calls.append(ToolCall(id=f"fake_{self.agent}_{self._n}", name=name, args=args))
        return self._resp("", calls, messages)

    def _resp(self, text: str, calls: list[ToolCall], messages: list[Msg]) -> LLMResponse:
        inp = sum(estimate_tokens(m.content) for m in messages)
        out = estimate_tokens(text + json.dumps([{"n": c.name, "a": c.args} for c in calls], default=str))
        return LLMResponse(
            text=text,
            tool_calls=calls,
            usage=Usage(inp, out),
            stop_reason="tool_use" if calls else "end_turn",
            model=self.model,
        )


def _observation(messages: list[Msg]) -> Any:
    """Tool results that followed the last assistant message (parsed JSON), or a user nudge."""
    tail: list[Msg] = []
    for m in reversed(messages):
        if m.role == "assistant":
            break
        tail.append(m)
    tail.reverse()
    results: list[Any] = []
    for m in tail:
        if m.role == "tool":
            try:
                results.append(json.loads(m.content))
            except ValueError:
                results.append({"raw": m.content})
        else:
            results.append({"__user__": m.content})
    return results

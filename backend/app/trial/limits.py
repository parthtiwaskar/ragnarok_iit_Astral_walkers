"""Budget tracking against the immutable `Limits`. Hitting a limit never raises: callers check and end cleanly."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.config import Limits


@dataclass
class TurnBudget:
    agent: str
    phase: str
    limits: Limits
    tool_calls: int = 0
    tokens: int = 0
    last_input: int = 0  # input tokens of the previous call in this turn (context already charged)
    started: float = field(default_factory=time.monotonic)
    limit_hit: str | None = None

    @property
    def tool_calls_left(self) -> int:
        return max(0, self.limits.MAX_TOOL_CALLS_PER_TURN - self.tool_calls)

    @property
    def tokens_left(self) -> int:
        return max(0, self.limits.MAX_TOKENS_PER_TURN - self.tokens)


@dataclass
class Budget:
    limits: Limits
    tool_calls: int = 0
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    duplicates_blocked: int = 0
    started: float = field(default_factory=time.monotonic)
    limits_hit: list[str] = field(default_factory=list)

    # ---------- checks ----------
    def can_call_tool(self, turn: TurnBudget) -> str | None:
        """Returns the name of the limit that blocks a tool call, or None."""
        if turn.tool_calls >= self.limits.MAX_TOOL_CALLS_PER_TURN:
            return "MAX_TOOL_CALLS_PER_TURN"
        if self.tool_calls >= self.limits.MAX_TOOL_CALLS_PER_CASE:
            return "MAX_TOOL_CALLS_PER_CASE"
        return None

    def can_call_llm(self, turn: TurnBudget) -> str | None:
        if turn.tokens >= self.limits.MAX_TOKENS_PER_TURN:
            return "MAX_TOKENS_PER_TURN"
        if self.tokens >= self.limits.MAX_TOKENS_PER_CASE:
            return "MAX_TOKENS_PER_CASE"
        if self.time_left() <= 0:
            return "CASE_TIMEOUT_S"
        return None

    def time_left(self) -> float:
        return self.limits.CASE_TIMEOUT_S - (time.monotonic() - self.started)

    @property
    def exhausted(self) -> bool:
        return (
            self.tool_calls >= self.limits.MAX_TOOL_CALLS_PER_CASE
            or self.tokens >= self.limits.MAX_TOKENS_PER_CASE
            or self.time_left() <= 0
        )

    def exhausted_reason(self) -> str | None:
        if self.tool_calls >= self.limits.MAX_TOOL_CALLS_PER_CASE:
            return "MAX_TOOL_CALLS_PER_CASE"
        if self.tokens >= self.limits.MAX_TOKENS_PER_CASE:
            return "MAX_TOKENS_PER_CASE"
        if self.time_left() <= 0:
            return "CASE_TIMEOUT_S"
        return None

    # ---------- spend ----------
    def charge_tool(self, turn: TurnBudget) -> None:
        self.tool_calls += 1
        turn.tool_calls += 1

    def charge_tokens(self, turn: TurnBudget, input_tokens: int, output_tokens: int) -> None:
        n = input_tokens + output_tokens
        self.tokens += n
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.llm_calls += 1
        turn.tokens += n

    def record_limit(self, name: str) -> None:
        if name not in self.limits_hit:
            self.limits_hit.append(name)

    def snapshot(self) -> dict[str, object]:
        return {
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.limits.MAX_TOOL_CALLS_PER_CASE,
            "tokens": self.tokens,
            "max_tokens": self.limits.MAX_TOKENS_PER_CASE,
            "llm_calls": self.llm_calls,
            "duplicates_blocked": self.duplicates_blocked,
            "elapsed_s": round(time.monotonic() - self.started, 1),
            "case_timeout_s": self.limits.CASE_TIMEOUT_S,
            "limits_hit": list(self.limits_hit),
        }

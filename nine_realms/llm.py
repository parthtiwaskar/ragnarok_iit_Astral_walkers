"""LLM interface, a scripted FakeLLM for tests, and an Anthropic adapter.

Messages are provider-neutral dicts:
  {"role": "user", "content": str}
  {"role": "assistant", "content": str | None, "tool_calls": [{"id", "name", "args"}]}
  {"role": "tool", "tool_call_id", "name", "content": str}
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class LLMResponse:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: int = 0
    cost_usd: float = 0.0  # known only for providers with a price table (Anthropic); 0 means "not metered"


class LLMClient(Protocol):
    def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[ToolSpec]
    ) -> LLMResponse: ...


class FakeLLM:
    """Policy player: `policy(system, messages, tools) -> LLMResponse`."""

    def __init__(self, policy: Callable[[str, list[dict[str, Any]], list[ToolSpec]], LLMResponse]) -> None:
        self.policy = policy
        self.calls = 0

    def complete(self, *, system: str, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> LLMResponse:
        self.calls += 1
        return self.policy(system, messages, tools)


# US$ per million tokens: input, output, cache write (5-minute TTL is 1.25x input), cache read.
ANTHROPIC_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}
_PRICE_UNKNOWN = ANTHROPIC_PRICES["claude-opus-5-5"]  # an unlisted model is billed at the dearest listed rate, never as free


def anthropic_cost(model: str, input_tokens: int, output_tokens: int, cache_written: int = 0, cache_read: int = 0) -> float:
    p_in, p_out, p_write, p_read = ANTHROPIC_PRICES.get(model, _PRICE_UNKNOWN)
    return (input_tokens * p_in + output_tokens * p_out + cache_written * p_write + cache_read * p_read) / 1_000_000


def anthropic_tuning(model: str) -> dict[str, Any]:
    """Optional speed/cost knobs from the environment (unset = the model's defaults).

    NINE_REALMS_EFFORT: low | medium | high | xhigh | max (lower is faster and cheaper).
    NINE_REALMS_THINKING=between_tools: no extended thinking (Claude Sonnet 5.5 only; effort must then be high or below).
    Combinations the API would reject are dropped here rather than failing a whole trial."""
    out: dict[str, Any] = {}
    effort = os.environ.get("NINE_REALMS_EFFORT", "").strip().lower()
    thinking = os.environ.get("NINE_REALMS_THINKING", "").strip().lower()
    if thinking == "between_tools" and model == "claude-sonnet-5-5":
        out["thinking"] = {"type": "between_tools"}
        if effort in ("xhigh", "max"):
            effort = "high"
    if effort in ("low", "medium", "high", "xhigh", "max"):
        out["output_config"] = {"effort": effort}
    return out


def _cache_last_turn(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark the newest message cacheable, so the next step of the same agent turn re-reads the whole conversation so far
    from cache instead of paying for it again. Copies what it marks; the caller's messages are not changed."""
    if not msgs:
        return msgs
    last = dict(msgs[-1])
    content = last["content"]
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [dict(b) for b in content]
    if not blocks:
        return msgs
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    last["content"] = blocks
    return [*msgs[:-1], last]


class AnthropicClient:
    """Anthropic Messages API. The system prompt and tool list are identical on every call an agent makes, so they are marked
    cacheable (ANTHROPIC_PROMPT_CACHE=0 turns it off): repeat calls read them from cache, which is cheaper and faster.
    If the API ever rejects the cache markers the client retries once without them and stays uncached."""

    def __init__(self, model: str | None = None, max_tokens: int = 4096, client: Any = None, cache: bool | None = None) -> None:
        if client is None:
            import anthropic  # imported lazily so tests need no SDK

            client = anthropic.Anthropic()
        self._client = client
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
        self.max_tokens = max_tokens
        self.cache = (os.environ.get("ANTHROPIC_PROMPT_CACHE", "1") != "0") if cache is None else cache
        self.tuning = anthropic_tuning(self.model)

    def complete(self, *, system: str, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> LLMResponse:
        msgs: list[dict[str, Any]] = []
        for m in messages:
            if m["role"] == "user":
                msgs.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["args"]})
                msgs.append({"role": "assistant", "content": blocks})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if msgs and msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], list):
                    msgs[-1]["content"].append(block)
                else:
                    msgs.append({"role": "user", "content": [block]})
        tool_defs = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]

        def request(cached: bool) -> Any:
            kwargs: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens,
                                      "messages": _cache_last_turn(msgs) if cached else msgs,
                                      "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] if cached else system}
            if tool_defs:
                kwargs["tools"] = [dict(t) for t in tool_defs]
                if cached:
                    kwargs["tools"][-1]["cache_control"] = {"type": "ephemeral"}  # caches the whole tool list up to here
            kwargs.update(self.tuning)
            return self._client.messages.create(**kwargs)

        try:
            resp = request(self.cache)
        except Exception as exc:
            if self.cache and "cache_control" in str(exc).lower():
                self.cache = False
                resp = request(False)
            else:
                raise
        text = "".join(b.text for b in resp.content if b.type == "text") or None
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        u = resp.usage
        written, read = (getattr(u, "cache_creation_input_tokens", 0) or 0), (getattr(u, "cache_read_input_tokens", 0) or 0)
        # cache reads cost about a tenth of normal input, so they count a tenth against the spending cap
        tokens = u.input_tokens + u.output_tokens + written + int(0.1 * read)
        return LLMResponse(text, calls, tokens, cost_usd=anthropic_cost(self.model, u.input_tokens, u.output_tokens, written, read))


class OpenAICompatClient:
    """Any OpenAI-compatible chat-completions endpoint (Groq by default). Stdlib only; retries 429/5xx."""

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.groq.com/openai/v1",
                 max_tokens: int = 4096, retries: int = 6) -> None:
        self.api_key, self.model, self.base_url = api_key, model, base_url.rstrip("/")
        self.max_tokens, self.retries = max_tokens, retries

    def complete(self, *, system: str, messages: list[dict[str, Any]], tools: list[ToolSpec]) -> LLMResponse:
        import json
        import time
        import urllib.error
        import urllib.request

        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "assistant":
                am: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    am["tool_calls"] = [{"id": c["id"], "type": "function",
                                         "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                                        for c in m["tool_calls"]]
                msgs.append(am)
            elif m["role"] == "tool":
                msgs.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                msgs.append({"role": "user", "content": m["content"]})
        body: dict[str, Any] = {"model": self.model, "messages": msgs, "max_tokens": self.max_tokens}
        if not self.model.startswith("anthropic/"):
            body["temperature"] = 0.2  # current Claude models reject sampling parameters, so they get none
        if "openrouter.ai" in self.base_url:
            body["usage"] = {"include": True}  # OpenRouter then reports what each call cost, so the dollar caps apply
        if self.model.startswith("openai/gpt-oss"):
            body["reasoning_effort"] = "low"  # reasoning tokens count against the per-minute budget
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                              "parameters": t.parameters}} for t in tools]
            body["tool_choice"] = "auto"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                     "User-Agent": "nine-realms/0.1"})
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                full = e.read().decode(errors="replace")
                detail = full[:300]
                if e.code in (429, 500, 502, 503) and attempt < self.retries:
                    try:
                        wait = float(e.headers.get("retry-after") or min(2 ** attempt * 3, 30))
                    except ValueError:
                        wait = 30.0
                    if wait > 90:  # daily-budget exhaustion, not a per-minute blip: fail loudly instead of sleeping for minutes
                        raise RuntimeError(f"LLM rate limit, retry in {int(wait)}s: {detail}") from e
                    time.sleep(wait + 0.5)
                    continue
                if e.code == 400 and "tool_use_failed" in detail:
                    salvaged = self._salvage(full, [t.name for t in tools])
                    if salvaged is not None:
                        return salvaged
                raise RuntimeError(f"LLM HTTP {e.code}: {detail}") from e
        msg = data["choices"][0]["message"]
        calls = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(c["id"], c["function"]["name"], args))
        usage = data.get("usage") or {}
        try:
            cost = float(usage.get("cost") or 0)  # US$, reported by OpenRouter when usage.include is set
        except (TypeError, ValueError):
            cost = 0.0
        return LLMResponse(msg.get("content") or None, calls, usage.get("total_tokens", 0), cost_usd=cost)

    @staticmethod
    def _salvage(detail: str, tool_names: list[str]) -> LLMResponse | None:
        """gpt-oss sometimes emits a malformed tool call. Recover what it meant:
        - a real tool we offered -> a normal tool call
        - any other name (typically "json") -> its arguments ARE the final JSON answer
        - not parseable -> the raw text, so the agent's validator can ask for a retry"""
        import json
        import re

        m = re.search(r'"failed_generation"\s*:\s*"((?:[^"\\]|\\.)*)"', detail, re.S)
        if not m:
            return None
        try:
            gen = json.loads('"' + m.group(1) + '"')
        except json.JSONDecodeError:
            return None
        try:
            call = json.loads(gen)
        except json.JSONDecodeError:
            return LLMResponse(gen, [])
        if isinstance(call, dict) and "name" in call and "arguments" in call:
            args = call["arguments"] if isinstance(call["arguments"], dict) else {}
            if call["name"] in tool_names:
                return LLMResponse(None, [ToolCall("salvaged-" + str(abs(hash(gen)) % 10**6), call["name"], args)])
            return LLMResponse(json.dumps(args), [])
        return LLMResponse(gen, [])

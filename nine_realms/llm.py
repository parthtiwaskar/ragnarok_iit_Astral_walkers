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


class AnthropicClient:
    def __init__(self, model: str | None = None, max_tokens: int = 4096) -> None:
        import anthropic  # imported lazily so tests need no SDK

        self._client = anthropic.Anthropic()
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
        self.max_tokens = max_tokens

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
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
        resp = self._client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system, messages=msgs, **kwargs
        )
        text = "".join(b.text for b in resp.content if b.type == "text") or None
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        return LLMResponse(text, calls, resp.usage.input_tokens + resp.usage.output_tokens)


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
        body: dict[str, Any] = {"model": self.model, "messages": msgs, "max_tokens": self.max_tokens, "temperature": 0.2}
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
                detail = e.read().decode(errors="replace")[:300]
                if e.code in (429, 500, 502, 503) and attempt < self.retries:
                    wait = float(e.headers.get("retry-after") or min(2 ** attempt * 3, 30))
                    time.sleep(wait + 0.5)
                    continue
                if e.code == 400 and "tool_use_failed" in detail:
                    salvaged = self._salvage(detail, bool(tools))
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
        return LLMResponse(msg.get("content") or None, calls, data.get("usage", {}).get("total_tokens", 0))

    @staticmethod
    def _salvage(detail: str, tools_offered: bool) -> LLMResponse | None:
        """gpt-oss sometimes emits a malformed tool call; recover it if we offered tools, else return its text."""
        import json
        import re

        try:
            gen = json.loads(detail.split('"failed_generation":', 1)[1].rsplit("}", 2)[0].strip())
        except (IndexError, json.JSONDecodeError):
            m = re.search(r'"failed_generation":"(.*)"\}', detail, re.S)
            gen = m.group(1).replace('\\"', '"') if m else ""
        try:
            call = json.loads(gen) if isinstance(gen, str) else gen
        except json.JSONDecodeError:
            return None
        if tools_offered and isinstance(call, dict) and "name" in call:
            return LLMResponse(None, [ToolCall("salvaged-" + str(abs(hash(gen)) % 10**6), call["name"], call.get("arguments") or {})])
        return LLMResponse(gen if isinstance(gen, str) else json.dumps(gen), [])

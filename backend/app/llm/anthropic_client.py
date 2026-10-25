"""Anthropic Messages API client (async) with client-defined tools.

- No sampling parameters: the Messages API rejects temperature/top_p/top_k (2026-10-21 API change).
- Effort via `output_config.effort` (ANTHROPIC_EFFORT); thinking stays at the model default (adaptive).
- Assistant content is replayed unchanged (thinking blocks must not be edited or dropped by us).
- Server-side refusal fallback opt-in (`fallbacks="default"`); a final refusal is a non-retryable error.
- SDK retries are off (max_retries=0): the agent loop owns retries so LLM_MAX_RETRIES is the hard limit.
"""

from __future__ import annotations

from typing import Any

from app.llm.client import LLMError, LLMResponse, Msg, ToolCall, ToolSpec, Usage

FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_MODEL = "claude-opus-5-5"


class AnthropicClient:
    provider = "anthropic"

    def __init__(
        self,
        model: str | None,
        api_key: str | None,
        effort: str | None = "high",
        timeout_s: float = 170.0,
        fallbacks: bool = True,
    ) -> None:
        import anthropic

        self._sdk = anthropic
        self.model = model or DEFAULT_MODEL
        self.effort = effort or None
        self.fallbacks = fallbacks
        self._client = anthropic.AsyncAnthropic(api_key=api_key or None, max_retries=0, timeout=timeout_s)

    @staticmethod
    def _messages(messages: list[Msg]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []  # tool_result + text blocks for the next user turn

        def flush() -> None:
            if pending:
                out.append({"role": "user", "content": list(pending)})
                pending.clear()

        for m in messages:
            if m.role == "assistant":
                flush()
                if m.raw is not None:
                    content: Any = m.raw
                else:
                    content = []
                    if m.content:
                        content.append({"type": "text", "text": m.content})
                    for tc in m.tool_calls:
                        content.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args})
                out.append({"role": "assistant", "content": content})
            elif m.role == "tool":
                pending.append({"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content})
            else:
                pending.append({"type": "text", "text": m.content})
        flush()
        return out

    async def chat(
        self, system: str, messages: list[Msg], tools: list[ToolSpec], max_tokens: int, temperature: float
    ) -> LLMResponse:
        sdk = self._sdk
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": self._messages(messages),
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ],
            "cache_control": {"type": "ephemeral"},
        }
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        try:
            if self.fallbacks:
                resp = await self._client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **kwargs
                )
            else:
                resp = await self._client.messages.create(**kwargs)
        except sdk.RateLimitError as exc:
            raise LLMError(f"rate limited: {exc}", retryable=True) from exc
        except sdk.APIStatusError as exc:
            code = getattr(exc, "status_code", 0) or 0
            raise LLMError(
                f"HTTP {code}: {getattr(exc, 'message', exc)}",
                retryable=code >= 500 or code in (408, 409, 429),
            ) from exc
        except sdk.APIConnectionError as exc:  # includes APITimeoutError
            raise LLMError(f"connection error: {exc}", retryable=True) from exc
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMError(f"model refused (category={category})", retryable=False)
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {"__raw__": block.input}
                calls.append(ToolCall(id=block.id, name=block.name, args=dict(args)))
        u = resp.usage
        input_total = (
            (u.input_tokens or 0)
            + (getattr(u, "cache_read_input_tokens", 0) or 0)
            + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        )
        return LLMResponse(
            text="".join(text_parts),
            tool_calls=calls,
            usage=Usage(input_tokens=input_total, output_tokens=u.output_tokens or 0),
            stop_reason=str(resp.stop_reason),
            model=resp.model,
            raw=list(resp.content),
        )

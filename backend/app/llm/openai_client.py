"""OpenAI Chat Completions client (async) with function tools. SDK retries are off; the agent loop retries."""

from __future__ import annotations

import json
from typing import Any

from app.llm.client import LLMError, LLMResponse, Msg, ToolCall, ToolSpec, Usage

DEFAULT_MODEL = "gpt-4.1"
NO_SAMPLING_PREFIXES = ("o1", "o3", "o4", "gpt-5")  # reasoning models reject temperature


class OpenAIClient:
    provider = "openai"

    def __init__(self, model: str | None, api_key: str | None, timeout_s: float = 170.0) -> None:
        import openai

        self._sdk = openai
        self.model = model or DEFAULT_MODEL
        self._client = openai.AsyncOpenAI(api_key=api_key or None, max_retries=0, timeout=timeout_s)

    @staticmethod
    def _messages(system: str, messages: list[Msg]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                        }
                        for tc in m.tool_calls
                    ]
                out.append(item)
            elif m.role == "tool":
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
            else:
                out.append({"role": "user", "content": m.content})
        return out

    async def chat(
        self, system: str, messages: list[Msg], tools: list[ToolSpec], max_tokens: int, temperature: float
    ) -> LLMResponse:
        sdk = self._sdk
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(system, messages),
            "tools": [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ],
            "max_completion_tokens": max_tokens,
        }
        if not self.model.startswith(NO_SAMPLING_PREFIXES):
            kwargs["temperature"] = temperature
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except sdk.RateLimitError as exc:
            raise LLMError(f"rate limited: {exc}", retryable=True) from exc
        except sdk.APIStatusError as exc:
            code = getattr(exc, "status_code", 0) or 0
            raise LLMError(f"HTTP {code}: {exc}", retryable=code >= 500 or code in (408, 409, 429)) from exc
        except sdk.APIConnectionError as exc:
            raise LLMError(f"connection error: {exc}", retryable=True) from exc
        choice = resp.choices[0]
        calls: list[ToolCall] = []
        for tc in choice.message.tool_calls or []:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            try:
                args = json.loads(fn.arguments or "{}")
            except json.JSONDecodeError:
                args = {"__raw__": fn.arguments}
            calls.append(
                ToolCall(id=tc.id, name=fn.name, args=args if isinstance(args, dict) else {"__raw__": args})
            )
        u = resp.usage
        return LLMResponse(
            text=choice.message.content or "",
            tool_calls=calls,
            usage=Usage(
                input_tokens=u.prompt_tokens if u else 0, output_tokens=u.completion_tokens if u else 0
            ),
            stop_reason=choice.finish_reason or "",
            model=resp.model,
        )

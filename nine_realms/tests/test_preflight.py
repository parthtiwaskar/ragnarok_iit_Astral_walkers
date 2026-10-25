"""The pre-demo key check: one tiny request, a clear verdict, never a crash."""
from __future__ import annotations

from nine_realms import preflight
from nine_realms.llm import LLMResponse, ToolCall


def run_with(llm, provider="anthropic"):
    lines: list[str] = []
    code = preflight.run(make_llm=lambda p: llm, choose_provider=lambda: (provider, "forced by test") if provider else ("", "no LLM key set"),
                         provider_model=lambda p: "claude-sonnet-5-5", out=lines.append)
    return code, "\n".join(lines)


class Ok:
    def complete(self, **_):
        return LLMResponse(None, [ToolCall("t1", "echo", {"word": "ready"})], 120, cost_usd=0.0004)


class NoTool:
    def complete(self, **_):
        return LLMResponse("ready", [], 50)


class Fails:
    def __init__(self, message):
        self.message = message

    def complete(self, **_):
        raise RuntimeError(self.message)


def test_a_working_key_reports_ready_with_time_tokens_and_cost():
    code, text = run_with(Ok())
    assert code == 0 and "READY" in text and "120 tokens" in text and "$0.00040" in text and "tool calling works" in text


def test_no_key_is_not_ready_and_says_why():
    code, text = run_with(Ok(), provider="")
    assert code == 2 and "NOT READY" in text and "no LLM key set" in text


def test_a_model_that_does_not_call_tools_is_not_ready():
    code, text = run_with(NoTool())
    assert code == 1 and "did NOT happen" in text


def test_provider_errors_are_explained_in_plain_words():
    cases = {
        "Error code: 400 - Your credit balance is too low to access the Anthropic API": "No credit",
        "You have reached your specified workspace API usage limits": "usage/spend limit",
        'LLM HTTP 402: {"error":{"message":"Insufficient Balance"}}': "no balance",
        "Error code: 401 - invalid x-api-key": "key was rejected",
        "Error code: 404 - model: claude-nope not_found_error": "model name is not available",
        "Error code: 429 - rate limit exceeded": "Rate limited",
    }
    for message, expected in cases.items():
        code, text = run_with(Fails(message))
        assert code == 1 and expected in text, (message, text)
    assert preflight.explain("something odd") == "Unexpected error from the provider."

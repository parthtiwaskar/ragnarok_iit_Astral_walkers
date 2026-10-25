"""Check the configured LLM key before a demo, with one tiny request (well under a cent):

    python -m nine_realms.preflight

It picks the provider the server would pick, asks the model to call one tool (the agents depend on tool calling), and
prints the provider, model, round-trip time and cost, or a plain explanation of what is wrong. Exit code 0 means ready.
"""
from __future__ import annotations

import sys
import time

from .env import load_env
from .llm import ToolSpec

PING_TOOL = ToolSpec("echo", "Echo one word back.", {"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]})

# (substring of the provider's error, plain explanation), checked in order
EXPLANATIONS = [
    ("credit balance is too low", "No credit on this Anthropic account: add credit in the Anthropic Console under Billing."),
    ("usage limit", "This account or workspace has reached its usage/spend limit: raise the limit in the provider's console or use another key."),
    ("insufficient balance", "The account has no balance: top it up in the provider's console."),
    ("insufficient credits", "The account has no credits: add credits in the provider's console."),
    ("402", "Payment required: the account has no balance."),
    ("invalid x-api-key", "The key was rejected: check it was copied whole, with no spaces or quotes."),
    ("authentication", "The key was rejected: check it was copied whole, with no spaces or quotes."),
    ("401", "The key was rejected: check it was copied whole, with no spaces or quotes."),
    ("permission", "The key is valid but not allowed to use this model or API."),
    ("403", "The key is valid but not allowed to use this model or API."),
    ("not_found", "The model name is not available to this key: check ANTHROPIC_MODEL (or the provider's model setting)."),
    ("404", "The model name is not available to this key: check ANTHROPIC_MODEL (or the provider's model setting)."),
    ("overloaded", "The provider is overloaded right now: wait a minute and try again."),
    ("rate limit", "Rate limited: wait a minute and try again."),
    ("429", "Rate limited: wait a minute and try again."),
    ("getaddrinfo", "No internet connection to the provider."),
    ("connection", "No connection to the provider: check the internet connection."),
    ("timed out", "The provider did not answer in time: check the connection and try again."),
]


def explain(error: str) -> str:
    low = error.lower()
    return next((plain for needle, plain in EXPLANATIONS if needle in low), "Unexpected error from the provider.")


def run(make_llm=None, choose_provider=None, provider_model=None, out=print) -> int:
    from . import server  # imported here so a broken key never stops `import nine_realms`

    make_llm = make_llm or server.make_llm
    choose_provider = choose_provider or server.choose_provider
    provider_model = provider_model or server.provider_model
    provider, why = choose_provider()
    if not provider:
        out(f"NOT READY: live trials are off - {why}")
        return 2
    out(f"provider: {provider} (model {provider_model(provider)}) - {why}")
    llm = make_llm(provider)
    if hasattr(llm, "retries"):
        llm.retries = 0  # one honest attempt; the server keeps its own retries
    started = time.time()
    try:
        r = llm.complete(system="You are a connectivity check. Call the echo tool once with the word ready.",
                         messages=[{"role": "user", "content": "Call echo with word=ready."}], tools=[PING_TOOL])
    except Exception as exc:  # report, never crash
        out(f"NOT READY: {explain(str(exc))}")
        out(f"  provider said: {str(exc)[:240]}")
        return 1
    seconds = time.time() - started
    cost = f", cost ${r.cost_usd:.5f}" if r.cost_usd else ""
    called = any(c.name == "echo" for c in r.tool_calls)
    out(f"READY: answered in {seconds:.1f}s, {r.tokens} tokens{cost}; tool calling {'works' if called else 'did NOT happen'}")
    if not called:
        out("  The model replied without calling the tool; the agents need tool calls, so try a stronger model.")
        return 1
    return 0


def main() -> int:
    load_env()
    return run()


if __name__ == "__main__":
    sys.exit(main())

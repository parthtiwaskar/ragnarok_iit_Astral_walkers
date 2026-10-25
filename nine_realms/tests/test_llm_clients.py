"""Anthropic client request shape, prompt caching and the cache fallback, checked with a fake SDK client."""
from __future__ import annotations

from types import SimpleNamespace as NS

from nine_realms.llm import AnthropicClient, ToolSpec

TOOLS = [ToolSpec("a", "first", {"type": "object", "properties": {}}), ToolSpec("b", "second", {"type": "object", "properties": {}})]


class FakeAPI:
    def __init__(self, fail_on_cache: bool = False):
        self.calls, self.fail_on_cache = [], fail_on_cache
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.fail_on_cache and isinstance(kw["system"], list):
            raise RuntimeError("400 invalid_request_error: cache_control is not supported here")
        return NS(content=[NS(type="text", text='{"ok": true}'),
                           NS(type="tool_use", id="t1", name="a", input={"query": "x"})],
                  usage=NS(input_tokens=100, output_tokens=20, cache_creation_input_tokens=500, cache_read_input_tokens=2000))


def test_system_prompt_and_tool_list_are_marked_cacheable():
    api = FakeAPI()
    r = AnthropicClient(client=api, model="m").complete(system="SYSTEM", messages=[{"role": "user", "content": "hi"}], tools=TOOLS)
    kw = api.calls[0]
    assert kw["system"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]
    assert "cache_control" not in kw["tools"][0] and kw["tools"][-1]["cache_control"] == {"type": "ephemeral"}   # one marker caches the whole list
    assert r.text == '{"ok": true}' and r.tool_calls[0].name == "a" and r.tool_calls[0].args == {"query": "x"}


def test_cache_reads_count_a_tenth_against_the_spending_cap():
    r = AnthropicClient(client=FakeAPI(), model="m").complete(system="S", messages=[{"role": "user", "content": "x"}], tools=[])
    assert r.tokens == 100 + 20 + 500 + 200


def test_caching_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_PROMPT_CACHE", "0")
    api = FakeAPI()
    AnthropicClient(client=api, model="m").complete(system="SYSTEM", messages=[{"role": "user", "content": "hi"}], tools=TOOLS)
    assert api.calls[0]["system"] == "SYSTEM" and all("cache_control" not in t for t in api.calls[0]["tools"])


def test_if_the_api_rejects_cache_markers_the_client_retries_once_without_them():
    api = FakeAPI(fail_on_cache=True)
    c = AnthropicClient(client=api, model="m")
    assert c.complete(system="S", messages=[{"role": "user", "content": "x"}], tools=TOOLS).text == '{"ok": true}'
    assert len(api.calls) == 2 and api.calls[1]["system"] == "S" and c.cache is False
    c.complete(system="S", messages=[{"role": "user", "content": "x"}], tools=TOOLS)
    assert len(api.calls) == 3 and api.calls[2]["system"] == "S"          # stays uncached afterwards


def test_tool_results_follow_their_assistant_turn_in_the_neutral_message_format():
    api = FakeAPI()
    msgs = [{"role": "user", "content": "go"}, {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "name": "a", "args": {"q": 1}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "a", "content": "result"}]
    AnthropicClient(client=api, model="m").complete(system="S", messages=msgs, tools=TOOLS)
    sent = api.calls[0]["messages"]
    assert sent[1]["content"][0] == {"type": "tool_use", "id": "c1", "name": "a", "input": {"q": 1}}
    assert sent[2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "result"}]}

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
    # the newest turn carries the conversation cache marker; otherwise the shape is unchanged
    assert sent[2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "result",
                                                     "cache_control": {"type": "ephemeral"}}]}


class FakeHTTP:
    """Stands in for urllib's urlopen: records each request body and answers like an OpenAI-compatible endpoint."""

    def __init__(self, usage):
        self.bodies, self.usage = [], usage

    def __call__(self, req, timeout=None):
        import io
        import json
        self.bodies.append(json.loads(req.data))
        reply = {"choices": [{"message": {"content": "ok", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "echo", "arguments": '{"word": "ready"}'}}]}}], "usage": self.usage}
        resp = io.BytesIO(json.dumps(reply).encode())
        resp.__enter__ = lambda *a: resp
        resp.__exit__ = lambda *a: False
        return resp


def test_openrouter_claude_gets_no_temperature_and_reports_cost(monkeypatch):
    import urllib.request

    from nine_realms.llm import OpenAICompatClient
    http = FakeHTTP({"total_tokens": 150, "cost": 0.0123})
    monkeypatch.setattr(urllib.request, "urlopen", http)
    r = OpenAICompatClient("k", "anthropic/claude-sonnet-5.5", base_url="https://openrouter.ai/api/v1").complete(
        system="S", messages=[{"role": "user", "content": "x"}], tools=TOOLS)
    body = http.bodies[0]
    assert "temperature" not in body                      # current Claude models reject sampling parameters
    assert body["usage"] == {"include": True}             # ask OpenRouter for the cost of each call
    assert r.cost_usd == 0.0123 and r.tokens == 150 and r.tool_calls[0].args == {"word": "ready"}


def test_other_openai_compatible_models_keep_temperature_and_report_no_cost(monkeypatch):
    import urllib.request

    from nine_realms.llm import OpenAICompatClient
    http = FakeHTTP({"total_tokens": 80})
    monkeypatch.setattr(urllib.request, "urlopen", http)
    r = OpenAICompatClient("k", "deepseek-chat", base_url="https://api.deepseek.com/v1").complete(
        system="S", messages=[{"role": "user", "content": "x"}], tools=[])
    assert http.bodies[0]["temperature"] == 0.2 and "usage" not in http.bodies[0]
    assert r.cost_usd == 0.0                              # unmetered: the token cap still applies


def test_the_conversation_so_far_is_cached_without_changing_the_callers_messages():
    api = FakeAPI()
    msgs = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "second"}]
    AnthropicClient(client=api, model="m").complete(system="S", messages=msgs, tools=[])
    sent = api.calls[0]["messages"]
    assert sent[-1]["content"] == [{"type": "text", "text": "second", "cache_control": {"type": "ephemeral"}}]
    assert sent[0]["content"] == "first"                          # only the newest turn is marked
    assert msgs[-1]["content"] == "second"                        # the agent's own history is untouched


def test_each_call_reports_its_dollar_cost_from_the_price_table():
    from nine_realms.llm import anthropic_cost
    r = AnthropicClient(client=FakeAPI(), model="claude-sonnet-5-5").complete(system="S", messages=[{"role": "user", "content": "x"}], tools=[])
    # 100 input at $2, 20 output at $10, 500 cache writes at $2.50, 2000 cache reads at $0.20 (per million)
    assert abs(r.cost_usd - (100 * 2 + 20 * 10 + 500 * 2.5 + 2000 * 0.2) / 1e6) < 1e-12
    assert anthropic_cost("claude-unlisted", 1_000_000, 0) == 4.0  # unknown models bill at the dearest listed rate


def test_effort_and_thinking_knobs_reach_the_request_and_invalid_combinations_are_dropped(monkeypatch):
    monkeypatch.setenv("NINE_REALMS_EFFORT", "low")
    api = FakeAPI()
    AnthropicClient(client=api, model="claude-sonnet-5-5").complete(system="S", messages=[{"role": "user", "content": "x"}], tools=[])
    assert api.calls[0]["output_config"] == {"effort": "low"} and "thinking" not in api.calls[0]

    monkeypatch.setenv("NINE_REALMS_THINKING", "between_tools")
    monkeypatch.setenv("NINE_REALMS_EFFORT", "max")             # the API rejects between_tools above high
    api = FakeAPI()
    AnthropicClient(client=api, model="claude-sonnet-5-5").complete(system="S", messages=[{"role": "user", "content": "x"}], tools=[])
    assert api.calls[0]["thinking"] == {"type": "between_tools"} and api.calls[0]["output_config"] == {"effort": "high"}

    api = FakeAPI()                                               # between_tools exists only on Sonnet 5.5
    AnthropicClient(client=api, model="claude-haiku-5-5").complete(system="S", messages=[{"role": "user", "content": "x"}], tools=[])
    assert "thinking" not in api.calls[0]

    monkeypatch.delenv("NINE_REALMS_THINKING")
    monkeypatch.setenv("NINE_REALMS_EFFORT", "turbo")             # not a real level: send nothing rather than fail
    api = FakeAPI()
    AnthropicClient(client=api, model="claude-sonnet-5-5").complete(system="S", messages=[{"role": "user", "content": "x"}], tools=[])
    assert "output_config" not in api.calls[0]

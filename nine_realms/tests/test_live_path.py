"""The live-trial path, without spending money.

1. Provider selection: swapping keys must pick the intended provider and explain the choice.
2. End to end through the real HTTP server: POST /api/cases -> background trial (scripted FakeLLM) ->
   store -> GET the case, its events and its briefing. Only `make_llm` is replaced.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from nine_realms import FakeLLM, server
from nine_realms.llm import AnthropicClient, OpenAICompatClient
from nine_realms.server import App, Config, choose_provider, make_handler, make_llm, provider_model
from nine_realms.store import Store

from .fixtures import AS_OF, Q, StubToolBox, make_policy

KEYS = ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "NINE_REALMS_PROVIDER",
        "OPENAI_BASE_URL", "NINE_REALMS_OPENAI_MODEL", "DEEPSEEK_MODEL", "ANTHROPIC_MODEL", "GROQ_MODEL")


@pytest.fixture()
def env(monkeypatch):
    """A clean slate: no provider keys, whatever the developer's shell or .env holds."""
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


# ---- 1. provider selection ---------------------------------------------------------

def test_no_key_disables_live_trials_and_says_why(env):
    provider, why = choose_provider()
    assert provider == "" and "no LLM key set" in why


def test_first_key_in_order_wins_and_the_others_are_named(env):
    env.setenv("ANTHROPIC_API_KEY", "a")
    env.setenv("OPENAI_API_KEY", "o")
    provider, why = choose_provider()
    assert provider == "anthropic"
    assert "OPENAI_API_KEY" in why and "NINE_REALMS_PROVIDER" in why  # a stale key would otherwise win silently
    env.setenv("DEEPSEEK_API_KEY", "d")
    assert choose_provider()[0] == "deepseek"


def test_forcing_a_provider_overrides_auto_detection(env):
    env.setenv("DEEPSEEK_API_KEY", "stale")
    env.setenv("OPENAI_API_KEY", "fresh")
    env.setenv("NINE_REALMS_PROVIDER", "openai")
    assert choose_provider() == ("openai", "forced by NINE_REALMS_PROVIDER (OPENAI_API_KEY is set)")


def test_forcing_a_provider_without_its_key_is_reported_not_silently_ignored(env):
    env.setenv("DEEPSEEK_API_KEY", "d")
    env.setenv("NINE_REALMS_PROVIDER", "anthropic")
    provider, why = choose_provider()
    assert provider == "" and "ANTHROPIC_API_KEY is not set" in why
    env.setenv("NINE_REALMS_PROVIDER", "gemini")
    provider, why = choose_provider()
    assert provider == "" and "not one of" in why


def test_each_provider_builds_the_right_client_without_any_network(env):
    env.setenv("DEEPSEEK_API_KEY", "d")
    c = make_llm("deepseek")
    assert isinstance(c, OpenAICompatClient) and c.base_url == "https://api.deepseek.com/v1" and c.model == "deepseek-chat"

    env.setenv("OPENAI_API_KEY", "o")
    c = make_llm("openai")
    assert c.base_url == "https://api.openai.com/v1" and c.model == "gpt-4.1-mini"
    env.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1/")
    c = make_llm("openai")
    assert c.base_url == "https://openrouter.ai/api/v1" and c.model == "openai/gpt-4.1-mini"  # gateways want vendor/model
    env.setenv("NINE_REALMS_OPENAI_MODEL", "anthropic/claude-sonnet-5-5")
    assert make_llm("openai").model == "anthropic/claude-sonnet-5-5"
    assert provider_model("openai") == "anthropic/claude-sonnet-5-5"

    env.setenv("GROQ_API_KEY", "g")
    assert make_llm("groq").base_url == "https://api.groq.com/openai/v1"

    env.setenv("ANTHROPIC_API_KEY", "a")
    assert isinstance(make_llm("anthropic"), AnthropicClient) and provider_model("anthropic") == "claude-sonnet-5-5"


# ---- 2. end to end through the server ----------------------------------------------

class StubCorpus(StubToolBox):
    """The fixture corpus, plus the one attribute the case routes read."""

    pack_as_of = AS_OF


@pytest.fixture()
def live(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_make_llm(provider: str) -> FakeLLM:
        calls.append(provider)
        return FakeLLM(make_policy())

    monkeypatch.setattr(server, "make_llm", fake_make_llm)
    app = App(StubCorpus(), Store(), Config(token="t0k", memory_dir=str(tmp_path / "memory")), "deepseek")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {"Content-Type": "application/json", "Authorization": "Bearer t0k"})
        r = c.getresponse()
        raw = r.read()
        return r.status, (json.loads(raw) if r.getheader("Content-Type", "").startswith("application/json") else raw.decode())

    yield app, call, calls
    httpd.shutdown()
    httpd.server_close()


def _wait_until_finished(app: App, call, case_id: str, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app.running is None:
            code, case = call("GET", f"/api/cases/{case_id}")
            assert code == 200
            return case  # type: ignore[return-value]
        time.sleep(0.05)
    raise AssertionError(f"trial {case_id} did not finish within {timeout}s")


def test_a_trial_started_over_http_runs_to_completion_and_is_readable_back(live):
    app, call, calls = live
    code, body = call("POST", "/api/cases", {"question": Q, "as_of": AS_OF})
    assert code == 202, body
    case_id = body["case_id"]  # type: ignore[index]
    assert calls == ["deepseek"]  # the configured provider was the one asked for a client

    case = _wait_until_finished(app, call, case_id)
    assert case["status"] == "COMPLETE", case.get("status_reasons")
    assert len(case["runs"]) == 2  # the protocol's remand and second run, over HTTP
    assert case["claims"] and case["rulings"] and case["attacks"] and case["responses"]
    assert set(case["rulings"]) == set(case["claims"])  # Odin ruled on every claim
    assert case["ranges"]

    code, events = call("GET", f"/api/cases/{case_id}/events")  # since defaults to -1: seq 0 included, like the dashboard
    assert code == 200
    kinds = {e.get("type") or e.get("event") for e in events["events"]}  # type: ignore[index]
    assert {"case_started", "claim_submitted", "case_finished"} <= kinds

    code, md = call("GET", f"/api/cases/{case_id}/briefing.md")
    assert code == 200 and "Established findings" in md  # type: ignore[operator]

    code, listed = call("GET", "/api/cases")
    assert code == 200 and case_id in json.dumps(listed)


def test_a_second_trial_is_refused_while_one_is_running(live):
    app, call, _ = live
    app.cfg.cooldown_s = 0  # otherwise the per-client cooldown answers 429 before the busy check can answer 409
    gate = threading.Event()
    real = app._run

    def slow_run(*args):  # hold the first trial open until the second request has been answered
        gate.wait(10)
        real(*args)

    app._run = slow_run
    code, first = call("POST", "/api/cases", {"question": Q, "as_of": AS_OF})
    assert code == 202
    code, second = call("POST", "/api/cases", {"question": Q, "as_of": AS_OF})
    assert code == 409 and second["case_id"] == first["case_id"]  # type: ignore[index]
    gate.set()
    _wait_until_finished(app, call, first["case_id"])  # type: ignore[index]


def test_a_provider_failure_ends_the_case_cleanly_with_a_reason(live, monkeypatch):
    """A rejected key (e.g. HTTP 402) must leave a finished case with the provider's reason, never a hung OPEN case."""
    app, call, _ = live

    class Broke:
        def complete(self, **_):
            raise RuntimeError('LLM HTTP 402: {"error":{"message":"Insufficient Balance"}}')

    monkeypatch.setattr(server, "make_llm", lambda provider: Broke())
    code, body = call("POST", "/api/cases", {"question": Q, "as_of": AS_OF})
    assert code == 202
    case = _wait_until_finished(app, call, body["case_id"])  # type: ignore[index]
    assert case["status"] == "ABORTED"
    assert "Insufficient Balance" in " ".join(case["status_reasons"])

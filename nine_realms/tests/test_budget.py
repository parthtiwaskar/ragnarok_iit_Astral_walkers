"""Spending caps for a small API budget: a per-case dollar cap, and a reserve only Odin may spend so a capped case still ends
with his rulings and a written briefing instead of the rulings-only fallback."""
from __future__ import annotations

import pytest

from nine_realms import FakeLLM, Thing
from nine_realms.llm import LLMResponse
from nine_realms.tools import Budget, LimitHit, Limits

from .fixtures import AS_OF, Q, StubToolBox, make_policy


def test_other_agents_stop_short_of_the_cap_but_odin_may_use_the_reserve():
    b = Budget(Limits(max_tokens=1000, odin_reserve=0.2))
    b.add_tokens(850)                                   # past the 800 the others may use, inside Odin's 1000
    with pytest.raises(LimitHit):
        b.charge_llm("THOR")
    b.charge_llm("ODIN")
    b.add_tokens(200)
    with pytest.raises(LimitHit):                       # the hard cap holds for Odin too
        b.charge_llm("ODIN")


def test_the_dollar_cap_stops_a_case_and_is_off_when_unset():
    b = Budget(Limits(max_usd=1.0, odin_reserve=0.1))
    b.add_tokens(10, usd=0.95)
    with pytest.raises(LimitHit, match="max_usd=1.0"):
        b.charge_llm("LOKI")
    b.charge_llm("ODIN")                                 # 0.95 < 1.0
    unmetered = Budget(Limits(max_usd=0.0))
    unmetered.add_tokens(10, usd=99.0)
    unmetered.charge_llm("THOR")                         # 0 means no dollar cap (providers that report no cost)


def priced(policy, usd_per_call: float):
    """The scripted trial, with every reply priced like a real metered call."""
    def wrapped(system, messages, tools):
        r = policy(system, messages, tools)
        return LLMResponse(r.text, r.tool_calls, r.tokens or 1000, cost_usd=usd_per_call)
    return wrapped


def test_a_case_that_hits_the_dollar_cap_ends_cleanly_with_odins_briefing():
    llm = FakeLLM(priced(make_policy(), usd_per_call=0.05))
    t = Thing(llm, StubToolBox(), limits=Limits(max_usd=0.80, odin_reserve=0.15))
    case = t.run_case(Q, AS_OF)
    assert case.status == "INCOMPLETE" and any("max_usd" in r for r in case.status_reasons)
    assert case.rulings, "Odin ruled before the cap"
    assert case.briefing_md and "Odin could not draft" not in case.briefing_md  # the reserve paid for the written briefing
    assert t.budget.usd <= 0.80 + 0.05 + 1e-9           # never more than one call past the hard cap
    finished = [e for e in t.bus.events if e["type"] == "case_finished"][-1]
    assert finished["usd"] == round(t.budget.usd, 4) and finished["llm_calls"] == t.budget.llm_calls


def test_the_daily_budget_refuses_a_trial_that_could_overspend_it(tmp_path):
    import time

    from nine_realms.server import App, Config
    from nine_realms.store import Store

    store = Store()
    app = App(object(), store, Config(max_usd_per_day=4.50, max_case_usd=1.00, cooldown_s=0, max_trials_per_day=99,
                                      memory_dir=str(tmp_path)), "anthropic")
    assert app.allow_trial("judge")[0]                                   # nothing spent yet
    store.add_event("CASE-1", {"seq": 0, "ts": time.time(), "type": "case_finished", "usd": 3.20})
    assert app.allow_trial("judge")[0]                                   # 3.20 + 1.00 worst case fits in 4.50
    store.add_event("CASE-2", {"seq": 0, "ts": time.time(), "type": "case_finished", "usd": 0.40})
    ok, why, retry = app.allow_trial("judge")                            # 3.60 + 1.00 would not
    assert not ok and "$3.60 of $4.50" in why and retry > 0
    store.add_event("CASE-0", {"seq": 0, "ts": time.time() - 2 * 86400, "type": "case_finished", "usd": 50.0})
    assert store.spent_since(time.time() - 86400) == pytest.approx(3.60)  # yesterday's spend does not count today

    unlimited = App(object(), store, Config(max_usd_per_day=0.0, cooldown_s=0, max_trials_per_day=99, memory_dir=str(tmp_path)), "anthropic")
    assert unlimited.allow_trial("judge")[0]                             # 0 turns the daily cap off


def test_an_unmetered_provider_is_not_stopped_by_the_dollar_cap():
    t = Thing(FakeLLM(make_policy()), StubToolBox(), limits=Limits(max_usd=0.01))
    assert t.run_case(Q, AS_OF).status == "COMPLETE"     # FakeLLM reports no cost, like DeepSeek/OpenAI/Groq

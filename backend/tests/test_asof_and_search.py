"""The as-of firewall on all four leak routes, plus hybrid search filters."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.memory.store import MemoryStore
from app.search.dense import HashEmbedder
from app.search.filters import SearchFilters
from app.tools.asof import Clock, as_of_status, month_visible
from app.tools.context import Services
from app.tools.event_tools import event_profile
from app.tools.trade_tools import trade_stats


def _ids(res: dict) -> list[str]:  # type: ignore[type-arg]
    return [r["doc_id"] for r in res["results"]]


def test_route1_documents_after_as_of_are_invisible(services: Services) -> None:
    clock = Clock(date(2025, 4, 10))
    res = services.searcher.search(
        "canal survey further damage reopening delayed June", SearchFilters(), clock, k=20
    )
    assert "RM-DOC-0116" not in _ids(res)  # dated 2025-04-20
    later = services.searcher.search(
        "canal survey further damage reopening delayed June", SearchFilters(), Clock(date(2025, 4, 30)), k=20
    )
    assert "RM-DOC-0116" in _ids(later)


def test_route2_trade_rows_month_end_rule(services: Services) -> None:
    assert not month_visible("2025-04", Clock(date(2025, 4, 10)))
    assert month_visible("2025-03", Clock(date(2025, 4, 10)))
    assert month_visible("2025-04", Clock(date(2025, 4, 10), trade_rule="month_start"))
    st = trade_stats(services.repo, "TT-A01", date(2025, 3, 2), Clock(date(2025, 4, 10)))
    assert st is not None and st.post_months == ["2025-03"] and st.hidden_future_rows == 3
    assert st.observed_change is not None and st.observed_change < -0.9
    st_early = trade_stats(services.repo, "TT-A01", date(2025, 3, 2), Clock(date(2025, 3, 20)))
    assert st_early is not None and st_early.observed_change is None  # March not complete yet


def test_route3_retraction_published_after_as_of_is_ignored(services: Services) -> None:
    before = as_of_status(services.repo, "RM-DOC-0105", Clock(date(2025, 3, 8)))
    after = as_of_status(services.repo, "RM-DOC-0105", Clock(date(2025, 3, 12)))
    assert before["retracted"] is False and after["retracted"] is True
    assert as_of_status(services.repo, "RM-DOC-0102", Clock(date(2025, 3, 5)))["corrected_by"] is None


def test_route4_factual_memory_respects_knowledge_date(tmp_path: Path) -> None:
    m = MemoryStore("thor", tmp_path / "thor.sqlite", HashEmbedder())
    m.add("fact", "the canal reopening was delayed into June", ["duration"], "C1", 1, "2025-06-30")
    m.add("procedural", "corroborate duration claims with Tier 1 sources", ["duration"])
    early = m.recall("canal reopening duration", 5, Clock(date(2025, 4, 1)))
    late = m.recall("canal reopening duration", 5, Clock(date(2025, 7, 1)))
    assert [x.kind for x in early] == ["procedural"]
    assert {x.kind for x in late} == {"fact", "procedural"}


def test_event_profile_verdicts_are_as_of_aware(services: Services) -> None:
    assert event_profile(services.repo, "EVT-A", Clock(date(2025, 4, 10)))["rule_verdict"] == "VERIFIED"
    assert event_profile(services.repo, "EVT-R", Clock(date(2025, 4, 10)))["rule_verdict"] == "REFUTED"
    assert event_profile(services.repo, "EVT-R", Clock(date(2025, 4, 1)))["rule_verdict"] == "UNCONFIRMED"
    nyk = event_profile(services.repo, "EVT-B", Clock(date(2025, 4, 10)))
    assert nyk["rule_verdict"] == "NOT_YET_KNOWN" and "per_tier" not in nyk  # reveals nothing
    assert event_profile(services.repo, "EVT-ZZ", Clock(date(2025, 4, 10)))["rule_verdict"] == "NOT_FOUND"


def test_filters_combine_and_empty_note(services: Services, clock: Clock) -> None:
    res = services.searcher.search("canal closure", SearchFilters(event_id="EVT-A", tier_max=1), clock, k=10)
    assert res["results"] and all(r["tier"] == 1 and r["event_id"] == "EVT-A" for r in res["results"])
    assert res["filters"] == {"event_id": ["evt-a"], "tier_max": 1, "date_to": "2025-04-10"}
    none = services.searcher.search(
        "canal closure", SearchFilters(event_id="EVT-A", source_type="nonexistent"), clock
    )
    assert none["results"] == [] and none["note"] == "no results for these filters"


def test_background_off_manifest_and_quarantined_excluded(services: Services, clock: Clock) -> None:
    q = "Brenmark electronics imports arrive through the Serpent Canal"
    ids = _ids(services.searcher.search(q, SearchFilters(), clock, k=30))
    assert "RM-DOC-0121" not in ids  # background excluded by default
    assert "RM-DOC-0199" not in ids and "RM-DOC-0190" not in ids and "RM-DOC-0193" not in ids
    with_bg = _ids(services.searcher.search(q, SearchFilters(include_background=True), clock, k=30))
    assert "RM-DOC-0121" in with_bg


def test_agents_cannot_pass_a_later_date(services: Services, clock: Clock) -> None:
    f = SearchFilters(date_from=date(2025, 4, 15), date_to=date(2026, 1, 1)).clamp(clock)
    assert f.date_to == clock.as_of and f.date_from == clock.as_of

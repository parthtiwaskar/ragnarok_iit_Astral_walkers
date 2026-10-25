"""HTTP surface tests (API.md). The API never takes part in a trial: it opens cases and reads projections."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from tests.conftest import AS_OF, QUESTION

ISO = AS_OF.isoformat()


@pytest.fixture()
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from app import main

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    with TestClient(main.app) as c:
        yield c


def test_health_and_limits_report_the_index(client: TestClient) -> None:
    health = client.get("/api/health").json()
    assert health["status"] == "ok" and health["docs"] > 0 and health["chunks"] > 0
    assert health["index_version"] and health["embedder"]
    limits = client.get("/api/config/limits").json()
    assert limits["MAX_RUNS"] >= 1 and limits["MAX_TOOL_CALLS_PER_TURN"] >= 1


def test_open_case_returns_202_and_lists_the_case(client: TestClient) -> None:
    opened = client.post("/api/cases", json={"question": QUESTION, "as_of": ISO})
    assert opened.status_code == 202, opened.text
    case_id = opened.json()["case_id"]
    assert opened.json()["events_url"].endswith(f"/cases/{case_id}/events")
    assert case_id in {c["case_id"] for c in client.get("/api/cases").json()}

    cf = client.get(f"/api/cases/{case_id}").json()
    assert cf["question"] == QUESTION and cf["as_of"] == ISO and cf["case_id"] == case_id

    assert client.get("/api/cases/UNKNOWN").status_code == 404
    assert client.post("/api/cases", json={"question": "hi", "as_of": ISO}).status_code == 422


def test_document_click_through_respects_the_as_of_firewall(client: TestClient) -> None:
    case_id = client.post("/api/cases", json={"question": QUESTION, "as_of": ISO}).json()["case_id"]

    doc = client.get(
        "/api/docs/RM-DOC-0101",
        params={"case_id": case_id, "highlight": "expected to last at least eight weeks"},
    )
    assert doc.status_code == 200
    body: dict[str, Any] = doc.json()
    assert body["doc_id"] == "RM-DOC-0101" and body["tier"] == 1
    assert body["highlights"], "the requested quote should be located in the body"
    assert body["as_of_status"]["retracted"] is False

    after = client.get("/api/docs/RM-DOC-0116", params={"case_id": case_id})
    assert after.status_code == 403 and after.json()["error"]["code"] == "NOT_AVAILABLE_AS_OF"
    # without a case there is no clock to enforce, but an unknown id is still a 404
    assert client.get("/api/docs/RM-DOC-9999").status_code == 404


def test_trade_click_through_hides_months_after_the_as_of(client: TestClient) -> None:
    case_id = client.post("/api/cases", json={"question": QUESTION, "as_of": ISO}).json()["case_id"]
    table = client.get("/api/trade/TT-A01", params={"case_id": case_id, "row": 9})
    assert table.status_code == 200
    payload = table.json()
    assert payload["highlight_row"] == 9
    hidden = [r for r in payload["rows"] if not r["visible"]]
    assert hidden, "April 2025 is not month-end visible on 2025-04-10"
    assert all(r["value"] is None and r["volume"] is None for r in hidden)
    assert all(r["value"] is not None for r in payload["rows"] if r["visible"] and r["quality"] == "OK")


def test_feed_and_search_are_filtered_by_as_of(client: TestClient) -> None:
    items = client.get("/api/feed", params={"as_of": ISO}).json()
    assert items and all(item["pub_date"] <= ISO for item in items)
    assert "body" not in items[0], "the feed carries summaries, not full documents"

    hits = client.get("/api/search", params={"q": "canal closure", "as_of": ISO}).json()
    assert hits["results"] and all(r["pub_date"] <= ISO for r in hits["results"])
    assert hits["query"] == "canal closure"
    # RM-DOC-0116 is published after the as-of, so no amount of querying surfaces it
    later = client.get("/api/search", params={"q": "reopening ahead of schedule", "as_of": ISO}).json()
    assert all(r["doc_id"] != "RM-DOC-0116" for r in later["results"])

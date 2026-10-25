"""Local FastAPI surface for the Trial dashboard.

The HTTP layer never participates in a trial: it opens a case, exposes stored
projections, and streams the EventBus that the deterministic runtime writes.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.obs.events import EventBus, system_bus
from app.obs.replay import export_zip, load_case
from app.runtime import Services, build_services, ingest, open_case, run_case
from app.search.filters import SearchFilters
from app.tools.asof import Clock, as_of_status, doc_visible, month_visible, table_visible
from app.tools.trade_tools import row_stats
from app.trial.casefile import CaseFile


class OpenCase(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    as_of: date


class RuntimeCase(BaseModel):
    case_id: str
    question: str
    as_of: date
    context: Any
    task: Any = None

    model_config = {"arbitrary_types_allowed": True}


def _error(status: int, code: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, "details": details})


def _case(app: FastAPI, case_id: str) -> RuntimeCase:
    current = app.state.cases.get(case_id)
    if current is not None:
        return current
    path = app.state.settings.runs_dir / case_id / "case_file.json"
    if not path.exists():
        raise _error(404, "CASE_NOT_FOUND", f"No case named {case_id}")
    cf = load_case(path.parent)
    context = open_case(app.state.services, cf.question, cf.as_of, cf.case_id)
    context.cf = cf
    current = RuntimeCase(case_id=cf.case_id, question=cf.question, as_of=cf.as_of, context=context)
    app.state.cases[cf.case_id] = current
    return current


def _public_case(current: RuntimeCase) -> dict[str, Any]:
    cf: CaseFile = current.context.cf
    # Odin's view is deliberately the least privileged shared Case File view.
    return cf.view_for("odin", cf.phase).model_dump(mode="json")


def _clock(current: RuntimeCase) -> Clock:
    return current.context.clock


def _event_stream(bus: EventBus, since: int) -> StreamingResponse:
    async def generate() -> Any:
        for event in bus.events(since):
            yield f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
        queue = bus.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
                except TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


def _range_rows(current: RuntimeCase) -> list[dict[str, Any]]:
    cf = current.context.cf
    latest = cf.runs[-1] if cf.runs else None
    if latest is None:
        return []
    positions = {(p.country, p.sector, p.side): p for run in cf.runs for p in run.positions}
    rows: list[dict[str, Any]] = []
    for final in latest.final_ranges:
        stats = row_stats(
            current.context.services.repo,
            final.trade_tables,
            cf.scope.event_date if cf.scope else None,
            current.context.clock,
            current.context.services.settings.BASELINE_MONTHS,
        )
        base = stats.get("baseline_value")
        horizon = current.context.services.settings.HORIZON_MONTHS
        thor, loki = (
            positions.get((final.country, final.sector, "thor")),
            positions.get((final.country, final.sector, "loki")),
        )
        rows.append(
            {
                "country": final.country,
                "sector": final.sector,
                "thor": thor.model_dump(mode="json") if thor else None,
                "loki": loki.model_dump(mode="json") if loki else None,
                "odin": final.model_dump(mode="json"),
                "observed_anchor": stats.get("observed_anchor"),
                "baseline_value": base,
                "value_at_risk": (
                    {
                        "low": round(final.low / 100 * base * horizon, 2),
                        "high": round(final.high / 100 * base * horizon, 2),
                    }
                    if base is not None
                    else None
                ),
                "data_quality": stats.get("rows_excluded", []),
            }
        )
    return rows


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    settings = get_settings()
    app.state.settings = settings
    app.state.services = build_services(settings)
    app.state.cases = {}
    app.state.ingest_report = None
    yield
    app.state.services.repo.db.close()


app = FastAPI(title="Trial of the Nine Realms", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[item.strip() for item in get_settings().CORS_ORIGINS.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    detail = (
        exc.detail
        if isinstance(exc.detail, dict)
        else {"code": "HTTP_ERROR", "message": str(exc.detail), "details": {}}
    )
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.get("/api/health")
def health(request: Request) -> dict[str, Any]:
    services: Services = request.app.state.services
    return {
        "status": "ok",
        "index_version": services.repo.index_version(),
        **services.repo.counts(),
        "embedder": services.embedder.name,
    }


@app.get("/api/config/limits")
def limits(request: Request) -> dict[str, Any]:
    return request.app.state.settings.limits.model_dump()


@app.post("/api/ingest/scan")
def scan(request: Request) -> dict[str, Any]:
    report = ingest(request.app.state.services)
    request.app.state.ingest_report = report.to_dict()
    return request.app.state.ingest_report


@app.get("/api/ingest/report")
def ingest_report(request: Request) -> dict[str, Any]:
    return request.app.state.ingest_report or {"status": "not_run"}


@app.get("/api/ingest/events")
def ingest_events(request: Request, since: int = 0) -> StreamingResponse:
    return _event_stream(system_bus(), since)


@app.post("/api/cases", status_code=202)
async def create_case(request: Request, body: OpenCase) -> dict[str, Any]:
    services: Services = request.app.state.services
    if services.repo.counts().get("docs_usable", 0) == 0:
        raise _error(409, "INDEX_NOT_READY", "Ingest the Runemark corpus before opening a case")
    context = open_case(services, body.question, body.as_of)
    current = RuntimeCase(case_id=context.case_id, question=body.question, as_of=body.as_of, context=context)
    request.app.state.cases[current.case_id] = current
    current.task = asyncio.create_task(run_case(services, body.question, body.as_of, ctx=context))
    return {
        "case_id": current.case_id,
        "status": "OPEN",
        "events_url": f"/api/cases/{current.case_id}/events",
    }


@app.get("/api/cases")
def cases(request: Request) -> list[dict[str, Any]]:
    out = []
    for case_id, current in request.app.state.cases.items():
        cf = current.context.cf
        out.append(
            {
                "case_id": case_id,
                "question": cf.question,
                "as_of": cf.as_of,
                "status": cf.status,
                "phase": cf.phase,
                "runs": len(cf.runs),
            }
        )
    return sorted(out, key=lambda item: item["case_id"], reverse=True)


@app.get("/api/cases/{case_id}")
def case(request: Request, case_id: str) -> dict[str, Any]:
    return _public_case(_case(request.app, case_id))


@app.get("/api/cases/{case_id}/events")
def case_events(request: Request, case_id: str, since: int = 0) -> StreamingResponse:
    current = _case(request.app, case_id)
    header = request.headers.get("last-event-id")
    return _event_stream(current.context.bus, max(since, int(header or 0)))


@app.get("/api/cases/{case_id}/runs/{run}")
def run(request: Request, case_id: str, run: int) -> dict[str, Any]:
    cf = _case(request.app, case_id).context.cf
    item = next((item for item in cf.runs if item.run == run), None)
    if item is None:
        raise _error(404, "RUN_NOT_FOUND", f"Run {run} has not been created")
    return item.model_dump(mode="json")


@app.get("/api/cases/{case_id}/exposure")
def exposure(request: Request, case_id: str) -> dict[str, Any]:
    current = _case(request.app, case_id)
    return {"unit": "pct_of_baseline_trade_value", "rows": _range_rows(current)}


@app.get("/api/cases/{case_id}/briefing")
def briefing(request: Request, case_id: str, format: str = "json") -> Any:
    current = _case(request.app, case_id)
    path = current.context.case_dir / ("briefing.md" if format == "md" else "briefing.json")
    if not path.exists():
        raise _error(404, "BRIEFING_NOT_READY", "The briefing is not ready")
    if format == "md":
        return {"markdown": path.read_text(encoding="utf-8")}
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/cases/{case_id}/logs")
def logs(request: Request, case_id: str) -> FileResponse:
    current = _case(request.app, case_id)
    return FileResponse(current.context.case_dir / "events.jsonl", media_type="application/x-ndjson")


@app.get("/api/cases/{case_id}/export")
def export(request: Request, case_id: str) -> FileResponse:
    current = _case(request.app, case_id)
    return FileResponse(export_zip(current.context.case_dir), media_type="application/zip")


@app.get("/api/docs/{doc_id}")
def document(
    request: Request, doc_id: str, case_id: str | None = None, highlight: str | None = None
) -> dict[str, Any]:
    repo = request.app.state.services.repo
    doc = repo.get_doc(doc_id)
    if doc is None:
        raise _error(404, "DOC_NOT_FOUND", f"No document named {doc_id}")
    clock = _clock(_case(request.app, case_id)) if case_id else Clock(date.max)
    if case_id and not doc_visible(doc, clock):
        raise _error(403, "NOT_AVAILABLE_AS_OF", "Document is not available as of this case date")
    highlights = []
    if highlight:
        start = doc.body.lower().find(highlight.lower())
        if start >= 0:
            highlights.append({"start": start, "end": start + len(highlight), "similarity": 100})
    return {**doc.__dict__, "as_of_status": as_of_status(repo, doc_id, clock), "highlights": highlights}


@app.get("/api/trade/{table_id}")
def trade(
    request: Request, table_id: str, case_id: str | None = None, row: int | None = None
) -> dict[str, Any]:
    repo = request.app.state.services.repo
    table = repo.get_trade_table(table_id)
    if table is None:
        raise _error(404, "TABLE_NOT_FOUND", f"No trade table named {table_id}")
    clock = _clock(_case(request.app, case_id)) if case_id else Clock(date.max)
    if case_id and not table_visible(table, repo, clock):
        raise _error(403, "NOT_AVAILABLE_AS_OF", "Trade table is not available as of this case date")
    rows = []
    for item in repo.trade_rows(table_id):
        visible = month_visible(item.month, clock)
        payload = item.__dict__.copy()
        payload["visible"] = visible
        if case_id and not visible:
            payload.update(value=None, volume=None)
        rows.append(payload)
    return {
        **table.__dict__,
        "period": [table.period_start, table.period_end],
        "rows": rows,
        "highlight_row": row,
    }


@app.get("/api/feed")
def feed(
    request: Request, as_of: date, event_id: str | None = None, limit: int = Query(200, le=500)
) -> list[dict[str, Any]]:
    clock = Clock(as_of)
    docs = [
        doc
        for doc in request.app.state.services.repo.docs()
        if doc_visible(doc, clock) and (not event_id or doc.event_id == event_id)
    ]
    return [
        {**doc.summary(), "snippet": re.sub(r"\s+", " ", doc.body)[:240]}
        for doc in sorted(docs, key=lambda item: item.pub_date or "", reverse=True)[:limit]
    ]


@app.get("/api/search")
def search(
    request: Request,
    q: str,
    as_of: date,
    event_id: str | None = None,
    country: str | None = None,
    sector: str | None = None,
    tier_max: int | None = None,
    k: int = Query(8, ge=1, le=20),
) -> dict[str, Any]:
    filters = SearchFilters(event_id=event_id, country=country, sector=sector, tier_max=tier_max)
    return request.app.state.services.searcher.search(q, filters, Clock(as_of), k=k)

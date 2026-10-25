# API Contract (FastAPI ↔ React)

Base URL: `http://localhost:8000/api`. JSON everywhere, dates as `YYYY-MM-DD`. Live updates use **Server-Sent Events**. TypeScript types in `frontend/src/api/types.ts` mirror [CASE_FILE_SCHEMA.md](CASE_FILE_SCHEMA.md). Generate them from the FastAPI OpenAPI schema (`openapi-typescript`) so they never drift.

## 1. Cases

### `POST /cases`
Open a case. Only the user can do this (no agent can open cases).

```json
// request
{ "question": "How exposed is Country A's electronics sector to the canal closure?", "as_of": "2025-03-20" }
// 202 response
{ "case_id": "CASE-20261025-a1f3", "status": "OPEN", "events_url": "/api/cases/CASE-20261025-a1f3/events" }
```
Errors: `422` if `as_of` is missing or invalid, `409` if the index isn't ready (ingestion running for the first time).

### `GET /cases`
List cases: `[{case_id, question, as_of, status, phase, runs, opened_at}]`.

### `GET /cases/{case_id}`
The current Case File (public view: sealed claims hidden until revealed).

### `GET /cases/{case_id}/events?since=<seq>` (SSE)
`Content-Type: text/event-stream`. Each message:
```
id: 57
event: claim_submitted
data: {"seq":57,"ts":"…","run":1,"actor":"thor","type":"claim_submitted","payload":{…}}
```
- Replays from `since` (or from 0), then streams live. `Last-Event-ID` is honoured on reconnect.
- Heartbeat comment every 15 s.
- UI event types: `phase_changed`, `sealed_commit` (side committed, no content), `claim_submitted` (after reveal), `position_submitted`, `attack_filed`, `response_filed`, `citation_check`, `ruling_issued`, `final_range`, `sufficiency`, `remand`, `research_report`, `memory_read`, `memory_write`, `memory_influence`, `tool_call`, `retrieval`, `duplicate_blocked`, `trap_detected`, `trade_row_excluded`, `limit_hit`, `agent_failed`, `briefing_ready`, `budget` (counters).

### `GET /cases/{case_id}/runs/{n}`
`{claims, positions, attacks, responses, rulings, final_ranges, sufficiency, research}` for run n.

### `GET /cases/{case_id}/exposure`
```json
{ "unit": "pct_of_baseline_trade_value",
  "rows": [{ "country": "A", "sector": "electronics",
             "loki": {"low": 2.0, "high": 16.8, "headline": 2.0, "claims": ["R2-L-C02"]},
             "thor": {"low": 26.4, "high": 58.5, "headline": 58.5, "claims": ["R2-T-C01"]},
             "odin": {"low": 6.4, "high": 24.8, "rulings": ["RUL:R2-T-C02:R2"], "contested_params": []},
             "observed_anchor": null, "baseline_value": 1000, "value_at_risk": {"low": 192, "high": 744},
             "data_quality": ["TT-017#r09 BAD_NUMBER excluded"] }] }
```

### `GET /cases/{case_id}/briefing?format=md|json`
Briefing text and the structured digest. `404` until `briefing_ready`.

### `GET /cases/{case_id}/logs`
Downloads `events.jsonl`. `GET /cases/{case_id}/export` → zip of the case folder.

## 2. Sources (click-through, FR7.4)

### `GET /docs/{doc_id}?case_id=&highlight=<quote>`
```json
{ "doc_id": "RM-DOC-0101", "title": "…", "outlet": "…", "tier": 1, "pub_date": "2025-03-02",
  "event_id": "EVT-07", "country": ["A"], "sector": ["electronics"], "source_type": "official_notice",
  "format": "pdf", "status": "OK", "flags": [],
  "as_of_status": {"retracted": false, "corrected_by": null},     // relative to the case's as_of
  "header": {"…": "…"}, "body": "…",
  "highlights": [{"start": 812, "end": 960, "similarity": 97}] }
```
With `case_id`, returns `403 NOT_AVAILABLE_AS_OF` if the doc is dated after the case's as-of date.

### `GET /trade/{table_id}?case_id=&row=<n>`
```json
{ "table_id": "TT-017", "exporter": "A", "importer": "B", "sector": "electronics", "unit": "…",
  "period": ["2024-07", "2025-06"], "event_id": "EVT-07", "columns": ["row","month","value","volume"],
  "rows": [{"row": 8, "month": "2025-02", "value": 812.0, "volume": 40.1, "quality": "OK", "visible": true, "raw": "…"}],
  "highlight_row": 8 }
```
Rows after the as-of date come back with `visible: false` and their values nulled (when `case_id` is given).

## 3. Feed (FR7.1)

### `GET /feed?as_of=&event_id=&limit=200`
Docs dated ≤ as-of, newest first: `[{doc_id, title, outlet, tier, pub_date, source_type, event_id, status, flags, snippet}]`.

### `GET /feed/stream?as_of=&from=&speed=` (SSE)
**Replays** Runemark chronologically from `from` to `as_of` with a simulated clock (`speed` = simulated days per second). Each doc becomes `event: feed_item`. Docs ingested live also show up as `feed_item` with `live: true`.

## 4. Search (debug panel, acceptance #2 demo)

### `GET /search?q=&event_id=&country=&sector=&tier_max=&source_type=&date_from=&date_to=&as_of=&k=`
Same result shape as the `search_documents` tool. Doesn't touch any case ledger.

## 5. Ingestion (live packs)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/ingest/scan` | Rescan `data/runemark` + `data/live_packs` (incremental) |
| `POST` | `/ingest/upload` | multipart zip of a pack → unzip to `live_packs/<name>` → ingest |
| `GET` | `/ingest/report` | latest ingestion report ([DATA_PIPELINE.md §12](DATA_PIPELINE.md#12-ingestion-report-runs_ingestreport_tsjson)) |
| `GET` | `/ingest/events` (SSE) | `ingest_update` / `ingest_issue` stream |

## 6. Memory inspector (read-only, for judges)

`GET /memory/{agent}?case_id=` → memories (optionally only those touched by the case) + `memory_diff`. No write endpoints. Agents can never call this.

## 7. System

- `GET /health` → `{status, index_version, docs, trade_tables, embeddings_ready, llm_provider}`
- `GET /config/limits` → current limits (for the budget meters)

## 8. Error format

```json
{ "error": { "code": "NOT_AVAILABLE_AS_OF", "message": "Document is dated after the case as-of date.", "details": {} } }
```

CORS is open to `http://localhost:5173` in dev. No auth is needed (local demo). Add a shared token header if deploying publicly.

# Observability and Reproducibility

NFR4: *log every claim, query, retrieved item, memory use and ruling.*
NFR6: *save the Case File, the as-of date and the rulings for every run, so a briefing can be traced back to Runemark documents.*
Submission: *saved logs of one full case with two runs.*

Most of the final round is scored **from these logs**, so treat them as a product, not a debugging aid.

Code: `backend/app/obs/`

## 1. Event log

Every case writes an append-only `runs/<case_id>/events.jsonl`. The same events feed the SSE stream to the dashboard (the frontend is a projection of this log).

### Envelope

```json
{
  "seq": 57,
  "ts": "2026-10-25T10:42:13.512Z",
  "case_id": "CASE-20261025-a1f3",
  "run": 1,
  "phase": "R1_SUBMIT",
  "actor": "thor",                       // heimdall | thor | loki | odin | thing | tool | ingest | system
  "type": "tool_call",
  "payload": { },
  "usage": {"input_tokens": 1830, "output_tokens": 212},   // when an LLM call is involved
  "latency_ms": 412,
  "parent_seq": 55                       // links tool results to the call, responses to attacks, etc.
}
```

### Event types

| Type | Payload (key fields) | Required by |
|---|---|---|
| `case_opened` | question, as_of, index_version, limits, models, prompt_hashes | NFR6 |
| `phase_changed` | from, to, reason | trace |
| `llm_call` | agent, model, provider, prompt_hash, n_messages, tool_names, stop_reason | NFR4 |
| `tool_call` | tool, args (canonical), fingerprint | NFR4 (query) |
| `retrieval` | call_id, items [{doc_id/row_ref, rank, scores, tier, date, status}] | NFR4 (retrieved item) |
| `duplicate_blocked` | fingerprint, original_call_id | NFR5 |
| `as_of_block` | requested ref, reason | clock rule |
| `claim_submitted` | full Claim (after reveal) + sealed commit time | NFR4 (claim) |
| `position_submitted` | Position (calc_id) | FR7.3 |
| `exposure_computed` | calc_id, inputs, outputs, anchor_applied | reproducibility |
| `attack_filed` / `response_filed` | full Attack / Response | debate quality |
| `citation_check` | evidence_ref, valid, issues, similarity | NFR2 |
| `ruling_issued` | full Ruling | NFR4 (ruling) |
| `final_range` | FinalRange | FR7.3 |
| `sufficiency` | verdict, checklist, deficiencies | FR4.3 |
| `remand` | deficiencies | FR5.4 |
| `research_report` | queries, per_deficiency, new_items | FR1.4 |
| `memory_read` / `memory_write` | agent, memory_ids, kind | NFR4 (memory use) |
| `memory_influence` | agent, memory_id, effect, action_ref | acceptance #9 |
| `trap_detected` | type (RUMOUR, RETRACTION, CORRECTION, INJECTION, WRONG_EVENT), refs | trap scoring |
| `ingest_update` / `ingest_issue` | file, status, reason | NFR3 |
| `trade_row_excluded` | ref, flag | PS §4.5 |
| `limit_hit` | limit, value, action_taken | NFR5 |
| `agent_failed` | agent, phase, error, retries | NFR3 |
| `briefing_ready` | path, status, status_reasons, validator_result | FR8 |

All writes go through one `EventBus.emit()`, which writes to the file first (fsync per phase boundary) and then fans out to SSE subscribers.

## 2. Saved artifacts per case

```
runs/CASE-20261025-a1f3/
├── config.json              # limits, models/providers per agent, temperatures, prompt hashes, index_version, git commit
├── manifest_snapshot.json   # doc ids + content hashes visible as of the date (proves which docs existed)
├── events.jsonl             # full event log
├── run_1/
│   ├── case_file.json       # Case File at end of run 1
│   ├── claims.json  positions.json  attacks.json  responses.json
│   ├── citation_checks.json
│   ├── rulings.json  final_ranges.json  sufficiency.json  remand.json
├── run_2/
│   ├── research_report.json # Heimdall's response to the remand (new evidence ids)
│   └── … same files …
├── memory_diff/
│   ├── heimdall.json  thor.json  loki.json  odin.json      # memories added/changed during this case
├── briefing.md
├── briefing.json            # structured version incl. digest used
└── summary.json             # status, runs, counts, limits used, trap counts, citation validity %
```

## 3. Trace chain (shown in the UI, provable from files)

```
briefing sentence ──cites──► [RM-DOC-0101, 2025-03-02]
        │                         │
        └─ from ruling RUL:R1-T-C02:R1 (rulings.json)
                 └─ on claim R1-T-C02 (claims.json) ─ evidence quote ─► doc span (manifest_snapshot + store)
                 └─ attacks/responses on it (attacks.json / responses.json)
                 └─ citation_check results (citation_checks.json)
        ▲
case_file.json (as_of, scope, pool, index_version) + config.json (prompts, models)
```

## 4. Replay and scoring CLI

```bash
python cli.py replay  CASE-20261025-a1f3          # re-render briefing from saved rulings; recompute ranges from saved inputs;
                                                  # re-verify every citation against the manifest; diff with saved briefing
python cli.py score   CASE-20261025-a1f3 [--truth truth.json]   # citation validity %, range coverage/width (if truth), dup queries,
                                                  # limits respected, trap counts, memory influence count
python cli.py export  CASE-20261025-a1f3 --zip    # bundle the folder for submission
```

`replay` is **deterministic** (no LLM calls). If it reproduces the briefing exactly, reproducibility is demonstrated.

## 5. Console and dev logging

- `structlog` JSON logs to stdout for the server. `LOG_LEVEL` in `.env`.
- Optional: `OTEL_EXPORTER_OTLP_ENDPOINT` to send spans (one span per turn, child spans per tool and LLM call). Not needed for judging.

## 6. Submission checklist (logs)

- [ ] One case folder with **two runs** (`run_1/`, `run_2/`), a remand in `run_1/remand.json`, and new evidence in `run_2/research_report.json`
- [ ] `events.jsonl` contains: ≥2 metadata-filtered searches, ≥1 attack per side, ≥1 REJECTED or CONCEDED ruling, ≥1 `memory_influence`, ≥1 handled damaged doc/row, ≥1 `duplicate_blocked` or zero duplicates proven by `score`
- [ ] `summary.json` shows citation validity = 100%
- [ ] `replay` reproduces `briefing.md`

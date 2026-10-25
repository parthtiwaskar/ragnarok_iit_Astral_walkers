# TASKS — phased build plan

Legend: **[M]** must-have (acceptance criteria) · **[S]** should-have (scoring edge) · **[C]** could-have (polish)
Suggested owners for a team of 4: **A** Data & Search · **B** Agents & Trial · **C** Frontend · **D** Integration, Eval & Docs. Adjust to your team.

**Critical path:** P1 → P2 → P3 → P4 → P5 → P8 (an end-to-end CLI case) → P9 (dashboard) → P10 (rehearsal). Get a **thin end-to-end slice** working early (CLI, FakeLLM), then deepen.

---

## P0 — Setup (D) · ~½ day
- [ ] [M] Repo skeleton per `docs/ARCHITECTURE.md §5`. `pyproject.toml`, `package.json`, Makefile, docker-compose
- [ ] [M] `.env.example` + `config.py` (pydantic-settings) with all limits and model settings
- [ ] [M] `LLMClient` interface + OpenAI + Anthropic implementations + `FakeLLM` (scripted)
- [ ] [M] `obs/events.py` EventBus (JSONL writer + in-memory fan-out) and `runs/<case_id>/` artifact helper
- [ ] [M] CI: ruff, mypy, pytest, eslint, tsc
- [ ] [S] Secret scanning pre-commit (gitleaks)

## P1 — Ingestion (A) · ~1½ days
- [ ] [M] File discovery + magic-byte type sniffing
- [ ] [M] Extractors: PDF (PyMuPDF → pdfplumber fallback), DOCX (python-docx → XML fallback), TXT (charset-normalizer + ftfy)
- [ ] [M] Metadata header parser with key alias map + validation (`DATA_PIPELINE.md §5`)
- [ ] [M] Manifest loader + reconcile (OFF_MANIFEST, MISSING, METADATA_CONFLICT)
- [ ] [M] Damage checks → statuses (EMPTY, NO_HEADER, BAD_ENCODING, TRUNCATED, DUPLICATE, UNSUPPORTED) + quarantine table
- [ ] [M] Trade-table parser with per-row quality flags + month visibility rule
- [ ] [M] Reference-sheet parser (countries, ports/chokepoints, sectors, firms, outlets+tiers) + tier cross-check
- [ ] [S] Relations: corrections/retractions/denials edges (as-of aware)
- [ ] [S] Injection scanner
- [ ] [M] `safe_ingest` wrapper (try/except + timeout) + ingestion report JSON
- [ ] [M] Live-pack watcher (watchdog, debounce, unzip, incremental) + `cli.py ingest`
- [ ] [M] Fixtures `mini_runemark/` with every damage type + tests

## P2 — Index and search (A) · ~1 day
- [ ] [M] SQLite schema: docs, chunks, chunks_meta, chunks_fts (FTS5), trade_rows, relations, reference tables
- [ ] [M] Chunker (≈350 tokens, 60 overlap, paragraph-aware)
- [ ] [M] Dense embeddings (fastembed bge-small) → `vectors.npy` with append support. Warm-up at startup
- [ ] [M] Hybrid search: BM25 + cosine + RRF + doc collapse
- [ ] [M] Filters: event_id, country, sector, tier_min/max, source_type, date range, include_background + always-on as-of/status filters
- [ ] [M] Tests: as-of filtering, filters combine, empty results return a note
- [ ] [S] `index_version` hash

## P3 — Tool layer (A + B) · ~1 day
- [ ] [M] Tool registry with pydantic arg models → JSON schemas for both providers
- [ ] [M] Guard chain: allowlist → budget → clock → dedupe ledger → execute (timeout/retry) → post-process (untrusted wrap) → log → error wrap
- [ ] [M] Tools: search_documents, read_document, find_related_docs, list_trade_tables, read_trade_table, trade_stats, get_reference
- [ ] [M] Heimdall tools: list_events, event_profile (rule verdict)
- [ ] [M] `verify_citation` (manifest, as-of, retracted/corrected as of, wrong event, quote fuzzy ≥ 90, damaged row)
- [ ] [M] `compute_exposure` (formula, corners, anchor, MIN_PARAM_WIDTH, calc_id logging) + property tests (monotonicity)
- [ ] [M] flag_injection / flag_data_issue
- [ ] [M] Tests for dedupe canonicalization and allowlist denial

## P4 — Agents (B) · ~1½ days
- [ ] [M] `agents/base.py` turn loop (view → recall → LLM tool loop → validate → re-prompt once → memory write → influence log)
- [ ] [M] Prompts from `docs/PROMPTS.md` into `agents/prompts/*.md` + renderer + hash logging
- [ ] [M] Heimdall: gather (parse question, find event, verify, scope, pool) + research (deficiencies → new queries, memory skip)
- [ ] [M] Thor and Loki: submit (claims + positions via compute_exposure), attack, respond
- [ ] [M] Odin: judge (rule all claims, final ranges, sufficiency) + brief
- [ ] [M] Submission validators: claim (evidence ≥1, schema), attack (target exists, evidence when required), response (DEFEND needs new evidence), ruling coverage
- [ ] [S] Per-agent model/provider overrides

## P5 — The Thing / trial protocol (B) · ~1 day
- [ ] [M] Case File model + `view_for()` (sealed filtering) + versioned saves per phase
- [ ] [M] FSM with all states and transitions (`TRIAL_PROTOCOL.md §1`)
- [ ] [M] Sealed parallel Round 1 (commit → reveal) + "each searched ≥1" check
- [ ] [M] Round 2 attack (min 1 each) and respond (every attack answered)
- [ ] [M] Deterministic sufficiency checklist S1–S9 → Odin
- [ ] [M] Remand → research → new-evidence check → next run. Carry-forward/amend/supersede semantics. Effective rulings
- [ ] [M] Limits + Budget. `limit_hit` → clean transition. Case timeout
- [ ] [M] Failure paths: side FAILED → unchallenged, both failed, Odin failed, Heimdall failed → ABORTED
- [ ] [M] Integration tests with FakeLLM: 2-run case, remand, side failure, budget exhaustion, rumour rejection

## P6 — Memory (B) · ~½ day
- [ ] [M] Per-agent SQLite store, recall (FTS + cosine + boosts), knowledge_date filter
- [ ] [M] Query log written by the tool layer. Heimdall deterministic skip/reformulate + `memory_influence` event
- [ ] [M] End-of-case reflection (reflect.md) with fact/procedural classification + regex reclassification
- [ ] [M] `memory_influence` validation (cited IDs must have been recalled)
- [ ] [S] memory_diff artifact per case, `cli.py memory-reset`
- [ ] [M] Isolation test (agent A cannot access B's store)

## P7 — Briefing (B + D) · ~½ day
- [ ] [M] Rulings digest builder
- [ ] [M] Odin briefing turn (sections from digest only)
- [ ] [M] Deterministic renderers: exposure table, sources, data quality, rejected appendix, trace
- [ ] [M] Briefing validator (citations ⊆ digest, dates match, ruling coverage, assumptions linked) + sanitize fallback
- [ ] [M] COMPLETE/INCOMPLETE status logic
- [ ] [S] Export md → PDF

## P8 — API (D) · ~1 day
- [ ] [M] Cases: POST/GET/list, runs, exposure, briefing, logs, export
- [ ] [M] SSE `/cases/{id}/events?since=` with Last-Event-ID + heartbeat
- [ ] [M] Docs/trade click-through endpoints with highlight + as-of checks
- [ ] [M] Feed list + SSE replay stream (+ live items)
- [ ] [M] Ingestion scan/upload/report/events
- [ ] [S] Memory inspector (read-only), health, limits, search debug
- [ ] [M] `cli.py ask | replay | score | export` (deterministic replay)

## P9 — Dashboard (C) · ~2 days (start on mocks from day 1 using recorded events.jsonl)
- [ ] [M] App shell, CaseBar (question + as-of + open), phase pipeline, status banner
- [ ] [M] SSE hook + reducer store (+ offline "replay mode" from a saved events.jsonl)
- [ ] [M] FeedPanel with replay controls, tier badges, retracted/corrected/injection flags
- [ ] [M] TrialView: Thor | Odin | Loki columns, ClaimCard, CrossExamThread, RulingBadge, run tabs
- [ ] [M] ExposureTable + RangeBar (Loki/Thor/Odin bands, anchor tick)
- [ ] [M] SourceDrawer: DocViewer (highlight) + TradeTableViewer (row highlight, quality colours)
- [ ] [M] BriefingView with CitationChip click-through
- [ ] [S] LogsView (virtualised, filters), IngestionView (upload/rescan), BudgetMeters
- [ ] [S] SealedEnvelope animation, TrapRadar, MemoryView with diff
- [ ] [C] Range-convergence animation R1→R2, dark/light toggle
- [ ] [S] Playwright e2e: open case → click claim → doc highlighted

## P10 — Hardening, eval and demo (D + all) · ~1½ days
- [ ] [M] Chaos tests (`TRAPS_AND_RELIABILITY.md §5`) green
- [ ] [M] Synthetic eval with truth.json. Tune widths and thresholds. Record results
- [ ] [M] Hold-out rehearsal ×2 packs, < 6 min per case
- [ ] [M] Produce and commit **one full two-run case** in `runs/` (submission)
- [ ] [M] Export the architecture diagram PNG to `docs/img/architecture.png`
- [ ] [M] README polish, team table, demo video backup
- [ ] [M] Rehearse the demo script + architecture talk track + Q&A (`DEMO_SCRIPT.md`)
- [ ] [S] Ask the OC the open questions (`BRAINSTORM.md §8`) and adjust `EXPOSURE_METRIC` / visibility rule

---

## Cut list (if time runs short, drop in this order)
1. [C] animations, theme toggle
2. [S] memory diff viewer (keep the influence log!)
3. [S] PDF export
4. [S] OpenTelemetry
5. [S] per-agent model overrides
6. Never cut: anything marked [M], the citation validator, the as-of firewall, limits, failure paths, the saved two-run log.

## Daily integration ritual
- End of each day: run one full `cli.py ask` on fixtures (FakeLLM) **and** one on real Runemark (real LLM), commit the `runs/` output to a scratch branch, and note regressions here.

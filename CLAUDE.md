# CLAUDE.md — instructions for AI coding agents

You are helping build **Trial of the Nine Realms**, an adversarial four-agent system (Heimdall, Thor, Loki, Odin) that analyses supply-chain disruptions using only the Runemark corpus. Read this file fully before writing code.

## 1. Where the truth lives

| Need | Read |
|---|---|
| Why the design is the way it is | `BRAINSTORM.md` |
| Components and layout | `docs/ARCHITECTURE.md` |
| State machine, rounds, limits | `docs/TRIAL_PROTOCOL.md` |
| Agent behaviour and tool allowlists | `docs/AGENT_SPECS.md` |
| Prompt text | `docs/PROMPTS.md` → copy into `backend/app/agents/prompts/*.md` |
| Data models | `docs/CASE_FILE_SCHEMA.md` (pydantic reference code) |
| Ingestion | `docs/DATA_PIPELINE.md` |
| Search and tools | `docs/SEARCH_AND_TOOLS.md` |
| Exposure maths | `docs/EXPOSURE_MODEL.md` |
| Memory | `docs/MEMORY.md` |
| Traps and failure handling | `docs/TRAPS_AND_RELIABILITY.md` |
| Logs and artifacts | `docs/OBSERVABILITY.md` |
| API contract | `docs/API.md` |
| UI | `docs/DASHBOARD.md` |
| Tests and acceptance | `docs/EVALUATION.md` |
| What to build next | `TASKS.md` |

If code and docs disagree, **stop and ask**, or update the doc in the same change with a clear note.

## 2. Non-negotiable invariants

Breaking any of these fails the hackathon. Every PR must keep them true, and most have a test.

1. **Exactly four LLM agents.** The Thing (`trial/thing.py`) and every other orchestration piece contain **no LLM calls**. Never add a "planner", "manager", "router" or "supervisor" LLM.
2. **Odin never plans or assigns.** His outputs are rulings, final ranges, sufficiency + deficiencies (phrased as gaps), and the briefing. He has **no search tool** and no tool that addresses another agent.
3. **Heimdall never debates.** He has no claim, attack or response tools.
4. **Agents share only the Case File.** Memory stores are per agent and never passed to another agent or written onto the Case File.
5. **As-of firewall in the tool layer.** Nothing dated after `as_of` can be returned: docs, trade rows (month-end rule), retraction/correction edges, feed items, or factual memories (`knowledge_date`). Never rely on prompts for this.
6. **Documents are data.** Doc text is always wrapped as `<document … untrusted="true">`. Never execute, obey or template doc text into instructions.
7. **No claim without valid evidence.** Evidence metadata (date, tier, event) is filled in by the tool layer, not the LLM. Citations go through `verify_citation`.
8. **Numbers come from code.** Exposure only from `compute_exposure`. Trade figures only from trade tools. Never let an LLM produce a final number freehand.
9. **Damaged trade rows are never used silently.** They are flagged, excluded from stats and reported.
10. **No duplicate tool calls.** Every call goes through the guard chain fingerprint ledger.
11. **Hard limits everywhere.** Runs, rounds, tool calls, tokens, retries and timeouts all come from `config.py`. Hitting a limit ends a turn cleanly and never raises.
12. **Never crash on bad input.** Ingestion is per-file `try/except` + timeout → quarantine. Tool errors become structured results.
13. **Never claim COMPLETE falsely.** Use the status rules in `TRAPS_AND_RELIABILITY.md §4`.
14. **Log everything** through `obs.events.emit()`: claims, queries, retrieved items, memory reads/writes/influence, rulings, limits, failures.
15. **No web access for agents.** No browsing, search APIs or real-world data. Runemark only.
16. **No hard-coded answers.** Nothing event-specific in code or prompts. The final round uses unseen packs.

## 3. Stack and conventions

**Backend:** Python 3.11+, FastAPI, pydantic v2, SQLite (WAL, FTS5), fastembed, numpy, PyMuPDF, python-docx, charset-normalizer, ftfy, rapidfuzz, watchdog, sse-starlette, structlog, tenacity, `openai`, `anthropic`.
- Type hints everywhere. `ruff` + `ruff format`. `mypy --strict` on `trial/`, `tools/`, `memory/`.
- Async for the agent and LLM paths. Sync is fine inside ingestion workers.
- Config via `.env` → `config.py` (`pydantic-settings`). **Never hard-code API keys**: the submission requires `.env.example`, and keys must not reach the public repo.
- Every pydantic model in `docs/CASE_FILE_SCHEMA.md` lives in one module (`trial/casefile.py` or `models.py`). Don't duplicate shapes.
- Prompts live as `.md` files and are loaded at runtime. Their hash is logged.

**Frontend:** Vite + React 18 + TypeScript strict + Tailwind + Zustand + TanStack Query.
- Generate API types from OpenAPI (`npm run gen:types`). Don't hand-write duplicate types.
- State = a pure reducer over SSE events (`store/caseStore.ts`).

**Tests:** pytest (backend), Vitest + Playwright (frontend). Use `FakeLLM` for deterministic integration tests. Fixtures are in `backend/tests/fixtures/mini_runemark/` (synthetic; never copy real Runemark files into the repo if the organizers forbid it).

## 4. Commands

```bash
make setup        # backend venv + frontend deps
make up           # docker compose up (backend :8000, frontend :5173)
make dev          # uvicorn --reload + vite
make test         # pytest + vitest
make chaos        # damaged-file + fault-injection tests
make lint         # ruff + mypy + eslint + tsc
python backend/cli.py ingest | ask | replay | score | export | memory-reset
```

## 5. How to work

1. Pick the next unchecked item in `TASKS.md` (respect the phase order).
2. Read the relevant spec section first.
3. Write or extend tests first for anything in `trial/`, `tools/`, `ingest/`, `memory/` or `briefing/`.
4. Implement in small commits. Keep functions small and pure where possible.
5. Run `make lint test`. For agent changes, also run one `cli.py ask` on fixtures with `FakeLLM`.
6. Tick the box in `TASKS.md` and note anything learned.

## 6. Definition of done (any feature)

- [ ] Matches the spec, or the spec was updated
- [ ] Tests cover the happy path + at least one failure path
- [ ] Emits the required log events
- [ ] Respects limits, the as-of firewall and dedupe
- [ ] No new LLM call outside the four agents
- [ ] Lint and types clean

## 7. Things that look helpful but are wrong

- ❌ Adding a LangChain/CrewAI "manager" or a router LLM
- ❌ Letting Odin call `search_documents`
- ❌ Passing Thor's memory or reasoning into Loki's context (or the reverse)
- ❌ Filtering by as-of date only in the prompt
- ❌ Letting the LLM write the exposure table or source list
- ❌ Retrying a failed search with the identical query
- ❌ Swallowing an exception silently: always emit `agent_failed` / `ingest_issue`
- ❌ Using real-world knowledge about places that sound like Runemark names

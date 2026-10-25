# ⚖️ Trial of the Nine Realms

**An adversarial multi-agent system that puts every supply-chain disruption on trial.**

Ask a question about a disruption in **Runemark**. Four Norse agents build the case, argue opposite sides and rule on every claim. The result is a briefing that separates **what is certain, what is disputed, and how wide the risk range is**, with every number and sentence traceable to a Runemark document or trade-table row.

| Agent | Role | Does | Never |
|---|---|---|---|
| 🌈 **Heimdall** | Gatekeeper | Turns the question into a Case File, gathers evidence, checks the event is real, re-searches on remand | Argues or judges |
| ⚡ **Thor** | The Alarm | Builds the strongest honest case that exposure is **high**, with cited claims and a range | Cites without a source |
| 🐍 **Loki** | The Trickster | Builds the strongest honest case that exposure is **limited**, with cited claims and a range | Says "maybe it'll be fine" |
| 👁 **Odin** | The Allfather (judge) | Rules on every claim, classifies findings, decides sufficiency, sends the case back, writes the briefing | Plans, assigns or searches |

---

## Architecture

```mermaid
flowchart LR
  U((User)) -->|question + as-of| THING[The Thing<br/>deterministic trial protocol<br/>no LLM · not an agent]
  THING <--> CF[(Case File<br/>shared blackboard)]
  H[Heimdall] <--> CF
  T[Thor] <--> CF
  L[Loki] <--> CF
  O[Odin] <--> CF
  H & T & L & O --> TOOLS[Tool layer<br/>as-of firewall · dedupe · budgets · logging]
  TOOLS --> IDX[(Runemark index<br/>SQLite FTS5 + embeddings<br/>trade rows + quality flags)]
  H -.- MH[(private memory)]
  T -.- MT[(private memory)]
  L -.- ML[(private memory)]
  O -.- MO[(private memory)]
  THING --> LOG[(events.jsonl + run artifacts)] --> UI[React dashboard via SSE]
```

**Flow:** USER → HEIMDALL → THOR ∥ LOKI (sealed) → cross-examination → ODIN → briefing **or** remand → Heimdall re-search → next run.

- **No boss.** The Thing is court procedure (turn order, sealed submissions, limits), not a planner. Agents decide everything about content. Odin's remand lists *deficiencies*, never instructions.
- **LLMs argue, code computes.** Exposure is a deterministic function of cited parameters (route share × severity × duration × (1 − reroute offset)). Odin's rulings decide which bounds survive.
- **Grounding is enforced.** Every citation is checked against the manifest, the as-of date, retractions (as of that date), the event ID and an exact-quote match.

Full details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## Features

- **Ingestion** of PDF, TXT and DOCX documents, trade tables and reference sheets. Damaged files are quarantined with reasons, and bad trade rows are flagged and excluded, never used silently. **Live packs** dropped into `data/live_packs/` are indexed automatically.
- **Hybrid search**: BM25 (SQLite FTS5) + dense (bge-small) fused with RRF, with filters on event, country, sector, tier, source type, date and background.
- **Real debate**: sealed simultaneous claims, mandatory cross-examination, defences that must bring *new* evidence, honest concessions.
- **Judge with teeth**: rulings on every claim (sourced fact vs inference; Established / Contested / Conceded), a sufficiency check, a remand, and a second run with new evidence.
- **Private lasting memory** per agent, with logged `memory_influence` events. Memory obeys the simulated clock (`knowledge_date`).
- **Trap handling**: rumours, retractions, corrections, contradictions, prompt injections, look-alike docs, future information.
- **Dashboard**: question box, live feed replay, trial view, exposure range table, click-through to the exact quote or trade row, logs, ingestion, memory.
- **Reproducible**: event-sourced logs, per-run artifacts, and a deterministic `replay`.

---

## Quickstart

```bash
git clone https://github.com/<team>/nine-realms && cd nine-realms
cp .env.example .env              # add OPENAI_API_KEY and/or ANTHROPIC_API_KEY, choose LLM_PROVIDER
# put the Runemark folder at data/runemark/

# backend
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python cli.py ingest              # builds the index, prints the ingestion report
uvicorn app.main:app --reload     # http://localhost:8000

# frontend (new terminal)
cd frontend && npm install && npm run dev   # http://localhost:5173
```

Or `make up` (docker compose: backend + frontend).

### CLI

```bash
python cli.py ingest [--path data/live_packs/pack1]
python cli.py ask "How exposed is <country>'s <sector> to <event>?" --as-of 2025-03-20
python cli.py replay <case_id>         # rebuild briefing from saved rulings, no LLM
python cli.py score  <case_id> [--truth truth.json]
python cli.py export <case_id> --zip
python cli.py memory-reset --agent all
```

## Dataset

The E5 challenge corpus is included at [`data/runemark/`](data/runemark/). It contains the original 76 PDF, TXT and DOCX documents, `manifest.json`, `hashes.sha256`, and the pack's README and sample question. Verify the source files from that directory with `sha256sum -c hashes.sha256`.

The pack's `runemark_map.png` is a human-reference image only; do not pass it to agents or the retrieval pipeline.

---

## Example output (briefing excerpt)

```
# Briefing — How exposed is Country A's electronics sector to the canal closure?
Case CASE-20261025-a1f3 · As of 2025-03-20 · Runs: 2 · Status: COMPLETE

## Established findings
- The canal authority closed the northern channel to all traffic from 2025-03-02 [RM-DOC-0101, 2025-03-02].
## Contested findings
- Reopening date: Thor cites an analyst estimate of 3 months [RM-DOC-0122, 2025-03-08]; Loki cites a
  Tier 1 notice of partial reopening by mid-April [RM-DOC-0140, 2025-03-18]. Unresolved.
## Conceded findings
- Thor conceded that the 40% port backlog figure was retracted [RM-DOC-0131, 2025-03-12].
## Exposure table
| Country | Sector | Loki range | Thor range | Odin range | Unit |
| A | Electronics | 2.0–16.8 | 26.4–58.5 | 6.4–24.8 | % of baseline trade value, next quarter |
```

*(Placeholder names and IDs, for illustration only.)*

---

## Repository map

| Path | What |
|---|---|
| `backend/app/ingest` | parsers, header and trade-table parsing, damage checks, watcher |
| `backend/app/search` | FTS5 + dense + RRF hybrid search |
| `backend/app/tools` | tool catalogue + guard chain + citation validator + exposure calculator |
| `backend/app/agents` | Heimdall, Thor, Loki, Odin + prompts |
| `backend/app/trial` | the Thing (FSM), Case File, limits, sufficiency |
| `backend/app/memory` | per-agent private memory |
| `backend/app/obs` | event log, artifacts, replay |
| `frontend/src` | React dashboard |
| `runs/` | saved cases (logs + artifacts) |
| `docs/` | all specs |

## Documentation

[Brainstorm](BRAINSTORM.md) · [Architecture](docs/ARCHITECTURE.md) · [Trial protocol](docs/TRIAL_PROTOCOL.md) · [Agents](docs/AGENT_SPECS.md) · [Prompts](docs/PROMPTS.md) · [Schemas](docs/CASE_FILE_SCHEMA.md) · [Data pipeline](docs/DATA_PIPELINE.md) · [Search & tools](docs/SEARCH_AND_TOOLS.md) · [Exposure model](docs/EXPOSURE_MODEL.md) · [Memory](docs/MEMORY.md) · [Traps & reliability](docs/TRAPS_AND_RELIABILITY.md) · [Observability](docs/OBSERVABILITY.md) · [API](docs/API.md) · [Dashboard](docs/DASHBOARD.md) · [Evaluation](docs/EVALUATION.md) · [Demo & runbook](docs/DEMO_SCRIPT.md) · [Tasks](TASKS.md)

## Submission artifacts

- Saved logs of a full two-run case: `runs/<case_id>/` (see [OBSERVABILITY.md §6](docs/OBSERVABILITY.md#6-submission-checklist-logs))
- Architecture diagram: above, plus `docs/img/architecture.png`
- `.env.example`: all configuration and hard limits

## Tech stack

Python 3.11 · FastAPI · pydantic v2 · SQLite (FTS5, WAL) · fastembed (bge-small) · NumPy · PyMuPDF · python-docx · charset-normalizer · ftfy · rapidfuzz · watchdog · OpenAI / Anthropic SDKs · React 18 · TypeScript · Vite · Tailwind · Zustand · TanStack Query

## Team

| Name | Role |
|---|---|
| Parth Denge | … |
| … | … |

*Runemark is a fictional world provided by the organizers. The system uses no real-world data and no web access.*

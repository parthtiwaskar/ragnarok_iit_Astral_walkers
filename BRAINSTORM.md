# Brainstorm — Trial of the Nine Realms (PS 1)

> Supply Chain Disruption Impact Analyzer. Four Norse agents put a disruption on trial, using only the **Runemark** corpus.

This file is the thinking that comes before the build: what the problem statement really asks for, what the scoring rewards, the traps, the key design decisions and the risks. The other docs turn these decisions into specs.

---

## 1. The problem in one paragraph

A user asks a question about a supply-chain disruption, as of a date. **Heimdall** turns it into a Case File, collects evidence from Runemark (PDF/TXT/DOCX news plus trade tables) and checks that the event is real. **Thor** argues that exposure is high and **Loki** argues that it is limited. Both cite Runemark document IDs and give a numeric range for each country and sector. They cross-examine each other. **Odin** rules on every claim, sorts findings into Established, Contested and Conceded, decides whether the case is good enough, and either sends it back for a second run with new evidence or writes the briefing. It all appears on one dashboard with click-through to sources. In the final round, **5 unseen event packs** arrive live, some with damaged files. Hard-coded demos fail.

## 2. What the scoring really rewards (Section 5, decoded)

| Scored item | What it means for the design |
|---|---|
| **Citation validity** (share of citations that resolve to the manifest) | Never let an LLM type a doc ID freehand. Every citation goes through a validator that checks it against the manifest. Fabricated IDs get stripped. The briefing **must cite the final-round pack docs**, so newly ingested docs have to be searchable straight away. |
| **Range accuracy** (truth inside Thor's range, Loki's range **and** Odin's range) | Thor and Loki each give a **range**, not just one number. Thor argues the upper part, but his range still has to contain the truth. Exaggeration loses points. The same goes for Loki on the low side. |
| **Calibration** (how narrow Odin's range is when it's correct) | Odin should not just take the hull [Loki low, Thor high]. He narrows using the rulings: bounds that rest on rejected or conceded claims are dropped. Numbers come from a **deterministic exposure calculator** fed with cited parameters. |
| **Trap handling** | Fake events rejected, retractions caught (as of the date!), contradictions marked Contested, injected instructions ignored, look-alike docs from other events excluded. Each trap gets its own detector and its own log event. |
| **Debate quality** | Real attacks with evidence, defences that bring **new** evidence (code checks this), and honest concessions. Prompts reward conceding when the evidence demands it. |
| **Reliability** | Every file format, every damaged file and every failed tool call is handled without a crash. Hard limits apply. **Zero repeated queries**, enforced by a fingerprint ledger. |
| **Reproducibility** | Every run saves its Case File, as-of date, rulings, config and prompt hashes. A `replay` command rebuilds the briefing from saved rulings and re-checks the citations. |

Most of the final round is **scored from saved logs and briefings**, so the logs are a deliverable, not a debugging aid.

## 3. Hidden requirements and gotchas

These are the details that are easy to miss and that judges will probe.

1. **"Exactly four agents" + "no central planner".** The thing that runs the loop must **not** be an LLM. It's a deterministic state machine. We call it **the Thing**, after the Norse assembly where disputes were judged. It enforces turn order, sealed submissions and limits, like court rules of procedure. It never reads content and never decides what an agent does. If it were an LLM, we'd have a fifth agent and a boss.
2. **Odin is a judge, not a boss.** A remand lists **deficiencies** ("no post-event trade data for Country X / Sector Y"), never tasks or queries. **Heimdall decides how to re-search.** This wording matters when explaining the system to judges.
3. **Heimdall does not debate.** He has no claim, attack or defence tools. That gets enforced through each agent's tool allowlist, not just its prompt.
4. **Simulated-clock leakage has four routes**, and all four must be blocked:
   - search results dated after the as-of date
   - **trade rows for months after the as-of date** (tables cover before **and after** each event, so the answer is sitting in the data). Only months that **ended** on or before the as-of date count.
   - **retractions published after the as-of date**. A doc retracted later is still "valid" as of the earlier date, and the system must not "know" about the retraction.
   - **agent memory from an earlier case with a later as-of date**. Memory items carry a `knowledge_date`, and factual memories are filtered by it (see [MEMORY.md](docs/MEMORY.md)).
5. **Sealed Round 1.** "Thor and Loki submit claims at the same time" → run them in parallel with **commit-then-reveal**. Neither can see the other's claims until both have committed.
6. **Round 2 is mandatory and checked:** each side must attack **at least one** of the other's claims. The Thing validates this and re-prompts once if needed.
7. **A second run must bring new evidence.** The Thing checks that the pool after the remand holds doc/row IDs that weren't there in Run 1. Heimdall's first pass is bounded on purpose so that a remand-driven second pass can widen it.
8. **Damaged trade rows must be reported, never used silently.** Every row gets a quality flag (`OK / MISSING_VALUE / DUPLICATE_ROW / BAD_NUMBER / TRUNCATED`). The calculator skips flagged rows and the briefing lists them.
9. **File format says nothing about reliability.** Tier comes from the header (cross-checked against the outlets reference sheet), never from the extension.
10. **Names are fixed and fictional.** Prompts must forbid real-world knowledge. Example: a Runemark "strait" that sounds like a real one isn't that one.
11. **Look-alike docs.** Documents that look related but belong to a different event or to `background`. Filter by `event_id`, and have Odin reject a citation whose event doesn't match (background docs are allowed only as labelled context).
12. **Prompt injection is a scored trap.** Wrap doc text in `<document untrusted>` tags, run a regex/heuristic detector that logs `INJECTION_DETECTED`, and validate structured output. The doc stays usable as *data*, and the briefing notes that an injection was found and ignored.
13. **"If one side fails, say the other side went unchallenged."** Claims carry an `unchallenged` flag. Unchallenged claims can't become *Established* by default.
14. **"Never claim a case is complete when it is not."** The briefing has a `status: COMPLETE | INCOMPLETE` with reasons (limits hit, remand budget used up, side failed).
15. **Live ingestion without hand-editing code or index.** A watched `live_packs/` folder plus an upload endpoint, with incremental indexing and an ingestion report in the UI.
16. **Query dedupe is system-wide.** Each tool call is fingerprinted (`tool + canonical args`). A duplicate is never executed. It returns a pointer to the earlier result and is logged as `DUPLICATE_BLOCKED`.
17. **Off-Runemark sources** must be labelled. Simplest rule: the agents have **no web tool at all**, so nothing off-Runemark can get in. The label exists anyway, as a safety net.
18. **"Maybe it will be fine" is not a claim.** The claim schema needs evidence, an estimate and a fact/inference label. A validator rejects claims with no evidence before Odin sees them.
19. **Memory must visibly change behaviour** (acceptance #9). Build at least one *deterministic* proof (Heimdall skips a query that memory says returned nothing) plus LLM-level `memory_influence` declarations.

## 4. Core design ideas

### 4.1 Blackboard + the Thing (no planner)
All agents read and write one **Case File**, the blackboard. Each agent activates when the Case File reaches its trigger state. The Thing is about 200 lines of deterministic code: a state machine that gates turns and enforces limits. Every content decision belongs to an agent: queries, claims, attacks, concessions, rulings, sufficiency and re-search strategy.

### 4.2 Exposure as a function of cited parameters
LLMs are bad at producing numbers and good at arguing about them. So exposure is computed by a **deterministic calculator**:

```
exposure% = route_share × severity × (duration_in_horizon / horizon) × (1 − reroute_offset)
```

Each parameter is a **range**, and each bound must be backed by a claim with citations. The debate becomes a debate over parameters. Odin's ruling decides which bounds survive, and the calculator turns the surviving bounds into his final range. The result is explainable, reproducible and calibrated. Observed post-event trade rows (dated on or before the as-of date) act as anchors. Details: [EXPOSURE_MODEL.md](docs/EXPOSURE_MODEL.md).

### 4.3 Deterministic validators around every LLM step
- **Citation validator:** checks that the ID is in the manifest, the date is on or before the as-of date, the doc isn't quarantined, isn't retracted *as of the date*, matches the case event (or is background), and that the **quote actually appears in the doc** (fuzzy ≥ 90).
- **Claim validator:** schema, evidence present, estimate unit, fact/inference label.
- **Defence validator:** a "defend" response must cite evidence the claim hasn't cited before.
- **Briefing validator:** every citation in the briefing must appear in Odin's rulings, otherwise the sentence is dropped and the briefing regenerated.

### 4.4 The as-of firewall
One function, `visible(item, as_of)`, applied in the **tool layer**: search, read, trade rows, retraction graph, memory recall and the feed replay all go through it. Agents never get the chance to break the clock rule.

### 4.5 Event-sourced logs
Every action is an append-only event (`events.jsonl`). The dashboard is a projection of the event stream over SSE. Replay and reproducibility come for free.

## 5. Alternatives considered

| Decision | Options | Choice and why |
|---|---|---|
| Agent framework | LangGraph, CrewAI, AutoGen, custom | **Custom asyncio loop.** CrewAI and AutoGen push a manager/planner pattern that breaks "no boss". LangGraph would work, but a custom loop gives full control over limits, dedupe, sealed bids and logging, and is easier to explain on the architecture diagram. |
| LLM | OpenAI or Claude | **Both, behind one `LLMClient` interface** (`LLM_PROVIDER` plus per-agent overrides). Thor and Loki use the **same** model for fairness. Odin can use the strongest model. |
| Keyword search | rank_bm25, Elasticsearch, SQLite FTS5 | **SQLite FTS5** (built-in BM25, metadata columns, zero infrastructure). |
| Dense search | Chroma, LanceDB, FAISS, numpy | **fastembed (bge-small, local ONNX) + numpy brute-force cosine.** The corpus is only hundreds of docs, so a vector DB would be overkill. Falls back to OpenAI embeddings. |
| Fusion | Weighted sum, RRF | **Reciprocal Rank Fusion (k=60).** No score calibration needed. |
| Numbers | LLM writes numbers, or calculator | **Calculator.** No hallucinated figures, and the result is reproducible. |
| Live updates | WebSocket, SSE | **SSE.** One-way, simpler, works with `EventSource`, auto-reconnects. |
| Frontend | Next.js, Vite React | **Vite + React + TS + Tailwind.** Fast, with no SSR needed. |
| Memory store | Vector DB, JSON files, SQLite | **One SQLite file per agent.** Lasting, isolated by file, and easy to diff for the demo. |

## 6. Features that stand out

- **Trace chain:** click a briefing sentence → ruling → claim → highlighted quote in the source doc, or the highlighted trade row.
- **Sealed-envelope animation** in Round 1, then a reveal.
- **Range-bar exposure table:** Loki band, Thor band, Odin band and an observed-data marker on one axis per country × sector.
- **Trap radar panel:** a live count of rumours rejected, retractions applied, injections ignored, damaged files quarantined and bad trade rows excluded.
- **Memory diff viewer:** each agent's memory before and after a case, plus the log line where a memory changed an action.
- **Chaos mode** for the demo: drop a corrupt DOCX into the live folder mid-demo and watch it get quarantined, with no crash.
- **Budget meters:** tool calls, tokens and runs against their hard limits, live.

## 7. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| LLM fabricates doc IDs or quotes | Citation-validity score drops | Validator plus quote matching. Invalid citations are stripped and the claim is auto-flagged before Odin rules. |
| Thor and Loki agree too easily ("cooperative loop") | Debate score drops | Opposing system prompts, the mandatory-attack rule, and scored incentives in the prompt ("an honest concession beats a losing defence; a missed range loses"). |
| Ranges too wide or too narrow | Range/calibration score | Calculator-based ranges, Odin narrowing rules, and a self-eval harness on synthetic events with known truth. |
| Runaway loops or token blow-up | Crash or cost | Hard limits in config, a budget object passed to every call, and the Thing forcing BRIEFING(INCOMPLETE) when limits are hit. |
| Damaged files crash ingestion | Reliability score, demo failure | Per-file try/except plus timeout, quarantine with reason, an ingestion report, and fuzz tests. |
| Live pack ingestion is slow | Final-round time | Embedding model pre-warmed, incremental indexing, and per-file progress over SSE. |
| API outage or rate limits during the final | Fatal | Switch provider through env (OpenAI ↔ Claude), retries with backoff, and a smaller per-agent model fallback. |
| Memory leaks future knowledge across cases | Clock-rule violation | `knowledge_date` on factual memories, filtered on recall. |
| Exposure unit differs from what the organizers expect | Range-accuracy score | Report **both** a % of baseline trade value and an absolute value. Ask the OC (see §8). |

## 8. Open questions to ask the organizers early

1. What is the **unit of "true exposure"**: % change in trade value, absolute value lost, volume, or an index? Is it per country × sector over the next quarter?
2. For trade rows, does a month row count as visible if the as-of date falls **inside** that month? (Default: only completed months.)
3. Do final-round packs ship with their **own manifest** or an updated global manifest?
4. Is the **event date** given in the docs or headers, or must it be inferred?
5. What does the reference sheet say about **routes** (which country pairs use the canal, strait or rail link)? This drives `route_share`.
6. Is any off-Runemark source allowed at all, or is labelling just a safety rule?
7. How many questions per pack, and what is the time limit per question in the final round?

## 9. Scope: MVP first, then stretch

**MVP (must work end to end before anything fancy):**
ingestion (3 formats + trade tables + damage flags) → hybrid search with filters → 4 agents with tool loops → Thing state machine (2 runs, limits) → validators → exposure calculator → rulings plus briefing → event log → minimal dashboard (question box, feed, trial view, exposure table, click-through).

**Stretch:** trap radar, memory diff viewer, chaos mode, briefing PDF export, scorecard, multi-case comparison, provider diversity experiment (Thor on one model, Loki on another).

## 10. How the docs fit together

| Doc | Purpose |
|---|---|
| [README.md](README.md) | What judges read first |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Diagram and component explanation (required for judging) |
| [docs/TRIAL_PROTOCOL.md](docs/TRIAL_PROTOCOL.md) | The Thing state machine, rounds, runs, limits, sufficiency, remand |
| [docs/AGENT_SPECS.md](docs/AGENT_SPECS.md) | Each agent's role, tools, inputs, outputs and failure behaviour |
| [docs/PROMPTS.md](docs/PROMPTS.md) | Full system prompts |
| [docs/CASE_FILE_SCHEMA.md](docs/CASE_FILE_SCHEMA.md) | All data contracts |
| [docs/DATA_PIPELINE.md](docs/DATA_PIPELINE.md) | Ingestion, parsers, trade tables, damage handling, live packs |
| [docs/SEARCH_AND_TOOLS.md](docs/SEARCH_AND_TOOLS.md) | Hybrid search, filters, tool layer, guards |
| [docs/EXPOSURE_MODEL.md](docs/EXPOSURE_MODEL.md) | How the numbers are made |
| [docs/MEMORY.md](docs/MEMORY.md) | Private lasting memory and proof that it influences behaviour |
| [docs/TRAPS_AND_RELIABILITY.md](docs/TRAPS_AND_RELIABILITY.md) | Each trap → detector → handling |
| [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) | Logs, saved artifacts, replay |
| [docs/API.md](docs/API.md) | REST + SSE contract |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | React UI spec |
| [docs/EVALUATION.md](docs/EVALUATION.md) | Tests, self-scoring, traceability matrix |
| [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) | Demo, architecture talk track, final-round runbook |
| [CLAUDE.md](CLAUDE.md) / [AGENTS.md](AGENTS.md) | Rules for AI coding agents building this repo |
| [TASKS.md](TASKS.md) | Phased build plan |

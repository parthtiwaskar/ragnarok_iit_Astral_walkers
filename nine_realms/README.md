# nine_realms: agents + orchestrator

Heimdall, Thor, Loki and Odin, plus the Thing (court procedure, no LLM), as a self-contained Python package.
The corpus is reached through a `ToolBox` interface; `corpus.py` is a SQLite-backed implementation for a Runemark pack.

```bash
pip install -r requirements-nine-realms.txt
export ANTHROPIC_API_KEY=...      # or: --provider groq with GROQ_API_KEY
python run_e5.py                  # ingests dev pack E5 into data/runemark.sqlite, runs the trial, writes runs/<case>/
python -m pytest nine_realms/tests
```

## Dashboard + API (one local server)
```bash
export ANTHROPIC_API_KEY=...      # optional: only needed to start new trials from the UI (or GROQ_API_KEY)
python -m nine_realms.server --port 8765     # http://127.0.0.1:8765
```
First start builds `data/ragnarisk.sqlite` (corpus + cases + event log) and imports any `runs/*/case.json` that has claims.
The dashboard reads everything from `/api/*` (documents + full-text search, news feed, cases, claims, rulings, events);
`POST /api/cases {question}` starts a trial in the background (one at a time; keys come from the server's environment only).

## Search (hybrid)
`search_documents` fuses BM25 (SQLite FTS5, stemmed) with dense cosine similarity by reciprocal-rank fusion; every filter
(as-of clock, country, sector, event, source type, tier, dates) applies to both sides. Vectors live in the `para_vecs` table.
Embedder: `fastembed` bge-small (384-d, downloaded once into `data/models/`) when installed, else a built-in hashed
n-gram embedder, so search works offline. Force one with `NINE_REALMS_EMBEDDER=hash|fastembed`.

## Keys
`python -m nine_realms.server` and `run_e5.py` read `ANTHROPIC_API_KEY` / `GROQ_API_KEY` (and `GROQ_MODEL`) from the
environment or a gitignored `.env` in the repo root. Keys are never sent to the browser. Groq's free tier is too small
(daily token cap, weak JSON discipline on the 20b model) to complete a trial; use Anthropic.

## Charts
The dashboard draws its charts with plain SVG (`charts.js`, no dependencies): monthly trade flows with a year-on-year overlay
(from `/api/trade`), Thor/Loki/Odin range comparison, a source timeline by month and tier, and claim status bars.

## Debate quality
Cross-examination is held to rules, not just prompts (`debate.py`, enforced in `agents/advocates.py`): every attack needs a specific
kind, an explanation and verified, relevant counter-evidence; a concession must say what was refuted, a Tier 1-2 sourced fact is not
conceded to weaker evidence without a warning, and a side can REVISE (narrow) a claim instead of dropping it. Each run gets a
deterministic scorecard (specific / evidence-backed attacks, defended / revised / conceded, concession rate). A collapsed debate
(>=70% of attacked claims conceded) is flagged to Odin and sends the case back.

Layout: `models.py` Case File · `tools.py` tool catalogue, allowlists, limits, guard chain · `agents/` the four agents and
prompts · `thing.py` orchestrator · `briefing.py` digest + renderer · `memory.py` per-agent private memory ·
`corpus.py` ingestion into SQLite + tools · `store.py` cases/claims/rulings/events tables · `server.py` API + dashboard · `embed.py` embedders for semantic search · `env.py` .env loader · `llm.py` Anthropic / OpenAI-compatible clients + `FakeLLM`.

## Status of the saved run (`runs/E5-24322/`)
Status **INCOMPLETE**. Run 1 finished (8 claims, 6 attacks, 5 concessions, rulings, remand to a second run, which brought new
evidence). Run 2 submitted 6 claims but the case hit the 600,000-token limit before cross-examination, so Odin could not judge
run 2 or write the briefing; `briefing.md` is the rulings-only fallback. Tolan maize has no range (no trade table found).
Odin's ranges are the output of one partial run and have not been checked against the organisers' truth.

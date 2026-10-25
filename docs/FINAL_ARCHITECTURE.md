# Architecture of the nine_realms build

![Architecture](img/architecture.svg)

The diagram is `docs/img/architecture.svg` (it follows light and dark mode). Four layers, top to bottom.

## 1. Intake: new event packs, no code change

A pack is a folder with `manifest.json` and `docs/`. It arrives as a zip upload, a dropped folder, or a rescan of the watched folder (`livepacks.py`). `corpus.ingest_pack` then
reads PDF, TXT and DOCX, repairs mixed encodings, flags duplicates and truncated copies, quarantines empty or unreadable files, parses trade-note rows (and excludes bad,
duplicate, conflicting or outlier rows, or tables that stop early), and links retractions and corrections. The result is an **intake report** per pack. Ingesting the same pack
twice changes nothing; a changed file is re-read.

## 2. Corpus and tools: where the as-of clock lives

One SQLite file holds documents, paragraphs, trade rows, pack reports, FTS5 keyword indexes and vector embeddings. Search is hybrid (keyword + vector, merged by rank) and every
metadata filter (country, sector, event, source type, tier, dates, as-of) applies to both halves.

Agents never touch the corpus directly. Every call goes through the **tool guard**: per-agent allowlist, per-turn budget, the as-of firewall (an agent cannot move the clock),
de-duplication, retries, an injection scan, and wrapping of document text as untrusted data. Tools are: hybrid search, read a document, list and read trade tables,
`trade_stats`, `import_summary` (the net change in a country's total imports from all origins), verify a citation, and compute exposure.

## 3. The trial: agents argue, code checks

- **The Thing** is the orchestrator. It is plain code and makes no content decisions: it opens the case, seals round 1, runs cross-examination, asks Odin to rule, applies the
  sufficiency checklist, sends a case back (remand), and enforces hard limits (runs, LLM calls, tokens, tool calls per turn, timeouts). A failed turn ends that turn, never the case.
- **The Case File** is the only thing agents share. Round 1 is sealed: Thor and Loki cannot see each other's claims until both have submitted.
- **Heimdall** verifies the event is real, builds the evidence pool, flags traps (rumours, retractions, injections), and researches on remand.
- **Thor** argues high exposure, **Loki** argues limited exposure, each with their own queries and evidence, then cross-examine each other. Attacks must name a specific weakness and
  bring counter-evidence that bears on the claim. Defences need a document not already cited.
- **Odin** rules on every claim (sourced fact or inference; established, contested, conceded, rejected), sets the ranges, names what is missing, and writes the briefing from rulings only.
- Each agent has **private memory** (its own SQLite file, clock-aware). Memory changes behaviour in one deterministic way: an agent that conceded a claim cannot re-argue it without new evidence, and the system logs when it blocks that.
- The **LLM client** is swappable: Anthropic (with prompt caching), any OpenAI-compatible endpoint (Groq), or a scripted offline policy used by the tests and `dryrun`.

## 4. Record and product

The case store keeps every claim, query, retrieved item, attack, response, ruling and memory use as an append-only event log, so any case can be audited or replayed as data.
The HTTP API (token, origin check, rate limits, spend caps) serves the dashboard and `GET /api/cases/<id>/acceptance`, which checks a case against the ten minimum criteria.

## Why this shape

Models are good at arguing and bad at keeping accounts, so the arguing is theirs and the accounting is code: citations, dates, retractions, limits and arithmetic are verified outside the model,
and the judge may only cite what is on the Case File.

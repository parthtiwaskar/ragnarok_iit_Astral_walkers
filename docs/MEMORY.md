# Agent Memory — private and lasting

FR6.1: *every agent keeps its own private, lasting memory; agents share only the Case File.*
Acceptance #9: *a logged action shows memory changing what an agent did.*

Code: `backend/app/memory/`

## 1. Storage and isolation

- One SQLite file per agent: `memory/heimdall.sqlite`, `memory/thor.sqlite`, `memory/loki.sqlite`, `memory/odin.sqlite`.
- Each `Agent` object owns a `MemoryStore` bound to **its** file. No other agent gets a handle. There's no shared memory tool, and the Case File never contains memory content.
- The data survives restarts and cases ("lasting"). `python cli.py memory-reset --agent all` exists for clean demos, but **don't reset between the two demo cases** you use to show influence.
- The dashboard has a **read-only** memory inspector for judges (an API endpoint, not an agent tool).

## 2. Schema

```sql
CREATE TABLE memories (
  id             TEXT PRIMARY KEY,          -- thor-M0031
  kind           TEXT NOT NULL,             -- procedural | fact | query | precedent | source_note | turn_note
  text           TEXT NOT NULL,
  tags           TEXT,                      -- json list
  case_id        TEXT,
  run            INTEGER,
  knowledge_date DATE,                      -- as_of of the case that produced it (NULL for procedural)
  confidence     REAL DEFAULT 0.6,
  uses           INTEGER DEFAULT 0,
  created_at     TIMESTAMP,
  embedding      BLOB                       -- bge-small vector
);
CREATE TABLE query_log (
  fingerprint TEXT, query TEXT, filters TEXT, result_count INTEGER, useful INTEGER,
  case_id TEXT, run INTEGER, knowledge_date DATE, created_at TIMESTAMP
);
```

## 3. Memory kinds per agent

| Agent | What it remembers | Example |
|---|---|---|
| Heimdall | query outcomes (hits / zero hits), rumour patterns, outlet reliability notes, verification outcomes | `heimdall-M0007 (query): "event_id=EVT-07 tier_max=1 'reopening' → 0 results"` |
| Thor | which claim styles were rejected or upheld, attacks that worked, his own query history | `thor-M0012 (procedural): "Duration claims backed only by one Tier 3 analyst were ruled INFERENCE and lost to a Tier 1 notice. Corroborate first."` |
| Loki | trap patterns caught, concessions that were correct, attacks that worked | `loki-M0004 (procedural): "Check find_related_docs on every alarming Tier 2 report; corrections often exist."` |
| Odin | precedents (how he ruled on recurring claim types), calibration notes | `odin-M0020 (precedent): "Single Tier 4 source + no corroboration → REJECTED, basis UNSUPPORTED."` |

## 4. The clock rule for memory (avoiding future leakage)

A memory produced in a case with as-of `2025-06-30` might "know" that a doc was retracted in May. If a later case is asked as of `2025-04-01`, that knowledge must not leak.

- `kind ∈ {fact, query, source_note, precedent-with-doc-refs}` → stored with `knowledge_date = as_of` of the producing case, and **recalled only if `knowledge_date ≤ current as_of`**.
- `kind = procedural` → method-level lessons with no event or document facts. Date-agnostic, always recallable.
- The reflection prompt forces the agent to label lessons `fact` vs `procedural`. A regex check reclassifies any "procedural" lesson that mentions a doc ID, table ID or event ID as `fact`.

## 5. Read path (every turn)

```
mems = store.recall(query=task_context_summary, k=MEMORY_TOP_K (5), as_of=case.as_of)
       # hybrid: FTS over text + cosine over embedding, + boost by confidence and uses
log memory_read {agent, memory_ids, scores}
→ inject into the turn prompt as:  [thor-M0012] (procedural, conf 0.8) Duration claims backed only…
```

The agent may also call `memory_recall(query)` during a turn (logged the same way).

## 6. Write path

| When | What | Who |
|---|---|---|
| after every tool call (search) | `query_log` row with result count | tool layer (deterministic, for the agent who called it) |
| end of each turn | `turn_note`: what I submitted and why (short) | agent |
| end of case | 1–3 lessons from the `reflect.md` prompt, using rulings that concern this agent | agent |
| Odin, end of case | precedents for notable rulings | Odin |

Each write logs `memory_write {agent, memory_id, kind}`.

## 7. Proving influence (acceptance #9)

Two independent mechanisms, so the demo never depends on luck:

### A. Deterministic: Heimdall's query memory (guaranteed)
Before running a remand query, Heimdall's research step checks its `query_log` (this case plus earlier cases with `knowledge_date ≤ as_of`) for queries with the same fingerprint or a near-duplicate (cosine > 0.92) that returned **0 results**. Those queries are **skipped or reformulated**, and the following is logged:

```json
{"type": "memory_influence", "agent": "heimdall", "memory_id": "heimdall-M0007",
 "effect": "skipped query 'canal reopening' (tier_max=1): returned 0 results in R1; reformulated to source_type=official_notice, date_from=2025-03-10",
 "action_ref": "CASE-…:0088"}
```

### B. LLM-declared influence (rich and visible)
Every submission schema has `memory_influence: [{memory_id, effect}]`. The prompt says: *"If a memory changes what you do, cite its id."* The Thing validates that cited IDs were actually recalled this turn (a bogus ID is dropped and logged), then logs `memory_influence`.

### Demo script for memory
1. **Case A**: Thor makes a duration claim backed only by a Tier 3 analyst. Loki attacks it, Odin rules it INFERENCE/REJECTED, and Thor's reflection writes `thor-M00xx`.
2. **Case B** (different event, later or equal as-of): Thor's recall surfaces `thor-M00xx`. He corroborates the duration with a Tier 1 notice before claiming and cites the memory. The log shows `memory_read` → `memory_influence` → the changed `search_documents` call (`tier_max=2`).
3. Inside a single case, the deterministic Heimdall skip (A) shows up in Run 2.

The dashboard's **memory diff** view shows each agent's memory before and after the case and highlights `memory_influence` events in the log.

## 8. Limits and hygiene

- `MEMORY_TOP_K = 5` per turn, ≤ 3 lessons per agent per case, dedupe lessons by cosine > 0.95 (merge and bump confidence).
- Confidence decays slightly when a recalled lesson is contradicted by a later ruling (`conf -= 0.1`), and grows when it's reinforced.
- Memory text is untrusted too: it's wrapped as `<memory>` and can't override the system prompt.

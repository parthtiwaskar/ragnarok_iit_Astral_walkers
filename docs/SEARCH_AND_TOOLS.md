# Search and Tool Layer

Agents touch Runemark **only** through typed tools. Every tool call passes through the same **guard chain**, which is where the clock rule, dedupe, budgets, logging and error handling are enforced (NFR2–NFR5). Code: `backend/app/search/`, `backend/app/tools/`.

## 1. Hybrid search (acceptance #2)

```
query ──► normalize ──► FTS5 BM25 (top 50, filtered) ──┐
                   └──► embed ──► cosine (top 50, filtered) ──┤──► RRF fuse ──► doc-level collapse ──► top k
```

- **BM25:** SQLite FTS5 `bm25(chunks_fts)` over chunk text, joined with `chunks_meta` for filters.
- **Dense:** query embedding (bge-small) · normalized chunk matrix → cosine. Filters are applied as a boolean mask *before* top-k.
- **Fusion:** Reciprocal Rank Fusion, `score = Σ 1/(60 + rank_i)`, with optional weights `W_BM25 = 1.0`, `W_DENSE = 1.0`.
- **Collapse:** at most 2 chunks per doc in the result list. Each result keeps the best chunk as its snippet.
- **Always-on filters** (not overridable by agents): `pub_date ≤ as_of`, `status ∉ {QUARANTINED, MISSING}`, `off_manifest = false`.

### Metadata filters (agent-controllable)

| Filter | Type | Example |
|---|---|---|
| `event_id` | str \| list | `"EVT-07"` |
| `country` | str \| list | `["A", "B"]` |
| `sector` | str \| list | `"electronics"` |
| `tier_max` / `tier_min` | int | `tier_max=2` → only Tier 1–2 |
| `source_type` | str \| list | `"official_notice"`, `"correction"` |
| `date_from` / `date_to` | date | clamped to `≤ as_of` |
| `include_background` | bool | default `false` |
| `format` | `pdf\|txt\|docx` | debugging only |

The demo shows a query with ≥2 filters (e.g. `event_id` + `tier_max`) in the trial log and in the debug search panel.

### Result shape

```json
{
  "call_id": "CASE-…:0042",
  "query": "canal closure duration",
  "filters": {"event_id": "EVT-07", "tier_max": 2},
  "results": [
    {
      "doc_id": "RM-DOC-0101", "title": "…", "outlet": "…", "tier": 1,
      "pub_date": "2025-03-02", "event_id": "EVT-07", "country": ["…"], "sector": ["…"],
      "source_type": "official_notice", "status": "OK",
      "as_of_status": {"retracted": false, "corrected_by": null},
      "flags": [], "snippet": "…", "chunk_id": "RM-DOC-0101#c2",
      "scores": {"rrf": 0.031, "bm25_rank": 1, "dense_rank": 3}
    }
  ],
  "note": null
}
```

An empty result returns `results: []` with `note: "no results for these filters"`. That's a normal outcome, not an error.

## 2. Tool catalogue

All tools are pydantic-typed. Their JSON schemas are generated for OpenAI `tools` / Anthropic `tools`.

| Tool | Args | Returns | Notes |
|---|---|---|---|
| `search_documents` | `query, filters?, k=8` | results (above) | hybrid |
| `read_document` | `doc_id, offset=0, max_chars=6000` | header + body slice wrapped in `<document untrusted>` + `as_of_status` + flags | refuses docs after as-of → `NOT_AVAILABLE_AS_OF` (never reveals content) |
| `find_related_docs` | `doc_id` | corrections, retractions, denials, duplicates (as-of aware) | |
| `list_events` | `query?` | candidate event_ids with doc counts per tier (as-of) | Heimdall only |
| `event_profile` | `event_id` | per-tier counts, distinct outlets, retractions, denials, first/last dates, rule verdict | Heimdall only. Deterministic |
| `list_trade_tables` | `event_id?, country?, sector?` | table ids + header + quality summary | |
| `read_trade_table` | `table_id, rows?` | visible rows with quality flags (damaged rows shown, marked) | rows after as-of hidden |
| `trade_stats` | `table_id, event_date` | `baseline` (mean of last N clean pre-event months), `observed_change` (clean post-event months ≤ as-of), rows used, rows excluded + why | deterministic |
| `get_reference` | `sheet, key?` | reference rows | |
| `compute_exposure` | `country, sector, params{p:(lo,hi)}, param_claims` | `{low, high, unit, breakdown, calc_id}` | see [EXPOSURE_MODEL.md](EXPOSURE_MODEL.md) |
| `verify_citation` | `evidence` | `CitationCheck` | deterministic |
| `flag_injection` | `doc_id, span` | ack | logged trap |
| `flag_data_issue` | `ref, issue` | ack | |
| `memory_recall` | `query, k=5` | own memories (knowledge_date-filtered) | own store only |
| `memory_note` | `text, kind, tags` | memory_id | own store only |
| `submit_*` | structured output | validation result | terminal |

## 3. Guard chain

Every call goes through `guards.run(agent, tool, args)`:

```
1. allowlist      tool ∈ AGENT_TOOLS[agent]?          else → error "tool not permitted for {agent}"
2. budget         per-turn & per-case tool calls/tokens  else → limit_hit (turn ends cleanly)
3. clock          inject as_of; clamp date filters     (agents cannot pass a later date)
4. dedupe         fp = sha256(tool + canonical(args)); if fp in ledger → return {duplicate_of, result_ref}; log duplicate_blocked
5. execute        with timeout (TOOL_TIMEOUT_S=15) + TOOL_MAX_RETRIES on transient errors
6. post-process   wrap doc text as untrusted; attach as_of_status; strip anything after as_of
7. log            tool_call + retrieval events (ids, scores, latency, result count)
8. errors         any exception → {"error": "...", "retryable": bool}; never raised to the agent loop
```

**Canonicalization for dedupe:** lowercase, trimmed, collapsed whitespace for `query`. Filters sorted, lists sorted, defaults filled in, `k` excluded (so the same query with a different `k` still counts as a duplicate).

**Ledger scope:** per case, shared across agents and runs. Each agent's prompt also lists **its own** previous queries.

## 4. Citation validator (`tools/citation.py`)

```python
def verify_citation(ev: Evidence, case: CaseFile) -> CitationCheck:
    if ev.kind == "doc":
        doc = manifest.get(ev.doc_id)              # NOT_IN_MANIFEST
        doc.pub_date > case.as_of                  # AFTER_AS_OF
        doc.status in {QUARANTINED, MISSING}       # QUARANTINED
        doc.off_manifest                           # NOT_IN_MANIFEST
        retracted_as_of(doc, case.as_of)           # RETRACTED_AS_OF
        corrected_as_of(doc, case.as_of)           # CORRECTED_AS_OF (warning; superseded_by set)
        doc.event_id != case.scope.event_id        # WRONG_EVENT (BACKGROUND_ONLY if background)
        sim = rapidfuzz.partial_ratio(norm(ev.quote), norm(doc.body))
        sim < 90                                   # QUOTE_MISMATCH
    else:
        row = trade_rows.get(ev.table_id, ev.row)  # NOT_IN_MANIFEST if missing
        row.month_end > case.as_of                 # AFTER_AS_OF
        row.quality != "OK"                        # DAMAGED_ROW
    valid = no blocking issues (CORRECTED_AS_OF and BACKGROUND_ONLY are warnings, not blockers)
```

The tool layer **fills in** `pub_date`, `tier` and `event_id` on evidence from the store, so the LLM can't misstate them.

## 5. LLM client (`llm/client.py`)

```python
class LLMClient(Protocol):
    async def chat(self, system: str, messages: list[Msg], tools: list[ToolSpec],
                   max_tokens: int, temperature: float) -> LLMResponse: ...
# implementations: OpenAIClient (Chat Completions / Responses tools), AnthropicClient (Messages tools)
# LLMResponse: text, tool_calls[{id, name, args}], usage{input, output}, stop_reason, model
```

- Selected by `LLM_PROVIDER` and per-agent overrides.
- Retries with exponential backoff on 429/5xx (`LLM_MAX_RETRIES`).
- Usage is counted into `Budget` and logged per call.
- Structured outputs come **only** through terminal tool calls with JSON schema, validated by pydantic.

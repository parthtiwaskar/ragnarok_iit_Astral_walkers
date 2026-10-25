# Agent Specs

Four agents, each with **its own prompt, its own tools and its own private memory** (NFR1, FR6). They interact only through the Case File. Prompts are in [PROMPTS.md](PROMPTS.md), schemas in [CASE_FILE_SCHEMA.md](CASE_FILE_SCHEMA.md), tools in [SEARCH_AND_TOOLS.md](SEARCH_AND_TOOLS.md).

## 0. Shared agent runtime (`agents/base.py`)

Every agent is an instance of `Agent(name, system_prompt, tools, memory, llm, limits)`. One **turn**:

```
1. view   = casefile.view_for(agent, phase)        # role-filtered: sealed sections hidden, memory never included
2. mems   = memory.recall(task_context, as_of)     # top-k, knowledge_date-filtered → log memory_read
3. msgs   = [system_prompt, turn_instruction(phase), view, mems, query_ledger(agent)]
4. loop (≤ MAX_TOOL_CALLS_PER_TURN):
     resp = llm.chat(msgs, tools=allowlist)        # tokens counted against Budget
     if resp.tool_calls: run each through guards → append results → continue
     if resp calls a terminal submit_* tool: validate → done
5. on validation error: one re-prompt with errors (MAX_REPROMPTS) → else FAILED
6. memory.write(...)                               # turn notes, query results → log memory_write
7. emit memory_influence for any memory_ids the agent cited in its output
```

- **Terminal tools** end the turn: `submit_case_file`, `submit_claims`, `submit_attacks`, `submit_responses`, `submit_rulings`, `submit_briefing`. Their arguments are the structured output (validated with pydantic).
- **Document text** always arrives wrapped as `<document id="…" tier="…" date="…" untrusted="true">…</document>`.
- **Temperature:** 0.2 for all agents (reproducibility). Thor and Loki use the same model.
- **Model config:** `{AGENT}_PROVIDER` / `{AGENT}_MODEL` override `LLM_PROVIDER` / `LLM_MODEL`.

### Tool allowlist matrix

| Tool | Heimdall | Thor | Loki | Odin |
|---|:-:|:-:|:-:|:-:|
| `search_documents` (hybrid + filters) | ✅ | ✅ | ✅ | ❌ |
| `read_document` | ✅ | ✅ | ✅ | ✅ (cited docs only) |
| `list_events` / `event_profile` | ✅ | ❌ | ❌ | ❌ |
| `find_related_docs` (corrections/retractions graph) | ✅ | ✅ | ✅ | ✅ |
| `list_trade_tables` / `read_trade_table` | ✅ | ✅ | ✅ | ✅ |
| `trade_stats` (baseline, observed change) | ✅ | ✅ | ✅ | ✅ |
| `get_reference` (countries, ports, chokepoints, sectors, firms, outlets) | ✅ | ✅ | ✅ | ✅ |
| `compute_exposure` | ❌ | ✅ | ✅ | ✅ |
| `verify_citation` | ✅ | ✅ | ✅ | ✅ |
| `flag_injection` / `flag_data_issue` | ✅ | ✅ | ✅ | ✅ |
| `memory_recall` / `memory_note` (own store only) | ✅ | ✅ | ✅ | ✅ |
| `submit_case_file` | ✅ | ❌ | ❌ | ❌ |
| `submit_claims` / `submit_attacks` / `submit_responses` | ❌ | ✅ | ✅ | ❌ |
| `submit_rulings` / `submit_briefing` | ❌ | ❌ | ❌ | ✅ |

Odin has **no search**: a judge weighs the evidence put before him. Missing evidence becomes a deficiency, not his own investigation. Heimdall has **no debate tools** (FR1.4).

---

## 1. Heimdall — The Gatekeeper (case builder)

**Mission:** turn the user's question into a verified, well-scoped Case File, and re-search when the case is remanded. He never takes a side.

**Triggers:** `GATHERING`, `RESEARCH`.

**GATHERING steps:**
1. **Parse the question** → `event_hint`, `countries[]`, `sectors[]`, `as_of` (the UI value wins over a date in the text), `horizon = next quarter after as_of`, `metric = pct_of_baseline_trade_value` (see [EXPOSURE_MODEL.md](EXPOSURE_MODEL.md)).
2. **Find the event:** `list_events(query, as_of)` returns candidate `event_id`s with doc counts per tier. Pick the best match. If ambiguous, record the alternatives in the Case File.
3. **Verify (FR1.3).** The deterministic `event_profile(event_id, as_of)` returns per-tier counts, distinct outlets, retractions, corrections and denials, all as-of aware. Rules (configurable):
   - `VERIFIED`: ≥1 non-retracted Tier 1, **or** ≥2 non-retracted Tier 2 docs from distinct outlets
   - `UNCONFIRMED`: only Tier 3–4 sources (typical rumour)
   - `REFUTED`: a Tier 1–2 doc explicitly denies the event
   - `RETRACTED`: every Tier 1–2 report on it was retracted as of the date
   - `NOT_YET_KNOWN`: the event exists in the corpus, but only after the as-of date (the message must not reveal anything about it)
   - `NOT_FOUND`: no matching event
   Heimdall reads the key docs and writes a **reasoned verdict**. If he disagrees with the rule-based verdict, he must state why, and the disagreement is logged.
4. **Scope:** if the question names no countries or sectors, take the top (country, sector) rows by baseline trade value among tables tagged with the event (max `MAX_SCOPE_ROWS = 6`).
5. **Gather the evidence pool:** bounded at `POOL_SIZE_R1 = 20` docs. Mix tiers, include corrections/retractions, include trade tables for every scope row (with quality summaries). Background docs are labelled.
6. **Flag** injections and damaged or quarantined docs touching the event.
7. `submit_case_file(...)`

**RESEARCH steps (after a remand):**
1. Read Odin's deficiencies.
2. Recall memory: past queries and their hit counts (this case and earlier ones), useful source patterns.
3. Write **new** queries (the ledger blocks repeats; memory steers him away from dead ends, which gets logged as `memory_influence`).
4. Add evidence to the pool with `added_in_run = n+1`. Also check `live_packs` additions (index refresh).
5. `submit_case_file(delta)` with a short note on each deficiency: `addressed | partially | no evidence found`.

**Memory writes:** queries and hit counts, verification outcomes per rumour pattern ("Tier 4-only event reports were UNCONFIRMED"), outlet reliability observations (with `knowledge_date`).

**Failure:** fails in GATHERING → case `ABORTED` with an explanation. Fails in RESEARCH → INCOMPLETE (`RESEARCH_FAILED`).

---

## 2. Thor — The Alarm (case for high exposure)

**Mission:** build the **strongest honest** case that exposure is high (FR2). His range must still contain the truth, because exaggeration is scored as a miss.

**Triggers:** `R1_SUBMIT`, `R2_ATTACK`, `R2_RESPOND` (each run).

**What he looks for:** Tier 1 notices of closures and durations. High route share in the trade tables. Observed post-event drops. Force-majeure statements from firms. Few alternative routes or a long rerouting penalty. Low inventories. Knock-on effects to other sectors. Reports that contradict Loki's optimistic sources.

**Outputs:**
- **Claims** (≤ 8). Each one has: statement, `type` = SOURCED_FACT or INFERENCE (with premises), evidence items (doc_id + exact quote, or table_id + row), the parameter it supports (`route_share | severity | duration | reroute_offset`) with a bound value, and the (country, sector) it applies to.
- **Position** per (country, sector): `{low, high, headline=high}`, computed with `compute_exposure` from his cited parameter ranges.
- **Attacks** (≥ 1) on Loki's claims.
- **Responses** to every attack: `DEFEND` (new evidence), `REVISE`, or `CONCEDE`.

**Rules:** at least one `search_documents` call per Round 1 turn (acceptance #3). No claim may rely only on Tier 4 as a SOURCED_FACT. Concede when a Tier 1 correction or a clean trade row contradicts him.

**Memory use:** lessons such as "single-source Tier 3 duration claims were rejected; corroborate first", plus his own query history. He must cite `memory_ids` when a memory changes what he does.

**Failure:** the side is marked FAILED, Loki's claims become `unchallenged`, and the briefing says so.

---

## 3. Loki — The Trickster (case for limited exposure)

**Mission:** build the **strongest honest** case that exposure is limited (FR3). "Maybe it will be fine" is not a claim. Every point needs a source and a low estimate.

**What he looks for:** corrections and retractions of alarming reports. Tier 4 exaggerations. Low route share. Alternative ports, rail or reroutes. Inventories and buffers. Short announced durations or reopening notices. Trade rows showing resilience. Look-alike docs from another event that Thor may have cited.

**Outputs:** the same shapes as Thor, with `headline = low`.

**Rules and memory:** mirror Thor's. Loki is especially rewarded for catching trap docs (wrong event, retracted, after as-of, injection) in his attacks.

---

## 4. Odin — The Allfather (judge)

**Mission:** rule on **every** claim, sort the findings, decide sufficiency, and write the briefing **from rulings only** (FR4). He never plans, assigns or searches.

**Triggers:** `JUDGING`, `BRIEFING`.

**JUDGING inputs:** all claims, attacks and responses for the run, the citation-validator results per evidence item, source metadata (tier, date, event, status), the deterministic sufficiency checklist, and effective rulings from earlier runs.

**Ruling per claim:**
- `verdict`: `UPHELD | PARTIALLY_UPHELD | REJECTED | CONCEDED`
- `basis`: `SOURCED_FACT` (the cited text states it) | `INFERENCE` (reasoned from sourced facts) | `UNSUPPORTED`
- `finding`: `ESTABLISHED` (upheld and not credibly contested) | `CONTESTED` (credible evidence both ways, or unresolved contradiction) | `CONCEDED` (withdrawn by its side) | `REJECTED` (not a finding; listed in the appendix)
- `rationale` (2–4 sentences) and `decisive_evidence[]`
- `citation_issues[]`, copied from the validator

**Ruling guidelines:**
- Any citation that fails validation (not in manifest, after as-of, retracted as of the date, wrong event, quote mismatch, quarantined) cannot support a claim. If nothing valid is left, the claim is `REJECTED`.
- Tier 4 alone is never `ESTABLISHED`.
- Two credible sources in conflict with no resolution → `CONTESTED`, with both cited.
- An `UNREBUTTED` attack with valid evidence normally defeats the claim.
- `unchallenged` claims can be at most `CONTESTED`, unless they're backed by a Tier 1 source or a clean trade row.

**Final range per (country, sector):** for each exposure parameter, keep the bounds backed by UPHELD or PARTIALLY_UPHELD claims, drop the rest, then call `compute_exposure` (see [EXPOSURE_MODEL.md](EXPOSURE_MODEL.md#odin-final-range)). Odin writes a one-line justification per row.

**Sufficiency:** apply the rubric in [TRIAL_PROTOCOL.md](TRIAL_PROTOCOL.md#4-sufficiency-rubric-odin) and output `SUFFICIENT` or `INSUFFICIENT + deficiencies[]`, phrased as gaps, never as instructions.

**BRIEFING:** the input is a **rulings digest** (upheld, contested and conceded items with their citations, plus final ranges, data-quality notes, verification and failures). Odin writes only: the executive summary, the narrative for the Established/Contested/Conceded sections, and the next-quarter outlook with explicit assumptions. Each sentence must carry a citation drawn from the digest. The exposure table, source list and trace section are **rendered by code**. Then the briefing validator runs ([TRAPS_AND_RELIABILITY.md](TRAPS_AND_RELIABILITY.md#briefing-validator)).

**Memory use:** precedents ("ruled duration claims from a single Tier 3 analyst as INFERENCE"), so his rulings stay consistent across cases. Recalled memories are logged.

**Failure:** see [TRIAL_PROTOCOL.md §5](TRIAL_PROTOCOL.md#5-failure-handling-inside-the-protocol-nfr3).

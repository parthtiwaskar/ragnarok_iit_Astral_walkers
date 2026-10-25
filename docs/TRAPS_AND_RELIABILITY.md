# Traps, Grounding and Reliability

Covers PS §4.4 (deliberate traps), NFR2 (grounding), NFR3 (reliability) and the "trap handling" + "reliability" scoring lines. Every trap gets a **detector**, a **handling rule**, a **log event** and a **visible place** in the UI and briefing.

## 1. Trap matrix

| Trap (PS §4.4) | Detector | Handling | Log event | Shown in |
|---|---|---|---|---|
| **Rumour about an event that never happened** | `event_profile`: only Tier 3–4 sources, no Tier 1 / no 2×Tier 2, or official denial | Heimdall verdict `UNCONFIRMED` / `REFUTED` → `CASE_REJECTED`, with reasons and doc IDs shown to the user. No trial. | `verification` (verdict, reasons), `trap_detected{type: RUMOUR}` | Case bar banner, briefing "Event verification" |
| **Retracted article** | `doc_relations` `retracts` edge with `src.pub_date ≤ as_of` | Doc gets `RETRACTED_AS_OF`. The citation validator blocks it, and claims citing it fall to REJECTED unless other valid support exists | `trap_detected{type: RETRACTION, doc, by}` | Feed card strikethrough, claim citation chip red, briefing data quality |
| **Corrected article** | `corrects` edge (as-of aware) | `CORRECTED_AS_OF` warning, with `superseded_by` pointing to the correction. Agents must use the corrected figure. Odin rejects claims relying on the superseded number | `trap_detected{type: CORRECTION}` | Feed badge "corrected", chip amber |
| **Retraction published after the as-of date** | edge exists but `src.pub_date > as_of` | **Ignored**. The doc is valid as of the date, and nothing about the later retraction is revealed | `as_of_filter{hidden_relation}` (internal) | — |
| **Contradicting sources** | Agents via `find_related_docs` + attacks. Odin compares the claims on the same parameter | Marked `CONTESTED` with both sources cited. Unresolved contradictions → deficiency S5 | `ruling{finding: CONTESTED}` | Trial view, briefing "Contested" |
| **Evidence for both sides** | by design | Both sides cite it. Parameter intervals reflect it | — | — |
| **Facts that make conceding correct** | Strong counter-evidence (Tier 1 or a clean trade row) in an attack | The prompt rewards an honest concession. A `CONCEDE` response → `CONCEDED` finding | `response{action: CONCEDE}` | Trial view "conceded" badge, briefing "Conceded" |
| **Instructions aimed at the AI** | Ingest-time regex/heuristic scan + agents' `flag_injection` | Doc text is always wrapped `untrusted`. Injected instructions are ignored, and the doc is still usable as data. Structured output validation means injected text can't change behaviour. | `trap_detected{type: INJECTION, doc, span}` | Feed warning badge, briefing data quality ("instructions in RM-DOC-0150 ignored") |
| **Look-alike doc from a different event / no event** | `event_id` mismatch, `is_background` | Search defaults to the case `event_id` when agents filter. The validator flags `WRONG_EVENT` (blocking) or `BACKGROUND_ONLY` (warning: context, not evidence) | `citation_check{issues: [WRONG_EVENT]}` | Chip warning, Odin rationale |
| **Future information (simulated clock)** | Always-on `pub_date ≤ as_of`, trade month-end ≤ as_of, as-of aware relations, memory `knowledge_date` | Cannot be retrieved. `read_document` on a future doc → `NOT_AVAILABLE_AS_OF` | `as_of_block` | Limits panel (counter) |
| **Damaged documents** (empty, cut off, duplicated, badly encoded) | Ingestion damage checks ([DATA_PIPELINE.md §4](DATA_PIPELINE.md#4-damage-detection-documents)) | Quarantine / repair / alias / partial-with-warning. Never a crash | `ingest_issue` | Ingestion view, briefing data quality |
| **Damaged trade rows** (cut-off table, missing value, repeated row, bad number) | Row quality flags | Kept but **never used**. Excluded from stats and listed | `trade_row_excluded` | Trade viewer (red rows), exposure table warning icon, briefing data quality |
| **Off-Runemark source** | No web tools exist. Validator `OFF_RUNEMARK` safety label | Can't be cited as Runemark. Would be labelled if it ever appeared | `citation_check` | — |

## 2. Grounding rules (NFR2), enforced in code

1. **No claim without a source.** The pydantic `Claim.evidence` has `min_length=1`. A claim with zero *valid* evidence after validation → auto-flag `NO_VALID_SUPPORT` → Odin must rule `REJECTED`.
2. **Facts vs inferences are visibly separate.** `Claim.type` and `Ruling.basis`. The UI uses distinct styles (solid chip = sourced fact, dashed = inference), and the briefing marks inferences with "(inference)".
3. **Every citation resolves to a real doc.** Validator against the manifest. The briefing validator (below) removes any citation not in the rulings digest.
4. **"If evidence is missing, say so."** Agents output `NO EVIDENCE FOUND`, Odin turns gaps into deficiencies, and the briefing lists unresolved gaps.

## 3. Briefing validator

After Odin writes the briefing sections:

```
1. extract citations with regex  \[([A-Z0-9_\-]+)(?: r(\d+))?, (\d{4}-\d{2}(?:-\d{2})?)\]
2. each citation must (a) exist in the rulings digest, (b) match its date, (c) pass verify_citation
3. every factual sentence must contain ≥1 citation (heuristic: sentences with numbers, dates or named entities)
4. every ESTABLISHED/CONTESTED/CONCEDED ruling id must be mentioned in its section
5. outlook assumptions A1..An must each reference a ruling id
violations → one regeneration with the error list → if still failing, offending sentences are removed and
           status_reasons += "BRIEFING_SANITIZED"
```

The exposure table, source list, data-quality section and trace are **rendered by code**, so they can't hallucinate.

## 4. Reliability matrix (NFR3)

| Failure | Where caught | Behaviour | Visible as |
|---|---|---|---|
| Missing feed / folder missing / manifest missing | ingestion startup | Start with what exists, report `MISSING`. If the manifest is missing, build a synthetic manifest from headers and flag **all citations as unverifiable** (status INCOMPLETE) | ingestion banner |
| Empty / unreadable / damaged doc | `safe_ingest` | quarantine with reason | ingestion view |
| Parser hangs on a bad PDF | per-file timeout | quarantine `TIMEOUT` | ingestion view |
| Tool exception | guard chain | `{"error", "retryable"}` to the agent, retried once if transient | log |
| Empty search results | search | `results: []` + note | log |
| LLM 429/5xx/timeout | `LLMClient` | backoff retries → side FAILED | trial view "Thor failed this round" |
| Invalid structured output | validator | 1 re-prompt with errors → FAILED | log |
| One side fails | the Thing | the other side's claims are marked `unchallenged`, and the briefing says so explicitly | trial view banner + briefing |
| Budget/time limit | the Thing | clean end of turn → BRIEFING (INCOMPLETE) | limits panel, briefing status |
| SSE client disconnect | API | case continues. The client resyncs via `GET /api/cases/{id}` + `?since=seq` | — |
| Server restart mid-case | artifacts | case marked `ABORTED` on restart (no silent resume). Saved run artifacts remain | case list |

**Never claim complete when it isn't:** `status = COMPLETE` requires `SUFFICIENT` on the last run, no failed side, no limit hit, no `NO_RULING`, and a briefing validator pass without sanitization. Otherwise `INCOMPLETE` with an explicit list of reasons, rendered at the top of the briefing.

## 5. Chaos tests (run before the final)

- Drop `empty.pdf`, `cut.docx` (zip truncated), `latin1_mojibake.txt`, `dup_of_0101.txt`, `no_header.txt` and `garbage.bin` (renamed `.pdf`) into `live_packs/` while a case runs → no crash, all quarantined or repaired, report correct.
- Trade table with one row of each damage type → stats exclude them, briefing lists them.
- Kill the LLM key mid-case (`LLM_FAULT_INJECT=thor`) → Thor FAILED, Loki unchallenged, briefing INCOMPLETE with a correct explanation.
- Ask about a rumour event → `CASE_REJECTED` with reasons.
- Ask with an as-of date before a retraction → the original doc counts as valid. After it → blocked.
- Injection doc on topic → logged and ignored. The briefing mentions it, and the agents' behaviour is unchanged (assert on outputs).

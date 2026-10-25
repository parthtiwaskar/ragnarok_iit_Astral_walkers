# Demo Script, Architecture Talk Track and Final-Round Runbook

## 1. Live demo (≈ 8 minutes)

**Before you start:** backend running, index warm, the dashboard open on a clean screen, one previous case (Case A) already run so memory exists, the chaos zip ready on the desktop, a saved two-run case loaded in a second tab as a fallback ("replay mode").

| Time | Action | Say | Shows |
|---|---|---|---|
| 0:00 | Ingestion tab | "Runemark ingested: PDF, TXT, DOCX, trade tables. Five damaged files quarantined with reasons, three bad trade rows flagged and excluded, never used silently." | acceptance 1, 10 |
| 0:40 | Ask a **rumour** question | "First, a trap. Heimdall checks whether the event is real…" → CASE REJECTED with reasons (Tier 4 only + Tier 1 denial) | trap handling |
| 1:30 | Ask the **main** question + as-of date | "Only the user can open a case. Heimdall builds the Case File." Point at the feed replaying docs up to the as-of date, retracted docs struck through | FR1, FR7.1 |
| 2:10 | Round 1 | "Thor and Loki submit at the same time, sealed. Neither can see the other." The envelopes lock, then reveal. Open the logs: each side ran its own filtered searches | acceptance 2, 3 |
| 3:00 | Click a claim citation | Doc opens with the quote highlighted. Click a trade chip → row highlighted | acceptance 7 (click-through) |
| 3:30 | Cross-examination | "Loki attacks Thor's duration claim: the cited report was retracted as of this date. Thor concedes. Here Loki defends with *new* evidence." | acceptance 4 |
| 4:20 | Odin rulings | "Odin rules on every claim: fact or inference, Established, Contested or Conceded. This claim is rejected because its only source is Tier 4." | acceptance 5 |
| 4:50 | Remand | "Odin isn't satisfied. He lists *what's missing*, not what to do. Heimdall decides how to search again. Run 2 brings new evidence (pulsing in the feed)." | acceptance 8 |
| 5:40 | Memory | Memory tab: "Heimdall skipped a query that memory says returned nothing. Thor recalled a lesson from Case A and corroborated before claiming." Click `memory_influence` → linked action | acceptance 9 |
| 6:20 | Exposure tab | "Loki's range, Thor's range, Odin's final range per country and sector. Watch Odin's band narrow from Run 1 to Run 2. Every bound traces to a ruling." | FR7.3 |
| 6:50 | Briefing | Executive summary, E/C/C sections, exposure table, outlook with assumptions, citations with timestamps, data-quality notes | acceptance 6 |
| 7:20 | Chaos drop | Drag the broken zip into Ingestion → quarantined live, no crash, the case keeps running | acceptance 10 |
| 7:40 | Logs + replay | "Every claim, query, retrieved item, memory use and ruling is logged. `replay` rebuilds this briefing from the saved rulings with zero LLM calls." | NFR4, NFR6 |

## 2. Architecture talk track (≈ 2 minutes, with ARCHITECTURE.md diagram)

1. **"Four agents, one blackboard, no boss."** Heimdall, Thor, Loki and Odin are the only LLM components. They share only the Case File. Each has its own prompt, its own tool allowlist and its own private SQLite memory.
2. **"The Thing is procedure, not a planner."** It's deterministic code, the court's rules of procedure: whose turn it is, sealed submissions, *"did each side attack at least once"*, limits. It never reads content or picks actions. Odin doesn't command anyone. He issues rulings and lists deficiencies, and Heimdall decides how to re-search.
3. **"LLMs argue, code computes."** Exposure = a deterministic function of cited parameters (route share, severity, duration, reroute offset). Thor and Loki argue the parameters. Odin's rulings decide which bounds survive. The calculator turns them into his range. That's how we stay calibrated instead of just safe.
4. **"Grounding is enforced, not requested."** Every citation is checked against the manifest, the as-of date, retractions as of that date, event match and an exact quote match. The as-of firewall sits in the tool layer, so agents can't see the future even if they try, and that includes memory, through `knowledge_date`.
5. **"Robust by construction."** Per-file safe ingestion with quarantine, per-row trade quality flags, a guard chain on every tool call (dedupe, budgets, timeouts), and graceful failure paths ("Thor failed, Loki went unchallenged"). Status is never COMPLETE unless it's true.
6. **"Fully traceable."** An event-sourced log, artifacts per run, and a deterministic replay.

## 3. Likely judge questions

| Question | Answer |
|---|---|
| "Isn't your state machine a central planner?" | It makes no content decisions: no LLM, no query choice, no task assignment. It's the equivalent of court procedure. Remove it and the rules (simultaneous submission, mandatory attack, limits) can't be enforced. Every decision about *what* to do belongs to an agent. |
| "How does Odin send a case back without being a boss?" | He states deficiencies ("no post-event trade data for X/Y"). There's no channel to instruct agents. Heimdall reads the deficiencies and chooses his own queries. |
| "How do you stop hallucinated citations?" | Quotes must match the doc (fuzzy ≥ 90), IDs must be in the manifest, and the tool layer fills in metadata. The briefing validator drops anything not in the rulings. |
| "Why is Odin's range narrower than the hull?" | Intersection where both honest sides overlap, rejected or conceded bounds removed, observed trade data as an anchor. Wide only where the evidence really conflicts, and then it's marked Contested. |
| "What happens with a retraction after the as-of date?" | It's ignored. The original counts as valid as of that date. Relations are as-of aware. |
| "How is memory private?" | Separate SQLite files owned by each agent object, with no cross-agent tool. The dashboard inspector is read-only and for humans only. |
| "Could memory leak the future?" | Factual memories carry `knowledge_date` and are only recalled if it's on or before the current as-of date. Procedural lessons carry no facts. |
| "What if the LLM provider goes down?" | Retries, then switch `LLM_PROVIDER` in `.env` (OpenAI ↔ Anthropic) and restart. Cases already saved replay without an LLM. |
| "How do you avoid duplicate queries?" | Fingerprint ledger per case across agents and runs. Duplicates are never executed and are logged. |

## 4. Final-round runbook

### T−1 day
- [ ] `make test && make chaos && cli.py eval --events all` all green
- [ ] Rehearsal with 2 hold-out packs passes (EVALUATION.md §4), < 6 min per case
- [ ] API keys for **both** providers in `.env`, with credit checked
- [ ] `memory/` kept from rehearsal runs (lasting memory), with a backup copy saved
- [ ] Laptop: power, no sleep, hotspot as network backup

### T−30 min
- [ ] `make up` (backend + frontend). `GET /health` → embeddings_ready true, docs count correct
- [ ] Clear old `runs/` from view (archive them, don't delete)
- [ ] Open the dashboard, Ingestion tab ready

### For each of the 5 packs
1. Drop the pack folder/zip into `data/live_packs/` (or upload in the UI). **Don't touch code or the index by hand.**
2. Watch the ingestion report: note quarantined files and flagged rows (say them out loud to the judges).
3. Paste the OC's question + as-of date → Open case.
4. While it runs, narrate the phases. If a limit is hit, show that it's handled.
5. When the briefing is ready, check that its status and citations include pack docs → `cli.py export <case_id> --zip`.
6. Save the case ID in a running list (`final_round_cases.txt`).

### If something breaks
| Symptom | Action |
|---|---|
| LLM 429 / 5xx storms | Switch `LLM_PROVIDER`, restart the backend (cases are independent) |
| Ingestion stuck on a file | It times out by itself (20 s). If the watcher is dead → `POST /ingest/scan` |
| Frontend frozen | Refresh. The SSE resumes from `lastSeq` |
| Backend crash | Restart. The old case is marked ABORTED, so re-open the question |
| Totally stuck | Show the last completed case in replay mode and explain the logs |

## 5. Submission checklist (PS §3.4)

- [ ] Public GitHub repo with README + `.env.example` (no real keys committed. Run a secret scan)
- [ ] Working demo (video backup recorded)
- [ ] Saved logs of one full case with **two runs** in `runs/<case_id>/` (committed or attached)
- [ ] Architecture diagram (in README + ARCHITECTURE.md, plus an exported PNG in `docs/img/`)
- [ ] Team ready to explain the diagram (§2 talk track)

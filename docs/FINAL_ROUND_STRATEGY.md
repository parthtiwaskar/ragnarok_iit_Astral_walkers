# Final round: strategy and runbook

Written against what is built and tested in this repo. Where something is **not yet verified with a real model**, it says so.

## 1. What the final round asks, and what answers it

| Requirement | Where it lives | Status |
|---|---|---|
| New event packs with **no code edits** | `POST /api/ingest`, dashboard upload, watched drop folder, `/api/ingest/scan` (`livepacks.py`, `corpus.ingest_pack`) | Tested offline: damaged and fake-event rehearsal packs |
| Damaged docs and tables | Encoding repair, duplicate and truncated-copy detection, empty-file quarantine, bad-row exclusion, cut-off table detection (`corpus.py`) | Tested offline |
| Fake events | Heimdall verifies the event before the trial; unverified → `REJECTED`, no trial | Tested offline with a scripted model |
| Injection attempts | Pattern scan on every tool result, content wrapped as untrusted data, flagged in the log | Tested offline |
| As-of clock | Parsed from the question (`dates.py`); enforced by the tool layer, not the model | Tested |
| 10 acceptance criteria | `python -m nine_realms.acceptance <case>` and `GET /api/cases/<id>/acceptance` | 9 of 10 pass in a scripted end-to-end run; #7 (dashboard) is checked by eye |
| Saved logs of one full **two-run** case | `runs/<case>/events.jsonl`, `case.json`, `briefing.md` | **Not done with a real model.** Blocked by the Anthropic usage cap |
| Architecture diagram | `docs/img/architecture.svg`, explained in `docs/ARCHITECTURE.md` | Done |
| Public repo, README, `.env.example` | `deploy/.env.example` lists every variable | Done; root README needs the final pointers (section 8) |

Evaluation criteria and our position:

| Criterion | What we do | Honest gap |
|---|---|---|
| Citation validity | Code checks every citation: ID in the manifest, dated on or before as-of, not retracted, quote found in the text. Odin cannot cite anything not on the Case File | Measured on one real run (46 of 46); re-measure on the final run |
| Range accuracy | Exposure is computed from the trade tables by a tool (`import_summary`), and Odin must justify any range that ignores it | We do not know the organisers' truth |
| Calibration | Ranges are as narrow as the evidence allows; a flow with no trade table gets "no data", never a guess (Tolan maize in E5) | Width is not yet tuned against a known answer |
| Trap handling | Retractions, rumours, injections, duplicates, truncation, bad rows, fake events, all shown in the Council view and the intake report | Real-model behaviour on a fresh pack is untested |
| Debate quality | Eight specific attack kinds, evidence-backed attacks, a guard against conceding sourced facts to weaker evidence, a scorecard per run | The enforced rules are tested with scripted models only |
| Reliability | Hard limits, provider errors end a trial as `INCOMPLETE`, never a crash, concurrency cap | A real two-run case has not completed since the limits were raised |
| Reproducibility | Every case stores its event log, as-of date, provider and limits; offline `dryrun` replays the whole pipeline | No one-command replay of a real model run (a model is not deterministic) |

## 2. The strategy in six decisions

**1. Fix the metric once, say it everywhere, and let the question override it.**
Default: exposure is the **positive % fall in the importing country's total imports of the product, from all origins, by trade value (USD)**, against the same months a year earlier, from the break to the as-of date. Reasons: the question asks how *exposed a country* is, not how much one route lost; gross route loss overstates exposure whenever other suppliers fill the gap; value avoids tonnage-versus-price disputes. Volume is reported beside it. If a final-round question names another measure ("volume", "share of supply", "route loss"), the prompts tell every agent to follow the question, and Heimdall writes the unit into the Case File so the briefing states it. Check the unit line of the first briefing of the day before trusting any range.

**2. Let tools settle the arithmetic; let agents argue only what the tools cannot.**
`import_summary` returns the net change per window (whole period, since the break, last 3 months), the loss on affected routes and the substitution on others. Thor and Loki argue the unsettled parts: how long the shock lasts, how much rerouting persists, how reliable a source is. Odin's range must account for the computed net figure or give a reason.

**3. Report absence, never invent.**
No trade table for a flow → Odin sets `unsupported` and the briefing says "n/a, no evidence". A missing number is a calibrated answer; a guessed one costs range accuracy and trust.

**4. Never trust a document for what it says about itself.**
Tier 4 sources can add colour but never carry a claim. Retracted and after-as-of documents fail citation checks. Instructions inside documents are data. Duplicates and truncated copies are flagged, and a truncated table is not used for a full-period figure.

**5. Make the debate earn its conclusions.**
Attacks must name a specific weakness and bring counter-evidence that bears on the claim. Defences need a document not already cited. Concession needs an explanation, and a Tier 1-2 fact cannot be conceded to weaker evidence without a warning. If 70% or more of attacked claims are conceded, the scorecard flags a collapse and Odin sees it. Read the scorecard after every real run: it is the first place a model that agrees too easily shows up.

**6. Keep the case bounded and cheap enough to repeat.**
Hard limits per case: 3 runs, 150 LLM calls, 2.5M tokens, 20 tool calls per turn. Measured on the one real Anthropic run that got furthest (before prompt caching): 51 LLM calls and about 780k tokens to reach the middle of run 2, so a full two-run case is roughly 1 to 1.5M uncounted-by-cache tokens. The old 1M default cap would have aborted a full case, so it is now 2.5M. Prompt caching now bills repeat reads of the system prompt and tool list at a tenth of the price.

## 3. Race-day runbook

**Night before**

1. New Anthropic key (the old one is capped until 1 Nov 2026 and was pasted in chat; **rotate it and the Groq key**). Set `ANTHROPIC_API_KEY` and `APP_ACCESS_TOKEN` in `deploy/.env` or the Render dashboard. Never commit them.
2. Raise limits for the day: `MAX_TRIALS_PER_DAY=60`, `TRIAL_COOLDOWN_SECONDS=0`, `MAX_CONCURRENT_TRIALS=2` (3 only if the key's rate limit allows; each trial sends large prompts).
3. Run the rehearsal below with the **real model** on a damaged pack, read the briefing and the acceptance report, and save the case as the submission log.
4. Open the deployed URL on a phone and a laptop; check the token prompt, the intake panel and the exposure table.
5. Keep one finished case loaded, so the demo never waits on an LLM.

**Rehearsal (no keys needed, about a second)**

```bash
python -m nine_realms.rehearsal --out /tmp/pack_e9 --kind damaged        # a synthetic pack with every damage type, plus the expected report
python -m nine_realms.rehearsal --out /tmp/pack_e8 --kind fake-event     # an event with only rumours: Heimdall must reject it
python -m nine_realms.dryrun --extra-pack /tmp/pack_e9 --db /tmp/dry.sqlite --out /tmp/dry   # scripted court over the real corpus; prints the 10 criteria
python -m pytest nine_realms/tests -q
```

The dry run is scripted. It proves the pipeline and the acceptance checker; it says nothing about how well a model argues. Never submit it as the two-run log.

**When a pack arrives**

1. Upload it (dashboard, Evidence page, Data intake, "Upload pack (.zip)"; or `POST /api/ingest`; or drop it in `LIVE_PACKS_DIR`). Open the intake report.
2. Read the report before asking anything: documents ingested, quarantined (with reasons), duplicates, repaired encodings, flagged trade rows, incomplete tables. If a Tier 1 trade table is cut off or quarantined, tell the team: the exposure numbers for that flow will say "no data".
3. Choose **Full trial** (Fast answer is one run and never sends a case back, so it fails criterion #8), then paste the question. The as-of date is read from the text ("As of 25 January 2027"); check the chip under the box shows the right date. If the question has no date, the pack's own as-of date applies.
4. Start the trial. Watch the live feed: Heimdall's verdict first. A fake event ends there as `REJECTED` and costs almost nothing.
5. When it finishes, open Briefing and Council. Check (a) the unit line, (b) every range has a rationale naming its window and metric, (c) the scorecard (concession under 70%, attacks specific), (d) the red flags match what the intake report found.
6. `GET /api/cases/<id>/acceptance` for the checklist; save `runs/<case>/` for the record.

**If something breaks**

| Symptom | Cause | Action |
|---|---|---|
| Status `INCOMPLETE`, reason mentions usage limits or rate limit | Provider cap | Switch key or provider; the case keeps its saved evidence; rerun |
| Status `INCOMPLETE`, "could not bring new evidence" | Odin asked for more and the corpus had nothing | Expected for thin packs; the briefing still lists what is missing |
| Status `ABORTED` | Server error | Read the server log; rerun; the case row stays for the record |
| Intake error for a pack | Unsafe or unreadable archive | The report says why; fix the zip; the rest of the corpus is untouched |
| Range far from the table | Metric mismatch | Read Odin's rationale for the window; check the unit line |

## 3a. Producing the submission log (the one real two-run case)

One command does it all:

```bash
# 1. put ONE key in .env: DEEPSEEK_API_KEY, ANTHROPIC_API_KEY or OPENAI_API_KEY (NINE_REALMS_PROVIDER if several). Groq's free tier cannot finish a case.
# 2. raise the per-case cap if the provider reports cost: MAX_CASE_USD in .env, or --max-usd below
python -m nine_realms.realrun --max-usd 6
```

It checks the key with one tiny request, runs the sample question as a **Full trial** (the Fast answer mode is one run and can never meet criterion #8), prints the debate as it happens,
then checks the saved case. It writes `runs/<case_id>/` (`case.json`, `events.jsonl`, `briefing.md`, `acceptance.json`) **only if** the case is COMPLETE, has at least two runs
and no failed criterion. Otherwise it says exactly what is missing, writes nothing, and exits non-zero, so a half-finished case cannot be mistaken for the log.
Commit the folder (it holds no keys). After it succeeds, read the briefing's unit line and the debate scorecard by eye.

The same result can be had by hand: Ask screen, **Full trial**, then `python -m nine_realms.acceptance <case_id>` (expect 9 passed, 1 manual) and `python -m nine_realms.export <case_id>`.

## 4. Cost and time budget

Measured once on Anthropic (pre-caching): a trial to the middle of run 2 took about 4.5 minutes and 51 LLM calls. Budget about 8 to 10 minutes and roughly 1.5M tokens per full two-run case until you have a measured number from a complete case. For five packs run sequentially that is under an hour; with `MAX_CONCURRENT_TRIALS=2` about half. Record the real numbers after the rehearsal and replace this paragraph.

## 5. What a judge should be able to verify in two minutes

1. Open a case, click a citation: the document, the paragraph and the trade row are shown.
2. Open the Council: attacks with counter-evidence, defences with new documents, concessions with reasons, rulings with a basis.
3. Open the memory inspector: Thor's memory holds the claim he conceded; in run 2 an event shows the system stopping him from re-arguing it without new evidence.
4. Upload a damaged pack: the intake report lists every problem; the trial does not use the damaged rows.
5. Ask a question about the fake event: the case ends `REJECTED` with Heimdall's reason.

## 6. Things not to claim

- That the ranges are accurate. We do not have the organisers' values.
- That a full two-run case ran on a real model, until one has.
- That memory "learns" in general. It does two concrete things: stops an agent re-arguing a conceded claim without new evidence, and carries lessons into recall.
- That search is semantic on the lightweight embedder (Render starter): say "fuzzy matching".

## 7. Risks, ordered by how much they can cost

1. **No real completed case yet.** Mitigation: run it first thing once the key works. If a run fails, the saved evidence and the briefing fallback still show the structure.
2. **Metric mismatch with the organisers' definition.** Mitigation: unit line in every briefing, the question overrides the default, both windows visible in the rationale.
3. **Real models agreeing too easily or attacking vaguely.** Mitigation: enforced rules and the scorecard; inspect after the first real run and tighten the prompts if needed.
4. **Rate limits with concurrent trials.** Mitigation: `MAX_CONCURRENT_TRIALS`, sequential fallback, no Groq free tier for the final.
5. **A pack layout we have not seen** (different manifest fields, new file types). Mitigation: the intake report names anything it could not read; unsupported files are quarantined, not fatal.

## 8. Submission checklist

- [ ] Public repo contains `nine_realms/`, the dashboard, `deploy/`, `docs/`, `requirements.txt`, `.env.example` (copy `deploy/.env.example` to the repo root as well)
- [ ] No key, token, `.env`, SQLite database or memory file committed (`git ls-files | grep -iE "env$|sqlite|key"` is empty apart from `.env.example`)
- [ ] README: what it is, how to run (`python -m nine_realms.server`), how to add a pack, how to read a briefing, the architecture diagram, known limits
- [ ] `runs/<case>/` for one full two-run case from a real model, with `events.jsonl`, `case.json`, `briefing.md`, and its acceptance report
- [ ] Architecture diagram committed as SVG (and PNG if the submission form needs it)
- [ ] The pitch script reviewed against section 6

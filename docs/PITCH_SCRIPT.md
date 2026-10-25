# Trial of the Nine Realms: Pitch Script

Written against what is actually built and running (the `nine_realms` agents, the Ragnarisk dashboard, the database/API, the Docker/Render deployment).
It replaces the demo beats in `docs/DEMO_SCRIPT.md` that describe features we do not have (see section 9).

---

## 1. The pitch in one breath

> "After a supply-chain shock, every tool gives you a summary. A summary can't tell you what's certain, what's disputed, or how wide the risk is.
> We built a **courtroom**: four AI agents argue opposite sides of the question, cross-examine each other with evidence, and a judge rules on every claim.
> What you get is a briefing where every sentence traces to a document, and every claim is marked **Established, Contested or Conceded**."

Tagline for slide one: **Not a summary. A trial.**

## 2. The five things to land (in this order)

1. **Adversarial by design.** Thor argues the exposure is high, Loki argues it's limited. They can't see each other's claims until both are in. A single model can't disagree with itself; two advocates with opposing mandates can.
2. **Grounded, not trusted.** Every citation is checked by code: the document exists, it's dated on or before the as-of date, it isn't retracted, and the quote is really in it. In our real run, 46 of 46 citations resolved, and none cited a retracted, after-as-of, Tier 4 or duplicate document.
3. **Built for a dirty corpus.** The pack is full of traps. We catch them and show them: a retraction (ORV-0043), tabloid rumours, an instruction aimed at the AI (ORV-0020), duplicate documents, a file in two encodings (ORV-0007), an empty file (ORV-0023).
4. **We grade our own debate.** Every run gets a scorecard (how many attacks were specific and evidence-backed, how many claims were conceded). It caught our own system's weakness, and we fixed it with enforced rules.
5. **It's a product, not a notebook.** A live dashboard, one database, an API, a hardened Docker image, and a Render blueprint, behind an access token with spending caps.

## 3. The 5-minute version

| Time | Show | Say |
|---|---|---|
| 0:00 | Title + the question | "As of 25 January 2027, how exposed are Midgard and Tolan to the end of the Orvan corridor agreement? Give a low and high range for wheat and maize." |
| 0:25 | **Overview**, trade chart (Orvan→Midgard wheat) | "This is what the trade tables say, before any AI. Imports break in September: down 44% over the half-year, 65% by October to December. The question is how much of that is real exposure after rerouting." |
| 1:00 | **Evidence explorer**: type *"whaet imprts Midgrad collapse"* | "Hybrid search: exact words plus meaning. A typo finds nothing by words and still finds the right documents by meaning." Then click the **timeline** bar for Sep 2026. |
| 1:30 | **Council**, Heimdall card | "Heimdall verifies the event is real before the trial starts, and flags what not to trust." Point at: ORV-0043 retracted, ORV-0020 injection, rumours. |
| 2:15 | Thor vs Loki columns, open one claim's citation | "Every claim carries a document ID and an exact quote. Click it: here is the sentence, here is the trade-table row." |
| 3:00 | Cross-examination + **debate scorecard** | "Attacks must name a specific weakness and cite evidence. Our first real run scored 0% specific and 75% conceded. The scorecard flagged it, so we changed the rules: evidence-backed attacks, no conceding a sourced fact to weaker evidence, and a 'revise' move." |
| 3:45 | Odin's rulings + the remand | "Odin rules on every claim as fact or inference. He wasn't satisfied: he lists what's missing, not orders. A second run brings new evidence." |
| 4:15 | **Briefing**: rating cards + three-views chart | "Low, Moderate, High, Severe per flow, with Thor's, Loki's and Odin's ranges on one axis. Tolan maize says 'No data' because there is no trade table, rather than a guess." |
| 4:50 | Deployed URL + token prompt | "It runs as a hardened container behind an access token, with daily trial limits, so it can't run up a bill." |

## 4. The architecture talk track (2 minutes, with the diagram)

1. **"Four agents, one case file, no boss."** Heimdall (gatekeeper), Thor (high), Loki (limited), Odin (judge). They share only the Case File. Each has its own prompt, its own tools and its own private memory.
2. **"The Thing is procedure, not a planner."** Plain code, no LLM. It decides turn order, sealed first round, limits and failure handling. It never decides content.
3. **"LLMs argue, code checks."** Citations are verified by code. The as-of date is enforced by the tool layer, so an agent cannot reach past it. Exposure estimates come from the trade tables through a computation tool.
4. **"Robust by construction."** Damaged files are quarantined with a reason, bad trade rows are excluded, tool calls are de-duplicated and budgeted, and every claim, query and ruling is logged to the database.
5. **"Deployable."** One SQLite database, a token-protected API, a Docker image with the embedding model baked in, and a Render blueprint.

## 5. Mapping to the evaluation criteria

| Criterion | What we can show | Honest caveat |
|---|---|---|
| Citation validity | 46 of 46 citations resolve; none to retracted, after-as-of, Tier 4 or duplicate documents | Measured on one run |
| Trap handling | Retraction, rumours, injection, duplicates, repaired encoding, quarantined file all surfaced in the Council page | A "fake event" rejection case was not run: the E5 pack has none |
| Quality of debate | The scorecard, the new enforced rules, 8 offline tests for them | Real-model behaviour under the new rules is **not yet verified** |
| Reliability | Provider outages end cleanly as INCOMPLETE, never a crash; hard limits on runs, tool calls, tokens | |
| Reproducibility | Every run is saved with its event log, as-of date, provider and limits; `dryrun` replays the whole pipeline offline | No one-command replay of a real model run |
| Range accuracy and calibration | Ranges shown with width and a Thor/Loki gap | We do not know the true values. Do **not** claim accuracy |
| Briefing and UI | Rating cards, three-views chart, citation click-through to the exact row, mobile layout | |

## 6. Questions judges will ask

- **"Isn't your orchestrator a central planner?"** It has no LLM and makes no content decisions: no queries, no assignments. It enforces procedure like a court clerk. Odin's remand lists deficiencies, never instructions.
- **"How do you stop hallucinated citations?"** The quote must appear in the document, the ID must be in the manifest, the date must be on or before the as-of date, and retracted documents are rejected. All checked in code, not by asking the model.
- **"Why did your agents concede so much?"** Honest answer: in our first real run, 6 of 8 attacked claims were conceded and attacks were vague. We measured that, then enforced evidence-backed attacks, a guard on conceding well-sourced facts, and a revise option. They are tested offline; the next real run will show whether they work.
- **"Is the range accurate?"** "We don't have the organisers' truth. We can show it brackets the trade data (Midgard wheat fell 44% over the half-year), and we are explicit where the evidence runs out (Tolan maize)."
- **"Can it handle new documents in the final round?"** Yes, live and with no code change: upload a pack zip (or drop it in a watched folder), and the intake report lists what was repaired, quarantined or excluded. New documents are searchable at once, and the as-of date is read from the question. We rehearsed it with synthetic packs that contain every damage type, but not yet with a real model on a pack we have never seen.
- **"What if the LLM provider fails?"** The trial ends INCOMPLETE with the reason; saved cases stay viewable; providers are switchable by environment variable.
- **"Is it safe to deploy?"** Token-protected API, refuses a public bind without a token, daily trial caps, cross-origin POSTs blocked, strict headers, unprivileged container.

## 7. Things not to say

- Don't claim the ranges are accurate, or that a full two-run case completed, unless you have run one by then.
- Don't show a "replay" command or a sealed-envelope animation: neither exists. Do not present the scripted dry run (`python -m nine_realms.dryrun`) as a model result: it proves the pipeline and the acceptance checker, not the quality of the debate.
- Don't show the aborted Groq runs, or any API key, token or `.env`.
- Don't call a search "semantic" on the Render starter plan if it runs the lightweight embedder: say "fuzzy matching".

## 8. Before you pitch (in this order)

0. Read `docs/FINAL_ROUND_STRATEGY.md` (metric decision, runbook, checklist).

1. **Complete one full two-run case** on Anthropic (the key is capped until 1 Nov, or raise the limit). It is the strongest proof, and it tests the new debate rules for the first time.
2. Rotate the Groq and Anthropic keys that were pasted in chat.
3. Deploy on Render and open the URL on a phone to check the mobile layout.
4. Record a 3-minute backup video of the demo, and keep one finished case loaded so the demo never waits on an LLM.
5. Rehearse with a timer; keep the 5-minute version and the 8-minute version both ready.

## 9. Beats in `docs/DEMO_SCRIPT.md` that we still cannot demo as written

Sealed-envelope animation, a Memory tab with a diff (we have a read-only memory inspector and a log of when memory changed an action), `cli.py replay/eval/export`, "Odin's band narrows from Run 1 to Run 2" (no completed real run 2 yet), a rumour question that returns CASE REJECTED on the E5 pack (use the generated fake-event pack: `python -m nine_realms.rehearsal --kind fake-event`). The "chaos zip drag-and-drop" now exists: use the Evidence page, Data intake panel.

## 10. Closing line

> "Most AI tools tell you what happened. This one puts the question on trial, shows you what survived cross-examination, and tells you exactly how sure it is. **Nothing in the briefing is there on trust.**"

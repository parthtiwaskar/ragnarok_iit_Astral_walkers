# SPLIT_PLAN — remaining work, cut for two people

Everything in P0–P7 is built and green (53 backend tests). What is left is the real-corpus run,
the API and dashboard gaps, and submission hardening. This file cuts that into four phases with
two tracks per phase, so **A** and **B** never edit the same file at the same time.

- **A — Data, corpus and eval.** Owns `backend/app/ingest/**`, `backend/app/store/**`,
  `backend/app/search/**`, `backend/app/llm/fake*.py`, `backend/cli.py`, `backend/tests/**`
  (except `test_api.py`), `data/**`, `runs/**`.
- **B — API surface, dashboard and demo.** Owns `backend/app/main.py`, `backend/tests/test_api.py`,
  `frontend/**`, the root `index.html` / `app.js` / `styles.css`, `README.md`, `DEMO_SCRIPT.md`.

Branches: A stays on `build/backend-core`, B works on `ui/dashboard-live`. Both merge into `main`
by PR. Nobody force-pushes. Sync points below are the only places the two tracks must meet.

---

## Phase 1 — make the real Runemark pack load clean (~½ day)

**A1 — finish the corpus loader.** (Started; edits already in the working tree for
`sniff.py`, `parsers/txt.py`, `pipeline.py` — continue those, do not restart.)
- [ ] `sniff.py` + `parsers/txt.py`: a UTF-16 BOM partway into a file splits into a UTF-8 header
      and a UTF-16 body → **REPAIRED**, not quarantined (ORV-0007).
- [ ] `pipeline.py`: resolve a manifest `file` that omits its subdirectory (`ORV-0023.txt` vs
      `docs/ORV-0023.txt`) via a single-match `rglob`, so headerless and empty files still get
      their manifest doc id. ORV-0023 then lands as **QUARANTINED EMPTY**, not MISSING.
- [ ] `pipeline.py`: skip pack furniture (`README*`, `*.sha256`, `sample_question*`, images) unless
      the manifest lists it — keeps `runemark_map.png` out of the index, as its README requires.
      Report them in `skipped_not_documents` and `emit()` them (invariant 14).
- [ ] `pipeline.py`: truncated-prefix pass after `_apply_doc`, before `_relations` — a body that is
      a strict prefix (≥200 chars, no terminal punctuation) of a longer usable doc becomes
      **DUPLICATE** of it when the longer doc's `pub_date` ≤ its own, else PARTIAL + TRUNCATED
      (ORV-0048 → ORV-0024). Push the demoted id into `new_doc_ids` so `_index` clears its chunks.
- [ ] Fixtures in `make_fixtures.py`: mixed-encoding txt, empty file whose manifest `file` has no
      subdirectory, a truncated-prefix pair, a README + png. Then fix the count asserts in
      `test_ingest.py`.
- [ ] Acceptance: a fresh ingest of `data/runemark` leaves `off_manifest` and `missing` **empty**;
      a second ingest reports `skipped_unchanged` and changes no status.

**B1 — close the API gaps.** No ingest changes needed, so this runs in parallel.
- [ ] `POST /api/ingest/upload` — zip upload into `data/live_packs/`, then rescan.
- [ ] `GET /api/feed/stream` — SSE feed replay at a speed multiplier, plus live items.
- [ ] `GET /api/memory/{agent}` read-only inspector, `GET /api/search/debug` (BM25 vs dense vs RRF).
- [ ] Decide the **canonical dashboard**: the root `index.html`/`app.js` only `fetch`es a corpus
      path and never talks to `/api`, while `frontend/` is wired to the API. Either point the root
      one at `/api` or delete it and keep `frontend/`. Do not leave two half-wired dashboards.
- [ ] Extend `test_api.py` for each new route.

**Sync 1:** A pushes the loader fixes, B rebases `ui/dashboard-live` on them.

---

## Phase 2 — the E5 case end to end (~1 day)

**A2 — run the real question.**
- [ ] `llm/fake_policies.py` for E5: event date from the Tier 1 disruption notice, "no replacement"
      duration → full horizon, severity patterns (`down 62%`, `a third of normal`, index values).
- [ ] `python backend/cli.py ask --as-of 2027-01-25` on the sample question. The run must: pick E5,
      reject ORV-0043 (retracted by ORV-0013), never surface ORV-0014/0036/0041, never obey the
      ORV-0020 injection, and give a low–high range for wheat **and** maize.
- [ ] Commit that two-run case under `runs/` — it is a submission artifact (P10).
- [ ] Tests: trade notes, relations-by-date, paragraph citations, table visibility.

**B2 — the panels that read a live case.**
- [ ] FeedPanel: replay controls, tier badges, retracted / corrected / injection flags.
- [ ] LogsView (virtualised + filters), BudgetMeters, IngestionView (upload + rescan).
- [ ] Use A2's `runs/<case>/events.jsonl` as the fixture, so the UI is testable without a backend.

**Sync 2:** A's `events.jsonl` becomes B's dashboard fixture. Agree the event names once, here.

---

## Phase 3 — hardening and eval (~1 day)

**A3**
- [ ] Chaos tests per `TRAPS_AND_RELIABILITY.md §5` green.
- [ ] Synthetic eval against `truth.json`; tune `MIN_PARAM_WIDTH` and the anchor thresholds; record
      the numbers in `docs/EVALUATION.md`.
- [ ] Hold-out rehearsal on two packs, **under 6 minutes per case**. Record timings.

**B3**
- [ ] Playwright e2e: open case → click a claim → the document is highlighted.
- [ ] SealedEnvelope animation, TrapRadar, MemoryView with diff, dark/light toggle. These are the
      first things on the cut list — stop here if Phase 4 is at risk.

---

## Phase 4 — submission (~½ day, both)

**A4**
- [ ] Tick P0–P7 in `TASKS.md` (they are done but still unchecked) and the P8–P10 boxes as they land.
- [ ] Note the spec deviations in the matching doc, each in one line: hash-embedder fallback, the
      token-budget rule that charges context growth, Odin's two-step judging, final range computed
      in code, trade notes as derived tables, paragraph-level citations, the year-on-year anchor.
- [ ] `docs/img/architecture.png`.

**B4**
- [ ] README polish + team table, `DEMO_SCRIPT.md` rehearsal, demo video backup.
- [ ] One `make lint test` on a clean clone before the final PR.

---

## Rules that hold for both tracks

1. The 16 invariants in `CLAUDE.md` are not negotiable — four LLM agents, as-of firewall in the
   tool layer, numbers from code, never claim COMPLETE falsely.
2. Document text is untrusted. The ORV-0020 injection must never be obeyed; it is only ever flagged.
3. Never commit `.env` or a key. Scan the diff before pushing.
4. `runemark_map.png` is human reference only — it must never reach an agent.
5. End of each day: one `cli.py ask` on fixtures with FakeLLM **and** one on the real pack; commit
   the `runs/` output; note regressions at the bottom of `TASKS.md`.

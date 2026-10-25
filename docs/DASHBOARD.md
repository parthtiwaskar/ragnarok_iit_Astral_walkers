# Dashboard Spec (React)

FR7 + acceptance #7: **question box, live feed, trial view, exposure table, click-through to sources**, all on one screen. The dashboard is a **projection of the case event stream** (SSE). It never computes anything the backend hasn't logged.

Stack: Vite + React 18 + TypeScript (strict) + Tailwind + Zustand (state) + TanStack Query (REST) + `react-markdown` (briefing) + plain SVG for range bars (no chart lib needed).

## 1. Layout

```
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ CASE BAR  [ Ask a question about a disruption…                    ] [as-of 📅] [Open case] │
│ ●Heimdall ─ ●Round 1 (sealed) ─ ●Cross-exam ─ ●Odin ─ ●Remand/Briefing   Run 2/3   ⛔limits │
│ banner: VERIFIED / CASE REJECTED (why) / INCOMPLETE (reasons)                              │
├───────────────┬──────────────────────────────────────────────────────────────────────────┤
│ LIVE FEED     │  TABS:  [Trial]  [Exposure]  [Briefing]  [Logs]  [Ingestion]  [Memory]     │
│ (replay ▶ ⏸ 2x)│                                                                          │
│ 2025-03-02 T1 │  TRIAL VIEW (Run tabs R1 | R2)                                            │
│ Canal notice  │  ┌──── THOR (high) ─────┐  ┌──── ODIN ────┐  ┌──── LOKI (limited) ────┐   │
│ 2025-03-04 T4 │  │ R1-T-C01  sourced    │  │ UPHELD       │  │ R1-L-C01  inference     │   │
│ ⚠ rumour…     │  │ route_share 0.55–0.65│  │ ESTABLISHED  │  │ duration 0.5–1.5        │   │
│ 2025-03-06 T2 │  │ [RM-DOC-0101] [TT-17 │  │ rationale…   │  │ [RM-DOC-0133]           │   │
│ ~~retracted~~ │  │  r08]                │  │              │  │  ↳ attacked by R1-T-A01 │   │
│ …             │  │  ↳ attacked by L-A01 │  │ REJECTED     │  │    ↳ DEFEND (new ev.)   │   │
│               │  │    ↳ CONCEDE         │  │ CONCEDED     │  │                          │   │
│               │  └──────────────────────┘  └──────────────┘  └─────────────────────────┘   │
│               │  Trap radar: rumours 1 · retractions 2 · injections 1 · bad rows 3         │
└───────────────┴──────────────────────────────────────────────────────────────────────────┘
                         SOURCE DRAWER (slides in from right on any citation click)
```

On narrow screens the feed collapses into a tab and Thor/Odin/Loki stack vertically.

## 2. Components

| Component | Purpose | Data |
|---|---|---|
| `CaseBar` | question input, as-of date picker (required), open button, phase pipeline, run badge, status banner | `POST /cases`, SSE `phase_changed` |
| `BudgetMeters` | tool calls, tokens, runs vs limits, plus `duplicate_blocked` count | SSE `budget`, `/config/limits` |
| `FeedPanel` | live replay of Runemark docs ≤ as-of: date, outlet, tier badge, type, event tag, flags (retracted strikethrough, corrected, injection ⚠, background, live-ingested ✨). Play/pause/speed. Click → SourceDrawer | `/feed/stream` SSE |
| `TrialView` | run tabs. Three columns: Thor claims, Odin rulings aligned per claim, Loki claims | case store |
| `SealedEnvelope` | during R1_SUBMIT shows "Thor has committed 🔒" without content, then a reveal animation | SSE `sealed_commit` |
| `ClaimCard` | id, statement, SOURCED_FACT (solid) / INFERENCE (dashed), parameter + bound, country/sector, evidence chips, `unchallenged` flag, memory-influence icon | `claim_submitted` |
| `CrossExamThread` | attacks on a claim with their type, and the response (DEFEND/REVISE/CONCEDE) with new evidence chips | `attack_filed`, `response_filed` |
| `RulingBadge` | verdict + finding colours (Established green, Contested amber, Conceded grey, Rejected red) + expandable rationale + citation issues | `ruling_issued` |
| `ExposureTable` | row per (country, sector): Loki range, Thor range, Odin final range, unit, value at risk, data-quality icon, contested params | `/cases/{id}/exposure`, `final_range` |
| `RangeBar` | one SVG axis per row: Loki band, Thor band, Odin band (bold), observed-anchor tick | same |
| `BriefingView` | rendered markdown with `CitationChip` replacing `[ID, date]` patterns. Status banner. Download md / zip | `/briefing` |
| `CitationChip` | colour by validator state (valid / warning / invalid). Click → SourceDrawer | — |
| `SourceDrawer` → `DocViewer` | metadata header table, status banner (retracted/corrected/quarantined/partial/injection), body with highlighted quote span, related docs | `/docs/{id}?case_id&highlight` |
| `SourceDrawer` → `TradeTableViewer` | header, rows with quality colouring, highlighted row, hidden future rows greyed "not visible as of date" | `/trade/{id}?case_id&row` |
| `LogsView` | virtualised event list, filters by actor/type/run, search. Memory events highlighted purple. Click an event → JSON | SSE all |
| `IngestionView` | counts by format/status, quarantine table with reasons, flagged trade rows, upload pack (zip) button, rescan button | `/ingest/*` |
| `MemoryView` | per-agent memory list + diff for this case + list of `memory_influence` events with links to the action | `/memory/{agent}?case_id` |
| `TrapRadar` | counters for each trap type. Click → filtered logs | SSE `trap_detected` etc. |
| `SearchDebug` (dev drawer) | run a hybrid search with filters, to demo acceptance #2 | `/search` |

## 3. State management

```ts
// store/caseStore.ts
type CaseState = {
  caseId?: string; phase: Phase; run: number; status: Status; statusReasons: string[];
  verification?: Verification;
  claims: Record<string, Claim>; attacks: Record<string, Attack>; responses: Record<string, Response>;
  rulings: Record<string, Ruling>;            // keyed by claim_id → latest ruling
  positions: Position[]; finalRanges: FinalRange[];
  sealed: { thor: boolean; loki: boolean };
  traps: TrapEvent[]; memoryEvents: MemoryEvent[]; budget: Budget; events: LogEvent[]; lastSeq: number;
};
function reduce(state: CaseState, ev: LogEvent): CaseState { switch (ev.type) { /* one case per event type */ } }
```

- `useCaseStream(caseId)` opens an `EventSource` on `/cases/{id}/events?since=lastSeq`, and on reconnect resumes from `lastSeq`.
- Reducers are pure. Replaying `events.jsonl` offline gives an identical UI, which is handy for demo fallback ("replay mode": load a saved case with no backend LLM calls).

## 4. Visual language (Norse theme, accessible)

| Element | Colour | Notes |
|---|---|---|
| Thor | crimson `#DC2626` / amber accents | ⚡ icon |
| Loki | emerald `#059669` | 🐍-style icon (custom SVG, no copyrighted art) |
| Odin | gold `#CA8A04` | 👁 icon |
| Heimdall | teal/rainbow gradient (Bifröst) | 🌈 bridge on the pipeline |
| Established / Contested / Conceded / Rejected | green / amber / slate / red | always paired with a text label, never colour alone |
| Tier badges | T1 solid, T2 outline, T3 dotted, T4 striped | |

Dark background (slate-950), Inter or IBM Plex Sans, monospace for IDs. WCAG AA contrast and keyboard-navigable drawers.

## 5. Interactions that win demos

1. **Click-through everywhere:** claim chip → doc with highlighted quote, and trade chip → highlighted row. Briefing chips behave the same.
2. **Sealed reveal:** both envelopes lock, then flip open together.
3. **Remand moment:** Odin's deficiencies appear as a scroll card. Heimdall's new evidence items pulse in the feed with "added in Run 2".
4. **Range convergence:** in the Exposure tab, a run toggle animates Odin's band narrowing from Run 1 to Run 2.
5. **Chaos drop:** drag a broken zip onto the Ingestion view and watch quarantine entries appear live while the trial continues.

## 6. Acceptance checklist (UI)

- [ ] Question box + as-of date open a case
- [ ] Live feed replays docs ≤ as-of, including live-ingested ones
- [ ] Trial view shows Thor and Loki side by side with Odin's rulings
- [ ] Exposure table shows low/high per country × sector for Loki, Thor and Odin
- [ ] Every claim, attack, response and briefing citation is clickable to its doc or trade row
- [ ] Status banner shows INCOMPLETE reasons, unchallenged sides and rejected cases

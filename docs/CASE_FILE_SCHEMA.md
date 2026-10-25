# Case File and Data Contracts

The **Case File** is the only shared state between agents (FR6.1). It is a versioned pydantic model, written to `runs/<case_id>/run_<n>/case_file.json` at the end of every phase. Code: `backend/app/trial/casefile.py`.

## 1. ID conventions

| Object | Format | Example |
|---|---|---|
| Case | `CASE-<yyyymmdd>-<4 hex>` | `CASE-20261025-a1f3` |
| Run | `R<n>` | `R2` |
| Claim | `R<n>-<T\|L>-C<nn>` | `R1-T-C03` |
| Attack | `R<n>-<T\|L>-A<nn>` (side filing it) | `R1-L-A01` |
| Response | `R<n>-<T\|L>-D<nn>` | `R1-T-D02` |
| Ruling | `RUL:<claim_id>:R<n>` | `RUL:R1-T-C03:R2` |
| Deficiency | `R<n>-DEF<nn>` | `R1-DEF02` |
| Document | Runemark permanent ID (as in manifest) | `RM-DOC-0142` |
| Trade row | `<table_id>#r<row>` | `TT-017#r08` |
| Memory item | `<agent>-M<nnnn>` | `thor-M0031` |
| Tool call | `<case_id>:<seq>` | `CASE-20261025-a1f3:0057` |

> Runemark's real ID formats come from the manifest. The examples above are placeholders. Country, sector and doc names in any example in these docs are invented placeholders too, not Runemark data.

## 2. Pydantic models (reference implementation)

```python
from datetime import date, datetime
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field

# ---------- evidence ----------
class DocEvidence(BaseModel):
    kind: Literal["doc"] = "doc"
    doc_id: str
    quote: str                         # exact text copied from the document (≤ 400 chars)
    pub_date: date                     # filled by tool layer, not the LLM
    tier: int                          # 1..4, filled by tool layer
    event_id: str                      # filled by tool layer
    off_runemark: bool = False         # always False in practice (no web tools); safety label

class TradeEvidence(BaseModel):
    kind: Literal["trade"] = "trade"
    table_id: str
    row: int
    month: str                         # YYYY-MM
    value: float | None
    volume: float | None
    quality: Literal["OK","MISSING_VALUE","DUPLICATE_ROW","BAD_NUMBER","TRUNCATED"]

Evidence = DocEvidence | TradeEvidence

class CitationCheck(BaseModel):        # produced by verify_citation (deterministic)
    evidence_ref: str                  # doc_id or table_id#rN
    valid: bool
    issues: list[Literal[
        "NOT_IN_MANIFEST","AFTER_AS_OF","RETRACTED_AS_OF","CORRECTED_AS_OF","WRONG_EVENT",
        "BACKGROUND_ONLY","QUOTE_MISMATCH","QUARANTINED","DAMAGED_ROW","OFF_RUNEMARK"]] = []
    quote_similarity: float | None = None
    superseded_by: str | None = None   # correction doc id, if any (as-of aware)

# ---------- scope & verification ----------
class ScopeRow(BaseModel):
    country: str
    sector: str
    trade_tables: list[str]            # table ids used for this row
    baseline_value: float | None       # mean of clean pre-event months, from trade_stats
    unit: str

class Verification(BaseModel):
    verdict: Literal["VERIFIED","UNCONFIRMED","REFUTED","RETRACTED","NOT_YET_KNOWN","NOT_FOUND"]
    rule_verdict: str                  # deterministic suggestion from event_profile
    reasons: list[str]
    supporting_docs: list[str]
    contradicting_docs: list[str]
    retractions_applied: list[str]
    alternatives: list[str] = []       # other candidate event_ids

class Scope(BaseModel):
    event_id: str
    event_summary: str                 # sourced, with citations inline
    event_date: date | None
    chokepoints: list[str]
    rows: list[ScopeRow]
    horizon_start: date
    horizon_end: date                  # next quarter after as_of
    metric: Literal["pct_of_baseline_trade_value"] = "pct_of_baseline_trade_value"

class PoolItem(BaseModel):
    ref: str                           # doc_id or table_id
    kind: Literal["doc","trade_table"]
    added_in_run: int
    added_by: Literal["heimdall"]
    tier: int | None
    pub_date: date | None
    event_id: str | None
    status: Literal["OK","REPAIRED","PARTIAL","DUPLICATE","QUARANTINED","RETRACTED_AS_OF","CORRECTED_AS_OF"]
    flags: list[str] = []              # e.g. INJECTION_DETECTED, BACKGROUND

# ---------- debate ----------
Parameter = Literal["route_share","severity","duration_months","reroute_offset"]

class Claim(BaseModel):
    id: str
    run: int
    side: Literal["thor","loki"]
    statement: str
    type: Literal["SOURCED_FACT","INFERENCE"]
    premises: list[str] = []           # for INFERENCE: ids of claims/evidence it rests on
    country: str | None
    sector: str | None
    parameter: Parameter | None        # exposure parameter this claim supports
    bound_value: float | None          # the value this claim argues for
    evidence: list[Evidence] = Field(min_length=1)
    supersedes: str | None = None
    memory_influence: list[dict] = []  # [{memory_id, effect}]
    unchallenged: bool = False         # set by the Thing if the other side failed
    status: Literal["ACTIVE","CONCEDED","SUPERSEDED"] = "ACTIVE"

class Position(BaseModel):             # one per side per scope row per run
    side: Literal["thor","loki"]
    country: str
    sector: str
    params: dict[Parameter, tuple[float, float]]   # (low, high) per parameter
    param_claims: dict[Parameter, list[str]]       # claim ids backing each parameter
    low: float                         # from compute_exposure
    high: float
    headline: float                    # thor = high, loki = low
    unit: str = "pct_of_baseline_trade_value"
    calc_id: str                       # compute_exposure call id (reproducible)

class Attack(BaseModel):
    id: str
    run: int
    by: Literal["thor","loki"]
    target_claim: str
    type: Literal["SOURCE_RELIABILITY","RETRACTED_OR_CORRECTED","WRONG_EVENT","AFTER_AS_OF",
                  "QUOTE_MISMATCH","COUNTER_EVIDENCE","LOGIC_GAP","NUMERIC"]
    argument: str
    evidence: list[Evidence] = []

class Response(BaseModel):
    id: str
    run: int
    by: Literal["thor","loki"]
    attack_id: str
    action: Literal["DEFEND","REVISE","CONCEDE"]
    argument: str
    new_evidence: list[Evidence] = []  # DEFEND requires ≥1 item not previously cited by the claim
    revised_bound: float | None = None

# ---------- judgment ----------
class Ruling(BaseModel):
    id: str
    claim_id: str
    run: int
    verdict: Literal["UPHELD","PARTIALLY_UPHELD","REJECTED","CONCEDED","NO_RULING"]
    basis: Literal["SOURCED_FACT","INFERENCE","UNSUPPORTED"]
    finding: Literal["ESTABLISHED","CONTESTED","CONCEDED","REJECTED"]
    rationale: str
    decisive_evidence: list[str]
    citation_issues: list[CitationCheck] = []
    memory_influence: list[dict] = []

class FinalRange(BaseModel):
    country: str
    sector: str
    low: float
    high: float
    unit: str
    params: dict[Parameter, tuple[float, float]]
    supporting_rulings: list[str]
    observed_anchor: float | None      # observed post-event change, if visible
    justification: str
    calc_id: str

class Deficiency(BaseModel):
    id: str
    check: str                         # S1..S9
    statement: str                     # "No post-event trade evidence for …" — never an instruction
    related: list[str] = []            # claim/row/doc ids

class Sufficiency(BaseModel):
    verdict: Literal["SUFFICIENT","INSUFFICIENT"]
    checklist: dict[str, bool]         # S1..S9 → pass/fail
    deficiencies: list[Deficiency] = []

class ResearchReport(BaseModel):       # Heimdall's answer to a remand
    run: int
    queries: list[str]
    per_deficiency: dict[str, Literal["ADDRESSED","PARTIAL","NO_EVIDENCE_FOUND"]]
    new_items: list[str]
    memory_influence: list[dict] = []

# ---------- run & case ----------
class SideStatus(str, Enum):
    OK = "OK"; FAILED = "FAILED"; FAILED_TO_ATTACK = "FAILED_TO_ATTACK"

class Run(BaseModel):
    n: int
    started_at: datetime
    claims: list[Claim] = []
    positions: list[Position] = []
    attacks: list[Attack] = []
    responses: list[Response] = []
    rulings: list[Ruling] = []
    final_ranges: list[FinalRange] = []
    sufficiency: Sufficiency | None = None
    research: ResearchReport | None = None   # the research that fed THIS run (n ≥ 2)
    side_status: dict[str, SideStatus] = {"thor": "OK", "loki": "OK"}
    sealed: dict[str, bool] = {"thor": False, "loki": False}

class CaseFile(BaseModel):
    case_id: str
    version: int                       # increments on every write
    question: str
    as_of: date
    opened_at: datetime
    phase: str                         # Thing state
    index_version: str                 # manifest+index hash at open
    scope: Scope | None = None
    verification: Verification | None = None
    pool: list[PoolItem] = []
    data_quality: list[dict] = []      # damaged docs / rows touching scope
    traps: list[dict] = []             # injections, rumours, retractions, look-alikes detected
    runs: list[Run] = []
    status: Literal["OPEN","COMPLETE","INCOMPLETE","REJECTED","ABORTED"] = "OPEN"
    status_reasons: list[str] = []
    briefing_path: str | None = None
```

## 3. View filtering

`casefile.view_for(agent, phase)` returns a **copy** with:
- the opponent's claims **removed** while `phase == R1_SUBMIT` and their `sealed` flag is set (commit-reveal)
- no memory content of any agent (memory is never on the Case File)
- for Odin in `BRIEFING`: only the rulings digest (see §5)

## 4. Effective rulings

`effective_rulings(cf)` = for each claim chain (following `supersedes`), the latest ruling. The briefing and the final exposure table use only effective rulings from the **last run**, plus carried-forward claims' latest rulings.

## 5. Rulings digest (input to the briefing)

```json
{
  "case_id": "CASE-20261025-a1f3",
  "as_of": "2025-03-20",
  "status": "COMPLETE",
  "verification": {"verdict": "VERIFIED", "reasons": ["..."], "docs": ["RM-DOC-0101"]},
  "established": [{"ruling": "RUL:R1-T-C01:R1", "statement": "...", "citations": [{"ref": "RM-DOC-0101", "date": "2025-03-02"}]}],
  "contested":   [{"ruling": "...", "thor": {...}, "loki": {...}, "why": "..."}],
  "conceded":    [{"ruling": "...", "by": "loki", "statement": "...", "citations": [...]}],
  "rejected":    [{"ruling": "...", "why": "cited doc retracted as of 2025-03-15"}],
  "final_ranges": [{"country": "...", "sector": "...", "low": 6.0, "high": 11.5, "unit": "%"}],
  "data_quality": ["TT-017#r09 BAD_NUMBER excluded", "RM-DOC-0177 quarantined: empty"],
  "traps": ["RM-DOC-0150 contained embedded instructions; ignored"],
  "failures": []
}
```

## 6. Briefing (output)

Saved as `briefing.md` and `briefing.json`:

```
# Briefing — <question>
Case CASE-… · As of YYYY-MM-DD · Runs: 2 · Status: COMPLETE | INCOMPLETE (reasons)

## Executive summary              ← Odin (cited)
## Event verification             ← code from Verification + Odin one-liner
## Established findings           ← Odin (cited)
## Contested findings             ← Odin (both sides cited)
## Conceded findings              ← Odin (cited)
## Exposure table                 ← code: country | sector | Loki range | Thor range | Odin range | unit | basis rulings
## Next-quarter outlook           ← Odin: narrative + assumptions A1..An, each → ruling id + citation
## Data quality & limitations     ← code: damaged docs/rows, unchallenged sides, limits hit, injections ignored
## Rejected claims (appendix)     ← code
## Sources                        ← code: id | title | outlet | tier | date | status
## Trace                          ← code: case file version, rulings file, events.jsonl, prompt hashes, index_version
```

Inline citation formats: `[RM-DOC-0101, 2025-03-02]` and `[TT-017 r08, 2025-02]`. The dashboard turns them into clickable chips.

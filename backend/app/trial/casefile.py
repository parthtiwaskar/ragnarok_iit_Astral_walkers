"""The Case File: the only shared state between agents (CASE_FILE_SCHEMA.md).

All Case File data contracts live in this module. Do not duplicate these shapes elsewhere.
"""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

AgentName = Literal["heimdall", "thor", "loki", "odin"]
Side = Literal["thor", "loki"]
SIDES: tuple[Side, Side] = ("thor", "loki")
SIDE_LETTER: dict[str, str] = {"thor": "T", "loki": "L"}

Quality = Literal["OK", "MISSING_VALUE", "DUPLICATE_ROW", "BAD_NUMBER", "TRUNCATED"]
CitationIssue = Literal[
    "NOT_IN_MANIFEST",
    "AFTER_AS_OF",
    "RETRACTED_AS_OF",
    "CORRECTED_AS_OF",
    "WRONG_EVENT",
    "BACKGROUND_ONLY",
    "QUOTE_MISMATCH",
    "QUARANTINED",
    "DAMAGED_ROW",
    "OFF_RUNEMARK",
]
#: issues that do not block a citation (warnings only)
WARNING_ISSUES: frozenset[str] = frozenset({"CORRECTED_AS_OF", "BACKGROUND_ONLY"})

Parameter = Literal["route_share", "severity", "duration_months", "reroute_offset"]
PARAMETERS: tuple[Parameter, ...] = ("route_share", "severity", "duration_months", "reroute_offset")

AttackType = Literal[
    "SOURCE_RELIABILITY",
    "RETRACTED_OR_CORRECTED",
    "WRONG_EVENT",
    "AFTER_AS_OF",
    "QUOTE_MISMATCH",
    "COUNTER_EVIDENCE",
    "LOGIC_GAP",
    "NUMERIC",
]
#: attack types that must cite their own evidence
EVIDENCE_REQUIRED_ATTACKS: frozenset[str] = frozenset({"COUNTER_EVIDENCE", "NUMERIC"})

Verdict = Literal["VERIFIED", "UNCONFIRMED", "REFUTED", "RETRACTED", "NOT_YET_KNOWN", "NOT_FOUND"]


class Phase(StrEnum):
    OPENED = "OPENED"
    GATHERING = "GATHERING"
    CASE_REJECTED = "CASE_REJECTED"
    R1_SUBMIT = "R1_SUBMIT"
    R1_REVEAL = "R1_REVEAL"
    R2_ATTACK = "R2_ATTACK"
    R2_RESPOND = "R2_RESPOND"
    JUDGING = "JUDGING"
    REMANDED = "REMANDED"
    RESEARCH = "RESEARCH"
    BRIEFING = "BRIEFING"
    CLOSED = "CLOSED"
    ABORTED = "ABORTED"


# ---------- evidence ----------
class DocEvidence(BaseModel):
    kind: Literal["doc"] = "doc"
    doc_id: str
    quote: str = Field(max_length=400)  # exact text copied from the document
    pub_date: date  # filled by tool layer, not the LLM
    tier: int  # 1..4, filled by tool layer
    event_id: str  # filled by tool layer
    off_runemark: bool = False  # always False in practice (no web tools); safety label

    @property
    def ref(self) -> str:
        return self.doc_id


class TradeEvidence(BaseModel):
    kind: Literal["trade"] = "trade"
    table_id: str
    row: int
    month: str  # YYYY-MM
    value: float | None
    volume: float | None
    quality: Quality

    @property
    def ref(self) -> str:
        return f"{self.table_id}#r{self.row:02d}"


Evidence = Annotated[DocEvidence | TradeEvidence, Field(discriminator="kind")]


class CitationCheck(BaseModel):  # produced by verify_citation (deterministic)
    evidence_ref: str  # doc_id or table_id#rN
    valid: bool
    issues: list[CitationIssue] = []
    quote_similarity: float | None = None
    superseded_by: str | None = None  # correction doc id, if any (as-of aware)


# ---------- scope & verification ----------
class ScopeRow(BaseModel):
    country: str
    sector: str
    trade_tables: list[str]  # table ids used for this row
    baseline_value: float | None  # mean of clean pre-event months, from trade_stats
    unit: str
    route_flags: dict[str, float] = {}  # table_id -> share of that table's flow on the disrupted route


class Verification(BaseModel):
    verdict: Verdict
    rule_verdict: str  # deterministic suggestion from event_profile
    reasons: list[str]
    supporting_docs: list[str]
    contradicting_docs: list[str]
    retractions_applied: list[str]
    alternatives: list[str] = []  # other candidate event_ids
    disagreement: str | None = None  # Heimdall's reason when he departs from the rule verdict


class Scope(BaseModel):
    event_id: str
    event_summary: str  # sourced, with citations inline
    event_date: date | None
    chokepoints: list[str]
    rows: list[ScopeRow]
    horizon_start: date
    horizon_end: date  # next quarter after as_of
    metric: Literal["pct_of_baseline_trade_value"] = "pct_of_baseline_trade_value"


PoolStatus = Literal[
    "OK", "REPAIRED", "PARTIAL", "DUPLICATE", "QUARANTINED", "RETRACTED_AS_OF", "CORRECTED_AS_OF"
]


class PoolItem(BaseModel):
    ref: str  # doc_id or table_id
    kind: Literal["doc", "trade_table"]
    added_in_run: int
    added_by: Literal["heimdall"] = "heimdall"
    tier: int | None
    pub_date: date | None
    event_id: str | None
    status: PoolStatus
    flags: list[str] = []  # e.g. INJECTION_DETECTED, BACKGROUND
    title: str | None = None


# ---------- debate ----------
class Claim(BaseModel):
    id: str
    run: int
    side: Side
    statement: str
    type: Literal["SOURCED_FACT", "INFERENCE"]
    premises: list[str] = []  # for INFERENCE: ids of claims/evidence it rests on
    country: str | None
    sector: str | None
    parameter: Parameter | None  # exposure parameter this claim supports
    bound_value: float | None  # the value this claim argues for
    evidence: list[Evidence] = Field(min_length=1)
    supersedes: str | None = None
    memory_influence: list[dict[str, Any]] = []  # [{memory_id, effect}]
    unchallenged: bool = False  # set by the Thing if the other side failed
    status: Literal["ACTIVE", "CONCEDED", "SUPERSEDED"] = "ACTIVE"
    flags: list[str] = []  # e.g. NO_VALID_SUPPORT (set by validators)


class Position(BaseModel):  # one per side per scope row per run
    side: Side
    country: str
    sector: str
    params: dict[Parameter, tuple[float, float]]  # (low, high) per parameter
    param_claims: dict[Parameter, list[str]]  # claim ids backing each parameter
    low: float  # from compute_exposure
    high: float
    headline: float  # thor = high, loki = low
    unit: str = "pct_of_baseline_trade_value"
    calc_id: str  # compute_exposure call id (reproducible)
    assumed_params: list[Parameter] = []  # intervals with no backing claim


class Attack(BaseModel):
    id: str
    run: int
    by: Side
    target_claim: str
    type: AttackType
    argument: str
    evidence: list[Evidence] = []


class Response(BaseModel):
    id: str
    run: int
    by: Side
    attack_id: str
    action: Literal["DEFEND", "REVISE", "CONCEDE"]
    argument: str
    new_evidence: list[Evidence] = []  # DEFEND requires >=1 item not previously cited by the claim
    revised_bound: float | None = None


# ---------- judgment ----------
RulingVerdict = Literal["UPHELD", "PARTIALLY_UPHELD", "REJECTED", "CONCEDED", "NO_RULING"]
Finding = Literal["ESTABLISHED", "CONTESTED", "CONCEDED", "REJECTED"]


class Ruling(BaseModel):
    id: str
    claim_id: str
    run: int
    verdict: RulingVerdict
    basis: Literal["SOURCED_FACT", "INFERENCE", "UNSUPPORTED"]
    finding: Finding
    rationale: str
    decisive_evidence: list[str]
    citation_issues: list[CitationCheck] = []
    memory_influence: list[dict[str, Any]] = []
    adjusted_bound: float | None = None  # PARTIALLY_UPHELD may carry an Odin-adjusted bound


class FinalRange(BaseModel):
    country: str
    sector: str
    low: float
    high: float
    unit: str
    params: dict[Parameter, tuple[float, float]]
    supporting_rulings: list[str]
    observed_anchor: float | None  # observed post-event change, if visible
    justification: str
    calc_id: str
    contested_params: list[Parameter] = []
    unsupported_params: list[Parameter] = []
    anchor_conflict: bool = False


class Deficiency(BaseModel):
    id: str
    check: str  # S1..S9
    statement: str  # "No post-event trade evidence for ..." -- never an instruction
    related: list[str] = []  # claim/row/doc ids


class Sufficiency(BaseModel):
    verdict: Literal["SUFFICIENT", "INSUFFICIENT"]
    checklist: dict[str, bool]  # S1..S9 -> pass/fail
    deficiencies: list[Deficiency] = []
    width_reason: str | None = None
    overridden: bool = False  # Odin said SUFFICIENT but a deterministic check failed


class ResearchReport(BaseModel):  # Heimdall's answer to a remand
    run: int
    queries: list[str]
    per_deficiency: dict[str, Literal["ADDRESSED", "PARTIAL", "NO_EVIDENCE_FOUND"]]
    new_items: list[str]
    memory_influence: list[dict[str, Any]] = []
    notes: str = ""


# ---------- run & case ----------
class SideStatus(StrEnum):
    OK = "OK"
    FAILED = "FAILED"
    FAILED_TO_ATTACK = "FAILED_TO_ATTACK"


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
    research: ResearchReport | None = None  # the research that fed THIS run (n >= 2)
    side_status: dict[str, SideStatus] = Field(
        default_factory=lambda: {"thor": SideStatus.OK, "loki": SideStatus.OK}
    )
    sealed: dict[str, bool] = Field(default_factory=lambda: {"thor": False, "loki": False})
    sealed_at: dict[str, datetime | None] = Field(default_factory=lambda: dict.fromkeys(("thor", "loki")))
    citation_checks: dict[str, list[CitationCheck]] = {}  # claim/attack/response id -> checks
    unrebutted: list[str] = []  # attack ids left unanswered
    conceded_prior: list[str] = []  # earlier-run claim ids conceded in this run


class CaseFile(BaseModel):
    case_id: str
    version: int = 0  # increments on every write
    question: str
    as_of: date
    opened_at: datetime
    phase: Phase = Phase.OPENED
    index_version: str  # manifest+index hash at open
    pinned_ingest_seq: int = 0  # live-ingested docs above this are invisible until a re-search refreshes it
    scope: Scope | None = None
    verification: Verification | None = None
    pool: list[PoolItem] = []
    data_quality: list[dict[str, Any]] = []  # damaged docs / rows touching scope
    traps: list[dict[str, Any]] = []  # injections, rumours, retractions, look-alikes detected
    runs: list[Run] = []
    status: Literal["OPEN", "COMPLETE", "INCOMPLETE", "REJECTED", "ABORTED"] = "OPEN"
    status_reasons: list[str] = []
    briefing_path: str | None = None

    # ---------- construction ----------
    @classmethod
    def open(cls, question: str, as_of: date, index_version: str, pinned_ingest_seq: int = 0) -> CaseFile:
        now = datetime.now(UTC)
        case_id = f"CASE-{now:%Y%m%d}-{secrets.token_hex(2)}"
        return cls(
            case_id=case_id,
            question=question,
            as_of=as_of,
            opened_at=now,
            index_version=index_version,
            pinned_ingest_seq=pinned_ingest_seq,
        )

    def bump(self) -> int:
        self.version += 1
        return self.version

    # ---------- runs ----------
    @property
    def current_run(self) -> Run | None:
        return self.runs[-1] if self.runs else None

    def run(self, n: int) -> Run:
        for r in self.runs:
            if r.n == n:
                return r
        raise KeyError(f"run {n} not found")

    def start_run(self, n: int) -> Run:
        run = Run(n=n, started_at=datetime.now(UTC))
        self.runs.append(run)
        self.bump()
        return run

    # ---------- ids ----------
    def next_claim_id(self, run: int, side: Side) -> str:
        r = self.run(run)
        nums = [c.id for c in r.claims if c.side == side]
        return f"R{run}-{SIDE_LETTER[side]}-C{len(nums) + 1:02d}"

    def next_attack_id(self, run: int, side: Side) -> str:
        r = self.run(run)
        nums = [a.id for a in r.attacks if a.by == side]
        return f"R{run}-{SIDE_LETTER[side]}-A{len(nums) + 1:02d}"

    def next_response_id(self, run: int, side: Side) -> str:
        r = self.run(run)
        nums = [x.id for x in r.responses if x.by == side]
        return f"R{run}-{SIDE_LETTER[side]}-D{len(nums) + 1:02d}"

    # ---------- lookups ----------
    def all_claims(self) -> list[Claim]:
        return [c for r in self.runs for c in r.claims]

    def claim(self, claim_id: str) -> Claim | None:
        for c in self.all_claims():
            if c.id == claim_id:
                return c
        return None

    def all_attacks(self) -> list[Attack]:
        return [a for r in self.runs for a in r.attacks]

    def all_responses(self) -> list[Response]:
        return [x for r in self.runs for x in r.responses]

    def active_claims(self, side: Side | None = None) -> list[Claim]:
        return [c for c in self.all_claims() if c.status == "ACTIVE" and (side is None or c.side == side)]

    def pool_ids(self, upto_run: int | None = None) -> set[str]:
        return {p.ref for p in self.pool if upto_run is None or p.added_in_run <= upto_run}

    def new_evidence(self, run: int) -> set[str]:
        """IDs added to the pool for `run` that were not in the pool for any earlier run."""
        before = self.pool_ids(run - 1)
        return {p.ref for p in self.pool if p.added_in_run == run} - before

    def effective_rulings(self) -> dict[str, Ruling]:
        """Latest ruling per claim id (later runs override earlier ones)."""
        out: dict[str, Ruling] = {}
        for r in self.runs:
            for rul in r.rulings:
                out[rul.claim_id] = rul
        return out

    def claim_chain_head(self, claim_id: str) -> str:
        """Follow `supersedes` forward: the newest claim that (transitively) supersedes `claim_id`."""
        current = claim_id
        changed = True
        while changed:
            changed = False
            for c in self.all_claims():
                if c.supersedes == current:
                    current = c.id
                    changed = True
                    break
        return current

    def deficiencies(self, run: int) -> list[Deficiency]:
        r = self.run(run)
        return list(r.sufficiency.deficiencies) if r.sufficiency else []

    # ---------- views ----------
    def view_for(self, agent: AgentName, phase: Phase | str) -> CaseFile:
        """Role-filtered copy. Opponent's sealed claims are removed while Round 1 is open.

        Memory content is never on the Case File, so nothing needs stripping for it.
        """
        view = self.model_copy(deep=True)
        phase_value = phase.value if isinstance(phase, Phase) else phase
        if agent in ("thor", "loki") and phase_value == Phase.R1_SUBMIT.value and view.runs:
            # commit-then-reveal: the current run's opponent submission is hidden until both commit
            opponent: Side = "loki" if agent == "thor" else "thor"
            current = view.runs[-1]
            current.claims = [c for c in current.claims if c.side != opponent]
            current.positions = [p for p in current.positions if p.side != opponent]
            current.citation_checks = {
                k: v for k, v in current.citation_checks.items() if f"-{SIDE_LETTER[opponent]}-" not in k
            }
        return view

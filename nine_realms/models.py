"""Shared data model: the Case File and everything agents put on it."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SIDES = ("THOR", "LOKI")
CLAIM_KINDS = ("fact", "inference")
RULING_STATUSES = ("established", "contested", "conceded", "rejected")
RULING_BASES = ("sourced_fact", "inference")


@dataclass
class Evidence:
    doc_id: str
    quote: str = ""
    locator: str = ""  # paragraph anchor ("P3") or trade row ("row 4")
    date: str = ""  # filled by citation validation
    tier: int | None = None  # filled by citation validation

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Evidence":
        if not isinstance(d, dict) or not d.get("doc_id"):
            raise ValueError("evidence needs a doc_id")
        return cls(str(d["doc_id"]), str(d.get("quote", "")), str(d.get("locator", "")))


@dataclass
class Estimate:
    country: str
    sector: str
    low: float
    high: float
    unit: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any], default_unit: str = "") -> "Estimate":
        low, high = float(d["low"]), float(d["high"])
        if low > high:
            low, high = high, low
        return cls(str(d["country"]), str(d["sector"]), low, high, str(d.get("unit") or default_unit))

    @property
    def key(self) -> tuple[str, str]:
        return (self.country, self.sector)


@dataclass
class Claim:
    id: str
    side: str
    run: int
    text: str
    kind: str  # fact | inference
    evidence: list[Evidence]
    estimate: Estimate | None = None
    confidence: float = 0.5
    status: str = "live"  # live | conceded

    def doc_ids(self) -> set[str]:
        return {e.doc_id for e in self.evidence}


@dataclass
class Attack:
    id: str
    run: int
    by: str
    target_claim_id: str
    text: str
    kind: str = "other"
    counter_evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Response:
    attack_id: str
    by: str
    action: str  # DEFEND | REVISE | CONCEDE
    text: str
    evidence: list[Evidence] = field(default_factory=list)
    revised_estimate: Estimate | None = None
    note: str = ""  # e.g. "defence without new evidence -> conceded"


@dataclass
class Ruling:
    claim_id: str
    run: int
    basis: str  # sourced_fact | inference
    status: str  # established | contested | conceded | rejected
    rationale: str


@dataclass
class OdinRange:
    country: str
    sector: str
    low: float | None
    high: float | None
    rationale: str = ""
    claim_ids: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


@dataclass
class RunRecord:
    run: int
    new_doc_ids: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)
    attacks: list[str] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)
    rulings: list[Ruling] = field(default_factory=list)
    sufficient: bool | None = None
    deficiencies: list[str] = field(default_factory=list)
    unchallenged_side: str | None = None
    failures: list[str] = field(default_factory=list)
    debate: dict[str, Any] = field(default_factory=dict)  # deterministic scorecard, see debate.py


@dataclass
class CaseFile:
    case_id: str
    question: str
    as_of: str  # YYYY-MM-DD
    event_id: str = ""
    event_summary: str = ""
    verified: bool | None = None
    verdict_reason: str = ""
    unit: str = ""
    scope: list[tuple[str, str]] = field(default_factory=list)  # (country, sector)
    pool: list[str] = field(default_factory=list)
    red_flags: list[dict[str, Any]] = field(default_factory=list)
    claims: dict[str, Claim] = field(default_factory=dict)
    attacks: dict[str, Attack] = field(default_factory=dict)
    responses: dict[str, Response] = field(default_factory=dict)
    rulings: dict[str, Ruling] = field(default_factory=dict)  # effective = latest per claim
    ranges: list[OdinRange] = field(default_factory=list)
    runs: list[RunRecord] = field(default_factory=list)
    status: str = "OPEN"  # OPEN | COMPLETE | INCOMPLETE | REJECTED | ABORTED
    status_reasons: list[str] = field(default_factory=list)
    briefing: dict[str, Any] = field(default_factory=dict)
    briefing_md: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

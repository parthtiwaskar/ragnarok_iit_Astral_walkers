"""Terminal submit tools: the LLM-facing argument models. Their arguments ARE the structured output.

Evidence is accepted only as doc_id + quote or table_id + row; metadata is filled in by the tool layer.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.tools.citation import EvidenceIn
from app.tools.registry import ToolDef
from app.trial.casefile import AttackType, Parameter, Verdict


class MemoryInfluenceIn(BaseModel):
    memory_id: str
    effect: str = Field(description="one line on how the memory changed what you did")


class ScopeRowIn(BaseModel):
    country: str
    sector: str
    trade_tables: list[str] = Field(
        default_factory=list, description="trade table ids for this country-sector"
    )
    route_flags: dict[str, float] = Field(
        default_factory=dict,
        description="optional: table_id -> share (0-1) of that table's flow using the disrupted route",
    )


class SubmitCaseFileArgs(BaseModel):
    verdict: Verdict
    reasons: list[str] = Field(description="each reason with the doc ids behind it")
    event_id: str | None = None
    event_summary: str = Field(
        default="", description="sourced summary with inline [DOC_ID, YYYY-MM-DD] citations"
    )
    event_date: date | None = None
    chokepoints: list[str] = Field(default_factory=list)
    rows: list[ScopeRowIn] = Field(default_factory=list, description="country-sector scope rows")
    supporting_docs: list[str] = Field(default_factory=list)
    contradicting_docs: list[str] = Field(default_factory=list)
    retractions_applied: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list, description="other candidate event ids")
    disagreement: str | None = Field(
        default=None, description="required if your verdict differs from the rule verdict of event_profile"
    )
    pool: list[str] = Field(
        default_factory=list, description="doc ids (and trade table ids) for the evidence pool"
    )
    memory_influence: list[MemoryInfluenceIn] = Field(default_factory=list)


class SubmitResearchArgs(BaseModel):
    new_items: list[str] = Field(description="doc ids / trade table ids that are NEW to the pool")
    per_deficiency: dict[str, Literal["ADDRESSED", "PARTIAL", "NO_EVIDENCE_FOUND"]] = Field(
        description="deficiency id -> outcome"
    )
    notes: str = ""
    memory_influence: list[MemoryInfluenceIn] = Field(default_factory=list)


class ClaimIn(BaseModel):
    id: str | None = Field(default=None, description="your local id: C01, C02, ...")
    statement: str
    type: Literal["SOURCED_FACT", "INFERENCE"]
    premises: list[str] = Field(
        default_factory=list, description="for INFERENCE: claim ids / doc ids it rests on"
    )
    country: str | None = None
    sector: str | None = None
    parameter: Parameter | None = None
    bound_value: float | None = None
    evidence: list[EvidenceIn] = Field(min_length=1)
    supersedes: str | None = Field(default=None, description="id of your earlier-run claim this one amends")


class PositionIn(BaseModel):
    country: str
    sector: str
    params: dict[Parameter, list[float]] = Field(description="[low, high] per parameter")
    param_claims: dict[Parameter, list[str]] = Field(
        default_factory=dict, description="claim ids (C01.. or earlier-run ids) per parameter"
    )


class SubmitClaimsArgs(BaseModel):
    claims: list[ClaimIn] = Field(default_factory=list)
    positions: list[PositionIn]
    concede: list[str] = Field(
        default_factory=list, description="your earlier-run claim ids you now withdraw"
    )
    carry_forward: list[str] = Field(
        default_factory=list, description="your earlier-run claim ids kept unchanged"
    )
    memory_influence: list[MemoryInfluenceIn] = Field(default_factory=list)


class AttackIn(BaseModel):
    target_claim: str
    type: AttackType
    argument: str
    evidence: list[EvidenceIn] = Field(default_factory=list)


class SubmitAttacksArgs(BaseModel):
    attacks: list[AttackIn]
    memory_influence: list[MemoryInfluenceIn] = Field(default_factory=list)


class ResponseIn(BaseModel):
    attack_id: str
    action: Literal["DEFEND", "REVISE", "CONCEDE"]
    argument: str
    new_evidence: list[EvidenceIn] = Field(default_factory=list)
    revised_bound: float | None = None


class SubmitResponsesArgs(BaseModel):
    responses: list[ResponseIn]
    memory_influence: list[MemoryInfluenceIn] = Field(default_factory=list)


class RulingIn(BaseModel):
    claim_id: str
    verdict: Literal["UPHELD", "PARTIALLY_UPHELD", "REJECTED", "CONCEDED"]
    basis: Literal["SOURCED_FACT", "INFERENCE", "UNSUPPORTED"]
    finding: Literal["ESTABLISHED", "CONTESTED", "CONCEDED", "REJECTED"]
    rationale: str
    decisive_evidence: list[str] = Field(default_factory=list)
    adjusted_bound: float | None = Field(default=None, description="only with PARTIALLY_UPHELD")


class SubmitRulingsArgs(BaseModel):
    rulings: list[RulingIn]
    range_notes: dict[str, str] = Field(default_factory=dict, description="'Country/sector' -> one-line note")
    memory_influence: list[MemoryInfluenceIn] = Field(default_factory=list)


class DeficiencyIn(BaseModel):
    check: str = Field(description="S1..S9")
    statement: str = Field(description="what is missing, phrased as a gap, never an instruction")
    related: list[str] = Field(default_factory=list)


class SubmitSufficiencyArgs(BaseModel):
    verdict: Literal["SUFFICIENT", "INSUFFICIENT"]
    s5_contradictions_resolved: bool = Field(
        default=True, description="S5: conflicting sources resolved or marked CONTESTED"
    )
    width_reason: str | None = Field(
        default=None, description="S6: why a range wider than the limit is justified"
    )
    deficiencies: list[DeficiencyIn] = Field(default_factory=list)
    memory_influence: list[MemoryInfluenceIn] = Field(default_factory=list)


class AssumptionIn(BaseModel):
    id: str = Field(description="A1, A2, ...")
    text: str
    ruling_ids: list[str]


class SubmitBriefingArgs(BaseModel):
    executive_summary: str
    verification_note: str = ""
    established: str
    contested: str
    conceded: str
    outlook: str
    assumptions: list[AssumptionIn] = Field(default_factory=list)


class LessonIn(BaseModel):
    kind: Literal["procedural", "fact", "precedent"]
    text: str = Field(max_length=600)
    tags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.6, ge=0, le=1)


class SubmitLessonsArgs(BaseModel):
    lessons: list[LessonIn] = Field(default_factory=list, max_length=3)


SUBMIT_TOOLS: dict[str, ToolDef] = {
    "submit_case_file": ToolDef(
        "submit_case_file",
        "Submit the Case File (scope, verification, evidence pool).",
        SubmitCaseFileArgs,
        terminal=True,
        dedupe=False,
    ),
    "submit_research": ToolDef(
        "submit_case_file",
        "Submit the research delta after a remand.",
        SubmitResearchArgs,
        terminal=True,
        dedupe=False,
    ),
    "submit_claims": ToolDef(
        "submit_claims",
        "Submit sealed Round 1 claims and one position per scope row.",
        SubmitClaimsArgs,
        terminal=True,
        dedupe=False,
    ),
    "submit_attacks": ToolDef(
        "submit_attacks",
        "Submit Round 2A attacks on the other side's claims.",
        SubmitAttacksArgs,
        terminal=True,
        dedupe=False,
    ),
    "submit_responses": ToolDef(
        "submit_responses",
        "Answer every attack on your claims.",
        SubmitResponsesArgs,
        terminal=True,
        dedupe=False,
    ),
    "submit_rulings": ToolDef(
        "submit_rulings",
        "Rule on every listed claim. Returns final ranges and checklist.",
        SubmitRulingsArgs,
        terminal=True,
        dedupe=False,
    ),
    "submit_sufficiency": ToolDef(
        "submit_sufficiency",
        "Sufficiency verdict with deficiencies phrased as gaps.",
        SubmitSufficiencyArgs,
        terminal=True,
        dedupe=False,
    ),
    "submit_briefing": ToolDef(
        "submit_briefing",
        "Write the briefing sections from the rulings digest only.",
        SubmitBriefingArgs,
        terminal=True,
        dedupe=False,
    ),
    "submit_lessons": ToolDef(
        "submit_lessons",
        "Store up to 3 lessons in your own memory.",
        SubmitLessonsArgs,
        terminal=True,
        dedupe=False,
    ),
}


def submit_def(agent: str, phase: str, name: str) -> ToolDef:
    if name == "submit_case_file" and phase == "RESEARCH":
        return SUBMIT_TOOLS["submit_research"]
    return SUBMIT_TOOLS[name]

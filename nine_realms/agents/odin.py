"""Odin, the Allfather: rules on every claim, decides sufficiency, writes the briefing from rulings only.
He never plans, assigns or searches."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..models import (RULING_BASES, RULING_STATUSES, Attack, CaseFile, Claim, OdinRange, Response, Ruling)
from .advocates import _claim_view
from .base import Agent

_DOC_TOKEN = re.compile(r"\b[A-Z]{2,4}-\d{3,5}\b")


@dataclass
class JudgeResult:
    rulings: list[Ruling]
    ranges: list[OdinRange]
    sufficient: bool
    deficiencies: list[str] = field(default_factory=list)


class Odin(Agent):
    name = "ODIN"

    def judge(self, case: CaseFile, run: int, to_rule: list[Claim], attacks: list[Attack],
              responses: list[Response], unchallenged: str | None) -> JudgeResult:
        ids = {c.id for c in to_rule}

        def validate(d: dict[str, Any]) -> JudgeResult:
            rulings: dict[str, Ruling] = {}
            for r in d.get("rulings", []):
                cid = str(r.get("claim_id", ""))
                if cid not in ids:
                    raise ValueError(f"ruling for unknown claim {cid!r}; claims to rule: {sorted(ids)}")
                if r.get("basis") not in RULING_BASES or r.get("status") not in RULING_STATUSES:
                    raise ValueError(f"basis must be {RULING_BASES}, status must be {RULING_STATUSES}")
                if not str(r.get("rationale", "")).strip():
                    raise ValueError("every ruling needs a rationale")
                status = r["status"]
                if case.claims[cid].status == "conceded" and status != "conceded":
                    self.bus.emit("ruling_overridden", agent=self.name, run=run, claim_id=cid,
                                  asked=status, forced="conceded", reason="author conceded the claim")
                    status = "conceded"
                rulings[cid] = Ruling(cid, run, r["basis"], status, str(r["rationale"]))
            missing = sorted(ids - set(rulings))
            if missing:
                raise ValueError(f"you must rule on every claim; missing: {missing}")
            ranges = []
            for g in d.get("ranges", []):
                if g.get("unsupported"):  # no evidence in Runemark for this country/sector: say so, do not invent a range
                    ranges.append(OdinRange(str(g["country"]), str(g["sector"]), None, None, str(g.get("rationale", "")),
                                            [], ["unsupported_no_evidence"]))
                    continue
                low, high = float(g["low"]), float(g["high"])
                if low > high:
                    raise ValueError(f"range {g.get('country')}/{g.get('sector')}: low > high")
                ranges.append(OdinRange(str(g["country"]), str(g["sector"]), low, high,
                                        str(g.get("rationale", "")), [str(x) for x in g.get("claim_ids", [])]))
            suff = d.get("sufficiency")
            if not isinstance(suff, dict) or not isinstance(suff.get("sufficient"), bool):
                raise ValueError('"sufficiency": {"sufficient": bool, "deficiencies": [..]} is required')
            defs = [str(x) for x in suff.get("deficiencies", [])]
            if not suff["sufficient"] and not defs:
                raise ValueError("if not sufficient, say what is missing (deficiencies), as gaps and not as instructions")
            return JudgeResult(list(rulings.values()), ranges, suff["sufficient"], defs)

        result = self.turn(
            case=case, run=run, phase="judge",
            task=("Rule on EVERY claim listed in claims_to_rule: basis (sourced_fact | inference) and status "
                  "(established | contested | conceded | rejected), with rationale. Then give your final range per "
                  "country/sector inside the span of the evidence, and decide sufficiency. If insufficient, list "
                  "deficiencies (what is missing), never orders to other agents. You may spot-check with read_document / verify_citation."),
            payload={"claims_to_rule": [_claim_view(c) for c in to_rule],
                     "attacks": [a.__dict__ | {"counter_evidence": [e.doc_id for e in a.counter_evidence]} for a in attacks],
                     "responses": [r.__dict__ | {"evidence": [e.doc_id for e in r.evidence]} for r in responses],
                     "prior_rulings": [r.__dict__ for r in case.rulings.values() if r.claim_id not in ids],
                     "unchallenged_side": unchallenged, "scope": case.scope, "unit": case.unit,
                     "event_verified": case.verified, "red_flags": case.red_flags},
            validate=validate)
        for r in result.rulings:
            self.bus.emit("ruling", agent=self.name, **r.__dict__)
        self.bus.emit("sufficiency", agent=self.name, run=run, sufficient=result.sufficient, deficiencies=result.deficiencies)
        return result

    def brief(self, case: CaseFile, digest: dict[str, Any]) -> dict[str, Any]:
        """Prose from rulings only. Citations are attached by code from the referenced claims,
        so Odin cannot invent a source; document IDs he types himself are stripped if not in the digest."""
        allowed = {e["doc_id"] for item in digest["findings"] for e in item["evidence"]}
        claim_ids = {item["claim_id"] for item in digest["findings"]}

        def validate(d: dict[str, Any]) -> dict[str, Any]:
            out: dict[str, Any] = {"title": str(d.get("title", "")), "executive_summary": str(d.get("executive_summary", "")).strip()}
            if not out["executive_summary"]:
                raise ValueError('"executive_summary" is required')
            for sec in ("established", "contested", "conceded"):
                items = []
                for it in d.get(sec, []):
                    refs = [c for c in it.get("claim_ids", []) if c in claim_ids]
                    if not refs:
                        raise ValueError(f"every {sec} item must reference claim_ids from the rulings digest")
                    items.append({"text": str(it.get("text", "")), "claim_ids": refs})
                out[sec] = items
            outlook = d.get("outlook") or {}
            if not outlook.get("text") or not outlook.get("assumptions"):
                raise ValueError('"outlook" needs "text" and a non-empty "assumptions" list')
            out["outlook"] = {"text": str(outlook["text"]), "assumptions": [str(a) for a in outlook["assumptions"]]}
            return out

        out = self.turn(
            case=case, run=0, phase="brief",
            task=("Write the final briefing from the rulings digest ONLY. Group findings by their ruling status, "
                  "reference claim_ids (the system attaches citations), and give a next-quarter outlook with explicit assumptions. "
                  "Do not use Thor's or Loki's raw arguments."),
            payload={"digest": digest}, validate=validate)
        # strip any doc ID Odin typed that is not backed by a ruled claim
        def clean(s: str) -> str:
            return _DOC_TOKEN.sub(lambda m: m.group(0) if m.group(0) in allowed else "[unverified-id-removed]", s)
        out["executive_summary"] = clean(out["executive_summary"])
        for sec in ("established", "contested", "conceded"):
            for it in out[sec]:
                it["text"] = clean(it["text"])
        out["outlook"]["text"] = clean(out["outlook"]["text"])
        return out

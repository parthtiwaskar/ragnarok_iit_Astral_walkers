"""Thor (high exposure) and Loki (limited exposure): same machinery, opposite mandates."""
from __future__ import annotations

from typing import Any

from ..models import Attack, CaseFile, Claim, Estimate, Evidence, Response
from .base import Agent, parse_evidence


def _claim_view(c: Claim, ruling: str | None = None) -> dict[str, Any]:
    return {
        "id": c.id, "side": c.side, "run": c.run, "text": c.text, "kind": c.kind, "status": c.status,
        "confidence": c.confidence, "ruling": ruling,
        "estimate": None if not c.estimate else {"country": c.estimate.country, "sector": c.estimate.sector,
                                                  "low": c.estimate.low, "high": c.estimate.high, "unit": c.estimate.unit},
        "evidence": [{"doc_id": e.doc_id, "quote": e.quote, "date": e.date, "tier": e.tier} for e in c.evidence],
    }


class Advocate(Agent):
    side = ""

    @property
    def opponent(self) -> str:
        return "LOKI" if self.side == "THOR" else "THOR"

    # -- Round 1 -------------------------------------------------------------------
    def submit(self, case: CaseFile, run: int, deficiencies: list[str]) -> list[Claim]:
        n_before = len(self.queries)
        used_docs = {d for c in case.claims.values() if c.side == self.side for d in c.doc_ids()}
        cap = self.budget.limits.max_claims_per_side

        def validate(d: dict[str, Any]) -> list[Claim]:
            if len(self.queries) == n_before:
                raise ValueError("You have not searched Runemark in this run. Call search_documents with your own queries first.")
            raw = d.get("claims")
            if not isinstance(raw, list) or not raw:
                raise ValueError('"claims" must be a non-empty list')
            out: list[Claim] = []
            for item in raw[:cap]:
                text, kind = str(item.get("text", "")).strip(), item.get("kind")
                if not text or kind not in ("fact", "inference"):
                    raise ValueError('every claim needs "text" and kind "fact" or "inference"')
                ev = self.check_evidence(case, run, parse_evidence(item.get("evidence", [])))
                if not ev:
                    self.bus.emit("claim_rejected_validation", agent=self.name, run=run, text=text, reason="no valid evidence")
                    continue
                est = None
                if item.get("estimate"):
                    est = Estimate.from_dict(item["estimate"], case.unit)
                    if est.key not in case.scope:
                        raise ValueError(f"estimate {est.key} is outside the case scope {case.scope}")
                out.append(Claim(f"R{run}-{self.side[0]}{len(out) + 1}", self.side, run, text, kind, ev, est,
                                 float(item.get("confidence", 0.5))))
            if not out:
                raise ValueError("none of your claims had valid evidence; cite documents you retrieved, with exact quotes")
            if run > 1 and not any(c.doc_ids() - used_docs for c in out):
                raise ValueError("Run 2+ must bring NEW evidence: at least one claim must cite a document you have not cited before.")
            return out

        claims = self.turn(
            case=case, run=run, phase="round1",
            task=(f"{self.mandate} Submit claims now, sealed. Search Runemark with your own queries. "
                  "Each claim: text, kind (fact = stated in a document, inference = your reasoning), evidence "
                  "[{doc_id, quote (exact), locator}], estimate {country, sector, low, high} drawn from cited data."),
            payload={"event": case.event_summary, "event_id": case.event_id, "unit": case.unit, "scope": case.scope,
                     "pool": case.pool, "red_flags": case.red_flags, "deficiencies_from_odin": deficiencies,
                     "run": run, "prior_claims": [_claim_view(c, case.rulings[c.id].status if c.id in case.rulings else None)
                                                   for c in case.claims.values()] if run > 1 else []},
            validate=validate)
        for c in claims:
            self.bus.emit("claim_submitted", agent=self.name, run=run, claim=_claim_view(c))
        return claims

    # -- Round 2 -------------------------------------------------------------------
    def attack(self, case: CaseFile, run: int) -> list[Attack]:
        targets = {c.id: c for c in case.claims.values() if c.side == self.opponent and c.status != "conceded"}

        def validate(d: dict[str, Any]) -> list[Attack]:
            raw = d.get("attacks")
            if not isinstance(raw, list) or not raw:
                raise ValueError('"attacks" must be a non-empty list: you must attack at least one opposing claim')
            out: list[Attack] = []
            for item in raw:
                tid = str(item.get("target_claim_id", ""))
                if tid not in targets:
                    raise ValueError(f"target_claim_id {tid!r} is not a live {self.opponent} claim: {sorted(targets)}")
                text = str(item.get("text", "")).strip()
                if not text:
                    raise ValueError("every attack needs text")
                kind = str(item.get("kind", "other"))
                counter = self.check_evidence(case, run, parse_evidence(item.get("counter_evidence", [])))
                if kind in ("contradiction", "retracted", "stale", "wrong_event") and not counter:
                    self.bus.emit("attack_dropped", agent=self.name, run=run, target=tid, reason="needs counter-evidence")
                    continue
                out.append(Attack(f"R{run}-A{self.side[0]}{len(out) + 1}", run, self.side, tid, text, kind, counter))
            if not out:
                raise ValueError("no attack survived validation; give counter_evidence (retrieved doc IDs + exact quotes)")
            return out

        attacks = self.turn(
            case=case, run=run, phase="attack",
            task=(f"Cross-examination. Attack the weakest {self.opponent} claims: retracted or low-tier sources, "
                  "contradicting documents, wrong event, stale data, logic gaps. Search for counter-evidence."),
            payload={"opponent_claims": [_claim_view(c) for c in targets.values()],
                     "your_claims": [_claim_view(c) for c in case.claims.values() if c.side == self.side]},
            validate=validate)
        for a in attacks:
            self.bus.emit("attack_made", agent=self.name, run=run, attack=a.__dict__ | {
                "counter_evidence": [e.doc_id for e in a.counter_evidence]})
        return attacks

    def respond(self, case: CaseFile, run: int, attacks: list[Attack]) -> list[Response]:
        by_id = {a.id: a for a in attacks}

        def validate(d: dict[str, Any]) -> list[Response]:
            raw = d.get("responses")
            if not isinstance(raw, list):
                raise ValueError('"responses" must be a list')
            out: dict[str, Response] = {}
            for item in raw:
                aid = str(item.get("attack_id", ""))
                if aid not in by_id:
                    raise ValueError(f"unknown attack_id {aid!r}; valid: {sorted(by_id)}")
                action = str(item.get("action", "")).upper()
                if action not in ("DEFEND", "CONCEDE"):
                    raise ValueError('action must be "DEFEND" or "CONCEDE"')
                claim = case.claims[by_id[aid].target_claim_id]
                ev = self.check_evidence(case, run, parse_evidence(item.get("evidence", [])))
                note = ""
                new_ev = [e for e in ev if e.doc_id not in claim.doc_ids()]
                if action == "DEFEND" and not new_ev:
                    action, note = "CONCEDE", "defence brought no new evidence -> treated as concession"
                    self.bus.emit("defence_downgraded", agent=self.name, run=run, attack_id=aid, reason=note)
                rev = Estimate.from_dict(item["revised_estimate"], case.unit) if item.get("revised_estimate") else None
                out[aid] = Response(aid, self.side, action, str(item.get("text", "")), new_ev if action == "DEFEND" else ev, rev, note)
            missing = sorted(set(by_id) - set(out))
            if missing:
                raise ValueError(f"you must answer every attack; missing: {missing}")
            return list(out.values())

        responses = self.turn(
            case=case, run=run, phase="respond",
            task="You were attacked. For each attack: DEFEND with NEW evidence (a document you have not cited for that claim) or CONCEDE honestly.",
            payload={"attacks_against_you": [
                {"attack_id": a.id, "text": a.text, "kind": a.kind,
                 "counter_evidence": [{"doc_id": e.doc_id, "quote": e.quote, "date": e.date, "tier": e.tier} for e in a.counter_evidence],
                 "your_claim": _claim_view(case.claims[a.target_claim_id])} for a in attacks]},
            validate=validate)
        for r in responses:
            self.bus.emit("response_made", agent=self.name, run=run, attack_id=r.attack_id, action=r.action, note=r.note)
        return responses


class Thor(Advocate):
    name = "THOR"
    side = "THOR"
    mandate = "Build the strongest honest case that exposure is HIGH."


class Loki(Advocate):
    name = "LOKI"
    side = "LOKI"
    mandate = "Build the strongest honest case that exposure is LIMITED. 'Maybe it will be fine' is not a claim; cite rerouting, substitution, stock buffers or trade data."

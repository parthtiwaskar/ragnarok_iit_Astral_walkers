"""Thor (high exposure) and Loki (limited exposure): same machinery, opposite mandates."""
from __future__ import annotations

from typing import Any

from ..models import Attack, CaseFile, Claim, Estimate, Evidence, Response
from ..debate import ATTACK_KINDS, INTEGRITY_KINDS, audit_claim, best_tier, is_relevant
from .base import Agent, parse_evidence


def _claim_view(c: Claim, ruling: str | None = None, audit: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": c.id, "side": c.side, "run": c.run, "text": c.text, "kind": c.kind, "status": c.status,
        "confidence": c.confidence, "ruling": ruling,
        "estimate": None if not c.estimate else {"country": c.estimate.country, "sector": c.estimate.sector,
                                                  "low": c.estimate.low, "high": c.estimate.high, "unit": c.estimate.unit},
        "evidence": [{"doc_id": e.doc_id, "quote": e.quote, "date": e.date, "tier": e.tier} for e in c.evidence],
        **({"audit": audit} if audit else {}),
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
        audits = {cid: audit_claim(c) for cid, c in targets.items()}

        def validate(d: dict[str, Any]) -> list[Attack]:
            raw = d.get("attacks")
            if not isinstance(raw, list) or not raw:
                raise ValueError('"attacks" must be a non-empty list: you must attack at least one opposing claim')
            out: list[Attack] = []
            seen: set[tuple[str, tuple[str, ...]]] = set()
            for item in raw:
                tid = str(item.get("target_claim_id", ""))
                if tid not in targets:
                    raise ValueError(f"target_claim_id {tid!r} is not a live {self.opponent} claim: {sorted(targets)}")
                text = str(item.get("text", "")).strip()
                if len(text) < 25:
                    raise ValueError("say exactly what is wrong: state what the claim says, what your evidence shows, and why they differ (>=25 chars)")
                kind = str(item.get("kind", ""))
                if kind not in ATTACK_KINDS:
                    raise ValueError(f'kind must be one of {list(ATTACK_KINDS)} ("logic" and "other" are not attacks: name the specific weakness)')
                counter = self.check_evidence(case, run, parse_evidence(item.get("counter_evidence", [])))
                if not counter:
                    self.bus.emit("attack_dropped", agent=self.name, run=run, target=tid, reason="no verified counter-evidence (doc ID + exact quote)")
                    continue
                target = targets[tid]
                if kind == "contradiction" and not any(e.doc_id not in target.doc_ids() for e in counter):
                    self.bus.emit("attack_dropped", agent=self.name, run=run, target=tid, reason="a contradiction must cite a document the claim did not cite")
                    continue
                if not is_relevant(counter, target):
                    self.bus.emit("attack_dropped", agent=self.name, run=run, target=tid, reason="counter-evidence does not bear on this claim")
                    continue
                key = (tid, tuple(sorted(e.doc_id for e in counter)))
                if key in seen:
                    continue
                seen.add(key)
                out.append(Attack(f"R{run}-A{self.side[0]}{len(out) + 1}", run, self.side, tid, text, kind, counter))
            if not out:
                raise ValueError("no attack survived validation. Each needs a specific kind, an explanation, and counter_evidence "
                                 "(retrieved doc IDs + exact quotes) that bears on the claim you attack. Use the audit hints.")
            return out

        attacks = self.turn(
            case=case, run=run, phase="attack",
            task=(f"Cross-examination. Find REAL weaknesses in {self.opponent}'s claims and attack with evidence. Check, in order: "
                  "(1) integrity (retracted/corrected source, after the as-of date, wrong event); (2) a contradicting document or table row; "
                  "(3) scope (wrong country, product or period); (4) arithmetic (recompute the estimate from the table with trade_stats); "
                  "(5) tier (an unreliable source carrying a fact); (6) an inference the cited quotes do not support. "
                  "Every attack needs a specific kind, an explanation, and counter_evidence with exact quotes. Doubt alone is not an attack."),
            payload={"opponent_claims": [_claim_view(c, audit=audits[c.id]) for c in targets.values()],
                     "your_claims": [_claim_view(c) for c in case.claims.values() if c.side == self.side],
                     "attack_kinds": list(ATTACK_KINDS)},
            validate=validate)
        for a in attacks:
            self.bus.emit("attack_made", agent=self.name, run=run, attack=a.__dict__ | {
                "counter_evidence": [e.doc_id for e in a.counter_evidence]})
        return attacks

    def respond(self, case: CaseFile, run: int, attacks: list[Attack]) -> list[Response]:
        by_id = {a.id: a for a in attacks}
        warned: set[str] = set()  # soft rules warn once, then the agent's choice stands

        def warn_once(key: str, message: str, event: str, **fields: Any) -> None:
            if key not in warned:
                warned.add(key)
                self.bus.emit(event, agent=self.name, run=run, **fields)
                raise ValueError(message)

        def validate(d: dict[str, Any]) -> list[Response]:
            raw = d.get("responses")
            if not isinstance(raw, list):
                raise ValueError('"responses" must be a list')
            out: dict[str, Response] = {}
            for item in raw:
                aid = str(item.get("attack_id", ""))
                if aid not in by_id:
                    raise ValueError(f"unknown attack_id {aid!r}; valid: {sorted(by_id)}")
                attack, claim = by_id[aid], case.claims[by_id[aid].target_claim_id]
                action = str(item.get("action", "")).upper()
                if action not in ("DEFEND", "REVISE", "CONCEDE"):
                    raise ValueError('action must be "DEFEND", "REVISE" or "CONCEDE"')
                text = str(item.get("text", "")).strip()
                ev = self.check_evidence(case, run, parse_evidence(item.get("evidence", [])))
                new_ev = [e for e in ev if e.doc_id not in claim.doc_ids()]
                rev = Estimate.from_dict(item["revised_estimate"], case.unit) if item.get("revised_estimate") else None
                note = ""
                if action == "CONCEDE" and rev:
                    action, note = "REVISE", "concession with a revised estimate treated as a revision"
                if action == "REVISE" and not rev:
                    raise ValueError("REVISE keeps the claim but narrows it: give a revised_estimate {country, sector, low, high}")
                if action in ("REVISE", "CONCEDE") and len(text) < 20:
                    raise ValueError("explain exactly what the counter-evidence refutes (>=20 chars). Concede only what it refutes.")
                if action == "DEFEND" and not new_ev:
                    warn_once(f"d:{aid}", "A defence needs a document you have NOT cited for this claim. Search for one, or choose REVISE (narrow the claim) "
                              "or CONCEDE (only if the counter-evidence really refutes it).", "defence_retry", attack_id=aid)
                    action, note = "CONCEDE", "defence brought no new evidence -> treated as concession"
                    self.bus.emit("defence_downgraded", agent=self.name, run=run, attack_id=aid, reason=note)
                if action == "CONCEDE" and not note:
                    c_tier, a_tier = best_tier(claim.evidence), best_tier(attack.counter_evidence)
                    if (claim.kind == "fact" and c_tier is not None and c_tier <= 2 and a_tier is not None and a_tier > c_tier
                            and attack.kind not in INTEGRITY_KINDS):
                        warn_once(f"g:{aid}", f"Your claim is a sourced FACT from a Tier {c_tier} document; the attack's best evidence is Tier {a_tier}, "
                                  "which is weaker, and it does not show the source is retracted, stale or about another event. Do not concede a "
                                  "well-sourced fact to weaker evidence: DEFEND with a new document, or REVISE the part it really touches.",
                                  "concession_guard", attack_id=aid, claim_id=claim.id, claim_tier=c_tier, attack_tier=a_tier)
                        note = "concession accepted after a guard warning"
                kept = new_ev if action == "DEFEND" else ev
                out[aid] = Response(aid, self.side, action, text, kept, rev, note)
            missing = sorted(set(by_id) - set(out))
            if missing:
                raise ValueError(f"you must answer every attack; missing: {missing}")
            return list(out.values())

        responses = self.turn(
            case=case, run=run, phase="respond",
            task=("You were attacked. For EACH attack choose the proportionate answer: DEFEND (with a document you have not cited for that claim), "
                  "REVISE (keep the claim but narrow its estimate to what survives; give revised_estimate), or CONCEDE (only what the "
                  "counter-evidence actually refutes). Do not concede because someone doubts you. Do not concede a Tier 1-2 sourced fact to weaker evidence."),
            payload={"attacks_against_you": [
                {"attack_id": a.id, "text": a.text, "kind": a.kind,
                 "counter_evidence": [{"doc_id": e.doc_id, "quote": e.quote, "date": e.date, "tier": e.tier} for e in a.counter_evidence],
                 "your_claim": _claim_view(case.claims[a.target_claim_id], audit=audit_claim(case.claims[a.target_claim_id]))} for a in attacks]},
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

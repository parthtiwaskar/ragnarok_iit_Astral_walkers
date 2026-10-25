"""Heimdall, the Gatekeeper: builds the Case File, verifies the event, re-searches on remand.
He never argues and never judges."""
from __future__ import annotations

from typing import Any

from ..models import CaseFile
from .base import Agent


class Heimdall(Agent):
    name = "HEIMDALL"

    def gather(self, case: CaseFile) -> CaseFile:
        """FR1.1-1.3: question -> Case File fields, evidence pool, verification verdict."""
        n_before = len(self.queries)

        def validate(d: dict[str, Any]) -> dict[str, Any]:
            if not isinstance(d.get("verified"), bool):
                raise ValueError('"verified" must be true or false')
            if not str(d.get("verdict_reason", "")).strip():
                raise ValueError('"verdict_reason" is required, also when verified is true')
            if len(self.queries) == n_before and not self.seen:
                raise ValueError("You have not looked at Runemark yet. Use list_events / search_documents first.")
            pool = [p for p in d.get("pool", []) if p in self.seen]
            dropped = [p for p in d.get("pool", []) if p not in self.seen]
            if dropped:
                self.bus.emit("pool_dropped", agent=self.name, doc_ids=dropped, reason="never retrieved")
            if d["verified"]:
                if not d.get("event_id"):
                    raise ValueError('verified=true needs "event_id"')
                scope = [(str(s["country"]), str(s["sector"])) for s in d.get("scope", [])]
                if not scope:
                    raise ValueError('verified=true needs a non-empty "scope" of {country, sector}')
                if not pool:
                    raise ValueError('verified=true needs a "pool" of document IDs you retrieved')
                d["_scope"] = scope
            d["_pool"] = pool
            return d

        d = self.turn(
            case=case, run=0, phase="gather",
            task="Open the case: parse the question, find the event, gather evidence, verify the event is real as of the as-of date.",
            payload={"question": case.question, "as_of": case.as_of}, validate=validate)
        case.event_id = str(d.get("event_id", ""))
        case.event_summary = str(d.get("event_summary", ""))
        case.verified = d["verified"]
        case.verdict_reason = str(d["verdict_reason"])
        case.unit = str(d.get("unit", ""))
        case.pool = list(dict.fromkeys(d["_pool"]))
        case.red_flags = [f for f in d.get("red_flags", []) if isinstance(f, dict)]
        if case.verified:
            case.scope = d["_scope"]
        self.bus.emit("case_opened", agent=self.name, event_id=case.event_id, verified=case.verified,
                      reason=case.verdict_reason, pool=case.pool, scope=case.scope, red_flags=case.red_flags)
        return case

    def research(self, case: CaseFile, run: int, deficiencies: list[str]) -> list[str]:
        """FR1.4: Odin remanded the case. Search again for what is missing; return NEW doc IDs only."""
        def validate(d: dict[str, Any]) -> list[str]:
            ids = [i for i in d.get("new_doc_ids", []) if i in self.seen and i not in case.pool]
            if d.get("nothing_new") is not True and not ids:
                raise ValueError('Give "new_doc_ids" (retrieved, not already in the pool) or set "nothing_new": true.')
            return ids

        new = self.turn(
            case=case, run=run, phase="research",
            task="Odin returned the case. Search for the evidence his deficiencies say is missing. Do not repeat earlier queries.",
            payload={"deficiencies": deficiencies, "pool": case.pool, "event_id": case.event_id,
                     "scope": case.scope, "your_earlier_queries": self.queries[-20:]},
            validate=validate)
        case.pool.extend(new)
        self.bus.emit("research_done", agent=self.name, run=run, new_doc_ids=new, deficiencies=deficiencies)
        return new

"""Runtime context objects shared by the tool layer, the agents and the Thing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Limits, Settings
from app.obs.events import EventBus
from app.search.dense import Embedder, VectorIndex
from app.search.hybrid import HybridSearcher
from app.store.repo import Repo
from app.tools.asof import Clock
from app.trial.casefile import CaseFile
from app.trial.limits import Budget


@dataclass
class Services:
    """Process-wide services (one per server / CLI invocation)."""

    settings: Settings
    repo: Repo
    vectors: VectorIndex
    embedder: Embedder
    searcher: HybridSearcher
    embedder_note: str | None = None


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def fingerprint(tool: str, canonical_args: Any) -> str:
    return hashlib.sha256((tool + canonical_json(canonical_args)).encode()).hexdigest()


@dataclass
class LedgerEntry:
    call_id: str
    agent: str
    tool: str
    args: dict[str, Any]
    result: dict[str, Any]
    run: int | None
    phase: str | None


class Ledger:
    """Per-case fingerprint ledger, shared across agents and runs (NFR5)."""

    def __init__(self) -> None:
        self._entries: dict[str, LedgerEntry] = {}
        self._lock = threading.Lock()
        self.inflight: dict[str, asyncio.Event] = {}  # fingerprints being executed right now

    def get(self, fp: str) -> LedgerEntry | None:
        with self._lock:
            return self._entries.get(fp)

    def put(self, fp: str, entry: LedgerEntry) -> None:
        with self._lock:
            self._entries[fp] = entry

    def queries_by(self, agent: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "call_id": e.call_id,
                    "tool": e.tool,
                    "args": e.args,
                    "result_count": len(e.result.get("results", [])) if "results" in e.result else None,
                }
                for e in self._entries.values()
                if e.agent == agent and e.tool in ("search_documents", "list_events", "list_trade_tables")
            ]

    def entries(self) -> list[LedgerEntry]:
        with self._lock:
            return list(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)


@dataclass
class CaseContext:
    services: Services
    cf: CaseFile
    bus: EventBus
    clock: Clock
    limits: Limits
    budget: Budget
    case_dir: Path
    ledger: Ledger = field(default_factory=Ledger)
    calcs: dict[str, dict[str, Any]] = field(default_factory=dict)  # calc_id -> {inputs, outputs}
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    prompt_hashes: dict[str, str] = field(default_factory=dict)
    sides_searched: dict[str, set[int]] = field(default_factory=dict)  # side -> runs where it searched in R1
    pending_checklist: Any = None  # (checklist, gaps) between submit_rulings and submit_sufficiency

    @property
    def case_id(self) -> str:
        return self.cf.case_id

    def next_call_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"{self.cf.case_id}:{self._seq:04d}"

    @property
    def event_id(self) -> str | None:
        return self.cf.scope.event_id if self.cf.scope else None

    def emit(self, type: str, actor: str, payload: dict[str, Any] | None = None, **kw: Any) -> int:
        ev = self.bus.emit(type, actor=actor, payload=payload, **kw)
        return ev.seq

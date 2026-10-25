"""Event bus: append-only `events.jsonl` per case plus in-memory fan-out to SSE subscribers.

Every log-worthy action goes through `emit()` (OBSERVABILITY.md §1). The file is written first, then
subscribers are notified, so the dashboard is always a projection of what is on disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

SYSTEM_BUS_ID = "_system"


class Event(BaseModel):
    seq: int
    ts: str
    case_id: str
    run: int | None = None
    phase: str | None = None
    actor: str
    type: str
    payload: dict[str, Any] = {}
    usage: dict[str, int] | None = None
    latency_ms: int | None = None
    parent_seq: int | None = None


def to_json(obj: Any) -> str:
    return json.dumps(to_jsonable_python(obj), ensure_ascii=False, separators=(",", ":"))


class EventBus:
    """One bus per case. Thread-safe (ingestion emits from worker threads)."""

    def __init__(self, case_id: str, path: Path | None) -> None:
        self.case_id = case_id
        self.path = path
        self._lock = threading.Lock()
        self._seq = 0
        self._events: list[Event] = []
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[Event]]] = []
        self._fh = None
        self.context: dict[str, Any] = {"run": None, "phase": None}
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                self._load_existing(path)
            self._fh = path.open("a", encoding="utf-8")

    def _load_existing(self, path: Path) -> None:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = Event.model_validate_json(line)
                except ValueError:
                    continue
                self._events.append(ev)
                self._seq = max(self._seq, ev.seq)

    # ---------- write ----------
    def emit(
        self,
        type: str,
        *,
        actor: str,
        payload: dict[str, Any] | None = None,
        run: int | None = None,
        phase: str | None = None,
        usage: dict[str, int] | None = None,
        latency_ms: int | None = None,
        parent_seq: int | None = None,
    ) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(
                seq=self._seq,
                ts=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                case_id=self.case_id,
                run=run if run is not None else self.context.get("run"),
                phase=phase if phase is not None else self.context.get("phase"),
                actor=actor,
                type=type,
                payload=to_jsonable_python(payload or {}),
                usage=usage,
                latency_ms=latency_ms,
                parent_seq=parent_seq,
            )
            self._events.append(ev)
            if self._fh is not None:
                self._fh.write(ev.model_dump_json(exclude_none=False) + "\n")
                self._fh.flush()
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            with contextlib.suppress(RuntimeError):  # loop closed
                loop.call_soon_threadsafe(queue.put_nowait, ev)
        return ev

    def sync(self) -> None:
        """fsync at phase boundaries."""
        with self._lock:
            if self._fh is not None:
                self._fh.flush()
                with contextlib.suppress(OSError):
                    os.fsync(self._fh.fileno())

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.flush()
                self._fh.close()
                self._fh = None

    # ---------- read ----------
    @property
    def last_seq(self) -> int:
        return self._seq

    def events(self, since: int = 0) -> list[Event]:
        with self._lock:
            return [e for e in self._events if e.seq > since]

    def iter_type(self, *types: str) -> Iterator[Event]:
        for e in self.events():
            if e.type in types:
                yield e

    def subscribe(self) -> asyncio.Queue[Event]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Event] = asyncio.Queue()
        with self._lock:
            self._subscribers.append((loop, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        with self._lock:
            self._subscribers = [(lp, q) for lp, q in self._subscribers if q is not queue]


# ---------- registry ----------
_registry: dict[str, EventBus] = {}
_registry_lock = threading.Lock()


def open_bus(case_id: str, path: Path | None) -> EventBus:
    with _registry_lock:
        bus = _registry.get(case_id)
        if bus is None:
            bus = EventBus(case_id, path)
            _registry[case_id] = bus
        return bus


def get_bus(case_id: str) -> EventBus | None:
    with _registry_lock:
        return _registry.get(case_id)


def system_bus(path: Path | None = None) -> EventBus:
    """Bus for events that belong to no case (ingestion, watcher)."""
    with _registry_lock:
        bus = _registry.get(SYSTEM_BUS_ID)
        if bus is None:
            bus = EventBus(SYSTEM_BUS_ID, path)
            _registry[SYSTEM_BUS_ID] = bus
        return bus


def close_bus(case_id: str) -> None:
    with _registry_lock:
        bus = _registry.pop(case_id, None)
    if bus is not None:
        bus.close()


def emit(
    type: str,
    *,
    actor: str,
    payload: dict[str, Any] | None = None,
    case_id: str | None = None,
    bus: EventBus | None = None,
    **kwargs: Any,
) -> Event:
    """Single entry point for logging. Routes to the case bus, or the system bus when no case."""
    target = bus or (get_bus(case_id) if case_id else None) or system_bus()
    return target.emit(type, actor=actor, payload=payload, **kwargs)


def read_events_file(path: Path) -> list[Event]:
    out: list[Event] = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                with contextlib.suppress(ValueError):
                    out.append(Event.model_validate_json(line))
    return out

"""Append-only event log: every claim, query, retrieved item, memory use and ruling."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable


class EventBus:
    def __init__(self, path: str | Path | None = None, sink: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.sink = sink  # e.g. Store.add_event: persists every event as it happens
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._path = Path(path) if path else None
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, type: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            ev = {"seq": len(self.events), "ts": time.time(), "type": type, **fields}
            self.events.append(ev)
            if self._path:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, default=str) + "\n")
            if self.sink:
                try:
                    self.sink(ev)
                except Exception:  # persistence must never break a trial
                    pass
        return ev

    def of_type(self, type: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == type]

"""Live-pack watcher: new folders or zips in data/live_packs/ are ingested incrementally (2 s debounce)."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

from app.obs.events import emit

if TYPE_CHECKING:
    from app.tools.context import Services

DEBOUNCE_S = 2.0


class LivePackWatcher:
    def __init__(self, services: Services) -> None:
        self.services = services
        self._observer: Any = None
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._running = threading.Lock()
        self.last_report: dict[str, Any] | None = None

    def start(self) -> None:
        live = self.services.settings.live_packs_dir
        live.mkdir(parents=True, exist_ok=True)
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:  # pragma: no cover
            emit(
                "ingest_issue",
                actor="ingest",
                payload={
                    "file": str(live),
                    "status": "WATCHER_UNAVAILABLE",
                    "reason": "watchdog not installed",
                },
            )
            return
        watcher = self

        class Handler(FileSystemEventHandler):  # type: ignore[misc]
            def on_any_event(self, event: Any) -> None:
                if getattr(event, "event_type", "") in ("created", "modified", "moved"):
                    watcher.poke()

        self._observer = Observer()
        self._observer.schedule(Handler(), str(live), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        emit("ingest_update", actor="ingest", payload={"stage": "watching", "dir": str(live)})

    def poke(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_S, self._run)
            self._timer.daemon = True
            self._timer.start()

    def _run(self) -> None:
        from app.runtime import ingest, live_pack_roots

        if not self._running.acquire(blocking=False):
            self.poke()  # an ingestion is in progress: try again after it
            return
        try:
            time.sleep(0.2)
            roots = live_pack_roots(self.services.settings.live_packs_dir)
            if roots:
                self.last_report = ingest(self.services, roots).to_dict()
        except Exception as exc:  # never crash the server
            emit(
                "ingest_issue",
                actor="ingest",
                payload={"file": "*", "status": "WATCHER_ERROR", "reason": str(exc)},
            )
        finally:
            self._running.release()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)

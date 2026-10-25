"""Local API + dashboard server (standard library only).

    python -m nine_realms.server --port 8765

Serves index.html / app.js / styles.css and a JSON API backed by one SQLite file:
  GET  /api/health                      status, provider, whether a trial is running
  GET  /api/summary                     counts for the overview page
  GET  /api/documents?text=&country=&sector=&source_type=&tier=&limit=   corpus explorer (full-text search + filters)
  GET  /api/documents/<DOC_ID>          one document with paragraphs and parsed trade rows
  GET  /api/trade                       monthly trade rows per trade note (for the charts)
  GET  /api/feed                        news items in publication order (for the live-feed replay)
  GET  /api/cases                       saved cases
  GET  /api/cases/<id>                  full case (claims, evidence, attacks, responses, rulings, ranges, briefing)
  GET  /api/cases/<id>/events?since=N   event log, for live progress
  GET  /api/cases/<id>/briefing.md      the briefing as Markdown
  POST /api/cases {question, as_of?, options?}  start a trial in the background (one at a time) -> 202 {case_id}
                                         options: min_runs, max_runs, max_claims_per_side, max_steps_per_turn

Security: binds to 127.0.0.1; POST rejects cross-origin requests; API keys are read from the server's environment
and never sent to or accepted from the browser.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .corpus import LocalCorpus
from .env import load_env
from .events import EventBus
from .store import Store
from .thing import Thing
from .tools import Limits

ROOT = Path(__file__).resolve().parent.parent
STATIC = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/charts.js": "charts.js", "/styles.css": "styles.css"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Per-case limit overrides a client may ask for (e.g. a small trial). Anything else is ignored.
OPTION_RANGES = {"min_runs": (1, 3), "max_runs": (1, 3), "max_claims_per_side": (1, 8), "max_steps_per_turn": (4, 12)}


def make_llm(provider: str):
    from .llm import AnthropicClient, OpenAICompatClient

    if provider == "anthropic":
        return AnthropicClient(model=os.environ.get("ANTHROPIC_MODEL"), max_tokens=8192)
    if provider == "deepseek":  # OpenAI-compatible chat completions
        return OpenAICompatClient(os.environ["DEEPSEEK_API_KEY"], os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                                  base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                                  max_tokens=4096)
    return OpenAICompatClient(os.environ["GROQ_API_KEY"], os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"), max_tokens=2500)


PROVIDER_KEYS = {"deepseek": "DEEPSEEK_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "groq": "GROQ_API_KEY"}


def detect_provider() -> str:
    """NINE_REALMS_PROVIDER forces one; otherwise the first provider (in PROVIDER_KEYS order) with a key."""
    forced = os.environ.get("NINE_REALMS_PROVIDER", "")
    if forced in PROVIDER_KEYS:
        return forced if os.environ.get(PROVIDER_KEYS[forced]) else ""
    return next((p for p, key in PROVIDER_KEYS.items() if os.environ.get(key)), "")


class App:
    def __init__(self, corpus: LocalCorpus, store: Store, memory_dir: str, provider: str, port: int) -> None:
        self.corpus, self.store, self.memory_dir, self.provider, self.port = corpus, store, memory_dir, provider, port
        self.running: str | None = None
        self._lock = threading.Lock()

    def start_case(self, question: str, as_of: str, options: dict[str, int] | None = None) -> str:
        with self._lock:
            if self.running:
                raise RuntimeError(f"busy:{self.running}")
            case_id = f"CASE-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
            self.running = case_id
        threading.Thread(target=self._run, args=(case_id, question, as_of, options or {}), daemon=True).start()
        return case_id

    def _run(self, case_id: str, question: str, as_of: str, options: dict[str, int]) -> None:
        try:
            bus = EventBus(sink=lambda ev: self.store.add_event(case_id, ev))
            # small-context providers get trimmed tool results; Anthropic and DeepSeek (128k context) do not
            base = ({"max_llm_calls": 150} if self.provider in ("anthropic", "deepseek")
                    else {"max_llm_calls": 150, "max_tool_result_chars": 3500, "max_context_chars": 13000})
            lim = Limits(**base, **options)
            thing = Thing(make_llm(self.provider), self.corpus, limits=lim, bus=bus, memory_dir=self.memory_dir,
                          on_update=lambda c: self.store.save_case(c.to_dict(), self.provider))
            thing.run_case(question, as_of, case_id=case_id)
        except Exception as exc:  # never leave a case hanging as OPEN
            case = self.store.load_case(case_id)
            if case and case["status"] == "OPEN":
                case["status"], case["status_reasons"] = "ABORTED", [f"server error: {type(exc).__name__}: {exc}"]
                self.store.save_case(case, self.provider)
        finally:
            with self._lock:
                self.running = None


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Ragnarisk/1.0"

        def log_message(self, *a: Any) -> None:  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype == "application/json" else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, default=str).encode())

        def do_GET(self) -> None:
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = url.path.rstrip("/") or "/"
            try:
                if path in STATIC:
                    f = ROOT / STATIC[path]
                    return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "text/plain")
                if not path.startswith("/api/"):
                    return self._json(404, {"error": "not found"})
                return self._api_get(path, q)
            except Exception as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def _api_get(self, path: str, q: dict[str, str]) -> None:
            parts = path.split("/")[2:]
            c, s = app.corpus, app.store
            if parts == ["health"]:
                return self._json(200, {"ok": True, "provider": app.provider or None, "running": app.running, "as_of_default": c.pack_as_of})
            if parts == ["summary"]:
                cases = s.list_cases()
                return self._json(200, {"corpus": c.stats(), "store": s.counts(), "cases": cases[:5], "as_of": c.pack_as_of,
                                        "running": app.running, "can_run": bool(app.provider)})
            if parts == ["documents"]:
                tier = int(q["tier"]) if q.get("tier", "").isdigit() else None
                return self._json(200, c.list_documents(q.get("country", ""), q.get("sector", ""), q.get("source_type", ""), tier,
                                                        q.get("prefix", ""), q.get("text", ""), q.get("as_of", ""),
                                                        int(q.get("limit", 200)), int(q.get("offset", 0)),
                                                        q.get("date_from", ""), q.get("date_to", ""), q.get("exclude_type", "")))
            if len(parts) == 2 and parts[0] == "documents":
                d = c.get_document(parts[1])
                return self._json(200, d) if d else self._json(404, {"error": "no such document"})
            if parts == ["trade"]:
                return self._json(200, {"tables": c.trade_series(q.get("as_of", "")), "as_of": q.get("as_of") or c.pack_as_of})
            if parts == ["feed"]:
                return self._json(200, {"items": c.feed(q.get("as_of", ""), int(q.get("limit", 60)))})
            if parts == ["cases"]:
                return self._json(200, {"cases": s.list_cases(), "running": app.running})
            if len(parts) >= 2 and parts[0] == "cases":
                case = s.load_case(parts[1])
                if not case:
                    return self._json(404, {"error": "no such case"})
                if len(parts) == 2:
                    return self._json(200, {**case, "is_running": app.running == parts[1]})
                if parts[2] == "events":
                    return self._json(200, {"events": s.events(parts[1], int(q.get("since", -1)), int(q.get("limit", 500)),
                                                               q["types"].split(",") if q.get("types") else None),
                                            "is_running": app.running == parts[1], "status": case["status"]})
                if parts[2] == "briefing.md":
                    return self._send(200, case["briefing_md"].encode(), "text/markdown")
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path.rstrip("/")
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{app.port}", f"http://localhost:{app.port}"):
                return self._json(403, {"error": "cross-origin request refused"})
            if path != "/api/cases":
                return self._json(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 20_000)) or b"{}")
                question = str(body.get("question", "")).strip()
                as_of = str(body.get("as_of") or app.corpus.pack_as_of)
                if not 10 <= len(question) <= 1500:
                    return self._json(400, {"error": "question must be 10 to 1500 characters"})
                if not DATE.match(as_of):
                    return self._json(400, {"error": "as_of must be YYYY-MM-DD"})
                if not app.provider:
                    return self._json(503, {"error": "No LLM key is configured on the server. Set ANTHROPIC_API_KEY (or GROQ_API_KEY) and restart."})
                opts = {k: int(v) for k, v in (body.get("options") or {}).items()
                        if k in OPTION_RANGES and isinstance(v, int) and OPTION_RANGES[k][0] <= v <= OPTION_RANGES[k][1]}
                return self._json(202, {"case_id": app.start_case(question, as_of, opts), "options": opts})
            except RuntimeError as exc:
                if str(exc).startswith("busy:"):
                    return self._json(409, {"error": "A trial is already running.", "case_id": str(exc)[5:]})
                raise
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid JSON"})
            except Exception as exc:
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default=str(ROOT / "data" / "ragnarisk.sqlite"))
    ap.add_argument("--pack", default=str(ROOT / "03_Ragnarok Trial of the Nine Realms" / "dev_pack_E5"))
    ap.add_argument("--memory-dir", default=str(ROOT / "data" / "memory"))
    ap.add_argument("--import-runs", default=str(ROOT / "runs"), help="import saved runs/<case>/case.json on first start")
    args = ap.parse_args(argv)

    loaded = load_env()
    if loaded:
        print(f"loaded from .env: {', '.join(loaded)}")
    corpus = LocalCorpus(args.pack, args.db)
    store = Store(args.db)
    stale = store.abort_stale()
    if stale:
        print(f"marked {stale} unfinished case(s) from a previous run as ABORTED")
    runs = Path(args.import_runs)
    if runs.exists() and not store.list_cases():
        for d in sorted(runs.glob("*")):
            cid = store.import_run_dir(d)
            if cid:
                print(f"imported {cid}")
    provider = detect_provider()
    app = App(corpus, store, args.memory_dir, provider, args.port)
    print(f"corpus: {corpus.stats()}\nstore:  {store.counts()}")
    print(f"live trials: {'enabled via ' + provider if provider else 'DISABLED (no DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / GROQ_API_KEY in the environment)'}")
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    print(f"serving http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

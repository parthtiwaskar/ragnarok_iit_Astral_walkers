"""API + dashboard server (standard library only).

    python -m nine_realms.server                      # local: http://127.0.0.1:8765
    HOST=0.0.0.0 APP_ACCESS_TOKEN=... python -m nine_realms.server   # deployment (see deploy/README.md)

Serves index.html / app.js / charts.js / styles.css and a JSON API backed by one SQLite file:
  GET  /api/health                      liveness + whether a token is required (public)
  GET  /api/summary                     counts for the overview page
  GET  /api/documents?text=&country=&sector=&source_type=&tier=&date_from=&date_to=&limit=   corpus explorer
  GET  /api/documents/<DOC_ID>          one document with paragraphs and parsed trade rows
  GET  /api/trade                       monthly trade rows per trade note (for the charts)
  GET  /api/feed                        news items in publication order
  GET  /api/cases                       saved cases
  GET  /api/cases/<id>                  full case (claims, evidence, attacks, responses, rulings, ranges, briefing)
  GET  /api/cases/<id>/events?since=N   event log, for live progress
  GET  /api/cases/<id>/acceptance       the ten minimum acceptance criteria, checked against this case
  GET  /api/cases/<id>/briefing.md      the briefing as Markdown
  GET  /api/memory/<agent>              an agent's private memory, read-only (for the dashboard inspector)
  GET  /api/packs                       ingested packs with their intake reports (damage found, rows excluded, ...)
  POST /api/ingest (zip body)           ingest a new event pack LIVE, no restart (also: drop a folder/zip in LIVE_PACKS_DIR)
  POST /api/ingest/scan                 rescan LIVE_PACKS_DIR now
  POST /api/cases {question, as_of?, options?}  start a trial in the background -> 202 {case_id}; as_of is read from the question

Deployment safety:
  * APP_ACCESS_TOKEN protects every /api route except /api/health (Authorization: Bearer <token>)
  * the server refuses to listen on a non-loopback address without a token (override: ALLOW_INSECURE_PUBLIC=1)
  * trials cost money: MAX_TRIALS_PER_DAY and TRIAL_COOLDOWN_SECONDS cap them; one runs at a time
  * POST rejects cross-origin requests (same host, localhost, or ALLOWED_ORIGINS only)
  * API keys are read from the server's environment and never sent to or accepted from the browser
  * production mode (NINE_REALMS_ENV=production) hides internal error details from clients
"""
from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import re
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .corpus import LocalCorpus
from .dates import extract_as_of
from .env import load_env
from .events import EventBus
from .livepacks import LivePacks, UnsafeArchive
from .models import CaseFile
from .store import Store
from .thing import Thing
from .tools import Limits

__version__ = "1.0.0"
ROOT = Path(__file__).resolve().parent.parent
STATIC = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/charts.js": "charts.js", "/theme.js": "theme.js", "/styles.css": "styles.css"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Per-case limit overrides a client may ask for (e.g. a small trial). Anything else is ignored.
OPTION_RANGES = {"min_runs": (1, 3), "max_runs": (1, 3), "max_claims_per_side": (1, 8), "max_steps_per_turn": (4, 12)}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def default_pack() -> str:
    """The Runemark pack: RUNEMARK_PACK, else data/runemark (team repo layout), else the numbered dev-pack folder."""
    for cand in (os.environ.get("RUNEMARK_PACK", ""), ROOT / "data" / "runemark", ROOT / "03_Ragnarok Trial of the Nine Realms" / "dev_pack_E5"):
        if cand and (Path(cand) / "manifest.json").exists():
            return str(cand)
    return str(ROOT / "data" / "runemark")


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8765
    db: str = str(ROOT / "data" / "ragnarisk.sqlite")
    pack: str = ""
    memory_dir: str = str(ROOT / "data" / "memory")
    import_runs: str = str(ROOT / "runs")
    token: str = ""
    production: bool = False
    allowed_origins: set[str] = field(default_factory=set)
    trust_proxy: bool = False
    max_trials_per_day: int = 20
    cooldown_s: int = 30
    max_llm_calls: int = 150
    max_case_tokens: int = 2_500_000
    max_case_usd: float = 1.50  # per-trial spend cap where the provider reports cost (Anthropic); 0 = off
    max_usd_per_day: float = 0.0  # total spend cap per UTC day across trials; a trial starts only if it fits; 0 = off
    log_requests: bool = False
    live_dir: str = str(ROOT / "data" / "live_packs")
    watch_interval: float = 5.0
    max_upload_mb: int = 50
    max_concurrent: int = 1

    @classmethod
    def from_env(cls) -> "Config":
        data = os.environ.get("DATA_DIR", "")
        return cls(
            host=os.environ.get("HOST", "127.0.0.1"), port=_int("PORT", 8765),
            db=os.environ.get("NINE_REALMS_DB") or (str(Path(data) / "ragnarisk.sqlite") if data else str(ROOT / "data" / "ragnarisk.sqlite")),
            pack=default_pack(),
            memory_dir=os.environ.get("NINE_REALMS_MEMORY_DIR") or (str(Path(data) / "memory") if data else str(ROOT / "data" / "memory")),
            import_runs=os.environ.get("IMPORT_RUNS_DIR", str(ROOT / "runs")),
            token=os.environ.get("APP_ACCESS_TOKEN", ""),
            production=os.environ.get("NINE_REALMS_ENV", "").lower() == "production",
            allowed_origins={o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()},
            trust_proxy=os.environ.get("TRUST_PROXY", "") == "1",
            max_trials_per_day=_int("MAX_TRIALS_PER_DAY", 20), cooldown_s=_int("TRIAL_COOLDOWN_SECONDS", 30),
            max_llm_calls=_int("MAX_LLM_CALLS", 150), max_case_tokens=_int("MAX_CASE_TOKENS", 2_500_000), max_case_usd=_float("MAX_CASE_USD", 1.50), max_usd_per_day=_float("MAX_USD_PER_DAY", 0.0),
            log_requests=os.environ.get("LOG_LEVEL", "info").lower() in ("info", "debug"),
            live_dir=os.environ.get("LIVE_PACKS_DIR") or (str(Path(data) / "live_packs") if data else str(ROOT / "data" / "live_packs")),
            watch_interval=float(os.environ.get("WATCH_INTERVAL_SECONDS", 5)), max_upload_mb=_int("MAX_UPLOAD_MB", 50),
            max_concurrent=max(1, _int("MAX_CONCURRENT_TRIALS", 1)),
        )


def check_bind(host: str, token: str) -> None:
    """Never expose a server that can spend API money on a public interface without a token."""
    if host not in LOOPBACK and not token and os.environ.get("ALLOW_INSECURE_PUBLIC") != "1":
        raise SystemExit(f"Refusing to listen on {host} without APP_ACCESS_TOKEN: anyone could start trials on your API key. "
                         "Set APP_ACCESS_TOKEN (generate one: python -c \"import secrets;print(secrets.token_urlsafe(32))\"), "
                         "or ALLOW_INSECURE_PUBLIC=1 if the network is already protected.")


def make_llm(provider: str):
    from .llm import AnthropicClient, OpenAICompatClient

    if provider == "anthropic":
        return AnthropicClient(model=os.environ.get("ANTHROPIC_MODEL"), max_tokens=8192)
    if provider == "deepseek":  # OpenAI-compatible chat completions
        return OpenAICompatClient(os.environ["DEEPSEEK_API_KEY"], os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
                                  base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                                  max_tokens=4096)
    if provider == "gemini":  # Google AI Studio through its OpenAI-compatible endpoint
        return OpenAICompatClient(os.environ["GEMINI_API_KEY"], gemini_model(),
                                  base_url=os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
                                  max_tokens=4096)
    if provider == "github":  # GitHub Models (free with a GitHub account); free tier caps a request near 8k tokens in, 4k out
        return OpenAICompatClient(os.environ["GITHUB_MODELS_TOKEN"], github_model(),
                                  base_url=os.environ.get("GITHUB_MODELS_BASE_URL", "https://models.github.ai/inference"),
                                  max_tokens=3000)
    if provider == "openai":  # OpenAI itself, or any OpenAI-compatible gateway (e.g. OpenRouter) via OPENAI_BASE_URL
        return OpenAICompatClient(os.environ["OPENAI_API_KEY"], openai_model(), base_url=openai_base_url(), max_tokens=4096)
    return OpenAICompatClient(os.environ["GROQ_API_KEY"], os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"), max_tokens=2500)


def openai_base_url() -> str:
    return os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"


def openai_model() -> str:
    """NINE_REALMS_OPENAI_MODEL (LLM_MODEL belongs to backend/). Gateways like OpenRouter want vendor/model ids."""
    default = "openai/gpt-4.1-mini" if "openrouter.ai" in openai_base_url() else "gpt-4.1-mini"
    return os.environ.get("NINE_REALMS_OPENAI_MODEL") or default


# Auto-detection order when NINE_REALMS_PROVIDER is empty: the first provider whose key is set wins.
PROVIDER_KEYS = {"deepseek": "DEEPSEEK_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
                 "github": "GITHUB_MODELS_TOKEN", "groq": "GROQ_API_KEY"}
# Providers with large context windows keep full tool results; the rest get trimmed ones (see App._run).
LARGE_CONTEXT = {"anthropic", "deepseek", "openai", "gemini"}


def choose_provider() -> tuple[str, str]:
    """(provider or "", a one-line reason). NINE_REALMS_PROVIDER forces one; otherwise the first key set wins.

    The reason names every key that is set, because a stale key earlier in the order silently wins
    auto-detection: after swapping keys, either remove the old one or force the provider."""
    present = [key for key in PROVIDER_KEYS.values() if os.environ.get(key)]
    forced = os.environ.get("NINE_REALMS_PROVIDER", "").strip().lower()
    if forced:
        if forced not in PROVIDER_KEYS:
            return "", f"NINE_REALMS_PROVIDER={forced} is not one of {', '.join(PROVIDER_KEYS)}"
        if not os.environ.get(PROVIDER_KEYS[forced]):
            return "", f"NINE_REALMS_PROVIDER={forced} but {PROVIDER_KEYS[forced]} is not set"
        return forced, f"forced by NINE_REALMS_PROVIDER ({PROVIDER_KEYS[forced]} is set)"
    for provider, key in PROVIDER_KEYS.items():
        if os.environ.get(key):
            others = [k for k in present if k != key]
            also = f"; also set: {', '.join(others)} (set NINE_REALMS_PROVIDER to use one of those)" if others else ""
            return provider, f"auto-detected from {key}{also}"
    return "", f"no LLM key set ({' / '.join(PROVIDER_KEYS.values())})"


def detect_provider() -> str:
    return choose_provider()[0]


def github_model() -> str:
    return os.environ.get("GITHUB_MODELS_MODEL") or "openai/gpt-4.1-mini"


def gemini_model() -> str:
    return os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"


def provider_model(provider: str) -> str:
    """The model a provider will use, for logs and /api/health. Never includes a key."""
    return {
        "deepseek": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "anthropic": os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5-5",
        "openai": openai_model() if provider == "openai" else "",
        "gemini": gemini_model() if provider == "gemini" else "",
        "github": github_model() if provider == "github" else "",
        "groq": os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
    }.get(provider, "")


class App:
    def __init__(self, corpus: LocalCorpus, store: Store, cfg: Config, provider: str) -> None:
        self.corpus, self.store, self.cfg, self.provider = corpus, store, cfg, provider
        self._running: dict[str, None] = {}  # insertion-ordered set of trials in flight
        self.live: LivePacks | None = None
        self._lock = threading.Lock()
        self._last_start: dict[str, float] = {}  # client -> time of its last trial, for the cooldown

    @property
    def running(self) -> str | None:
        """The most recently started trial still running (the dashboard follows this one)."""
        return next(reversed(self._running), None)

    @running.setter
    def running(self, value: str | None) -> None:
        if value is None:
            self._running.clear()
        else:
            self._running[value] = None

    @property
    def running_ids(self) -> list[str]:
        return list(self._running)

    def allow_trial(self, client: str) -> tuple[bool, str, int]:
        """(ok, reason, retry_after_seconds). Trials spend real money, so they are rationed."""
        now = time.time()
        today = now - (now % 86400)
        if self.store.count_started_since(today) >= self.cfg.max_trials_per_day:
            return False, f"Daily trial limit reached ({self.cfg.max_trials_per_day}). Try again tomorrow.", int(today + 86400 - now)
        if self.cfg.max_usd_per_day:
            # count what finished trials spent, plus the worst case for trials still running and for this one
            spent = self.store.spent_since(today)
            committed = spent + self.cfg.max_case_usd * (len(self._running) + 1)
            if committed > self.cfg.max_usd_per_day + 1e-9:
                return (False, f"Today's AI budget is used up: ${spent:.2f} of ${self.cfg.max_usd_per_day:.2f} spent, and a trial can cost up "
                        f"to ${self.cfg.max_case_usd:.2f}. Saved answers still work; new trials resume tomorrow (UTC).", int(today + 86400 - now))
        wait = self._last_start.get(client, 0) + self.cfg.cooldown_s - now
        if wait > 0:
            return False, f"Please wait {int(wait) + 1}s before starting another trial.", int(wait) + 1
        return True, "", 0

    def start_case(self, question: str, as_of: str, options: dict[str, int] | None = None, client: str = "-") -> str:
        with self._lock:
            if len(self._running) >= self.cfg.max_concurrent:
                raise RuntimeError(f"busy:{self.running}")
            case_id = f"CASE-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
            self._running[case_id] = None
            self._last_start[client] = time.time()
        self.store.save_case(CaseFile(case_id, question, as_of).to_dict(), self.provider)  # visible (and counted) immediately
        threading.Thread(target=self._run, args=(case_id, question, as_of, options or {}), daemon=True).start()
        return case_id

    def _run(self, case_id: str, question: str, as_of: str, options: dict[str, int]) -> None:
        try:
            bus = EventBus(sink=lambda ev: self.store.add_event(case_id, ev))
            base: dict[str, Any] = {"max_llm_calls": self.cfg.max_llm_calls, "max_tokens": self.cfg.max_case_tokens,
                                    "max_usd": self.cfg.max_case_usd}
            # small-context providers, or NINE_REALMS_LEAN_CONTEXT=1 on a tight budget: shorter tool results and context
            if self.provider not in LARGE_CONTEXT or os.environ.get("NINE_REALMS_LEAN_CONTEXT") == "1":
                base |= {"max_tool_result_chars": 3500, "max_context_chars": 13000}  # fit small per-minute token budgets
            thing = Thing(make_llm(self.provider), self.corpus, limits=Limits(**base, **options), bus=bus, memory_dir=self.cfg.memory_dir,
                          on_update=lambda c: self.store.save_case(c.to_dict(), self.provider))
            thing.run_case(question, as_of, case_id=case_id)
        except Exception as exc:  # never leave a case hanging as OPEN
            case = self.store.load_case(case_id)
            if case and case["status"] == "OPEN":
                case["status"], case["status_reasons"] = "ABORTED", [f"server error: {type(exc).__name__}"]
                self.store.save_case(case, self.provider)
            print(f"[trial {case_id}] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            with self._lock:
                self._running.pop(case_id, None)


def make_handler(app: App):
    cfg = app.cfg

    class Handler(BaseHTTPRequestHandler):
        server_version = "Ragnarisk/" + __version__
        sys_version = ""

        def log_message(self, *a: Any) -> None:  # replaced by _log, which never prints query strings
            pass

        # -- plumbing ---------------------------------------------------------------
        def _client(self) -> str:
            if cfg.trust_proxy and self.headers.get("X-Forwarded-For"):
                return self.headers["X-Forwarded-For"].split(",")[0].strip()
            return self.client_address[0]

        def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict[str, str] | None = None) -> None:
            self._status = code
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype == "application/json" else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                         "Content-Security-Policy": CSP, "Cross-Origin-Resource-Policy": "same-origin", **(extra or {})}.items():
                self.send_header(k, v)
            if cfg.production and self.headers.get("X-Forwarded-Proto") == "https":
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            self.end_headers()
            if not getattr(self, "_head", False):  # HEAD gets the headers only
                self.wfile.write(body)

        def _json(self, code: int, obj: Any, extra: dict[str, str] | None = None) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), extra=extra)

        def _fail(self, exc: Exception) -> None:
            print(f"[server] {type(exc).__name__}: {exc}", file=sys.stderr)
            self._json(500, {"error": "internal error" if cfg.production else f"{type(exc).__name__}: {exc}"})

        def _log(self, started: float) -> None:
            if cfg.log_requests:
                print(f"{time.strftime('%H:%M:%S')} {self._client()} {self.command} {urlparse(self.path).path} {getattr(self, '_status', '-')} "
                      f"{(time.time() - started) * 1000:.0f}ms", flush=True)

        def _authorized(self) -> bool:
            if not cfg.token:
                return True
            got = self.headers.get("Authorization", "")
            return got.startswith("Bearer ") and hmac.compare_digest(got[7:].encode(), cfg.token.encode())

        def _discard_small_body(self, limit: int = 64 * 1024) -> None:
            """Read and drop a small request body before an early rejection. Closing a socket that still holds unread
            request data resets the connection on some systems (Windows), so the client would see a dropped connection
            instead of the 401/403/404. Large bodies are deliberately left unread: they are refused without reading."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            if 0 < n <= limit:
                self.rfile.read(n)

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            origin = origin.rstrip("/")
            host = self.headers.get("Host", "")
            same_host = urlparse(origin).netloc == host
            return same_host or origin in cfg.allowed_origins or origin in (f"http://127.0.0.1:{cfg.port}", f"http://localhost:{cfg.port}")

        # -- routes -----------------------------------------------------------------
        def do_HEAD(self) -> None:  # load balancers and uptime checkers often probe with HEAD
            self._head = True
            self.do_GET()

        def do_GET(self) -> None:
            started = time.time()
            try:
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                path = url.path.rstrip("/") or "/"
                if path in STATIC:
                    f = ROOT / STATIC[path]
                    self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "text/plain")
                elif path == "/api/health":
                    body: dict[str, Any] = {"ok": True, "version": __version__, "auth_required": bool(cfg.token)}
                    if self._authorized():
                        body |= {"provider": app.provider or None, "model": provider_model(app.provider) or None,
                                 "running": app.running, "running_ids": app.running_ids,
                                 "as_of_default": app.corpus.pack_as_of, "max_concurrent": cfg.max_concurrent}
                    self._json(200, body)
                elif not path.startswith("/api/"):
                    self._json(404, {"error": "not found"})
                elif not self._authorized():
                    self._json(401, {"error": "access token required"}, {"WWW-Authenticate": "Bearer"})
                else:
                    self._api_get(path, q)
            except Exception as exc:
                self._fail(exc)
            finally:
                self._log(started)

        def _api_get(self, path: str, q: dict[str, str]) -> None:
            parts = path.split("/")[2:]
            c, s = app.corpus, app.store
            if parts == ["summary"]:
                return self._json(200, {"corpus": c.stats(), "store": s.counts(), "cases": s.list_cases()[:5], "as_of": c.pack_as_of,
                                        "running": app.running, "can_run": bool(app.provider)})
            if parts == ["documents"]:
                tier = int(q["tier"]) if q.get("tier", "").isdigit() else None
                return self._json(200, c.list_documents(q.get("country", ""), q.get("sector", ""), q.get("source_type", ""), tier,
                                                        q.get("prefix", ""), q.get("text", ""), q.get("as_of", ""),
                                                        min(int(q.get("limit", 200)), 500), int(q.get("offset", 0)),
                                                        q.get("date_from", ""), q.get("date_to", ""), q.get("exclude_type", "")))
            if len(parts) == 2 and parts[0] == "documents":
                d = c.get_document(parts[1])
                return self._json(200, d) if d else self._json(404, {"error": "no such document"})
            if len(parts) == 2 and parts[0] == "memory":
                agent = parts[1].lower()
                if agent not in ("heimdall", "thor", "loki", "odin"):
                    return self._json(404, {"error": "no such agent"})
                path = Path(cfg.memory_dir) / f"{agent}.sqlite"
                if not path.exists():
                    return self._json(200, {"agent": agent.upper(), "memories": [], "total": 0})
                import sqlite3
                db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # the inspector can only read: memory is private to its agent
                rows = db.execute("SELECT id,kind,text,case_id,knowledge_date,tags FROM memories ORDER BY id DESC LIMIT 100").fetchall()
                total = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                db.close()
                return self._json(200, {"agent": agent.upper(), "total": total, "memories": [
                    dict(zip(("id", "kind", "text", "case_id", "knowledge_date", "tags"), r)) for r in rows]})
            if parts == ["packs"]:
                packs = c.list_packs()
                return self._json(200, {"packs": packs, "watching": bool(app.live and cfg.watch_interval > 0),
                                        "errors": app.live.errors if app.live else {}, "as_of_default": c.pack_as_of})
            if len(parts) == 2 and parts[0] == "packs":
                hit = [p for p in c.list_packs() if p["pack_id"] == parts[1]]
                return self._json(200, hit[0]) if hit else self._json(404, {"error": "no such pack"})
            if parts == ["trade"]:
                return self._json(200, {"tables": c.trade_series(q.get("as_of", "")), "as_of": q.get("as_of") or c.pack_as_of})
            if parts == ["feed"]:
                return self._json(200, {"items": c.feed(q.get("as_of", ""), min(int(q.get("limit", 60)), 200))})
            if parts == ["cases"]:
                return self._json(200, {"cases": s.list_cases(), "running": app.running})
            if len(parts) >= 2 and parts[0] == "cases":
                case = s.load_case(parts[1])
                if not case:
                    return self._json(404, {"error": "no such case"})
                if len(parts) == 2:
                    return self._json(200, {**case, "is_running": parts[1] in app.running_ids})
                if parts[2] == "events":
                    return self._json(200, {"events": s.events(parts[1], int(q.get("since", -1)), min(int(q.get("limit", 500)), 2000),
                                                               q["types"].split(",") if q.get("types") else None),
                                            "is_running": parts[1] in app.running_ids, "status": case["status"]})
                if parts[2] == "acceptance":
                    from .acceptance import check
                    res = check(case, s.events(parts[1], limit=100000), c.stats(), c.list_packs())
                    return self._json(200, {"checks": res, "passed": sum(1 for r in res if r["passed"]),
                                            "failed": sum(1 for r in res if r["passed"] is False)})
                if parts[2] == "briefing.md":
                    return self._send(200, case["briefing_md"].encode(), "text/markdown")
            self._json(404, {"error": "not found"})

        def _ingest(self, path: str) -> None:
            if app.live is None:
                return self._json(503, {"error": "live pack intake is not enabled on this server"})
            try:
                if path.endswith("/scan"):
                    reports = app.live.scan()
                    return self._json(200, {"ingested": [r["pack_id"] for r in reports], "reports": reports, "errors": app.live.errors})
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0:
                    return self._json(400, {"error": "send the pack as a zip file in the request body"})
                if length > cfg.max_upload_mb * 1024 * 1024:
                    self.close_connection = True  # the body is not read, so the connection cannot be reused
                    return self._json(413, {"error": f"upload larger than {cfg.max_upload_mb} MB"}, {"Connection": "close"})
                report = app.live.accept_upload(self.rfile.read(length), self.headers.get("X-Pack-Name", ""))
                self._json(201, {"pack_id": report["pack_id"], "report": report})
            except UnsafeArchive as exc:
                self._json(400, {"error": str(exc)})

        def do_POST(self) -> None:
            started = time.time()
            try:
                path = urlparse(self.path).path.rstrip("/")
                if not self._origin_ok():
                    self._discard_small_body()
                    return self._json(403, {"error": "cross-origin request refused"})
                if not self._authorized():
                    self._discard_small_body()
                    return self._json(401, {"error": "access token required"}, {"WWW-Authenticate": "Bearer"})
                if path == "/api/ingest" or path == "/api/ingest/scan":
                    return self._ingest(path)
                if path != "/api/cases":
                    self._discard_small_body()
                    return self._json(404, {"error": "not found"})
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 20_000)) or b"{}")
                question = str(body.get("question", "")).strip()
                as_of = str(body.get("as_of") or extract_as_of(question) or app.corpus.pack_as_of)
                if not 10 <= len(question) <= 1500:
                    return self._json(400, {"error": "question must be 10 to 1500 characters"})
                if not DATE.match(as_of):
                    return self._json(400, {"error": "as_of must be YYYY-MM-DD"})
                if not app.provider:
                    return self._json(503, {"error": "No LLM key is configured on the server. Set DEEPSEEK_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, GITHUB_MODELS_TOKEN or GROQ_API_KEY and restart."})
                ok, why, retry = app.allow_trial(self._client())
                if not ok:
                    return self._json(429, {"error": why}, {"Retry-After": str(retry)})
                opts = {k: int(v) for k, v in (body.get("options") or {}).items()
                        if k in OPTION_RANGES and isinstance(v, int) and OPTION_RANGES[k][0] <= v <= OPTION_RANGES[k][1]}
                self._json(202, {"case_id": app.start_case(question, as_of, opts, self._client()), "options": opts})
            except RuntimeError as exc:
                if str(exc).startswith("busy:"):
                    self._json(409, {"error": "A trial is already running.", "case_id": str(exc)[5:]})
                else:
                    self._fail(exc)
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON"})
            except Exception as exc:
                self._fail(exc)
            finally:
                self._log(started)

    return Handler


def build_app(cfg: Config) -> App:
    corpus = LocalCorpus(cfg.pack, cfg.db)
    store = Store(cfg.db)
    stale = store.abort_stale()
    if stale:
        print(f"marked {stale} unfinished case(s) from a previous run as ABORTED")
    runs = Path(cfg.import_runs)
    if runs.exists() and not store.list_cases():
        for d in sorted(runs.glob("*")):
            cid = store.import_run_dir(d)
            if cid:
                print(f"imported {cid}")
    app = App(corpus, store, cfg, detect_provider())
    app.live = LivePacks(corpus, cfg.live_dir, cfg.watch_interval)
    for report in app.live.scan():  # packs already waiting in the drop folder
        print(f"ingested live pack {report['pack_id']}: {report['ingested']} documents, {len(report['quarantined'])} quarantined")
    app.live.start()
    return app


def main(argv: list[str] | None = None) -> int:
    loaded = load_env()
    base = Config.from_env()
    ap = argparse.ArgumentParser(description="Ragnarisk API + dashboard server")
    ap.add_argument("--host", default=base.host)
    ap.add_argument("--port", type=int, default=base.port)
    ap.add_argument("--db", default=base.db)
    ap.add_argument("--pack", default=base.pack)
    ap.add_argument("--memory-dir", default=base.memory_dir)
    ap.add_argument("--import-runs", default=base.import_runs, help="import saved runs/<case>/case.json on first start")
    args = ap.parse_args(argv)
    cfg = base
    cfg.host, cfg.port, cfg.db, cfg.pack, cfg.memory_dir, cfg.import_runs = args.host, args.port, args.db, args.pack, args.memory_dir, args.import_runs

    check_bind(cfg.host, cfg.token)
    if loaded:
        print(f"loaded from .env: {', '.join(loaded)}")
    app = build_app(cfg)
    print(f"corpus: {app.corpus.stats()}\nstore:  {app.store.counts()}")
    provider, why = choose_provider()
    if provider:
        print(f"live trials: enabled via {provider} (model {provider_model(provider)}) - {why}")
    else:
        print(f"live trials: DISABLED - {why}")
    print(f"auth: {'access token required' if cfg.token else 'OFF (loopback only)'} · limits: {cfg.max_trials_per_day} trials/day, "
          f"{cfg.cooldown_s}s cooldown · ${cfg.max_case_usd:.2f} spend cap per trial{f', ${cfg.max_usd_per_day:.2f}/day' if cfg.max_usd_per_day else ''} · {cfg.max_concurrent} concurrent · mode: {'production' if cfg.production else 'development'}")
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(app))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())  # graceful stop in containers
    print(f"serving http://{cfg.host}:{cfg.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

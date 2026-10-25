"""Wiring: build services, the four agents (each with its own LLM client and memory) and run a case."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from app.agents.heimdall import Heimdall
from app.agents.odin import Odin
from app.agents.sides import Loki, Thor
from app.config import AGENTS, Settings
from app.ingest.pipeline import Ingestor, IngestReport, unzip_all
from app.llm.client import LLMClient
from app.memory.store import MemoryStore
from app.obs.artifacts import Artifacts
from app.obs.events import close_bus, emit, open_bus, system_bus
from app.search.dense import VectorIndex, make_embedder
from app.search.hybrid import HybridConfig, HybridSearcher
from app.store.db import Database
from app.store.repo import Repo
from app.tools.asof import Clock
from app.tools.context import CaseContext, Services
from app.trial.casefile import CaseFile
from app.trial.limits import Budget
from app.trial.thing import AgentSet, Thing


def build_services(settings: Settings) -> Services:
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    system_bus(settings.runs_dir / "_ingest" / "events.jsonl")
    db = Database(settings.db_path)
    repo = Repo(db)
    embedder, note = make_embedder(
        settings.EMBEDDINGS,
        settings.EMBEDDING_MODEL,
        settings.OPENAI_EMBEDDING_MODEL,
        settings.OPENAI_API_KEY,
    )
    if note:
        emit(
            "ingest_issue",
            actor="system",
            payload={"file": "*", "status": "EMBEDDER_FALLBACK", "reason": note},
        )
    vectors = VectorIndex(settings.vectors_dir, embedder.name, embedder.dim)
    searcher = HybridSearcher(
        repo,
        vectors,
        embedder,
        HybridConfig(rrf_k=settings.RRF_K, w_bm25=settings.W_BM25, w_dense=settings.W_DENSE),
    )
    return Services(
        settings=settings,
        repo=repo,
        vectors=vectors,
        embedder=embedder,
        searcher=searcher,
        embedder_note=note,
    )


def ingest(services: Services, paths: list[tuple[Path, str | None]] | None = None) -> IngestReport:
    s = services.settings
    if paths is None:
        paths = [(s.runemark_dir, None), *live_pack_roots(s.live_packs_dir)]
    ing = Ingestor(services.repo, services.vectors, services.embedder, s.INGEST_FILE_TIMEOUT_S, s.runs_dir)
    return ing.ingest(paths)


def live_pack_roots(live_dir: Path) -> list[tuple[Path, str | None]]:
    """Each folder (or unzipped archive) under live_packs/ is its own pack."""
    if not live_dir.exists():
        return []
    unzip_all(live_dir)
    return [(p, p.name) for p in sorted(live_dir.iterdir()) if p.is_dir() and not p.name.startswith(".")]


def make_llm(agent: str, settings: Settings) -> LLMClient:
    from app.llm.fake import FaultyLLM, PolicyLLM

    if settings.LLM_FAULT_INJECT and agent in [x.strip() for x in settings.LLM_FAULT_INJECT.split(",")]:
        return FaultyLLM(message=f"LLM_FAULT_INJECT={agent}")
    provider = settings.provider_for(agent)
    model = settings.model_for(agent)
    timeout = max(10.0, settings.TURN_TIMEOUT_S - 5)
    if provider == "anthropic":
        from app.llm.anthropic_client import AnthropicClient

        return AnthropicClient(
            model, settings.ANTHROPIC_API_KEY, settings.ANTHROPIC_EFFORT, timeout_s=timeout
        )
    if provider == "openai":
        from app.llm.openai_client import OpenAIClient

        return OpenAIClient(model, settings.OPENAI_API_KEY, timeout_s=timeout, base_url=settings.OPENAI_BASE_URL)
    return PolicyLLM(agent, settings)


class AgentPool:
    """The four agents. Each owns its memory store (one SQLite file per agent); nobody else gets a handle."""

    def __init__(self, services: Services, llm_override: dict[str, LLMClient] | None = None) -> None:
        s = services.settings
        s.memory_dir.mkdir(parents=True, exist_ok=True)
        self.memories = {a: MemoryStore(a, s.memory_dir / f"{a}.sqlite", services.embedder) for a in AGENTS}
        llms = {a: (llm_override or {}).get(a) or make_llm(a, s) for a in AGENTS}
        self.llms = llms
        self.set = AgentSet(
            heimdall=Heimdall("heimdall", llms["heimdall"], self.memories["heimdall"], s),
            thor=Thor("thor", llms["thor"], self.memories["thor"], s),
            loki=Loki("loki", llms["loki"], self.memories["loki"], s),
            odin=Odin("odin", llms["odin"], self.memories["odin"], s),
        )

    def models(self) -> dict[str, Any]:
        return {
            a: {"provider": getattr(c, "provider", "?"), "model": getattr(c, "model", "?")}
            for a, c in self.llms.items()
        }

    def close(self) -> None:
        for m in self.memories.values():
            m.close()


def open_case(services: Services, question: str, as_of: date, case_id: str | None = None) -> CaseContext:
    s = services.settings
    repo = services.repo
    pin = repo.current_ingest_seq()
    cf = CaseFile.open(question, as_of, repo.index_version(), pinned_ingest_seq=pin)
    if case_id:
        cf.case_id = case_id
    case_dir = s.runs_dir / cf.case_id
    bus = open_bus(cf.case_id, case_dir / "events.jsonl")
    clock = Clock(as_of=as_of, pin=pin, trade_rule=s.TRADE_ROW_VISIBILITY)
    return CaseContext(
        services=services,
        cf=cf,
        bus=bus,
        clock=clock,
        limits=s.limits,
        budget=Budget(s.limits),
        case_dir=case_dir,
    )


async def run_case(
    services: Services,
    question: str,
    as_of: date,
    pool: AgentPool | None = None,
    ctx: CaseContext | None = None,
) -> CaseFile:
    own_pool = pool is None
    pool = pool or AgentPool(services)
    ctx = ctx or open_case(services, question, as_of)
    try:
        thing = Thing(ctx, pool.set, Artifacts(ctx.case_dir), pool.models())
        return await thing.run()
    finally:
        close_bus(ctx.case_id)
        if own_pool:
            pool.close()

"""Hybrid search: BM25 + dense cosine fused with Reciprocal Rank Fusion, then doc-level collapse."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.search.dense import Embedder, VectorIndex
from app.search.filters import SearchFilters
from app.search.fts import allowed_chunks, bm25_search
from app.store.repo import Repo
from app.tools.asof import Clock, as_of_status

SNIPPET_CHARS = 480


@dataclass
class HybridConfig:
    rrf_k: int = 60
    w_bm25: float = 1.0
    w_dense: float = 1.0
    candidates: int = 50


class HybridSearcher:
    def __init__(
        self, repo: Repo, vectors: VectorIndex, embedder: Embedder, cfg: HybridConfig | None = None
    ) -> None:
        self.repo = repo
        self.vectors = vectors
        self.embedder = embedder
        self.cfg = cfg or HybridConfig()

    def search(self, query: str, filters: SearchFilters, clock: Clock, k: int = 8) -> dict[str, Any]:
        f = filters.clamp(clock)
        bm = bm25_search(self.repo, query, f, clock, self.cfg.candidates)
        dense: list[tuple[str, float]] = []
        if len(self.vectors) and query.strip():
            allowed = allowed_chunks(self.repo, f, clock)
            if allowed:
                qv = self.embedder.embed_query(query)
                dense = self.vectors.search(qv, allowed, self.cfg.candidates)
        bm_rank = {cid: i + 1 for i, (cid, _) in enumerate(bm)}
        dn_rank = {cid: i + 1 for i, (cid, _) in enumerate(dense)}
        fused: dict[str, float] = {}
        for cid, r in bm_rank.items():
            fused[cid] = fused.get(cid, 0.0) + self.cfg.w_bm25 / (self.cfg.rrf_k + r)
        for cid, r in dn_rank.items():
            fused[cid] = fused.get(cid, 0.0) + self.cfg.w_dense / (self.cfg.rrf_k + r)
        ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
        # collapse to one result per doc (best chunk as snippet, second-best chunk id kept)
        by_doc: dict[str, list[tuple[str, float]]] = {}
        order: list[str] = []
        for cid, score in ranked:
            doc_id = cid.split("#c")[0]
            if doc_id not in by_doc:
                by_doc[doc_id] = []
                order.append(doc_id)
            if len(by_doc[doc_id]) < 2:
                by_doc[doc_id].append((cid, score))
        results: list[dict[str, Any]] = []
        for doc_id in order[:k]:
            d = self.repo.get_doc(doc_id)
            if d is None:
                continue
            best_cid, best_score = by_doc[doc_id][0]
            ch = self.repo.chunk(best_cid)
            snippet = (ch["text"] if ch else "")[:SNIPPET_CHARS]
            st = as_of_status(self.repo, doc_id, clock)
            results.append(
                {
                    "doc_id": doc_id,
                    "title": d.title,
                    "outlet": d.outlet,
                    "tier": d.tier,
                    "pub_date": d.pub_date,
                    "event_id": d.event_id,
                    "country": d.countries,
                    "sector": d.sectors,
                    "source_type": d.source_type,
                    "status": d.status,
                    "as_of_status": {
                        "retracted": st["retracted"],
                        "retracted_by": st["retracted_by"],
                        "corrected_by": st["corrected_by"],
                    },
                    "flags": d.flags,
                    "snippet": snippet,
                    "chunk_id": best_cid,
                    "chunk_ids": [c for c, _ in by_doc[doc_id]],
                    "scores": {
                        "rrf": round(best_score, 5),
                        "bm25_rank": bm_rank.get(best_cid),
                        "dense_rank": dn_rank.get(best_cid),
                    },
                }
            )
        note = None if results else "no results for these filters"
        return {"query": query, "filters": f.active(), "results": results, "note": note}

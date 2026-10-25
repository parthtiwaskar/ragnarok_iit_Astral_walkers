"""Hybrid search on the real E5 pack, using the deterministic built-in embedder (no model download)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nine_realms import embed
from nine_realms.corpus import LocalCorpus

ROOT = Path(__file__).resolve().parents[2]
PACK = next((p for p in (ROOT / "03_Ragnarok Trial of the Nine Realms" / "dev_pack_E5", ROOT / "data" / "runemark") if (p / "manifest.json").exists()), None)
pytestmark = pytest.mark.skipif(PACK is None, reason="Runemark pack not found")
AS_OF = "2027-01-25"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    os.environ["NINE_REALMS_EMBEDDER"] = "hash"
    embed._cached = None
    return LocalCorpus(PACK, tmp_path_factory.mktemp("db") / "t.sqlite")


def search(c, q, **kw):
    return c.call("search_documents", {"query": q, "as_of": AS_OF, "limit": 8, **kw})["results"]


def test_every_paragraph_has_a_stored_vector(corpus):
    s = corpus.stats()
    assert s["embeddings"] == s["paragraphs"] > 0 and s["embed_backend"] == "hash-ngram-512"


def test_vectors_are_reused_when_the_database_is_reopened(corpus):
    again = LocalCorpus(PACK, corpus.db.execute("PRAGMA database_list").fetchone()[2])
    assert again.stats()["embeddings"] == corpus.stats()["embeddings"] and again.reused


def test_semantic_side_finds_what_exact_words_miss(corpus):
    q = "whaet imprts Midgrad"  # typos: no exact-word hit exists
    assert search(corpus, q, mode="lexical") == []
    hits = search(corpus, q, mode="hybrid")
    assert hits and all(h["matched_by"] == ["semantic"] for h in hits)
    assert any(h["country"] == "Midgard" or h["sector"] == "Wheat" for h in hits)


def test_documents_found_by_both_sides_rank_first(corpus):
    hits = search(corpus, "Ravik Rail Link capacity")
    assert hits[0]["matched_by"] == ["lexical", "semantic"]


def test_filters_apply_to_both_sides_including_the_clock(corpus):
    early = corpus.call("search_documents", {"query": "corridor agreement wheat Midgard", "as_of": "2026-09-30", "limit": 20})["results"]
    assert early and all(h["date"] <= "2026-09-30" for h in early)
    tier1 = search(corpus, "corridor agreement expired", tier_max=1)
    assert tier1 and all(h["tier"] == 1 for h in tier1)
    assert all(h["country"] == "Midgard" for h in search(corpus, "wheat imports fell", country="Midgard"))


def test_duplicates_are_not_returned_twice(corpus):
    ids = [h["doc_id"] for h in search(corpus, "Millers look beyond Orvan as wheat cargoes slip")]
    assert not ({"ORV-0006", "ORV-0040"} <= set(ids))


def test_hash_embedder_is_normalised_and_deterministic():
    e = embed.HashEmbedder()
    a, b = e.encode_query("grain shipments"), e.encode_query("grain shipments")
    assert a == b and abs(embed.dot(a, a) - 1) < 1e-4
    near = embed.dot(e.encode_query("midgard imports"), e.encode_query("midgrad imprts"))  # typos still similar
    far = embed.dot(e.encode_query("midgard imports"), e.encode_query("canal pilot schedule"))
    assert near > far

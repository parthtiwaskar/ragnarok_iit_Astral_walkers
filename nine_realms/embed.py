"""Embedders for the semantic half of hybrid search.

  * FastEmbedder: BAAI/bge-small-en-v1.5 (384-d, ONNX, CPU) via `fastembed` when it is installed
  * HashEmbedder: dependency-free fallback (hashed word + character-trigram features, 512-d). It is not "understanding",
    but it matches paraphrase-ish variants and typos that exact-word search misses, and it keeps search working offline.

Pick with NINE_REALMS_EMBEDDER = auto (default) | fastembed | hash. Vectors are L2-normalised so dot product = cosine.
"""
from __future__ import annotations

import math
import os
import re
import zlib
from array import array
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parent.parent
_WORD = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    name: str
    dim: int
    min_score: float  # cosine below this is treated as "no semantic match"
    margin: float  # ...and so is anything this far below the best hit

    def encode_docs(self, texts: list[str]) -> list[array]: ...
    def encode_query(self, text: str) -> array: ...


def _norm(vec: list[float]) -> array:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return array("f", (x / n for x in vec))


def _stem(w: str) -> str:
    for suf in ("ings", "ing", "edly", "ed", "es", "s", "ly"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


class HashEmbedder:
    name = "hash-ngram-512"
    dim = 512
    min_score = 0.12
    margin = 0.15  # semantic hits must be within this cosine of the best hit

    def _vec(self, text: str) -> array:
        v = [0.0] * self.dim
        for w in _WORD.findall(text.lower()):
            s = _stem(w)
            feats = [("w", s)] + [("c", f"^{s}$"[i: i + 3]) for i in range(max(len(s), 1))]  # every trigram of ^word$
            for kind, f in feats:
                h = zlib.crc32(f"{kind}:{f}".encode())
                v[h % self.dim] += (1.0 if h & 0x80000000 else -1.0) * (2.0 if kind == "w" else 1.0)
        return _norm(v)

    def encode_docs(self, texts: list[str]) -> list[array]:
        return [self._vec(t) for t in texts]

    def encode_query(self, text: str) -> array:
        return self._vec(text)


class FastEmbedder:
    dim = 384
    min_score = 0.58  # measured on the E5 pack: unrelated queries top out at ~0.48-0.54, genuine ones at 0.75-0.80
    margin = 0.10

    def __init__(self, model: str | None = None) -> None:
        from fastembed import TextEmbedding  # optional dependency

        self.model_name = model or os.environ.get("NINE_REALMS_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
        self.name = f"fastembed:{self.model_name}"
        cache = os.environ.get("FASTEMBED_CACHE") or str(ROOT / "data" / "models")
        self._m = TextEmbedding(self.model_name, cache_dir=cache)

    def encode_docs(self, texts: list[str]) -> list[array]:
        return [_norm([float(x) for x in v]) for v in self._m.embed(texts)]

    def encode_query(self, text: str) -> array:
        q = getattr(self._m, "query_embed", None)
        v = next(iter(q([text]))) if q else next(iter(self._m.embed([text])))
        return _norm([float(x) for x in v])


_cached: Embedder | None = None


def get_embedder(force: str | None = None) -> Embedder:
    """auto = FastEmbedder if usable, else HashEmbedder. The choice is cached per process."""
    global _cached
    choice = (force or os.environ.get("NINE_REALMS_EMBEDDER", "auto")).lower()
    if _cached is not None and force is None:
        return _cached
    emb: Embedder
    if choice == "hash":
        emb = HashEmbedder()
    else:
        try:
            emb = FastEmbedder()
        except Exception:
            if choice == "fastembed":
                raise
            emb = HashEmbedder()
    if force is None:
        _cached = emb
    return emb


def dot(a: array, b: array) -> float:
    return sum(x * y for x, y in zip(a, b))

"""Dense embeddings and the in-memory vector index (`vectors.npy` + `vector_ids.json`).

Embedders: fastembed (bge-small, local ONNX) | openai | hash. `hash` is a deterministic hashed n-gram
embedder used offline and in tests, and as the automatic fallback when the configured embedder cannot load.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import threading
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

Vec = npt.NDArray[np.float32]
_WORD = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    name: str
    dim: int

    def embed_passages(self, texts: list[str]) -> Vec: ...

    def embed_query(self, text: str) -> Vec: ...


def _l2(m: Vec) -> Vec:
    norms = np.linalg.norm(m, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    out: Vec = (m / norms).astype(np.float32)
    return out


class HashEmbedder:
    """Signed feature hashing of word unigrams, bigrams and character 4-grams. Deterministic, no model."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim
        self.name = f"hash-{dim}"

    def _features(self, text: str) -> list[str]:
        words = _WORD.findall(text.lower())
        stems = [w[:6] for w in words]
        feats = stems + [f"{a}_{b}" for a, b in itertools.pairwise(stems)]
        for w in words:
            padded = f"#{w}#"
            feats.extend(padded[i : i + 4] for i in range(max(1, len(padded) - 3)))
        return feats

    def _embed(self, text: str) -> Vec:
        v = np.zeros(self.dim, dtype=np.float32)
        for f in self._features(text):
            h = hashlib.blake2b(f.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            v[idx] += sign
        v = np.sign(v) * np.log1p(np.abs(v))
        return v

    def embed_passages(self, texts: list[str]) -> Vec:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _l2(np.stack([self._embed(t) for t in texts]))

    def embed_query(self, text: str) -> Vec:
        row: Vec = _l2(self._embed(text)[None, :])[0]
        return row


class FastEmbedEmbedder:
    def __init__(self, model: str) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model)
        self.name = f"fastembed:{model}"
        probe = next(iter(self._model.embed(["warm up"])))
        self.dim = len(probe)

    def embed_passages(self, texts: list[str]) -> Vec:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        embed = getattr(self._model, "passage_embed", self._model.embed)
        return _l2(np.asarray(list(embed(texts)), dtype=np.float32))

    def embed_query(self, text: str) -> Vec:
        if hasattr(self._model, "query_embed"):
            vecs = list(self._model.query_embed(text))
        else:
            vecs = list(self._model.embed([text]))
        row: Vec = _l2(np.asarray(vecs, dtype=np.float32))[0]
        return row


class OpenAIEmbedder:
    def __init__(self, model: str, api_key: str | None) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model
        self.name = f"openai:{model}"
        self.dim = len(self._embed(["warm up"])[0])

    def _embed(self, texts: list[str]) -> Vec:
        resp = self._client.embeddings.create(model=self._model, input=texts)
        return np.asarray([d.embedding for d in resp.data], dtype=np.float32)

    def embed_passages(self, texts: list[str]) -> Vec:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        out = [self._embed(texts[i : i + 256]) for i in range(0, len(texts), 256)]
        return _l2(np.concatenate(out))

    def embed_query(self, text: str) -> Vec:
        row: Vec = _l2(self._embed([text]))[0]
        return row


def make_embedder(
    kind: str, model: str, openai_model: str, api_key: str | None
) -> tuple[Embedder, str | None]:
    """Returns (embedder, fallback_reason). Never raises: falls back to the hash embedder."""
    if kind == "hash":
        return HashEmbedder(), None
    try:
        if kind == "openai":
            return OpenAIEmbedder(openai_model, api_key), None
        return FastEmbedEmbedder(model), None
    except Exception as exc:
        return (
            HashEmbedder(),
            f"{kind} embedder unavailable ({type(exc).__name__}: {str(exc)[:200]}); using hash",
        )


class VectorIndex:
    """Chunk-id -> unit vector matrix. Brute-force cosine; the corpus is small."""

    def __init__(self, directory: Path | None, embedder_name: str, dim: int) -> None:
        self.directory = directory
        self.embedder_name = embedder_name
        self.dim = dim
        self._lock = threading.RLock()
        self.ids: list[str] = []
        self._pos: dict[str, int] = {}
        self.matrix: Vec = np.zeros((0, dim), dtype=np.float32)
        self.stale = False  # embedder changed since the vectors were written: rebuild needed
        if directory is not None:
            self._load()

    def _paths(self) -> tuple[Path, Path, Path]:
        assert self.directory is not None
        return (
            self.directory / "vectors.npy",
            self.directory / "vector_ids.json",
            self.directory / "vectors_meta.json",
        )

    def _load(self) -> None:
        vp, ip, mp = self._paths()
        if not (vp.exists() and ip.exists()):
            return
        try:
            meta: dict[str, Any] = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
            if meta.get("embedder") != self.embedder_name:
                self.stale = True
                return
            m = np.load(vp)
            ids = json.loads(ip.read_text(encoding="utf-8"))
            if m.shape[0] == len(ids) and m.shape[1] == self.dim:
                self.matrix = m.astype(np.float32)
                self.ids = list(ids)
                self._pos = {c: i for i, c in enumerate(self.ids)}
            else:
                self.stale = True
        except (OSError, ValueError):
            self.stale = True

    def save(self) -> None:
        if self.directory is None:
            return
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            vp, ip, mp = self._paths()
            np.save(vp, self.matrix)
            ip.write_text(json.dumps(self.ids), encoding="utf-8")
            mp.write_text(json.dumps({"embedder": self.embedder_name, "dim": self.dim}), encoding="utf-8")

    def upsert(self, ids: list[str], vecs: Vec) -> None:
        with self._lock:
            self.remove(ids)
            if not ids:
                return
            start = len(self.ids)
            self.ids.extend(ids)
            self.matrix = np.concatenate([self.matrix, vecs.astype(np.float32)]) if len(self.matrix) else vecs
            for i, c in enumerate(ids):
                self._pos[c] = start + i

    def remove(self, ids: list[str]) -> None:
        with self._lock:
            drop = {c for c in ids if c in self._pos}
            if not drop:
                return
            keep = [i for i, c in enumerate(self.ids) if c not in drop]
            self.ids = [self.ids[i] for i in keep]
            self.matrix = self.matrix[keep] if keep else np.zeros((0, self.dim), dtype=np.float32)
            self._pos = {c: i for i, c in enumerate(self.ids)}

    def remove_prefix(self, prefix: str) -> None:
        self.remove([c for c in self.ids if c.startswith(prefix)])

    def search(self, q: Vec, allowed: set[str] | None, top: int) -> list[tuple[str, float]]:
        with self._lock:
            if not self.ids:
                return []
            if allowed is not None:
                idx = [self._pos[c] for c in allowed if c in self._pos]
                if not idx:
                    return []
                sub = self.matrix[idx]
                scores = sub @ q
                order = np.argsort(-scores)[:top]
                return [(self.ids[idx[i]], float(scores[i])) for i in order]
            scores = self.matrix @ q
            order = np.argsort(-scores)[:top]
            return [(self.ids[i], float(scores[i])) for i in order]

    def __len__(self) -> int:
        return len(self.ids)

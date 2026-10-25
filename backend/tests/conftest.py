"""Shared fixtures. Every test runs offline: hash embeddings + the FakeLLM policy player."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.runtime import build_services, ingest
from app.tools.asof import Clock
from app.tools.context import Services

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINI = FIXTURES / "mini_runemark"
PACK = FIXTURES / "live_pack_d"
AS_OF = date(2025, 4, 10)
QUESTION = "How exposed is Brenmark's electronics sector to the Serpent Canal closure?"


def make_settings(root: Path, index_dir: Path | None = None, **over: Any) -> Settings:
    index_dir = index_dir or root / "index"
    base: dict[str, Any] = dict(
        RUNEMARK_DIR=str(MINI),
        LIVE_PACKS_DIR=str(root / "live_packs"),
        DB_PATH=str(index_dir / "runemark.sqlite"),
        VECTORS_DIR=str(index_dir),
        MEMORY_DIR=str(root / "memory"),
        RUNS_DIR=str(root / "runs"),
        LLM_PROVIDER="fake",
        EMBEDDINGS="hash",
        _env_file=None,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture(scope="session")
def index_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One ingested copy of the fixture corpus, shared by read-only tests."""
    root = tmp_path_factory.mktemp("shared")
    s = make_settings(root)
    svc = build_services(s)
    ingest(svc)
    return root / "index"


@pytest.fixture()
def settings(tmp_path: Path, index_dir: Path) -> Settings:
    local = tmp_path / "index"
    shutil.copytree(index_dir, local)
    return make_settings(tmp_path, local)


@pytest.fixture()
def services(settings: Settings) -> Services:
    return build_services(settings)


@pytest.fixture()
def clock() -> Clock:
    return Clock(as_of=AS_OF)

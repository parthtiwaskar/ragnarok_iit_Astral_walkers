"""Settings loaded from `.env` (repo root) via pydantic-settings.

Every limit, model choice, path and threshold lives here. Nothing else in the codebase hard-codes them.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]

AgentName = Literal["heimdall", "thor", "loki", "odin"]
AGENTS: tuple[AgentName, ...] = ("heimdall", "thor", "loki", "odin")


class Limits(BaseModel):
    """Immutable hard limits passed to every turn (TRIAL_PROTOCOL §3)."""

    model_config = ConfigDict(frozen=True)

    MAX_RUNS: int
    MAX_ROUND2_EXCHANGES: int
    MAX_CLAIMS_PER_SIDE: int
    MIN_ATTACKS_PER_SIDE: int
    MAX_ATTACKS_PER_SIDE: int
    MAX_TOOL_CALLS_PER_TURN: int
    MAX_TOOL_CALLS_PER_CASE: int
    MAX_TOKENS_PER_TURN: int
    MAX_TOKENS_PER_CASE: int
    MAX_REPROMPTS: int
    LLM_MAX_RETRIES: int
    TOOL_MAX_RETRIES: int
    TOOL_TIMEOUT_S: float
    TURN_TIMEOUT_S: float
    CASE_TIMEOUT_S: float
    MAX_REMAND_QUERIES: int


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ---- LLM provider ----
    LLM_PROVIDER: Literal["openai", "anthropic", "fake"] = "openai"
    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str | None = None  # any OpenAI-compatible endpoint, e.g. https://openrouter.ai/api/v1
    ANTHROPIC_API_KEY: str | None = None
    LLM_MODEL: str | None = None
    LLM_TEMPERATURE: float = 0.2  # sent only to providers/models that accept sampling parameters
    LLM_MAX_OUTPUT_TOKENS: int = 16000
    ANTHROPIC_EFFORT: str | None = "high"  # output_config.effort for Claude models; blank to omit

    HEIMDALL_PROVIDER: str | None = None
    HEIMDALL_MODEL: str | None = None
    THOR_PROVIDER: str | None = None
    THOR_MODEL: str | None = None
    LOKI_PROVIDER: str | None = None
    LOKI_MODEL: str | None = None
    ODIN_PROVIDER: str | None = None
    ODIN_MODEL: str | None = None

    # ---- Embeddings ----
    EMBEDDINGS: Literal["fastembed", "openai", "hash"] = "fastembed"
    EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"

    # ---- Paths ----
    RUNEMARK_DIR: str = "./data/runemark"
    LIVE_PACKS_DIR: str = "./data/live_packs"
    DB_PATH: str = "./data/index/runemark.sqlite"
    VECTORS_DIR: str = "./data/index"
    MEMORY_DIR: str = "./memory"
    RUNS_DIR: str = "./runs"

    # ---- Trial limits (hard) ----
    MAX_RUNS: int = 3
    MAX_ROUND2_EXCHANGES: int = 1
    MAX_CLAIMS_PER_SIDE: int = 8
    MIN_ATTACKS_PER_SIDE: int = 1
    MAX_ATTACKS_PER_SIDE: int = 4
    MAX_TOOL_CALLS_PER_TURN: int = 12
    MAX_TOOL_CALLS_PER_CASE: int = 200
    MAX_TOKENS_PER_TURN: int = 60000
    MAX_TOKENS_PER_CASE: int = 600000
    MAX_REPROMPTS: int = 1
    LLM_MAX_RETRIES: int = 2
    TOOL_MAX_RETRIES: int = 1
    TOOL_TIMEOUT_S: float = 15
    TURN_TIMEOUT_S: float = 180
    CASE_TIMEOUT_S: float = 1200
    MAX_REMAND_QUERIES: int = 10

    # ---- Case building ----
    POOL_SIZE_R1: int = 20
    MAX_SCOPE_ROWS: int = 6
    HORIZON_MONTHS: int = 3
    VERIFY_MIN_TIER1: int = 1
    VERIFY_MIN_TIER2_OUTLETS: int = 2

    # ---- Exposure model ----
    EXPOSURE_METRIC: Literal["pct_value", "pct_volume", "abs_value"] = "pct_value"
    BASELINE_MONTHS: int = 6
    ANCHOR_TOL: float = 0.3
    MIN_PARAM_WIDTH: float = 0.05
    MAX_CONFIDENT_WIDTH_PP: float = 15
    TRADE_ROW_VISIBILITY: Literal["completed_month", "month_start"] = "completed_month"

    # ---- Search ----
    SEARCH_K_DEFAULT: int = 8
    RRF_K: int = 60
    W_BM25: float = 1.0
    W_DENSE: float = 1.0
    QUOTE_MATCH_MIN: float = 90

    # ---- Memory ----
    MEMORY_TOP_K: int = 5
    MEMORY_MAX_LESSONS_PER_CASE: int = 3

    # ---- Ingestion ----
    INGEST_FILE_TIMEOUT_S: float = 20
    WATCH_LIVE_PACKS: bool = True

    # ---- Server / UI ----
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    CORS_ORIGINS: str = "http://localhost:5173"
    LOG_LEVEL: str = "INFO"

    # ---- Testing / chaos ----
    LLM_FAULT_INJECT: str | None = None

    @field_validator(
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "LLM_MODEL",
        "HEIMDALL_PROVIDER",
        "HEIMDALL_MODEL",
        "THOR_PROVIDER",
        "THOR_MODEL",
        "LOKI_PROVIDER",
        "LOKI_MODEL",
        "ODIN_PROVIDER",
        "ODIN_MODEL",
        "LLM_FAULT_INJECT",
        "ANTHROPIC_EFFORT",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    # ---- derived ----
    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()

    @property
    def runemark_dir(self) -> Path:
        return self.path(self.RUNEMARK_DIR)

    @property
    def live_packs_dir(self) -> Path:
        return self.path(self.LIVE_PACKS_DIR)

    @property
    def db_path(self) -> Path:
        return self.path(self.DB_PATH)

    @property
    def vectors_dir(self) -> Path:
        return self.path(self.VECTORS_DIR)

    @property
    def memory_dir(self) -> Path:
        return self.path(self.MEMORY_DIR)

    @property
    def runs_dir(self) -> Path:
        return self.path(self.RUNS_DIR)

    @property
    def limits(self) -> Limits:
        return Limits(**{k: getattr(self, k) for k in Limits.model_fields})

    def provider_for(self, agent: str) -> str:
        override = getattr(self, f"{agent.upper()}_PROVIDER", None)
        return str(override or self.LLM_PROVIDER)

    def model_for(self, agent: str) -> str | None:
        override = getattr(self, f"{agent.upper()}_MODEL", None)
        return override or self.LLM_MODEL

    def public_dict(self) -> dict[str, object]:
        """Config without secrets, for config.json and /config endpoints."""
        data = self.model_dump()
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            data[key] = "set" if data.get(key) else None
        return data


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()

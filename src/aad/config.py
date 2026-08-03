"""Runtime configuration.

Every external dependency is optional. Anything unconfigured degrades to an explicit
"not configured" error at call time rather than a fabricated answer — the whole point
of this system is that a wrong torque spec is worse than no torque spec.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AAD_", env_file=".env", extra="ignore", case_sensitive=False
    )

    # --- Storage ---------------------------------------------------------
    data_dir: Path = REPO_ROOT / "data"
    index_dir: Path = REPO_ROOT / "data" / "index"
    cache_db: Path = REPO_ROOT / "data" / "offline_cache.sqlite3"

    # --- Model -----------------------------------------------------------
    # Bedrock model ids carry an "anthropic." prefix; the client layer adds it.
    model: str = "claude-opus-5"
    model_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_tokens: int = 16000
    provider: Literal["anthropic", "bedrock"] = "anthropic"
    aws_region: str = "us-east-1"

    # --- Embeddings ------------------------------------------------------
    # "local" is a deterministic offline embedder: no network, no API key, usable
    # in CI and in a workshop with no connectivity. "openai" uses the model named
    # in the architecture doc for production-quality retrieval.
    embedding_backend: Literal["local", "openai"] = "local"
    embedding_model: str = "text-embedding-3-large"
    embedding_dim: int = 512
    openai_api_key: str | None = None

    # --- Vector store ----------------------------------------------------
    vector_backend: Literal["local", "pinecone"] = "local"
    pinecone_api_key: str | None = None
    pinecone_index: str = "auto-mechanic"

    # --- Chunking (architecture doc: 1200 tokens, 200 overlap) -----------
    chunk_tokens: int = 1200
    chunk_overlap_tokens: int = 200

    # --- Document parsing ------------------------------------------------
    parser_backend: Literal["local", "llamaparse"] = "local"
    llamaparse_api_key: str | None = None

    # --- Commercial data providers --------------------------------------
    # No public API exists for these; each is wired through an adapter that
    # requires an explicit base URL + key from the shop's own subscription.
    torque_api_base: str | None = None
    torque_api_key: str | None = None
    labor_api_base: str | None = None
    labor_api_key: str | None = None
    parts_api_base: str | None = None
    parts_api_key: str | None = None
    obd2_api_base: str | None = None
    obd2_api_key: str | None = None
    wiring_api_base: str | None = None
    wiring_api_key: str | None = None

    # --- Estimating ------------------------------------------------------
    shop_labor_rate: float = 125.0
    parts_markup: float = 0.35
    tax_rate: float = 0.0

    # --- Retrieval -------------------------------------------------------
    retrieval_top_k: int = 8
    min_similarity: float = 0.05


@lru_cache
def get_settings() -> Settings:
    return Settings()

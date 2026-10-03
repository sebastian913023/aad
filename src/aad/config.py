"""Runtime configuration.

Every external dependency is optional. Anything unconfigured degrades to an explicit
"not configured" error at call time rather than a fabricated answer — the whole point
of this system is that a wrong torque spec is worse than no torque spec.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

# On Vercel (and similar serverless hosts) the deployed filesystem is read-only and only
# /tmp is writable, so anything that creates files at runtime has to live there. Elsewhere
# the repo's own data/ directory is used, exactly as before.
_WRITABLE_ROOT = Path("/tmp/aad") if os.environ.get("VERCEL") else REPO_ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AAD_", env_file=".env", extra="ignore", case_sensitive=False
    )

    # --- Storage ---------------------------------------------------------
    data_dir: Path = REPO_ROOT / "data"
    index_dir: Path = _WRITABLE_ROOT / "index"
    cache_db: Path = _WRITABLE_ROOT / "offline_cache.sqlite3"

    # --- API access control ------------------------------------------------
    # When api_token is set, every /api/v1/* request must carry it as either
    # `Authorization: Bearer <token>` or `X-API-Key: <token>`. Unset keeps the API open
    # (local dev, tests). Set it on any deployment reachable from the internet: without
    # it, anyone who finds the URL can spend the model key via /diagnose.
    api_token: str | None = None
    # Comma-separated allowed browser origins, or "*" for any.
    cors_origins: str = "*"

    # --- Model -----------------------------------------------------------
    # Bedrock model ids carry an "anthropic." prefix; the client layer adds it.
    model: str = "claude-opus-5"
    model_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_tokens: int = 16000
    provider: Literal["anthropic", "bedrock"] = "anthropic"
    aws_region: str = "us-east-1"
    # Unprefixed: this is the exact variable name the Anthropic SDK itself reads, and
    # the one every deployment host's "connect your API key" flow sets. Tracking it in
    # Settings (rather than leaving it to the SDK's own env lookup) is what lets
    # build_client() raise a clean NotConfiguredError instead of the model call
    # failing deep inside the SDK with an unhandled TypeError.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

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
    # No public API exists for these; each is described by a JSON profile
    # (see aad/providers/profiles/) and needs an explicit base URL + key from
    # the shop's own subscription. Point this at a directory of JSON files to
    # override the shipped profiles without touching the package.
    provider_profile_dir: Path | None = None
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

    # --- Production safety ------------------------------------------------
    # With production_mode on, documents flagged `synthetic` are excluded from
    # every retrieval. Turn it on for any deployment a technician can reach, so
    # demo data physically cannot reach a bay.
    production_mode: bool = False

    # --- Hallucination monitoring -----------------------------------------
    monitor_db: Path = _WRITABLE_ROOT / "monitor.sqlite3"
    # Below this mean semantic consistency an output is routed to human review even
    # when every literal value checks out — agreement on numbers is not agreement on
    # meaning.
    #
    # Calibrated for the offline hash embedder, which measures lexical overlap and
    # scores a terse sentence ("torque it to 9 Nm") low however well the source
    # supports it. Raise it toward ~0.5 when `embedding_backend` is a real model.
    # It is not the fabrication gate — that is lexical grounding, which is absolute.
    monitor_min_semantic: float = 0.15
    monitor_enabled: bool = True
    judge_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"

    # --- Live provider tests ---------------------------------------------
    # Opt-in so the default test run needs no network. CI sets it to exercise
    # NHTSA against the real service.
    live_tests: bool = False

    # --- Durable storage for the offline cache and monitor audit log -----
    # "sqlite" is the offline-first default: a single file, no server, correct for
    # a workshop with no connectivity. On a serverless host (Vercel, etc.) whose
    # filesystem is read-only outside /tmp, that file does not survive a cold
    # start — "postgres" backs both stores with a real database instead.
    storage_backend: Literal["sqlite", "postgres"] = "sqlite"
    # Deliberately unprefixed: this is the standard variable name every Postgres
    # host (Neon, Vercel Postgres, Supabase, RDS) sets automatically, and reusing
    # it means no extra configuration step beyond attaching the database.
    database_url: str | None = Field(default=None, alias="DATABASE_URL")


    def provider_env(self) -> dict[str, str]:
        """Environment map for provider profiles.

        Profiles resolve credentials by environment variable name, but
        pydantic-settings reads `.env` into this object without exporting it to
        `os.environ`. Without this merge a key set in `.env` would load here and
        still look unconfigured to the profile.
        """
        import os

        from_settings = {
            "AAD_TORQUE_API_BASE": self.torque_api_base,
            "AAD_TORQUE_API_KEY": self.torque_api_key,
            "AAD_LABOR_API_BASE": self.labor_api_base,
            "AAD_LABOR_API_KEY": self.labor_api_key,
            "AAD_PARTS_API_BASE": self.parts_api_base,
            "AAD_PARTS_API_KEY": self.parts_api_key,
            "AAD_OBD2_API_BASE": self.obd2_api_base,
            "AAD_OBD2_API_KEY": self.obd2_api_key,
            "AAD_WIRING_API_BASE": self.wiring_api_base,
            "AAD_WIRING_API_KEY": self.wiring_api_key,
        }
        return {**os.environ, **{k: v for k, v in from_settings.items() if v}}


@lru_cache
def get_settings() -> Settings:
    return Settings()

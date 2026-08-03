from __future__ import annotations

from pathlib import Path

import pytest

from aad.config import Settings
from aad.ingest.embeddings import LocalHashEmbedder
from aad.ingest.pipeline import ingest_path
from aad.models import Vehicle
from aad.rag.retriever import Retriever
from aad.rag.store import LocalVectorStore

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "sample_service_manual.md"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        index_dir=tmp_path / "index",
        cache_db=tmp_path / "cache.sqlite3",
        embedding_backend="local",
        vector_backend="local",
        parser_backend="local",
        embedding_dim=512,
        shop_labor_rate=125.0,
        parts_markup=0.35,
    )


@pytest.fixture
def store(settings: Settings) -> LocalVectorStore:
    return LocalVectorStore(settings.index_dir)


@pytest.fixture
def indexed_retriever(settings: Settings) -> Retriever:
    """A retriever over the synthetic sample manual, scoped to a 2004 G35."""
    ingest_path(SAMPLE, settings=settings)
    return Retriever(
        store=LocalVectorStore(settings.index_dir),
        embedder=LocalHashEmbedder(dim=settings.embedding_dim),
        top_k=settings.retrieval_top_k,
        min_similarity=settings.min_similarity,
    )


@pytest.fixture
def g35() -> Vehicle:
    return Vehicle(year=2004, make="INFINITI", model="G35", engine="3.5L V6 VQ35DE")

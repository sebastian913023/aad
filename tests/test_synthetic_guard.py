"""The synthetic-data guard.

Sample material must be unable to reach a technician. These tests pin the three
places that enforce it: the ingest flag, the production-mode retrieval filter,
and the readiness gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aad.config import Settings
from aad.errors import NoGroundingError
from aad.ingest.embeddings import LocalHashEmbedder
from aad.ingest.pipeline import DocumentMeta, ingest_path
from aad.models import Vehicle
from aad.rag.retriever import Retriever
from aad.rag.store import LocalVectorStore

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "data" / "samples"


def _retriever(settings: Settings, *, exclude_synthetic: bool) -> Retriever:
    return Retriever(
        store=LocalVectorStore(settings.index_dir),
        embedder=LocalHashEmbedder(dim=settings.embedding_dim),
        top_k=settings.retrieval_top_k,
        min_similarity=settings.min_similarity,
        exclude_synthetic=exclude_synthetic,
    )


def test_bundled_sample_is_flagged_synthetic_on_disk():
    """The shipped sample must declare itself. If this ever fails, the guard is
    inert and the sample looks like real service data to every other layer."""
    sidecar = json.loads(
        (SAMPLE_DIR / "sample_service_manual.md.meta.json").read_text(encoding="utf-8")
    )
    assert sidecar["synthetic"] is True


def test_ingest_marks_and_counts_synthetic_chunks(settings: Settings):
    report = ingest_path(SAMPLE_DIR / "sample_service_manual.md", settings=settings)
    assert report.chunks_written > 0
    assert report.synthetic_chunks == report.chunks_seen

    store = LocalVectorStore(settings.index_dir)
    assert all(chunk.synthetic for chunk in store._chunks)


def test_production_mode_hides_synthetic_material(settings: Settings, g35: Vehicle):
    ingest_path(SAMPLE_DIR / "sample_service_manual.md", settings=settings)

    dev = _retriever(settings, exclude_synthetic=False)
    assert dev.search("camshaft position sensor retaining bolt torque", g35)

    production = _retriever(settings, exclude_synthetic=True)
    with pytest.raises(NoGroundingError):
        production.search("camshaft position sensor retaining bolt torque", g35)


def test_production_mode_still_serves_licensed_material(settings: Settings, tmp_path: Path):
    """Excluding synthetic must not exclude everything — a real document indexed
    alongside sample data still has to be retrievable."""
    ingest_path(SAMPLE_DIR / "sample_service_manual.md", settings=settings)

    licensed = tmp_path / "licensed_excerpt.md"
    licensed.write_text(
        "Tighten the camshaft position sensor retaining bolt to 12 Nm. Bolt size M6 x 1.0.",
        encoding="utf-8",
    )
    ingest_path(
        licensed,
        meta=DocumentMeta(year=2004, make="INFINITI", model="G35", synthetic=False),
        settings=settings,
    )

    production = _retriever(settings, exclude_synthetic=True)
    hits = production.search(
        "camshaft position sensor retaining bolt torque",
        Vehicle(year=2004, make="INFINITI", model="G35"),
    )
    assert hits
    assert {hit.chunk.source for hit in hits} == {"licensed_excerpt.md"}
    assert not any(hit.chunk.synthetic for hit in hits)


def test_settings_wire_production_mode_into_the_retriever(settings: Settings):
    from aad.rag.retriever import get_retriever

    production = Settings(**{**settings.model_dump(), "production_mode": True})
    assert get_retriever(production).exclude_synthetic is True
    assert get_retriever(settings).exclude_synthetic is False

from __future__ import annotations

import pytest

from aad.errors import NoGroundingError, UnscopedRequestError
from aad.ingest.embeddings import LocalHashEmbedder
from aad.models import Chunk, Vehicle
from aad.rag.retriever import Retriever
from aad.rag.store import LocalVectorStore


def _chunk(chunk_id: str, text: str, **meta) -> Chunk:
    return Chunk(chunk_id=chunk_id, text=text, source="s.md", **meta)


def test_store_roundtrip_and_dedup(store: LocalVectorStore):
    embedder = LocalHashEmbedder(dim=64)
    chunks = [_chunk("a", "head bolt torque"), _chunk("b", "drain plug torque")]
    vectors = embedder.embed([c.text for c in chunks])

    assert store.upsert(chunks, vectors) == 2
    assert store.upsert(chunks, vectors) == 0  # same ids update in place
    assert store.count() == 2


def test_store_persists_across_instances(store: LocalVectorStore):
    embedder = LocalHashEmbedder(dim=64)
    chunks = [_chunk("a", "head bolt torque")]
    store.upsert(chunks, embedder.embed(["head bolt torque"]))

    reopened = LocalVectorStore(store.index_dir)
    assert reopened.count() == 1


def test_store_filters_by_vehicle_metadata(store: LocalVectorStore):
    embedder = LocalHashEmbedder(dim=64)
    chunks = [
        _chunk("g35", "camshaft sensor bolt torque", year=2004, make="infiniti", model="g35"),
        _chunk("civic", "camshaft sensor bolt torque", year=2004, make="honda", model="civic"),
    ]
    store.upsert(chunks, embedder.embed([c.text for c in chunks]))

    hits = store.query(embedder.embed(["camshaft sensor torque"])[0], 5, {"make": "infiniti"})
    assert [hit.chunk.chunk_id for hit in hits] == ["g35"]


def test_unlabeled_documents_match_every_vehicle(store: LocalVectorStore):
    embedder = LocalHashEmbedder(dim=64)
    generic = _chunk("generic", "P0340 camshaft position sensor circuit")
    store.upsert([generic], embedder.embed([generic.text]))

    hits = store.query(embedder.embed(["P0340"])[0], 5, {"make": "infiniti", "year": 2004})
    assert [hit.chunk.chunk_id for hit in hits] == ["generic"]


def test_delete_source(store: LocalVectorStore):
    embedder = LocalHashEmbedder(dim=64)
    chunks = [_chunk("a", "one"), _chunk("b", "two")]
    store.upsert(chunks, embedder.embed([c.text for c in chunks]))
    assert store.delete_source("s.md") == 2
    assert store.count() == 0


def test_retriever_rejects_unscoped_requests(indexed_retriever: Retriever):
    with pytest.raises(UnscopedRequestError):
        indexed_retriever.search("camshaft sensor torque", Vehicle())


def test_retriever_raises_when_nothing_matches(indexed_retriever: Retriever):
    other_car = Vehicle(year=1998, make="Saab", model="900")
    with pytest.raises(NoGroundingError):
        indexed_retriever.search("camshaft position sensor torque", other_car)


def test_retriever_finds_sample_content(indexed_retriever: Retriever, g35):
    hits = indexed_retriever.search("camshaft position sensor retaining bolt torque", g35)
    assert hits
    assert any("camshaft position sensor" in hit.chunk.text.lower() for hit in hits)
    assert all(hit.citation().source == "sample_service_manual.md" for hit in hits)


def test_retriever_falls_back_when_spec_type_filter_is_too_narrow(indexed_retriever: Retriever, g35):
    # fluid_capacity appears nowhere in the sample; the retry drops the filter.
    hits = indexed_retriever.search(
        "camshaft position sensor", g35, spec_type="fluid_capacity"
    )
    assert hits

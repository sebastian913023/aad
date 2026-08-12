from __future__ import annotations

from pathlib import Path

from aad.config import Settings
from aad.ingest.chunker import chunk_document, classify, estimate_tokens
from aad.ingest.embeddings import LocalHashEmbedder
from aad.ingest.parsers import LocalParser
from aad.ingest.pipeline import DocumentMeta, ingest_path


def test_classify_prefers_torque_over_procedure():
    text = "Tighten the camshaft position sensor retaining bolt to 9 Nm using bolt size M6 x 1.0."
    assert classify(text) == "torque_spec"


def test_classify_labor_and_wiring():
    assert classify("Labor time for sensor replacement: 0.7 hrs.") == "labor_time"
    assert classify("Pin 2 is the signal circuit, wire colour red, connector E12.") == "wiring_diagram"
    assert classify("The vehicle has four wheels and a roof.") == "general"


def test_chunking_respects_size_and_overlap():
    body = " ".join(f"word{i}" for i in range(2000))
    chunks = chunk_document(
        source="doc.md", pages=[(1, body)], chunk_tokens=200, overlap_tokens=50
    )
    assert len(chunks) > 1
    # Overlap means consecutive chunks share text.
    first_tail = chunks[0].text.split()[-10:]
    assert any(word in chunks[1].text for word in first_tail)
    for chunk in chunks:
        assert estimate_tokens(chunk.text) <= 260  # target + tolerance for the estimator


def test_chunk_ids_are_stable_and_carry_metadata():
    pages = [(1, "Tighten to 40 Nm. Bolt size M11 x 1.5.")]
    a = chunk_document(source="d.md", pages=pages, year=2004, make="Infiniti", model="G35")
    b = chunk_document(source="d.md", pages=pages, year=2004, make="Infiniti", model="G35")
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
    assert a[0].make == "infiniti" and a[0].year == 2004


def test_local_parser_reads_markdown(tmp_path: Path):
    path = tmp_path / "m.md"
    path.write_text("# Heading\n\nBody text here.", encoding="utf-8")
    pages = LocalParser().parse(path)
    assert len(pages) == 1 and "Body text" in pages[0].text


def test_ingest_indexes_sample_and_reports_spec_types(settings: Settings):
    sample = Path(__file__).resolve().parents[1] / "data" / "samples" / "sample_service_manual.md"
    report = ingest_path(sample, settings=settings)

    assert report.files == 1
    assert report.chunks_written > 0
    assert "torque_spec" in report.by_spec_type
    # The sidecar supplies vehicle metadata, so no unlabeled-document warning.
    assert report.warnings == []
    assert report.errors == []


def test_ingest_warns_when_document_has_no_vehicle_metadata(settings: Settings, tmp_path: Path):
    doc = tmp_path / "unlabeled.md"
    doc.write_text("\n\n".join(f"Section {i}. Tighten to {i} Nm bolt M6 x 1.0." for i in range(40)))
    report = ingest_path(doc, settings=settings)
    assert any("no vehicle metadata" in w for w in report.warnings)


def test_reingest_replaces_rather_than_duplicates(settings: Settings, tmp_path: Path):
    doc = tmp_path / "d.md"
    doc.write_text("Tighten the drain plug to 25 Nm. Bolt size M14 x 1.5.", encoding="utf-8")
    meta = DocumentMeta(year=2004, make="Infiniti", model="G35")

    ingest_path(doc, meta=meta, settings=settings)
    from aad.rag.store import LocalVectorStore

    first = LocalVectorStore(settings.index_dir).count()
    ingest_path(doc, meta=meta, settings=settings)
    assert LocalVectorStore(settings.index_dir).count() == first


def test_local_embedder_is_deterministic_and_normalized():
    embedder = LocalHashEmbedder(dim=128)
    a = embedder.embed(["torque the head bolts"])
    b = embedder.embed(["torque the head bolts"])
    assert (a == b).all()
    assert abs(float((a[0] ** 2).sum()) - 1.0) < 1e-5


def test_local_embedder_distinguishes_word_order():
    embedder = LocalHashEmbedder(dim=512)
    vectors = embedder.embed(["torque spec", "spec torque"])
    assert float(vectors[0] @ vectors[1]) < 0.999  # bigrams break the tie

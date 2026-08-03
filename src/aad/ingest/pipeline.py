"""Ingestion orchestration: parse -> chunk -> embed -> index.

Vehicle metadata comes from an explicit argument or a `<file>.meta.json` sidecar.
Documents indexed without metadata are searchable by every vehicle, which is correct
for generic references (OBD-II code definitions) and wrong for an OEM manual — the CLI
warns when a manual-sized document arrives unlabeled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from aad.config import Settings, get_settings
from aad.ingest.chunker import chunk_document
from aad.ingest.embeddings import get_embedder
from aad.ingest.parsers import SUPPORTED_SUFFIXES, get_parser
from aad.models import Chunk
from aad.rag.store import get_vector_store

EMBED_BATCH = 64


@dataclass(slots=True)
class DocumentMeta:
    year: int | None = None
    make: str | None = None
    model: str | None = None
    engine: str | None = None

    @classmethod
    def from_sidecar(cls, path: Path) -> DocumentMeta | None:
        sidecar = path.with_suffix(path.suffix + ".meta.json")
        if not sidecar.exists():
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return cls(
            year=data.get("year"),
            make=data.get("make"),
            model=data.get("model"),
            engine=data.get("engine"),
        )

    def is_empty(self) -> bool:
        return not any([self.year, self.make, self.model, self.engine])


@dataclass(slots=True)
class IngestReport:
    files: int = 0
    chunks_written: int = 0
    chunks_seen: int = 0
    by_spec_type: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def merge_chunks(self, chunks: list[Chunk]) -> None:
        self.chunks_seen += len(chunks)
        for chunk in chunks:
            self.by_spec_type[chunk.spec_type] = self.by_spec_type.get(chunk.spec_type, 0) + 1


def iter_documents(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES and ".meta" not in p.suffixes
    )


def ingest_path(
    path: Path,
    *,
    meta: DocumentMeta | None = None,
    settings: Settings | None = None,
    replace: bool = True,
) -> IngestReport:
    settings = settings or get_settings()
    parser = get_parser(settings)
    embedder = get_embedder(settings)
    store = get_vector_store(settings)
    report = IngestReport()

    for document in iter_documents(Path(path)):
        doc_meta = meta or DocumentMeta.from_sidecar(document) or DocumentMeta()
        try:
            pages = parser.parse(document)
        except Exception as exc:  # noqa: BLE001 - one bad file must not abort the batch
            report.errors.append(f"{document.name}: {exc}")
            continue

        chunks = chunk_document(
            source=document.name,
            pages=[(p.page, p.text) for p in pages],
            chunk_tokens=settings.chunk_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
            year=doc_meta.year,
            make=doc_meta.make,
            model=doc_meta.model,
            engine=doc_meta.engine,
        )
        if not chunks:
            report.errors.append(f"{document.name}: parsed but produced no chunks")
            continue

        if doc_meta.is_empty():
            # Always surfaced, at any document size. Silent unlabeled indexing is the
            # failure mode that puts one model's specs in front of another's.
            report.warnings.append(
                f"{document.name}: indexed with no vehicle metadata, so it will match every "
                f"vehicle. Correct for a generic reference; add {document.name}.meta.json or "
                "pass --year/--make/--model for anything model-specific."
            )

        if replace:
            store.delete_source(document.name)

        written = 0
        for start in range(0, len(chunks), EMBED_BATCH):
            batch = chunks[start : start + EMBED_BATCH]
            vectors = embedder.embed([c.text for c in batch])
            written += store.upsert(batch, vectors)

        report.files += 1
        report.chunks_written += written
        report.merge_chunks(chunks)

    return report

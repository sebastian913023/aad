"""Document ingestion pipeline: parse -> chunk -> embed -> index."""

from aad.ingest.chunker import chunk_document
from aad.ingest.embeddings import get_embedder
from aad.ingest.parsers import get_parser
from aad.ingest.pipeline import ingest_path

__all__ = ["chunk_document", "get_embedder", "get_parser", "ingest_path"]

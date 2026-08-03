"""Asset-scoped retrieval.

Two rules are enforced here rather than left to the prompt:

1. A request without vehicle identity is rejected. There is no unscoped search path.
2. Results below `min_similarity` are dropped, and an empty result set raises
   `NoGroundingError` instead of returning nothing quietly. A tool that returns "no
   data" is what keeps the model from inventing one.
"""

from __future__ import annotations

from aad.config import Settings, get_settings
from aad.errors import NoGroundingError, UnscopedRequestError
from aad.ingest.embeddings import Embedder, get_embedder
from aad.models import RetrievedChunk, SpecType, Vehicle
from aad.rag.store import VectorStore, get_vector_store


class Retriever:
    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        *,
        top_k: int = 8,
        min_similarity: float = 0.05,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.top_k = top_k
        self.min_similarity = min_similarity

    def search(
        self,
        query: str,
        vehicle: Vehicle,
        *,
        spec_type: SpecType | None = None,
        top_k: int | None = None,
        require_results: bool = True,
    ) -> list[RetrievedChunk]:
        if not vehicle.is_scoped():
            raise UnscopedRequestError()

        filters = vehicle.filter_dict()
        if spec_type:
            filters["spec_type"] = spec_type

        vector = self.embedder.embed([query])[0]
        results = self.store.query(vector, top_k or self.top_k, filters)
        results = [r for r in results if r.score >= self.min_similarity]

        if not results and spec_type:
            # The spec_type classifier is a heuristic; a miss on it should not look
            # like missing source material. Retry once without that constraint.
            filters.pop("spec_type")
            results = self.store.query(vector, top_k or self.top_k, filters)
            results = [r for r in results if r.score >= self.min_similarity]

        if not results and require_results:
            raise NoGroundingError(query, vehicle.label())
        return results


def get_retriever(settings: Settings | None = None) -> Retriever:
    settings = settings or get_settings()
    return Retriever(
        store=get_vector_store(settings),
        embedder=get_embedder(settings),
        top_k=settings.retrieval_top_k,
        min_similarity=settings.min_similarity,
    )

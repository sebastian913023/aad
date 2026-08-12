"""Vector storage.

`LocalVectorStore` keeps vectors in a single .npz and metadata in a .jsonl beside it.
Exact cosine search over a shop-sized corpus (tens of thousands of chunks) is a
millisecond-scale numpy dot product, and it works with no network — which is what the
offline-first requirement actually demands. `PineconeVectorStore` is the managed path
once the corpus outgrows a single node.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import numpy as np

from aad.config import Settings, get_settings
from aad.errors import NotConfiguredError, ProviderError
from aad.models import Chunk, RetrievedChunk


def _matches(metadata: dict, filters: dict) -> bool:
    for key, expected in filters.items():
        actual = metadata.get(key)
        if actual is None:
            # Unlabeled documents are treated as universal (e.g. a generic OBD-II
            # reference). Anything explicitly labeled must match exactly.
            continue
        if isinstance(expected, (list, tuple, set)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


class VectorStore(Protocol):
    def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> int: ...

    def query(
        self, vector: np.ndarray, top_k: int, filters: dict | None = None
    ) -> list[RetrievedChunk]: ...

    def delete_source(self, source: str) -> int: ...

    def count(self) -> int: ...


class LocalVectorStore:
    def __init__(self, index_dir: Path) -> None:
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.vectors_path = self.index_dir / "vectors.npz"
        self.meta_path = self.index_dir / "chunks.jsonl"
        self._vectors: np.ndarray | None = None
        self._chunks: list[Chunk] = []
        self._load()

    # --- persistence -----------------------------------------------------
    def _load(self) -> None:
        if self.meta_path.exists():
            self._chunks = [
                Chunk.model_validate_json(line)
                for line in self.meta_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        if self.vectors_path.exists():
            self._vectors = np.load(self.vectors_path)["vectors"].astype(np.float32)
        if self._vectors is not None and len(self._vectors) != len(self._chunks):
            raise ProviderError(
                f"index at {self.index_dir} is inconsistent "
                f"({len(self._vectors)} vectors vs {len(self._chunks)} chunks); rebuild it"
            )

    def _persist(self) -> None:
        vectors = self._vectors if self._vectors is not None else np.zeros((0, 0), dtype=np.float32)
        np.savez_compressed(self.vectors_path, vectors=vectors)
        with self.meta_path.open("w", encoding="utf-8") as handle:
            for chunk in self._chunks:
                handle.write(chunk.model_dump_json() + "\n")

    # --- api -------------------------------------------------------------
    def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> int:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must be the same length")
        if not chunks:
            return 0

        existing = {chunk.chunk_id: i for i, chunk in enumerate(self._chunks)}
        new_chunks: list[Chunk] = []
        new_rows: list[np.ndarray] = []
        for chunk, vector in zip(chunks, vectors):
            index = existing.get(chunk.chunk_id)
            if index is None:
                existing[chunk.chunk_id] = len(self._chunks) + len(new_chunks)
                new_chunks.append(chunk)
                new_rows.append(vector)
            else:
                self._chunks[index] = chunk
                if self._vectors is not None:
                    self._vectors[index] = vector

        if new_chunks:
            stacked = np.vstack(new_rows).astype(np.float32)
            self._vectors = stacked if self._vectors is None else np.vstack([self._vectors, stacked])
            self._chunks.extend(new_chunks)

        self._persist()
        return len(new_chunks)

    def query(
        self, vector: np.ndarray, top_k: int, filters: dict | None = None
    ) -> list[RetrievedChunk]:
        if self._vectors is None or not len(self._chunks):
            return []
        scores = self._vectors @ np.asarray(vector, dtype=np.float32).reshape(-1)

        filters = filters or {}
        candidates = [
            i for i in range(len(self._chunks)) if _matches(self._chunks[i].metadata(), filters)
        ]
        if not candidates:
            return []

        candidate_scores = scores[candidates]
        take = min(top_k, len(candidates))
        top = np.argpartition(-candidate_scores, take - 1)[:take]
        ordered = top[np.argsort(-candidate_scores[top])]
        return [
            RetrievedChunk(chunk=self._chunks[candidates[i]], score=float(candidate_scores[i]))
            for i in ordered
        ]

    def delete_source(self, source: str) -> int:
        keep = [i for i, chunk in enumerate(self._chunks) if chunk.source != source]
        removed = len(self._chunks) - len(keep)
        if not removed:
            return 0
        self._chunks = [self._chunks[i] for i in keep]
        self._vectors = self._vectors[keep] if self._vectors is not None else None
        self._persist()
        return removed

    def count(self) -> int:
        return len(self._chunks)

    def sources(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for chunk in self._chunks:
            counts[chunk.source] = counts.get(chunk.source, 0) + 1
        return counts


class PineconeVectorStore:
    """Managed vector search. `index_host` is the full host from the Pinecone console."""

    def __init__(self, api_key: str, index_host: str, namespace: str = "") -> None:
        self.api_key = api_key
        self.base_url = index_host if index_host.startswith("http") else f"https://{index_host}"
        self.namespace = namespace

    def _headers(self) -> dict[str, str]:
        return {"Api-Key": self.api_key, "Content-Type": "application/json"}

    def upsert(self, chunks: list[Chunk], vectors: np.ndarray) -> int:
        import httpx

        payload = {
            "namespace": self.namespace,
            "vectors": [
                {
                    "id": chunk.chunk_id,
                    "values": vector.tolist(),
                    "metadata": {
                        **{k: v for k, v in chunk.metadata().items() if v is not None},
                        "text": chunk.text,
                    },
                }
                for chunk, vector in zip(chunks, vectors)
            ],
        }
        response = httpx.post(
            f"{self.base_url}/vectors/upsert", headers=self._headers(), json=payload, timeout=60.0
        )
        if response.status_code >= 400:
            raise ProviderError(f"pinecone upsert failed ({response.status_code}): {response.text}")
        return int(response.json().get("upsertedCount", len(chunks)))

    def query(
        self, vector: np.ndarray, top_k: int, filters: dict | None = None
    ) -> list[RetrievedChunk]:
        import httpx

        payload: dict = {
            "namespace": self.namespace,
            "vector": np.asarray(vector).reshape(-1).tolist(),
            "topK": top_k,
            "includeMetadata": True,
        }
        if filters:
            payload["filter"] = {key: {"$eq": value} for key, value in filters.items()}

        response = httpx.post(
            f"{self.base_url}/query", headers=self._headers(), json=payload, timeout=30.0
        )
        if response.status_code >= 400:
            raise ProviderError(f"pinecone query failed ({response.status_code}): {response.text}")

        results: list[RetrievedChunk] = []
        for match in response.json().get("matches", []):
            meta = dict(match.get("metadata") or {})
            text = meta.pop("text", "")
            results.append(
                RetrievedChunk(
                    chunk=Chunk(chunk_id=match["id"], text=text, **_chunk_fields(meta)),
                    score=float(match.get("score", 0.0)),
                )
            )
        return results

    def delete_source(self, source: str) -> int:
        import httpx

        response = httpx.post(
            f"{self.base_url}/vectors/delete",
            headers=self._headers(),
            json={"namespace": self.namespace, "filter": {"source": {"$eq": source}}},
            timeout=30.0,
        )
        if response.status_code >= 400:
            raise ProviderError(f"pinecone delete failed ({response.status_code}): {response.text}")
        return -1  # Pinecone does not report a deleted count for filter deletes.

    def count(self) -> int:
        import httpx

        response = httpx.post(
            f"{self.base_url}/describe_index_stats", headers=self._headers(), json={}, timeout=30.0
        )
        if response.status_code >= 400:
            raise ProviderError(f"pinecone stats failed ({response.status_code})")
        return int(response.json().get("totalVectorCount", 0))


def _chunk_fields(meta: dict) -> dict:
    allowed = {"source", "page", "section", "spec_type", "year", "make", "model", "engine"}
    out = {k: v for k, v in meta.items() if k in allowed}
    out.setdefault("source", "unknown")
    return out


def get_vector_store(settings: Settings | None = None) -> VectorStore:
    settings = settings or get_settings()
    if settings.vector_backend == "local":
        return LocalVectorStore(settings.index_dir)
    if not settings.pinecone_api_key:
        raise NotConfiguredError("pinecone", "set AAD_PINECONE_API_KEY and AAD_PINECONE_INDEX")
    return PineconeVectorStore(api_key=settings.pinecone_api_key, index_host=settings.pinecone_index)

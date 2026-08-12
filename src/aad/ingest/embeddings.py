"""Embedding backends.

`LocalHashEmbedder` is the default: a deterministic hashed bag-of-ngrams projection.
It needs no network and no key, which makes the whole pipeline runnable in CI and in
a disconnected workshop. It is weaker than a learned model — use the OpenAI backend
for production retrieval quality.
"""

from __future__ import annotations

import hashlib
import re
from itertools import pairwise
from typing import Protocol

import numpy as np

from aad.config import Settings, get_settings
from aad.errors import NotConfiguredError, ProviderError

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9./-]*")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return an (len(texts), dim) L2-normalized float32 array."""


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


class LocalHashEmbedder:
    """Deterministic, offline, dependency-free embeddings.

    Unigrams and bigrams are hashed into a fixed-width vector with sublinear term
    weighting. Bigrams matter here: "torque spec" and "spec torque" are the same bag
    of unigrams but very different queries in a service manual.
    """

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _bucket(self, token: str) -> int:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.dim

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            tokens = tokenize(text)
            grams = tokens + [f"{a}_{b}" for a, b in pairwise(tokens)]
            counts: dict[int, float] = {}
            for gram in grams:
                idx = self._bucket(gram)
                counts[idx] = counts.get(idx, 0.0) + 1.0
            for idx, count in counts.items():
                out[row, idx] = 1.0 + np.log(count)
        return _normalize(out)


class OpenAIEmbedder:
    """Embeddings via the model named in the architecture doc (text-embedding-3-large).

    Embeddings only — all generation in this system goes through Claude.
    """

    def __init__(self, api_key: str, model: str, dim: int) -> None:
        self.api_key = api_key
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> np.ndarray:
        import httpx

        response = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts, "dimensions": self.dim},
            timeout=60.0,
        )
        if response.status_code != 200:
            raise ProviderError(f"embedding request failed ({response.status_code}): {response.text}")
        data = response.json()["data"]
        ordered = sorted(data, key=lambda item: item["index"])
        return _normalize(np.array([item["embedding"] for item in ordered], dtype=np.float32))


def get_embedder(settings: Settings | None = None) -> Embedder:
    settings = settings or get_settings()
    if settings.embedding_backend == "local":
        return LocalHashEmbedder(dim=settings.embedding_dim)
    if not settings.openai_api_key:
        raise NotConfiguredError("openai-embeddings", "set AAD_OPENAI_API_KEY")
    return OpenAIEmbedder(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
        dim=settings.embedding_dim,
    )

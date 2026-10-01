"""SQLite offline cache.

Workshops lose connectivity — steel buildings, basements, mobile service. Every
successful provider response is cached keyed by (namespace, request). On a network
failure the cached copy is served with its age attached, so a technician can see they
are looking at yesterday's data rather than today's.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    namespace  TEXT NOT NULL,
    request    TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS cache_namespace_idx ON cache (namespace);
"""


def _key(namespace: str, request: dict) -> str:
    canonical = json.dumps(request, sort_keys=True, default=str)
    digest = hashlib.blake2b(canonical.encode("utf-8"), digest_size=16).hexdigest()
    return f"{namespace}:{digest}"


class OfflineCache:
    def __init__(self, path: Path) -> None:
        self.path: Path | None = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @classmethod
    def connect_postgres(cls, database_url: str | None) -> OfflineCache:
        """Same cache, backed by Postgres instead of a local file — see `aad.storage`."""
        from aad.storage import connect_postgres

        self = cls.__new__(cls)
        self.path = None
        self._conn = connect_postgres(database_url, who="offline_cache")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        return self

    def get(self, namespace: str, request: dict, *, max_age: float | None = None) -> dict | None:
        row = self._conn.execute(
            "SELECT payload, created_at FROM cache WHERE key = ?", (_key(namespace, request),)
        ).fetchone()
        if row is None:
            return None
        # Named, not positional: a dict-row backend (Postgres) iterates column names,
        # not values, so `a, b = row` silently assigns the wrong thing.
        payload, created_at = row["payload"], row["created_at"]
        age = time.time() - created_at
        if max_age is not None and age > max_age:
            return None
        return {"data": json.loads(payload), "cached": True, "age_seconds": round(age, 1)}

    def put(self, namespace: str, request: dict, payload: Any) -> None:
        self._conn.execute(
            "INSERT INTO cache (key, namespace, request, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, "
            "created_at = excluded.created_at",
            (
                _key(namespace, request),
                namespace,
                json.dumps(request, sort_keys=True, default=str),
                json.dumps(payload, default=str),
                time.time(),
            ),
        )
        self._conn.commit()

    def through(
        self,
        namespace: str,
        request: dict,
        fetch: Callable[[], Any],
        *,
        max_age: float | None = None,
    ) -> dict:
        """Fetch fresh, cache the result; on failure fall back to any cached copy."""
        try:
            payload = fetch()
        except Exception as exc:
            cached = self.get(namespace, request, max_age=max_age)
            if cached is None:
                raise
            cached["stale_reason"] = f"live request failed ({type(exc).__name__}: {exc})"
            return cached
        self.put(namespace, request, payload)
        return {"data": payload, "cached": False, "age_seconds": 0.0}

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT namespace, COUNT(*) FROM cache GROUP BY namespace"
        ).fetchall()
        return {namespace: count for namespace, count in rows}

    def purge(self, older_than_seconds: float) -> int:
        cutoff = time.time() - older_than_seconds
        cursor = self._conn.execute("DELETE FROM cache WHERE created_at < ?", (cutoff,))
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()


_cache: OfflineCache | None = None


def get_cache(path: Path | None = None, *, settings: Any = None) -> OfflineCache:
    """The process-wide cache. An explicit `path` always means SQLite at that path —
    tests rely on this to stay isolated regardless of `AAD_STORAGE_BACKEND`.
    """
    global _cache
    if path is not None:
        return OfflineCache(path)
    if _cache is None:
        if settings is None:
            from aad.config import get_settings

            settings = get_settings()
        _cache = (
            OfflineCache.connect_postgres(settings.database_url)
            if settings.storage_backend == "postgres"
            else OfflineCache(settings.cache_db)
        )
    return _cache

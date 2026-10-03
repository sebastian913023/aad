"""Tests for the durable-storage backend selection.

No live Postgres server runs in CI by default — the same convention the NHTSA tests
use: hermetic by default, opt into the real thing with `-m live` and a real
`AAD_DATABASE_URL`/`DATABASE_URL`. What's tested unconditionally is the part that
matters without a server at all: a missing credential must fail loudly as
`NotConfiguredError`, never silently fall back to something that looks like it
worked, and `get_cache`/`get_monitor_store` must route to the backend the settings
actually name.
"""

from __future__ import annotations

import os

import pytest

from aad.cache.store import OfflineCache, get_cache
from aad.config import Settings
from aad.errors import NotConfiguredError
from aad.monitor.store import MonitorStore, get_monitor_store
from aad.storage import connect_postgres


def test_connect_postgres_without_a_url_is_not_configured():
    with pytest.raises(NotConfiguredError, match="not configured"):
        connect_postgres(None, who="test")


def test_offline_cache_postgres_without_a_url_is_not_configured():
    with pytest.raises(NotConfiguredError):
        OfflineCache.connect_postgres(None)


def test_monitor_store_postgres_without_a_url_is_not_configured():
    with pytest.raises(NotConfiguredError):
        MonitorStore.connect_postgres(None)


def test_get_cache_with_an_explicit_path_is_always_sqlite_regardless_of_backend(tmp_path):
    """An explicit path overrides the backend setting — this is what keeps every
    existing test isolated no matter what AAD_STORAGE_BACKEND is set to."""
    cache = get_cache(tmp_path / "c.sqlite3")
    assert isinstance(cache, OfflineCache)
    assert cache.path == tmp_path / "c.sqlite3"


def test_get_monitor_store_with_an_explicit_path_is_always_sqlite(tmp_path):
    store = get_monitor_store(tmp_path / "m.sqlite3")
    assert isinstance(store, MonitorStore)
    assert store.path == tmp_path / "m.sqlite3"


def test_get_cache_routes_to_postgres_when_backend_is_postgres_and_url_is_missing(tmp_path):
    """Proves the dispatch happens — reaching the same NotConfiguredError a direct
    OfflineCache.connect_postgres(None) call raises, not a silent sqlite fallback."""
    import aad.cache.store as cache_module

    cache_module._cache = None
    settings = Settings(
        data_dir=tmp_path,
        cache_db=tmp_path / "unused.sqlite3",
        storage_backend="postgres",
        database_url=None,
    )
    with pytest.raises(NotConfiguredError, match="offline_cache"):
        get_cache(settings=settings)
    cache_module._cache = None


def test_get_monitor_store_routes_to_postgres_when_backend_is_postgres_and_url_is_missing(
    tmp_path,
):
    settings = Settings(
        data_dir=tmp_path,
        monitor_db=tmp_path / "unused.sqlite3",
        storage_backend="postgres",
        database_url=None,
    )
    with pytest.raises(NotConfiguredError, match="monitor_store"):
        get_monitor_store(settings=settings)


def test_database_url_env_var_is_read_unprefixed(monkeypatch):
    """Postgres hosts (Neon, Vercel Postgres, Supabase) set the bare DATABASE_URL,
    not an AAD_-prefixed one — attaching a database must not require renaming it."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
    monkeypatch.delenv("AAD_DATABASE_URL", raising=False)
    assert Settings().database_url == "postgresql://example/db"


# --- live: exercises a real Postgres database ------------------------------------

_LIVE_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")


@pytest.mark.live
@pytest.mark.skipif(not _LIVE_DATABASE_URL, reason="no TEST_DATABASE_URL/DATABASE_URL set")
def test_offline_cache_round_trips_through_real_postgres():
    cache = OfflineCache.connect_postgres(_LIVE_DATABASE_URL)
    try:
        cache.put("live-test", {"q": "torque"}, {"value": "9 Nm"})
        result = cache.get("live-test", {"q": "torque"})
        assert result is not None
        assert result["data"] == {"value": "9 Nm"}
        assert result["cached"] is True
    finally:
        cache._conn.execute("DELETE FROM cache WHERE namespace = ?", ("live-test",))
        cache._conn.commit()
        cache.close()


@pytest.mark.live
@pytest.mark.skipif(not _LIVE_DATABASE_URL, reason="no TEST_DATABASE_URL/DATABASE_URL set")
def test_monitor_store_round_trips_through_real_postgres():
    from aad.monitor.pipeline import MonitorResult

    store = MonitorStore.connect_postgres(_LIVE_DATABASE_URL)
    try:
        result = MonitorResult(
            verdict="grounded",
            task_type="live-test",
            faithfulness=1.0,
            citation_accuracy=1.0,
            mean_semantic=1.0,
        )
        event_id = store.record(result, question="live test", output="ok")
        fetched = store.get(event_id)
        assert fetched is not None
        assert fetched["task_type"] == "live-test"
        assert fetched["verdict"] == "grounded"
    finally:
        store._conn.execute("DELETE FROM monitor_events WHERE task_type = ?", ("live-test",))
        store._conn.commit()
        store.close()

from __future__ import annotations

import pytest

from aad.cache.store import OfflineCache
from aad.estimates import build_estimate


def test_estimate_math(settings, g35):
    estimate = build_estimate(
        g35,
        labor_items=[{"operation": "replace CMP sensor", "hours": 0.7}],
        part_items=[{"description": "CMP sensor", "unit_price": 100.0, "quantity": 1}],
        fees=[{"description": "shop supplies", "amount": 15.0}],
        settings=settings,
    )
    assert estimate.labor_subtotal == pytest.approx(87.5)  # 0.7 * 125
    assert estimate.parts_subtotal == pytest.approx(135.0)  # 100 * 1.35 markup
    assert estimate.total == pytest.approx(237.5)  # + 15 fee, 0% tax
    assert estimate.disclaimers == []


def test_estimate_records_missing_inputs_instead_of_guessing(settings, g35):
    estimate = build_estimate(
        g35,
        labor_items=[{"operation": "replace timing chain", "hours": None}],
        part_items=[{"description": "timing chain kit", "unit_price": None}],
        settings=settings,
    )
    assert estimate.total == 0
    assert len(estimate.disclaimers) == 3  # two omissions plus "estimate is empty"
    assert any("no published time" in d for d in estimate.disclaimers)
    assert any("no price" in d for d in estimate.disclaimers)


def test_estimate_honors_override_rate_and_no_markup(settings, g35):
    estimate = build_estimate(
        g35,
        part_items=[{"description": "oil filter", "unit_price": 12.0, "apply_markup": False}],
        labor_items=[{"operation": "oil change", "hours": 0.4}],
        labor_rate=200.0,
        settings=settings,
    )
    assert estimate.labor_subtotal == pytest.approx(80.0)
    assert estimate.parts_subtotal == pytest.approx(12.0)


# --- cache ----------------------------------------------------------------
def test_cache_roundtrip(tmp_path):
    cache = OfflineCache(tmp_path / "c.sqlite3")
    cache.put("vin", {"vin": "ABC"}, {"make": "Infiniti"})
    hit = cache.get("vin", {"vin": "ABC"})
    assert hit["data"] == {"make": "Infiniti"}
    assert hit["cached"] is True
    assert cache.get("vin", {"vin": "OTHER"}) is None


def test_cache_serves_stale_copy_when_network_fails(tmp_path):
    cache = OfflineCache(tmp_path / "c.sqlite3")
    request = {"vin": "ABC"}

    fresh = cache.through("vin", request, lambda: {"make": "Infiniti"})
    assert fresh["cached"] is False

    def boom():
        raise ConnectionError("no signal in the bay")

    fallback = cache.through("vin", request, boom)
    assert fallback["cached"] is True
    assert fallback["data"] == {"make": "Infiniti"}
    assert "no signal in the bay" in fallback["stale_reason"]


def test_cache_reraises_when_nothing_cached(tmp_path):
    cache = OfflineCache(tmp_path / "c.sqlite3")

    def boom():
        raise ConnectionError("offline")

    with pytest.raises(ConnectionError):
        cache.through("vin", {"vin": "NEW"}, boom)


def test_cache_respects_max_age(tmp_path):
    cache = OfflineCache(tmp_path / "c.sqlite3")
    cache.put("ns", {"k": 1}, {"v": 1})
    assert cache.get("ns", {"k": 1}, max_age=3600) is not None
    assert cache.get("ns", {"k": 1}, max_age=-1) is None

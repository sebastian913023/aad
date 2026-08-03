from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aad.api.app import create_app, get_retriever_dep


@pytest.fixture
def client(settings, indexed_retriever) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[get_retriever_dep] = lambda: indexed_retriever
    return TestClient(app)


G35 = {"year": 2004, "make": "INFINITI", "model": "G35", "engine": "3.5L V6 VQ35DE"}


def test_healthz(client: TestClient):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"


def test_index_stats_reports_indexed_sources(client: TestClient):
    body = client.get("/api/v1/index/stats").json()
    assert body["chunks"] > 0
    assert "sample_service_manual.md" in body["sources"]


def test_manual_search_returns_citations(client: TestClient):
    response = client.post(
        "/api/v1/manuals/search",
        json={"query": "camshaft position sensor removal", "vehicle": G35},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results
    assert results[0]["citation"]["source"] == "sample_service_manual.md"


def test_torque_spec_endpoint(client: TestClient):
    response = client.post(
        "/api/v1/torque-specs",
        json={"component": "camshaft position sensor retaining bolt", "vehicle": G35},
    )
    assert response.status_code == 200
    specs = response.json()["specs"]
    assert specs and specs[0]["value"] == 9.0 and specs[0]["unit"] == "Nm"


def test_labor_time_endpoint(client: TestClient):
    response = client.post(
        "/api/v1/labor-times",
        json={"operation": "camshaft position sensor replacement", "vehicle": G35},
    )
    assert response.status_code == 200
    assert any(entry["hours"] == 0.7 for entry in response.json()["labor_times"])


def test_dtc_endpoint(client: TestClient):
    body = client.post("/api/v1/dtc/lookup", json={"code": "P0340", "vehicle": G35}).json()
    assert body["definition"] == "Camshaft Position Sensor Circuit Malfunction"


def test_unscoped_search_returns_422(client: TestClient):
    response = client.post("/api/v1/manuals/search", json={"query": "torque", "vehicle": {}})
    assert response.status_code == 422
    assert "VIN or year/make/model" in response.json()["detail"]


def test_missing_grounding_returns_404(client: TestClient):
    response = client.post(
        "/api/v1/manuals/search",
        json={"query": "flux capacitor alignment", "vehicle": {"year": 1998, "make": "Saab", "model": "900"}},
    )
    assert response.status_code == 404


def test_unconfigured_parts_provider_returns_501(client: TestClient):
    response = client.post("/api/v1/parts/search", json={"query": "sensor", "vehicle": G35})
    assert response.status_code == 501
    assert "not configured" in response.json()["detail"]


def test_estimate_endpoint(client: TestClient):
    response = client.post(
        "/api/v1/estimates",
        json={
            "vehicle": G35,
            "labor_items": [{"operation": "replace CMP sensor", "hours": 0.7}],
            "part_items": [{"description": "CMP sensor", "unit_price": 100.0}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["labor_subtotal"] == 87.5
    assert body["parts_subtotal"] == 135.0

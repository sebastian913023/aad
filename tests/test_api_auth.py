from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aad.api.app import create_app


@pytest.fixture
def secured(settings) -> TestClient:
    return TestClient(create_app(settings.model_copy(update={"api_token": "s3cret"})))


def test_open_when_no_token_configured(settings):
    client = TestClient(create_app(settings))
    assert client.get("/api/v1/index/stats").status_code == 200


def test_rejects_missing_and_wrong_token(secured):
    assert secured.get("/api/v1/index/stats").status_code == 401
    assert (
        secured.get("/api/v1/index/stats", headers={"authorization": "Bearer nope"}).status_code
        == 401
    )


def test_accepts_bearer_and_api_key_header(secured):
    ok = secured.get("/api/v1/index/stats", headers={"authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    ok = secured.get("/api/v1/index/stats", headers={"x-api-key": "s3cret"})
    assert ok.status_code == 200


def test_healthz_and_shell_stay_public(secured):
    assert secured.get("/healthz").status_code == 200
    assert secured.get("/").status_code == 200


def test_unauthorized_response_still_carries_cors_headers(secured):
    res = secured.get("/api/v1/index/stats", headers={"origin": "https://example.com"})
    assert res.status_code == 401
    assert res.headers["access-control-allow-origin"] == "*"


def test_preflight_is_not_blocked(secured):
    res = secured.options(
        "/api/v1/vin/decode",
        headers={
            "origin": "https://example.com",
            "access-control-request-method": "POST",
            "access-control-request-headers": "authorization,content-type",
        },
    )
    assert res.status_code == 200


def test_entrypoint_reports_startup_failure(monkeypatch):
    """If `aad` cannot be imported, the entrypoint serves a diagnosable 503, not a crash."""
    entry = Path(__file__).resolve().parents[1] / "api" / "index.py"
    monkeypatch.setitem(sys.modules, "aad.api", None)  # makes `from aad.api import app` fail
    spec = importlib.util.spec_from_file_location("entry_under_test", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    res = TestClient(module.app).get("/healthz")
    assert res.status_code == 503
    assert res.json()["status"] == "startup_failed"

from __future__ import annotations

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

"""Transport-failure handling for the live providers.

A network failure must surface as `ProviderError`, not as a raw httpx exception:
the API's error guard maps ProviderError to 502, and anything else escapes as an
unhandled 500. Covered offline by faking the transport, so the contract is pinned
whether or not the live job can reach NHTSA.
"""

from __future__ import annotations

import httpx
import pytest

from aad.errors import ProviderError
from aad.models import Vehicle
from aad.providers import tsb as tsb_provider
from aad.providers import vin as vin_provider


def _raise(exc: Exception):
    def _get(*_args, **_kwargs):
        raise exc

    return _get


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
        httpx.ProxyError("403 Forbidden"),
    ],
)
def test_vin_decode_wraps_transport_failures(monkeypatch, exc):
    monkeypatch.setattr(vin_provider.httpx, "get", _raise(exc))
    with pytest.raises(ProviderError, match="unreachable"):
        vin_provider.decode_vin("1M8GDM9AXKP042788")


def test_vin_decode_wraps_http_errors(monkeypatch):
    monkeypatch.setattr(
        vin_provider.httpx,
        "get",
        lambda *a, **k: httpx.Response(503, request=httpx.Request("GET", "https://vpic.test")),
    )
    with pytest.raises(ProviderError, match="503"):
        vin_provider.decode_vin("1M8GDM9AXKP042788")


def test_vin_decode_wraps_non_json_responses(monkeypatch):
    monkeypatch.setattr(
        vin_provider.httpx,
        "get",
        lambda *a, **k: httpx.Response(
            200, text="<html>maintenance</html>", request=httpx.Request("GET", "https://vpic.test")
        ),
    )
    with pytest.raises(ProviderError, match="non-JSON"):
        vin_provider.decode_vin("1M8GDM9AXKP042788")


def test_recalls_wraps_transport_failures(monkeypatch):
    monkeypatch.setattr(tsb_provider.httpx, "get", _raise(httpx.ConnectError("down")))
    with pytest.raises(ProviderError, match="unreachable"):
        tsb_provider.search_recalls(Vehicle(year=2018, make="Honda", model="Accord"))


def test_transport_failure_becomes_502_not_500(settings, indexed_retriever, monkeypatch):
    """End to end: the API must translate an unreachable provider into a 502."""
    from fastapi.testclient import TestClient

    from aad.api.app import create_app, get_retriever_dep

    monkeypatch.setattr(vin_provider.httpx, "get", _raise(httpx.ConnectError("no route")))

    app = create_app(settings)
    app.dependency_overrides[get_retriever_dep] = lambda: indexed_retriever
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/v1/vin/decode", json={"vin": "1M8GDM9AXKP042788"}
    )

    assert response.status_code == 502
    assert "unreachable" in response.json()["detail"]

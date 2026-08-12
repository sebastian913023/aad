"""Provider profile loading, request building, and response mapping.

Exercised against an in-process mock transport rather than a real vendor, so the
whole configuration surface — every auth scheme, parameter mapping and response
envelope — is covered without a subscription.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from aad.config import Settings
from aad.errors import NotConfiguredError, ProviderError
from aad.models import Vehicle
from aad.providers.client import build_request, call_provider, dig, map_item
from aad.providers.profiles import PROVIDER_NAMES, ProviderProfile, get_profile, load_profiles

G35 = {"year": 2004, "make": "INFINITI", "model": "G35", "engine": "3.5L V6", "vin": None}


def _profile(**overrides) -> ProviderProfile:
    base = {
        "name": "torque",
        "vendor": "Test Vendor",
        "base_url_env": "TEST_BASE",
        "credential_env": "TEST_KEY",
        "auth": {"type": "bearer"},
        "path": "/torque-specs",
        "params": {"year": "year", "make": "make", "component": "component"},
        "results_path": "specs",
        "field_map": {"component": "component", "value": "value", "unit": "unit"},
    }
    base.update(overrides)
    return ProviderProfile.model_validate(base)


ENV = {"TEST_BASE": "https://vendor.test/api", "TEST_KEY": "sk-secret-value"}


# --- shipped profiles -----------------------------------------------------
def test_every_provider_ships_a_profile():
    profiles = load_profiles()
    assert set(profiles) == set(PROVIDER_NAMES)


def test_shipped_profiles_are_structurally_sound():
    for name, profile in load_profiles().items():
        assert profile.validate_shape() == [], f"{name}: {profile.validate_shape()}"


def test_shipped_profiles_are_marked_unverified():
    """Nobody has exercised these against a real subscription. If one is flipped to
    verified, that must be a deliberate act with a test run behind it — not a default
    that quietly implies the mapping is known-good."""
    for name, profile in load_profiles().items():
        assert profile.verified is False, f"{name} claims verified without evidence"


def test_shipped_profiles_are_unconfigured_by_default():
    for profile in load_profiles().values():
        assert not profile.is_configured({})
        assert profile.missing({}) == [profile.base_url_env, profile.credential_env]


def test_override_directory_replaces_a_shipped_profile(tmp_path: Path):
    override = _profile(vendor="My Shop's Mitchell Proxy", path="/v2/torque")
    (tmp_path / "torque.json").write_text(override.model_dump_json(), encoding="utf-8")

    profiles = load_profiles(tmp_path)
    assert profiles["torque"].vendor == "My Shop's Mitchell Proxy"
    assert profiles["torque"].path == "/v2/torque"
    # Untouched profiles still come from the built-ins.
    assert profiles["labor"].name == "labor"


def test_unknown_profile_name_is_reported_with_options():
    with pytest.raises(ProviderError, match="known: labor, obd2, parts, torque, wiring"):
        get_profile("nonexistent")


def test_malformed_override_is_reported_with_its_path(tmp_path: Path):
    (tmp_path / "torque.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ProviderError, match="not valid JSON"):
        load_profiles(tmp_path)


# --- request building -----------------------------------------------------
def test_bearer_auth():
    request = build_request(_profile(), {**G35, "component": "head bolts"}, ENV)
    assert request["headers"]["Authorization"] == "Bearer sk-secret-value"
    assert request["url"] == "https://vendor.test/api/torque-specs"
    assert request["params"] == {"year": 2004, "make": "INFINITI", "component": "head bolts"}


def test_header_auth():
    profile = _profile(auth={"type": "header", "name": "X-Api-Key"})
    request = build_request(profile, G35, ENV)
    assert request["headers"]["X-Api-Key"] == "sk-secret-value"
    assert "Authorization" not in request["headers"]


def test_query_auth():
    profile = _profile(auth={"type": "query", "name": "apikey"})
    request = build_request(profile, G35, ENV)
    assert request["params"]["apikey"] == "sk-secret-value"


def test_basic_auth():
    profile = _profile(auth={"type": "basic", "username_env": "TEST_USER"})
    request = build_request(profile, G35, {**ENV, "TEST_USER": "shop42"})
    assert request["auth"] == ("shop42", "sk-secret-value")


def test_no_auth_needs_no_credential():
    profile = _profile(auth={"type": "none"})
    assert profile.is_configured({"TEST_BASE": "https://vendor.test"})
    request = build_request(profile, G35, {"TEST_BASE": "https://vendor.test"})
    assert "Authorization" not in request["headers"]


def test_post_sends_a_json_body():
    profile = _profile(method="POST")
    request = build_request(profile, {**G35, "component": "head bolts"}, ENV)
    assert "json" in request and "params" not in request
    assert request["json"]["component"] == "head bolts"


def test_static_params_and_headers_are_merged():
    profile = _profile(static_params={"format": "json"}, headers={"Accept": "application/json"})
    request = build_request(profile, G35, ENV)
    assert request["params"]["format"] == "json"
    assert request["headers"]["Accept"] == "application/json"


def test_empty_values_are_omitted_from_the_request():
    request = build_request(_profile(), {**G35, "component": ""}, ENV)
    assert "component" not in request["params"]


def test_missing_configuration_raises_not_configured():
    with pytest.raises(NotConfiguredError, match="TEST_BASE"):
        build_request(_profile(), G35, {})
    with pytest.raises(NotConfiguredError, match="TEST_KEY"):
        build_request(_profile(), G35, {"TEST_BASE": "https://vendor.test"})


# --- response mapping -----------------------------------------------------
def test_dig_follows_dotted_paths_and_list_indices():
    payload = {"data": {"items": [{"name": "first"}, {"name": "second"}]}}
    assert dig(payload, "data.items.1.name") == "second"
    assert dig(payload, "data.missing.name") is None
    assert dig(payload, "data.items.9.name") is None
    assert dig(payload, "") == payload


def test_map_item_drops_paths_that_resolve_to_nothing():
    mapped = map_item({"a": 1}, {"x": "a", "y": "does.not.exist"})
    assert mapped == {"x": 1}


def _transport(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_call_provider_maps_a_nested_response():
    profile = _profile(
        results_path="data.results",
        field_map={"component": "part.name", "value": "torque.value", "unit": "torque.unit"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-secret-value"
        return httpx.Response(
            200,
            json={
                "data": {
                    "results": [
                        {"part": {"name": "head bolt"}, "torque": {"value": 40, "unit": "Nm"}}
                    ]
                }
            },
        )

    results = call_provider(profile, G35, env=ENV, client=_transport(handler))
    assert results == [{"component": "head bolt", "value": 40, "unit": "Nm"}]


def test_call_provider_accepts_a_bare_list_body():
    profile = _profile(results_path="")

    def handler(_request):
        return httpx.Response(200, json=[{"component": "bolt", "value": 9, "unit": "Nm"}])

    results = call_provider(profile, G35, env=ENV, client=_transport(handler))
    assert results[0]["value"] == 9


def test_wrong_results_path_names_the_profile_as_the_problem():
    def handler(_request):
        return httpx.Response(200, json={"items": [{"component": "bolt"}]})

    with pytest.raises(ProviderError, match="results_path does not match"):
        call_provider(_profile(), G35, env=ENV, client=_transport(handler))


def test_credential_rejection_is_reported_distinctly():
    for status in (401, 403):

        def handler(_request, status=status):
            return httpx.Response(status, json={"error": "nope"})

        with pytest.raises(ProviderError, match="rejected the credential"):
            call_provider(_profile(), G35, env=ENV, client=_transport(handler))


def test_non_json_response_is_reported_clearly():
    def handler(_request):
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(ProviderError, match="non-JSON"):
        call_provider(_profile(), G35, env=ENV, client=_transport(handler))


def test_transport_failure_becomes_provider_error():
    def handler(_request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderError, match="unreachable"):
        call_provider(_profile(), G35, env=ENV, client=_transport(handler))


# --- credential safety ----------------------------------------------------
def test_credential_is_redacted_from_error_bodies():
    def handler(_request):
        return httpx.Response(500, text="upstream failed for key sk-secret-value")

    with pytest.raises(ProviderError) as exc:
        call_provider(_profile(), G35, env=ENV, client=_transport(handler))
    assert "sk-secret-value" not in str(exc.value)
    assert "***redacted***" in str(exc.value)


def test_credential_is_redacted_from_transport_errors():
    def handler(_request):
        raise httpx.ConnectError("failed connecting with sk-secret-value")

    with pytest.raises(ProviderError) as exc:
        call_provider(_profile(), G35, env=ENV, client=_transport(handler))
    assert "sk-secret-value" not in str(exc.value)


# --- settings integration -------------------------------------------------
def test_dotenv_values_reach_the_profile(settings: Settings):
    """A key set in .env lands on Settings, not os.environ. The profile must still
    see it, or configuration would silently appear absent."""
    configured = Settings(
        **{
            **settings.model_dump(),
            "torque_api_base": "https://prodemand.example/api",
            "torque_api_key": "sk-from-dotenv",
        }
    )
    profile = get_profile("torque")
    env = configured.provider_env()

    assert profile.is_configured(env)
    assert build_request(profile, G35, env)["headers"]["Authorization"] == "Bearer sk-from-dotenv"


def test_torque_lookup_prefers_a_configured_provider(settings: Settings, indexed_retriever):
    """With a provider configured, the API answer wins over document extraction."""
    from aad.providers import client as client_module
    from aad.providers import torque as torque_provider

    configured = Settings(
        **{
            **settings.model_dump(),
            "torque_api_base": "https://prodemand.example/api",
            "torque_api_key": "sk-test",
        }
    )

    def fake_call(profile, source, *, env=None, client=None):
        assert source["component"] == "cylinder head bolts"
        return [{"component": "cylinder head bolts", "value": 40.0, "unit": "Nm", "bolt_size": "M11x1.5"}]

    torque_provider.call_provider = fake_call
    try:
        result = torque_provider.lookup_torque_spec(
            "cylinder head bolts",
            Vehicle(**{k: v for k, v in G35.items() if v is not None}),
            indexed_retriever,
            configured,
        )
    finally:
        torque_provider.call_provider = client_module.call_provider

    assert result["specs"][0]["value"] == 40.0
    assert result["provider_verified"] is False
    assert "Mitchell 1" in result["source"]


def test_mapping_mismatch_is_reported_as_a_profile_problem(settings: Settings, indexed_retriever):
    from aad.providers import client as client_module
    from aad.providers import torque as torque_provider

    configured = Settings(
        **{**settings.model_dump(), "torque_api_base": "https://x.test", "torque_api_key": "k"}
    )

    torque_provider.call_provider = lambda *a, **k: [{"component": "bolt"}]  # no value/unit
    try:
        with pytest.raises(ProviderError, match="field_map"):
            torque_provider.lookup_torque_spec(
                "bolt", Vehicle(year=2004, make="INFINITI", model="G35"), indexed_retriever, configured
            )
    finally:
        torque_provider.call_provider = client_module.call_provider


def test_parts_not_configured_message_names_the_env_vars(settings: Settings):
    from aad.providers.parts import search_parts

    with pytest.raises(NotConfiguredError) as exc:
        search_parts("water pump", Vehicle(year=2004, make="INFINITI", model="G35"), settings)
    assert "AAD_PARTS_API_BASE" in str(exc.value)
    assert "AAD_PARTS_API_KEY" in str(exc.value)


def test_profile_json_files_are_readable_as_config():
    """The shipped profiles must stay hand-editable: that is the whole configuration
    story for a vendor integration."""
    from aad.providers.profiles import BUILTIN_PROFILE_DIR

    for path in BUILTIN_PROFILE_DIR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert {"name", "vendor", "base_url_env", "credential_env", "field_map"} <= set(data)

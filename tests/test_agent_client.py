"""The model client must fail the same way every other provider does.

Every commercial provider in this system raises `NotConfiguredError` on a missing
credential rather than letting the call fail downstream. The Anthropic client was the
one exception — a missing key reached the SDK's own request-building code and raised
a bare `TypeError`, which escaped the API as an unhandled 500 instead of the 501 every
other "not configured" gap produces. This pins the fix.
"""

from __future__ import annotations

import pytest

from aad.agent.client import build_client
from aad.config import Settings
from aad.errors import NotConfiguredError


def test_missing_anthropic_api_key_raises_not_configured():
    settings = Settings(provider="anthropic", anthropic_api_key=None)
    with pytest.raises(NotConfiguredError, match="anthropic"):
        build_client(settings)


def test_configured_anthropic_api_key_builds_a_client():
    settings = Settings(provider="anthropic", anthropic_api_key="sk-test-key")
    client = build_client(settings)
    assert client.model == settings.model
    assert client.supports_server_fallbacks is True


def test_anthropic_api_key_env_var_is_read_unprefixed(monkeypatch):
    """The Anthropic SDK and every deployment host's own "connect your key" flow set
    the bare ANTHROPIC_API_KEY — requiring AAD_ANTHROPIC_API_KEY instead would mean
    reconfiguring every one of them."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.delenv("AAD_ANTHROPIC_API_KEY", raising=False)
    assert Settings().anthropic_api_key == "sk-test-key"

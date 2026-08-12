"""Execute a provider profile.

One request path for every commercial provider: build the request from the profile,
attach the credential per the configured auth scheme, call, then map the response into
our own field names.

Credentials never appear in an exception message or a returned payload. Provider errors
get surfaced to the model and to HTTP callers verbatim, so a leaked key would travel a
long way.
"""

from __future__ import annotations

from typing import Any

import httpx

from aad.errors import NotConfiguredError, ProviderError
from aad.providers.profiles import ProviderProfile

REDACTED = "***redacted***"


def _redact(text: str, *secrets: str | None) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    return text


def dig(payload: Any, path: str) -> Any:
    """Follow a dotted path, tolerating list indices ("items.0.name")."""
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            if not part.isdigit() or int(part) >= len(current):
                return None
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def map_item(item: Any, field_map: dict[str, str]) -> dict[str, Any]:
    mapped = {field: dig(item, path) for field, path in field_map.items()}
    return {k: v for k, v in mapped.items() if v is not None}


def build_request(
    profile: ProviderProfile, source: dict[str, Any], env: dict[str, str] | None = None
) -> dict[str, Any]:
    """Assemble the request kwargs for a profile. Separated from the call so it can
    be asserted on directly in tests and printed by `aad providers show`."""
    base = profile.base_url(env)
    if base is None:
        raise NotConfiguredError(profile.name, f"set {profile.base_url_env}")

    credential = profile.credential(env)
    if profile.auth.type != "none" and credential is None:
        raise NotConfiguredError(profile.name, f"set {profile.credential_env}")

    params: dict[str, Any] = dict(profile.static_params)
    for request_key, source_key in profile.params.items():
        value = source.get(source_key)
        if value not in (None, ""):
            params[request_key] = value

    headers = dict(profile.headers)
    auth: tuple[str, str] | None = None

    if profile.auth.type == "bearer":
        headers["Authorization"] = f"Bearer {credential}"
    elif profile.auth.type == "header":
        headers[profile.auth.name or "X-Api-Key"] = credential or ""
    elif profile.auth.type == "query":
        params[profile.auth.name or "api_key"] = credential
    elif profile.auth.type == "basic":
        import os

        env_map = os.environ if env is None else env
        username = (env_map.get(profile.auth.username_env or "") or "").strip()
        auth = (username, credential or "")

    request: dict[str, Any] = {
        "method": profile.method,
        "url": f"{base.rstrip('/')}{profile.path}",
        "headers": headers,
        "timeout": profile.timeout,
    }
    if profile.method == "GET":
        request["params"] = params
    else:
        request["json"] = params
    if auth is not None:
        request["auth"] = auth
    return request


def call_provider(
    profile: ProviderProfile,
    source: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Call the provider and return results mapped into our field names."""
    request = build_request(profile, source, env)
    credential = profile.credential(env)

    owned = client is None
    http = client or httpx.Client()
    try:
        response = http.request(**request)
    except httpx.HTTPError as exc:
        raise ProviderError(
            _redact(f"{profile.vendor} unreachable: {type(exc).__name__}: {exc}", credential)
        ) from exc
    finally:
        if owned:
            http.close()

    if response.status_code in (401, 403):
        raise ProviderError(
            f"{profile.vendor} rejected the credential (HTTP {response.status_code}). "
            f"Check {profile.credential_env} and that the subscription covers this endpoint."
        )
    if response.status_code >= 400:
        body = _redact(response.text[:300], credential)
        raise ProviderError(f"{profile.vendor} returned HTTP {response.status_code}: {body}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(
            f"{profile.vendor} returned a non-JSON response "
            f"(content-type: {response.headers.get('content-type', 'unknown')})"
        ) from exc

    results = dig(payload, profile.results_path)
    if results is None:
        raise ProviderError(
            f"{profile.vendor} response has no {profile.results_path!r} field. "
            f"The profile's results_path does not match this API — compare against "
            f"{profile.docs_url or 'the vendor documentation'} and update the profile."
        )
    if not isinstance(results, list):
        results = [results]

    return [map_item(item, profile.field_map) for item in results]

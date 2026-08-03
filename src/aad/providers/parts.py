"""Parts lookup.

There is no free public catalog with real-time pricing, so this requires a configured
supplier account. Unconfigured, it returns a `NotConfiguredError` payload rather than
plausible-looking part numbers — a wrong part number is a wasted parts run.
"""

from __future__ import annotations

from pydantic import ValidationError

from aad.config import Settings, get_settings
from aad.errors import NotConfiguredError, ProviderError
from aad.models import Part, Vehicle
from aad.providers.client import call_provider
from aad.providers.profiles import get_profile


def search_parts(
    query: str,
    vehicle: Vehicle,
    settings: Settings | None = None,
    *,
    limit: int = 10,
) -> dict:
    settings = settings or get_settings()
    profile = get_profile("parts", settings.provider_profile_dir)

    env = settings.provider_env()
    if not profile.is_configured(env):
        raise NotConfiguredError(
            "parts-catalog",
            f"set {' and '.join(profile.missing(env))} for your supplier account "
            f"({profile.vendor}). Part numbers and prices are never inferred.",
        )

    rows = call_provider(
        profile, {**vehicle.model_dump(), "query": query, "limit": limit}, env=env
    )

    parts: list[Part] = []
    for row in rows[:limit]:
        try:
            parts.append(Part.model_validate(row))
        except ValidationError as exc:
            raise ProviderError(
                f"{profile.vendor} returned a row this profile cannot map to a part "
                f"({exc.error_count()} field error(s)). Compare the profile's field_map "
                f"against the vendor's response format: {exc}"
            ) from exc

    return {
        "query": query,
        "vehicle": vehicle.label(),
        "parts": [part.model_dump(exclude_none=True) for part in parts],
        "source": profile.vendor,
        "provider_verified": profile.verified,
    }

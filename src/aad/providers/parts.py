"""Parts lookup.

There is no free public catalog with real-time pricing, so this requires a configured
supplier account. Unconfigured, it returns a `NotConfiguredError` payload rather than
plausible-looking part numbers — a wrong part number is a wasted parts run.
"""

from __future__ import annotations

import httpx

from aad.config import Settings, get_settings
from aad.errors import NotConfiguredError, ProviderError
from aad.models import Part, Vehicle


def search_parts(
    query: str,
    vehicle: Vehicle,
    settings: Settings | None = None,
    *,
    limit: int = 10,
) -> dict:
    settings = settings or get_settings()
    if not (settings.parts_api_base and settings.parts_api_key):
        raise NotConfiguredError(
            "parts-catalog",
            "set AAD_PARTS_API_BASE and AAD_PARTS_API_KEY for your supplier account "
            "(NAPA, WorldPac, or equivalent). Part numbers and prices are never inferred.",
        )

    response = httpx.get(
        f"{settings.parts_api_base.rstrip('/')}/parts/search",
        headers={"Authorization": f"Bearer {settings.parts_api_key}"},
        params={
            "q": query,
            "year": vehicle.year,
            "make": vehicle.make,
            "model": vehicle.model,
            "engine": vehicle.engine,
            "vin": vehicle.vin,
            "limit": limit,
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise ProviderError(f"parts provider returned {response.status_code}: {response.text[:200]}")

    payload = response.json()
    parts = [Part.model_validate(item) for item in payload.get("parts", [])][:limit]
    return {
        "query": query,
        "vehicle": vehicle.label(),
        "parts": [part.model_dump(exclude_none=True) for part in parts],
        "source": payload.get("source", "supplier catalog"),
    }

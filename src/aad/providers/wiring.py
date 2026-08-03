"""Wiring diagrams, connector pinouts and circuit references.

Wire colours and pin numbers are returned only as retrieved text plus a citation.
They are never summarized into a structured answer, because a paraphrased pinout is
indistinguishable from an invented one once it reaches a test light.
"""

from __future__ import annotations

import httpx

from aad.config import Settings, get_settings
from aad.errors import NoGroundingError, ProviderError
from aad.models import Vehicle
from aad.rag.retriever import Retriever


def lookup_wiring(
    circuit: str,
    vehicle: Vehicle,
    retriever: Retriever,
    settings: Settings | None = None,
) -> dict:
    settings = settings or get_settings()

    if settings.wiring_api_base and settings.wiring_api_key:
        response = httpx.get(
            f"{settings.wiring_api_base.rstrip('/')}/wiring-diagrams",
            headers={"Authorization": f"Bearer {settings.wiring_api_key}"},
            params={
                "year": vehicle.year,
                "make": vehicle.make,
                "model": vehicle.model,
                "engine": vehicle.engine,
                "vin": vehicle.vin,
                "circuit": circuit,
            },
            timeout=30.0,
        )
        if response.status_code >= 400:
            raise ProviderError(
                f"wiring provider returned {response.status_code}: {response.text[:200]}"
            )
        payload = response.json()
        if payload.get("diagrams"):
            return {
                "circuit": circuit,
                "vehicle": vehicle.label(),
                "diagrams": payload["diagrams"],
                "source": "subscription wiring diagram API",
            }

    try:
        hits = retriever.search(
            f"{circuit} wiring diagram connector pinout wire color {vehicle.label()}",
            vehicle,
            spec_type="wiring_diagram",
        )
    except NoGroundingError as exc:
        return {
            "circuit": circuit,
            "vehicle": vehicle.label(),
            "diagrams": [],
            "unavailable_reason": str(exc),
            "instruction": "Do not describe wire colours or pin assignments from memory.",
        }

    return {
        "circuit": circuit,
        "vehicle": vehicle.label(),
        "diagrams": [
            {"text": hit.chunk.text, "citation": hit.citation().model_dump()} for hit in hits[:5]
        ],
        "source": "indexed wiring documentation",
        "instruction": "Quote wire colours and pin numbers verbatim from the cited text.",
    }

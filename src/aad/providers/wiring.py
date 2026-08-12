"""Wiring diagrams, connector pinouts and circuit references.

Wire colours and pin numbers are returned only as retrieved text plus a citation.
They are never summarized into a structured answer, because a paraphrased pinout is
indistinguishable from an invented one once it reaches a test light.
"""

from __future__ import annotations

from aad.config import Settings, get_settings
from aad.errors import NoGroundingError
from aad.models import Vehicle
from aad.providers.client import call_provider
from aad.providers.profiles import get_profile
from aad.rag.retriever import Retriever


def lookup_wiring(
    circuit: str,
    vehicle: Vehicle,
    retriever: Retriever,
    settings: Settings | None = None,
) -> dict:
    settings = settings or get_settings()

    profile = get_profile("wiring", settings.provider_profile_dir)
    if profile.is_configured(settings.provider_env()):
        diagrams = call_provider(
            profile, {**vehicle.model_dump(), "circuit": circuit}, env=settings.provider_env()
        )
        if diagrams:
            return {
                "circuit": circuit,
                "vehicle": vehicle.label(),
                "diagrams": diagrams,
                "source": profile.vendor,
                "provider_verified": profile.verified,
                "instruction": "Quote wire colours and pin numbers verbatim from the "
                "provider response.",
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

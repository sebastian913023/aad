"""Labor times.

Same discipline as torque specs: a configured labor-guide API first, then verbatim
extraction from indexed guides, then an explicit gap. Labor times drive customer
pricing, so an invented figure is a billing dispute.
"""

from __future__ import annotations

import re

import httpx

from aad.config import Settings, get_settings
from aad.errors import NoGroundingError, ProviderError
from aad.models import LaborTime, Vehicle
from aad.rag.retriever import Retriever

_HOURS_RE = re.compile(
    r"(?P<hours>\d{1,2}(?:\.\d{1,2})?)\s*(?:hrs?\.?|hours?)\b",
    re.IGNORECASE,
)
_WARRANTY_RE = re.compile(r"\bwarranty\b", re.IGNORECASE)


def extract_labor_times(text: str, operation: str) -> list[dict]:
    terms = re.findall(r"[a-z]{3,}", operation.lower())
    found: list[dict] = []
    for sentence in (s.strip() for s in re.split(r"(?<=[.;:])\s+|\n+", text) if s.strip()):
        for match in _HOURS_RE.finditer(sentence):
            hours = float(match.group("hours"))
            if hours <= 0 or hours > 60:  # Guard against page numbers and part numbers.
                continue
            found.append(
                {
                    "hours": hours,
                    "is_warranty_time": bool(_WARRANTY_RE.search(sentence)),
                    "snippet": sentence,
                    "relevance": sum(1 for term in terms if term in sentence.lower()),
                }
            )
    found.sort(key=lambda item: (-item["relevance"], item["hours"]))
    return found


def _from_api(operation: str, vehicle: Vehicle, settings: Settings) -> list[LaborTime]:
    response = httpx.get(
        f"{settings.labor_api_base.rstrip('/')}/labor-times",
        headers={"Authorization": f"Bearer {settings.labor_api_key}"},
        params={
            "year": vehicle.year,
            "make": vehicle.make,
            "model": vehicle.model,
            "engine": vehicle.engine,
            "vin": vehicle.vin,
            "operation": operation,
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise ProviderError(f"labor provider returned {response.status_code}: {response.text[:200]}")
    return [LaborTime.model_validate(item) for item in response.json().get("operations", [])]


def lookup_labor_time(
    operation: str,
    vehicle: Vehicle,
    retriever: Retriever,
    settings: Settings | None = None,
) -> dict:
    settings = settings or get_settings()

    if settings.labor_api_base and settings.labor_api_key:
        times = _from_api(operation, vehicle, settings)
        if times:
            return {
                "operation": operation,
                "vehicle": vehicle.label(),
                "labor_times": [t.model_dump(exclude_none=True) for t in times],
                "source": "subscription labor time API",
            }

    query = f"{operation} labor time book hours {vehicle.label()}"
    try:
        hits = retriever.search(query, vehicle, spec_type="labor_time")
    except NoGroundingError as exc:
        return {
            "operation": operation,
            "vehicle": vehicle.label(),
            "labor_times": [],
            "unavailable_reason": str(exc),
            "instruction": "Do not estimate labor hours. Report that no published time was found.",
        }

    entries: list[dict] = []
    for hit in hits:
        citation = hit.citation().model_dump()
        for candidate in extract_labor_times(hit.chunk.text, operation)[:3]:
            entries.append(
                {
                    "operation": operation,
                    "hours": candidate["hours"],
                    "is_warranty_time": candidate["is_warranty_time"],
                    "verbatim_source_text": candidate["snippet"],
                    "citation": citation,
                }
            )

    if not entries:
        return {
            "operation": operation,
            "vehicle": vehicle.label(),
            "labor_times": [],
            "unavailable_reason": (
                f"documentation for {vehicle.label()} was found but contains no published time for "
                f"{operation!r}"
            ),
            "instruction": "Do not estimate labor hours.",
        }

    return {
        "operation": operation,
        "vehicle": vehicle.label(),
        "labor_times": entries[:8],
        "source": "indexed labor guide",
    }

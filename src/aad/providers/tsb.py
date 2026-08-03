"""Technical service bulletins and recalls.

NHTSA publishes recalls and complaint data for free and without a key; that is used
directly. Full TSB text is not public, so bulletin bodies come from indexed
documentation with citations.
"""

from __future__ import annotations

import httpx

from aad.errors import NoGroundingError, ProviderError
from aad.models import Vehicle
from aad.rag.retriever import Retriever

RECALLS_URL = "https://api.nhtsa.gov/recalls/recallsByVehicle"


def search_recalls(vehicle: Vehicle, *, timeout: float = 20.0) -> dict:
    if not (vehicle.year and vehicle.make and vehicle.model):
        return {
            "recalls": [],
            "unavailable_reason": "NHTSA recall search needs year, make and model. "
            "Decode the VIN first.",
        }

    response = httpx.get(
        RECALLS_URL,
        params={"make": vehicle.make, "model": vehicle.model, "modelYear": vehicle.year},
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise ProviderError(f"NHTSA recalls returned {response.status_code}")

    results = response.json().get("results", []) or []
    return {
        "vehicle": vehicle.label(),
        "recalls": [
            {
                "campaign_number": item.get("NHTSACampaignNumber"),
                "component": item.get("Component"),
                "summary": item.get("Summary"),
                "consequence": item.get("Consequence"),
                "remedy": item.get("Remedy"),
                "report_received_date": item.get("ReportReceivedDate"),
            }
            for item in results
        ],
        "source": "NHTSA recalls API",
    }


def search_tsbs(
    symptom: str,
    vehicle: Vehicle,
    retriever: Retriever,
    *,
    include_recalls: bool = True,
) -> dict:
    out: dict = {"symptom": symptom, "vehicle": vehicle.label(), "bulletins": []}

    try:
        hits = retriever.search(
            f"technical service bulletin {symptom} {vehicle.label()}", vehicle, spec_type="tsb"
        )
    except NoGroundingError:
        hits = []

    out["bulletins"] = [
        {"text": hit.chunk.text, "citation": hit.citation().model_dump()} for hit in hits[:5]
    ]
    if not out["bulletins"]:
        out["bulletins_note"] = (
            f"no indexed bulletins matched {symptom!r} for {vehicle.label()}. This does not mean "
            "none exist — check the OEM bulletin portal."
        )

    if include_recalls:
        try:
            out["recalls"] = search_recalls(vehicle)
        except ProviderError as exc:
            out["recalls_error"] = str(exc)

    return out

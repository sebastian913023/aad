"""Live OBD-II scan retrieval.

The scan itself comes from whatever adapter or scan service the shop uses; this is the
integration point, not a scan-tool implementation. Codes returned by the service are
enriched locally via `providers.dtc` so definitions stay grounded.
"""

from __future__ import annotations

import httpx

from aad.config import Settings, get_settings
from aad.errors import NotConfiguredError, ProviderError
from aad.models import Vehicle
from aad.providers.dtc import decode_dtc
from aad.rag.retriever import Retriever


def scan_vehicle(
    vehicle: Vehicle,
    settings: Settings | None = None,
    *,
    scan_id: str | None = None,
    retriever: Retriever | None = None,
) -> dict:
    """Fetch the most recent (or a specific) scan for a vehicle."""
    settings = settings or get_settings()
    if not (settings.obd2_api_base and settings.obd2_api_key):
        raise NotConfiguredError(
            "obd2-scan",
            "set AAD_OBD2_API_BASE and AAD_OBD2_API_KEY for your scan service. Codes can "
            "also be entered manually and looked up with the DTC tool.",
        )

    params: dict[str, str | int] = {}
    if scan_id:
        params["scan_id"] = scan_id
    if vehicle.vin:
        params["vin"] = vehicle.vin
    for key in ("year", "make", "model"):
        value = getattr(vehicle, key)
        if value:
            params[key] = value

    response = httpx.get(
        f"{settings.obd2_api_base.rstrip('/')}/scans/latest",
        headers={"Authorization": f"Bearer {settings.obd2_api_key}"},
        params=params,
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise ProviderError(f"OBD2 provider returned {response.status_code}: {response.text[:200]}")

    payload = response.json()
    codes = []
    for entry in payload.get("codes", []):
        code = entry.get("code") if isinstance(entry, dict) else str(entry)
        structure = decode_dtc(str(code))
        codes.append(
            {
                **structure,
                "status": (entry.get("status") if isinstance(entry, dict) else None),
                "freeze_frame": (entry.get("freeze_frame") if isinstance(entry, dict) else None),
            }
        )

    return {
        "vehicle": vehicle.label(),
        "scan_id": payload.get("scan_id", scan_id),
        "scanned_at": payload.get("scanned_at"),
        "codes": codes,
        "readiness_monitors": payload.get("readiness_monitors"),
        "live_data": payload.get("live_data"),
        "source": "OBD2 scan service",
    }

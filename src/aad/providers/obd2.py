"""Live OBD-II scan retrieval.

The scan itself comes from whatever adapter or scan service the shop uses; this is the
integration point, not a scan-tool implementation. Codes returned by the service are
enriched locally via `providers.dtc` so definitions stay grounded — the service supplies
which codes are stored, not what they mean.
"""

from __future__ import annotations

from aad.config import Settings, get_settings
from aad.errors import NotConfiguredError
from aad.models import Vehicle
from aad.providers.client import call_provider
from aad.providers.dtc import decode_dtc
from aad.providers.profiles import get_profile
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
    profile = get_profile("obd2", settings.provider_profile_dir)

    env = settings.provider_env()
    if not profile.is_configured(env):
        raise NotConfiguredError(
            "obd2-scan",
            f"set {' and '.join(profile.missing(env))} for your scan service. Codes can "
            "also be entered manually and looked up with the DTC tool.",
        )

    rows = call_provider(profile, {**vehicle.model_dump(), "scan_id": scan_id}, env=env)

    codes = []
    for row in rows:
        code = str(row.get("code", "")).strip()
        if not code:
            continue
        codes.append(
            {
                **decode_dtc(code),
                "status": row.get("status"),
                "freeze_frame": row.get("freeze_frame"),
            }
        )

    return {
        "vehicle": vehicle.label(),
        "scan_id": scan_id,
        "codes": codes,
        "source": profile.vendor,
        "provider_verified": profile.verified,
        "instruction": "Code definitions come from the DTC tool, not from the scan "
        "service. Look up any code you intend to discuss.",
    }

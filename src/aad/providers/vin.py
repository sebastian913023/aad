"""VIN validation and decoding.

Validation is done locally with the ISO 3779 / FMVSS 115 check-digit algorithm before
any network call: a transposed character caught here is a wrong vehicle avoided
downstream. Decoding uses NHTSA's public vPIC service, which is free and requires no
key.
"""

from __future__ import annotations

import httpx

from aad.errors import ProviderError
from aad.models import Vehicle

VPIC_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}"

# I and O and Q are excluded from VINs precisely because they are confusable with 1/0.
_TRANSLITERATION = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}
_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def validate_vin(vin: str) -> tuple[bool, str]:
    """Return (is_valid, reason). Check digits are only mandated for North American
    market vehicles, so a failing digit is reported as suspect rather than fatal."""
    vin = vin.strip().upper()
    if len(vin) != 17:
        return False, f"VIN must be 17 characters, got {len(vin)}"
    invalid = {c for c in vin if c not in _TRANSLITERATION and c not in {"I", "O", "Q"}}
    if invalid:
        return False, f"VIN contains invalid characters: {''.join(sorted(invalid))}"
    if any(c in vin for c in "IOQ"):
        return False, "VIN contains I, O or Q, which are not valid VIN characters"

    total = sum(_TRANSLITERATION[char] * weight for char, weight in zip(vin, _WEIGHTS))
    remainder = total % 11
    expected = "X" if remainder == 10 else str(remainder)
    if vin[8] != expected:
        return False, (
            f"check digit mismatch: position 9 is {vin[8]!r}, expected {expected!r}. "
            "Verify the VIN was read correctly (non-North-American vehicles may not "
            "carry a valid check digit)."
        )
    return True, "valid"


def _engine_label(record: dict) -> str | None:
    displacement = record.get("DisplacementL")
    cylinders = record.get("EngineCylinders")
    configuration = record.get("EngineConfiguration") or ""
    parts: list[str] = []
    if displacement:
        try:
            parts.append(f"{float(displacement):.1f}L")
        except (TypeError, ValueError):
            parts.append(str(displacement))
    if cylinders:
        prefix = "V" if configuration.lower().startswith("v") else "I"
        parts.append(f"{prefix}{cylinders}")
    model_name = record.get("EngineModel")
    if model_name:
        parts.append(str(model_name))
    return " ".join(parts) or None


def decode_vin(vin: str, *, timeout: float = 20.0) -> dict:
    """Decode a VIN to a `Vehicle` plus the raw fields NHTSA returned."""
    vin = vin.strip().upper()
    valid, reason = validate_vin(vin)

    try:
        response = httpx.get(VPIC_URL.format(vin=vin), params={"format": "json"}, timeout=timeout)
    except httpx.HTTPError as exc:
        # Transport failures become ProviderError so callers see one error type
        # for "the provider did not answer" — and the API maps it to 502 rather
        # than leaking an unhandled exception as a 500.
        raise ProviderError(f"NHTSA vPIC unreachable: {type(exc).__name__}: {exc}") from exc

    if response.status_code >= 400:
        raise ProviderError(f"NHTSA vPIC returned {response.status_code} for VIN {vin}")

    try:
        results = response.json().get("Results") or []
    except ValueError as exc:
        raise ProviderError(f"NHTSA vPIC returned a non-JSON response for VIN {vin}") from exc

    if not results:
        raise ProviderError(f"NHTSA vPIC returned no results for VIN {vin}")
    record = results[0]

    error_text = (record.get("ErrorText") or "").strip()
    year = record.get("ModelYear")
    vehicle = Vehicle(
        vin=vin,
        year=int(year) if str(year).isdigit() else None,
        make=(record.get("Make") or None),
        model=(record.get("Model") or None),
        engine=_engine_label(record),
        trim=(record.get("Trim") or None),
    )

    interesting = {
        "body_class": record.get("BodyClass"),
        "drive_type": record.get("DriveType"),
        "fuel_type": record.get("FuelTypePrimary"),
        "transmission": record.get("TransmissionStyle"),
        "transmission_speeds": record.get("TransmissionSpeeds"),
        "plant_country": record.get("PlantCountry"),
        "manufacturer": record.get("Manufacturer"),
        "displacement_l": record.get("DisplacementL"),
        "engine_cylinders": record.get("EngineCylinders"),
        "engine_hp": record.get("EngineHP"),
        "series": record.get("Series"),
    }

    return {
        "vehicle": vehicle.model_dump(exclude_none=True),
        "check_digit_valid": valid,
        "check_digit_note": reason,
        "details": {k: v for k, v in interesting.items() if v not in (None, "", "Not Applicable")},
        "decode_errors": error_text or None,
        "source": "NHTSA vPIC",
    }

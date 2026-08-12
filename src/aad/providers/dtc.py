"""Diagnostic trouble code handling.

Two layers, deliberately separated:

* `decode_dtc` decomposes the code structurally (SAE J2012). This is pure format
  parsing and is always correct — it never claims to know what the code *means*.
* `lookup_dtc` returns a definition. Generic SAE definitions come from a small curated
  table; anything else must come from indexed manufacturer documentation, with
  citations. Manufacturer-specific codes ($1/$3 second digit) have no universal
  meaning, so guessing at one is a defect, not a convenience.
"""

from __future__ import annotations

import re

from aad.errors import NoGroundingError
from aad.models import Vehicle
from aad.rag.retriever import Retriever

DTC_RE = re.compile(r"^([PBCU])([0-3])([0-9A-F])([0-9A-F]{2})$", re.IGNORECASE)

_SYSTEMS = {
    "P": "Powertrain (engine and transmission)",
    "B": "Body (interior, comfort, safety)",
    "C": "Chassis (ABS, suspension, steering)",
    "U": "Network / communication (bus)",
}

# The second character distinguishes SAE-defined codes from OEM-defined ones. This
# distinction is what determines whether a definition can be given without a manual.
_CODE_TYPES = {
    "0": ("generic", "SAE/ISO standardized — definition is the same across manufacturers"),
    "1": ("manufacturer", "manufacturer-specific — definition varies by OEM"),
    "2": ("mixed", "manufacturer-specific in most systems; check OEM documentation"),
    "3": ("mixed", "jointly defined or manufacturer-specific; check OEM documentation"),
}

_P_SUBSYSTEMS = {
    "0": "Fuel and air metering / auxiliary emission controls",
    "1": "Fuel and air metering",
    "2": "Fuel and air metering (injector circuit)",
    "3": "Ignition system or misfire",
    "4": "Auxiliary emission controls",
    "5": "Vehicle speed control, idle control, auxiliary inputs",
    "6": "Computer output circuit / control module",
    "7": "Transmission",
    "8": "Transmission",
    "9": "Transmission / control module",
    "A": "Hybrid propulsion",
    "B": "Hybrid propulsion",
    "C": "Hybrid propulsion",
}

# Generic SAE powertrain codes only. Intentionally narrow: this table exists so common
# codes resolve offline, not to substitute for the manufacturer's diagnostic tree.
GENERIC_DTCS: dict[str, str] = {
    "P0101": "Mass or Volume Air Flow Circuit Range/Performance Problem",
    "P0102": "Mass or Volume Air Flow Circuit Low Input",
    "P0103": "Mass or Volume Air Flow Circuit High Input",
    "P0110": "Intake Air Temperature Circuit Malfunction",
    "P0113": "Intake Air Temperature Circuit High Input",
    "P0116": "Engine Coolant Temperature Circuit Range/Performance Problem",
    "P0117": "Engine Coolant Temperature Circuit Low Input",
    "P0118": "Engine Coolant Temperature Circuit High Input",
    "P0120": "Throttle/Pedal Position Sensor/Switch A Circuit Malfunction",
    "P0128": "Coolant Thermostat (Coolant Temperature Below Thermostat Regulating Temperature)",
    "P0130": "O2 Sensor Circuit Malfunction (Bank 1 Sensor 1)",
    "P0135": "O2 Sensor Heater Circuit Malfunction (Bank 1 Sensor 1)",
    "P0171": "System Too Lean (Bank 1)",
    "P0172": "System Too Rich (Bank 1)",
    "P0174": "System Too Lean (Bank 2)",
    "P0175": "System Too Rich (Bank 2)",
    "P0300": "Random/Multiple Cylinder Misfire Detected",
    "P0301": "Cylinder 1 Misfire Detected",
    "P0302": "Cylinder 2 Misfire Detected",
    "P0303": "Cylinder 3 Misfire Detected",
    "P0304": "Cylinder 4 Misfire Detected",
    "P0305": "Cylinder 5 Misfire Detected",
    "P0306": "Cylinder 6 Misfire Detected",
    "P0307": "Cylinder 7 Misfire Detected",
    "P0308": "Cylinder 8 Misfire Detected",
    "P0325": "Knock Sensor 1 Circuit Malfunction (Bank 1)",
    "P0335": "Crankshaft Position Sensor A Circuit Malfunction",
    "P0336": "Crankshaft Position Sensor A Circuit Range/Performance",
    "P0340": "Camshaft Position Sensor Circuit Malfunction",
    "P0341": "Camshaft Position Sensor Circuit Range/Performance",
    "P0344": "Camshaft Position Sensor Circuit Intermittent",
    "P0420": "Catalyst System Efficiency Below Threshold (Bank 1)",
    "P0430": "Catalyst System Efficiency Below Threshold (Bank 2)",
    "P0440": "Evaporative Emission Control System Malfunction",
    "P0442": "Evaporative Emission Control System Leak Detected (small leak)",
    "P0446": "Evaporative Emission Control System Vent Control Circuit Malfunction",
    "P0455": "Evaporative Emission Control System Leak Detected (gross leak)",
    "P0500": "Vehicle Speed Sensor Malfunction",
    "P0505": "Idle Control System Malfunction",
    "P0700": "Transmission Control System Malfunction",
}


def decode_dtc(code: str) -> dict:
    """Structural decomposition of a DTC. Format parsing only — no definition."""
    code = code.strip().upper()
    match = DTC_RE.match(code)
    if not match:
        return {
            "code": code,
            "valid": False,
            "error": "not a valid DTC: expected one of P/B/C/U followed by 4 hex digits "
            "(for example P0340)",
        }

    system_char, type_char, subsystem_char, _ = match.groups()
    code_type, code_type_note = _CODE_TYPES[type_char]
    result = {
        "code": code,
        "valid": True,
        "system": _SYSTEMS[system_char],
        "code_type": code_type,
        "code_type_note": code_type_note,
    }
    if system_char == "P":
        result["subsystem"] = _P_SUBSYSTEMS.get(subsystem_char.upper(), "Unknown subsystem")
    return result


def lookup_dtc(code: str, vehicle: Vehicle, retriever: Retriever | None = None) -> dict:
    """Resolve a DTC to a definition, preferring vehicle-specific documentation."""
    structure = decode_dtc(code)
    if not structure.get("valid"):
        return structure

    code = structure["code"]
    result = dict(structure)
    result["citations"] = []

    generic = GENERIC_DTCS.get(code)
    if generic:
        result["definition"] = generic
        result["definition_source"] = "SAE generic code table"

    if retriever is not None and vehicle.is_scoped():
        try:
            hits = retriever.search(
                f"{code} diagnostic trouble code definition diagnosis", vehicle, spec_type="dtc"
            )
        except NoGroundingError:
            hits = []
        if hits:
            result["documentation"] = [
                {"text": hit.chunk.text, "citation": hit.citation().model_dump()} for hit in hits[:4]
            ]
            result["citations"] = [hit.citation().model_dump() for hit in hits[:4]]

    if "definition" not in result:
        # No authoritative definition. Retrieved documentation may mention the code
        # without defining it, so its presence does not clear this caution — the
        # note stands either way.
        result["definition"] = None
        result["definition_source"] = None
        if result.get("documentation"):
            result["note"] = (
                f"{code} is a {structure['code_type']} code with no entry in the generic table. "
                "The documentation below was retrieved for this vehicle but may not define this "
                "code. Read the cited text before relying on it; do not assume a definition."
            )
        else:
            result["note"] = (
                f"{code} is a {structure['code_type']} code with no entry in the generic table "
                f"and no indexed documentation for {vehicle.label()}. Consult the OEM service "
                "information; do not assume a definition."
            )
    return result

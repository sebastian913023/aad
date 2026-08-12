"""External data providers and grounded fallbacks."""

from aad.providers.dtc import decode_dtc, lookup_dtc
from aad.providers.labor import lookup_labor_time
from aad.providers.obd2 import scan_vehicle
from aad.providers.parts import search_parts
from aad.providers.torque import lookup_torque_spec
from aad.providers.tsb import search_tsbs
from aad.providers.vin import decode_vin, validate_vin
from aad.providers.wiring import lookup_wiring

__all__ = [
    "decode_dtc",
    "decode_vin",
    "lookup_dtc",
    "lookup_labor_time",
    "lookup_torque_spec",
    "lookup_wiring",
    "scan_vehicle",
    "search_parts",
    "search_tsbs",
    "validate_vin",
]

from __future__ import annotations

import pytest

from aad.errors import NotConfiguredError
from aad.models import Vehicle
from aad.providers.dtc import decode_dtc, lookup_dtc
from aad.providers.labor import extract_labor_times, lookup_labor_time
from aad.providers.parts import search_parts
from aad.providers.torque import extract_torque_specs, lookup_torque_spec


# --- VIN ------------------------------------------------------------------
def test_vin_check_digit_validation():
    from aad.providers.vin import validate_vin

    # Known-good check digit (NHTSA's own documentation example).
    ok, _ = validate_vin("5UXWX7C5*BA")
    assert ok is False  # wrong length is rejected before anything else

    valid, reason = validate_vin("1M8GDM9AXKP042788")
    assert valid, reason

    tampered = "1M8GDM9A1KP042788"  # check digit changed X -> 1
    valid, reason = validate_vin(tampered)
    assert not valid and "check digit" in reason


def test_vin_rejects_illegal_characters():
    from aad.providers.vin import validate_vin

    valid, reason = validate_vin("1M8GDM9AXKO042788")
    assert not valid and ("I, O or Q" in reason or "invalid characters" in reason)


# --- DTC ------------------------------------------------------------------
def test_decode_dtc_structure():
    result = decode_dtc("P0340")
    assert result["valid"]
    assert result["code_type"] == "generic"
    assert "Powertrain" in result["system"]
    assert "Ignition" in result["subsystem"]


def test_decode_dtc_flags_manufacturer_specific():
    result = decode_dtc("P1610")
    assert result["code_type"] == "manufacturer"
    assert "varies by OEM" in result["code_type_note"]


def test_decode_dtc_rejects_garbage():
    assert decode_dtc("banana")["valid"] is False


def test_lookup_dtc_generic_definition(indexed_retriever, g35):
    result = lookup_dtc("P0340", g35, indexed_retriever)
    assert result["definition"] == "Camshaft Position Sensor Circuit Malfunction"
    assert result["definition_source"] == "SAE generic code table"


def test_lookup_dtc_refuses_to_invent_manufacturer_code(indexed_retriever, g35):
    result = lookup_dtc("P1610", g35, indexed_retriever)
    assert result.get("definition") is None
    assert "do not assume a definition" in result["note"].lower()


# --- torque extraction ----------------------------------------------------
def test_extract_torque_specs_reads_value_unit_and_bolt():
    text = "Tighten the camshaft position sensor retaining bolt to 9 Nm using bolt size M6 x 1.0."
    found = extract_torque_specs(text, "camshaft position sensor bolt")
    assert found
    assert found[0]["value"] == 9.0
    assert found[0]["unit"] == "Nm"
    assert found[0]["bolt_size"] == "M6x1.0"
    assert "9 Nm" in found[0]["snippet"]


def test_extract_torque_specs_handles_ranges_and_imperial():
    found = extract_torque_specs("Torque the drain plug to 18-22 ft-lbs.", "drain plug")
    assert found[0]["value"] == 18.0
    assert found[0]["value_high"] == 22.0
    assert found[0]["unit"] == "lb-ft"


def test_extract_torque_specs_detects_sequence_and_stages():
    text = "Stage 1: torque all bolts to 40 Nm in sequence shown. Bolt size M11 x 1.5."
    found = extract_torque_specs(text, "cylinder head bolts")
    assert found[0]["sequence"] is not None


def test_lookup_torque_spec_returns_cited_value(indexed_retriever, g35, settings):
    result = lookup_torque_spec(
        "camshaft position sensor retaining bolt", g35, indexed_retriever, settings
    )
    assert result["specs"], result
    top = result["specs"][0]
    assert top["value"] == 9.0 and top["unit"] == "Nm"
    assert top["citation"]["source"] == "sample_service_manual.md"
    assert "9 Nm" in top["verbatim_source_text"]


def test_lookup_torque_spec_reports_gap_for_unknown_vehicle(indexed_retriever, settings):
    other = Vehicle(year=1998, make="Saab", model="900")
    result = lookup_torque_spec("cylinder head bolts", other, indexed_retriever, settings)
    assert result["specs"] == []
    assert "unavailable_reason" in result
    assert "do not estimate" in result["instruction"].lower()


# --- labor ----------------------------------------------------------------
def test_extract_labor_times_ignores_implausible_numbers():
    found = extract_labor_times("See page 412 hrs is not a thing. Labor time is 0.7 hrs.", "labor")
    assert all(item["hours"] <= 60 for item in found)
    assert any(item["hours"] == 0.7 for item in found)


def test_lookup_labor_time_returns_cited_hours(indexed_retriever, g35, settings):
    result = lookup_labor_time(
        "camshaft position sensor replacement", g35, indexed_retriever, settings
    )
    assert result["labor_times"]
    assert any(entry["hours"] == 0.7 for entry in result["labor_times"])
    assert result["labor_times"][0]["citation"]["source"] == "sample_service_manual.md"


# --- parts ----------------------------------------------------------------
def test_parts_lookup_refuses_without_supplier_account(g35, settings):
    with pytest.raises(NotConfiguredError) as exc:
        search_parts("camshaft position sensor", g35, settings)
    assert "never inferred" in str(exc.value)


# --- wiring ---------------------------------------------------------------
def test_wiring_lookup_returns_verbatim_text(indexed_retriever, g35, settings):
    from aad.providers.wiring import lookup_wiring

    result = lookup_wiring(
        "camshaft position sensor signal circuit", g35, indexed_retriever, settings
    )
    assert result["diagrams"]
    assert "verbatim" in result["instruction"].lower()

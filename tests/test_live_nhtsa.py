"""Live tests against NHTSA's real services.

Deselected by default (`-m 'not live'` in pyproject) and additionally gated on
AAD_LIVE_TESTS=1, so a normal run needs no network. CI runs these in a dedicated
job — that separation is deliberate: an NHTSA outage should be legible as an
upstream problem, not as a failure of the diff under review.

These assert on *shape and semantics*, not on specific vehicle data, because the
vPIC dataset changes over time. A test that pins today's exact trim string would
fail for reasons that have nothing to do with this code.
"""

from __future__ import annotations

import os

import pytest

from aad.errors import ProviderError
from aad.models import Vehicle
from aad.providers.tsb import search_recalls
from aad.providers.vin import decode_vin, validate_vin

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("AAD_LIVE_TESTS") != "1",
        reason="set AAD_LIVE_TESTS=1 to run tests against real external services",
    ),
]

# NHTSA's own documented example VIN, used in their vPIC materials.
EXAMPLE_VIN = "1M8GDM9AXKP042788"


def test_vpic_decodes_example_vin():
    result = decode_vin(EXAMPLE_VIN)

    assert result["source"] == "NHTSA vPIC"
    assert result["check_digit_valid"] is True

    vehicle = Vehicle.model_validate(result["vehicle"])
    assert vehicle.vin == EXAMPLE_VIN
    assert vehicle.make, "vPIC returned no Make — decode contract changed"
    assert vehicle.year and 1980 <= vehicle.year <= 2100


def test_vpic_decode_produces_a_scoped_vehicle():
    """The decode has to yield something the retriever will actually accept."""
    result = decode_vin(EXAMPLE_VIN)
    vehicle = Vehicle.model_validate(result["vehicle"])
    assert vehicle.is_scoped(), (
        "decoded vehicle is not scoped for retrieval; every downstream lookup "
        "would be rejected"
    )
    assert vehicle.filter_dict().get("make") == (vehicle.make or "").lower()


def test_vpic_reports_errors_for_a_nonsense_vin():
    """A structurally-valid-length but meaningless VIN must not silently succeed."""
    result = decode_vin("11111111111111111")
    assert result["decode_errors"] or not result["vehicle"].get("make"), (
        "vPIC accepted a nonsense VIN without flagging it; the error path is not wired up"
    )


def test_vpic_response_populates_detail_fields():
    result = decode_vin(EXAMPLE_VIN)
    details = result["details"]
    assert isinstance(details, dict)
    # At least one enrichment field should come back for a real vehicle; which
    # ones are populated varies by make and year, so don't pin a specific key.
    assert details, "no detail fields returned — the field mapping may have drifted"


def test_check_digit_validation_matches_vpic_acceptance():
    """Our local check-digit maths must agree with the VIN vPIC accepts."""
    valid, reason = validate_vin(EXAMPLE_VIN)
    assert valid, reason


def test_recalls_endpoint_returns_wellformed_results():
    vehicle = Vehicle(year=2018, make="Honda", model="Accord")
    result = search_recalls(vehicle)

    assert result["source"] == "NHTSA recalls API"
    assert isinstance(result["recalls"], list)
    for recall in result["recalls"][:3]:
        # Whether this vehicle has recalls varies; the field contract must not.
        assert set(recall) >= {"campaign_number", "component", "summary", "remedy"}


def test_recalls_requires_year_make_model():
    result = search_recalls(Vehicle(vin=EXAMPLE_VIN))
    assert result["recalls"] == []
    assert "year, make and model" in result["unavailable_reason"]


def test_provider_error_surfaces_for_a_bad_host(monkeypatch):
    """A transport failure must raise ProviderError, not return junk."""
    from aad.providers import vin as vin_module

    monkeypatch.setattr(
        vin_module, "VPIC_URL", "https://vpic.nhtsa.dot.gov/api/vehicles/NoSuchEndpoint/{vin}"
    )
    with pytest.raises(ProviderError):
        decode_vin(EXAMPLE_VIN)

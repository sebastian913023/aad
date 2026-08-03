"""Labor times.

Same discipline as torque specs: a configured labor-guide API first, then verbatim
extraction from indexed guides, then an explicit gap. Labor times drive customer
pricing, so an invented figure is a billing dispute.
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from aad.config import Settings, get_settings
from aad.errors import NoGroundingError, ProviderError
from aad.models import LaborTime, Vehicle
from aad.providers.client import call_provider
from aad.providers.profiles import get_profile
from aad.rag.retriever import Retriever

_HOURS_RE = re.compile(
    r"(?P<hours>\d{1,2}(?:\.\d{1,2})?)\s*(?:hrs?\.?|hours?)\b",
    re.IGNORECASE,
)
_WARRANTY_RE = re.compile(r"\bwarranty\b", re.IGNORECASE)

# Words shared by every labor entry, so a match on them alone identifies nothing.
_GENERIC_OPERATION = frozenset(
    {
        "labor", "time", "times", "hours", "hrs", "operation", "operations",
        "replace", "replacement", "remove", "removal", "install", "installation",
        "service", "repair", "the", "and", "for", "with", "from",
    }
)


def _words(text: str) -> set[str]:
    """Whole words of 3+ characters, with trailing plurals folded.

    Substring matching would equate 'shaft' with 'camshaft' and hand back an
    unrelated operation's hours, so matching is on whole words only.
    """
    words = set(re.findall(r"[a-z]{3,}", text.lower()))
    return {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words}


def _operation_terms(operation: str) -> tuple[set[str], set[str]]:
    words = _words(operation)
    return words - _GENERIC_OPERATION, words & _GENERIC_OPERATION


def extract_labor_times(text: str, operation: str, section: str | None = None) -> list[dict]:
    """Extract published hours, gated on the operation actually being named.

    Same reasoning as the torque extractor: retrieval returns the closest chunks for
    the vehicle, so without this gate an operation absent from the guide picks up an
    unrelated time and it lands on a customer's estimate.
    """
    distinctive, generic = _operation_terms(operation)
    heading_words = _words(section or "")

    found: list[dict] = []
    # Not splitting on ':' — "bank 1: 0.7 hrs" must stay attached to the operation
    # that labels it, or the hours land in a fragment identifying nothing.
    for sentence in (s.strip() for s in re.split(r"(?<=[.;])\s+|\n+", text) if s.strip()):
        sentence_words = _words(sentence)
        matched_distinctive = distinctive & (sentence_words | heading_words)
        matched_generic = generic & sentence_words

        if distinctive and not matched_distinctive:
            continue
        if not distinctive and not matched_generic:
            continue

        for match in _HOURS_RE.finditer(sentence):
            hours = float(match.group("hours"))
            if hours <= 0 or hours > 60:  # Guard against page numbers and part numbers.
                continue
            found.append(
                {
                    "hours": hours,
                    "is_warranty_time": bool(_WARRANTY_RE.search(sentence)),
                    "snippet": sentence,
                    "relevance": len(matched_distinctive) * 2 + len(matched_generic),
                }
            )
    found.sort(key=lambda item: (-item["relevance"], item["hours"]))
    return found


def _from_api(operation: str, vehicle: Vehicle, settings: Settings) -> list[LaborTime]:
    profile = get_profile("labor", settings.provider_profile_dir)
    rows = call_provider(
        profile, {**vehicle.model_dump(), "operation": operation}, env=settings.provider_env()
    )

    times: list[LaborTime] = []
    for row in rows:
        try:
            times.append(LaborTime.model_validate(row))
        except ValidationError as exc:
            raise ProviderError(
                f"{profile.vendor} returned a row this profile cannot map to a labor "
                f"time ({exc.error_count()} field error(s)). Compare the profile's "
                f"field_map against the vendor's response format: {exc}"
            ) from exc
    return times


def lookup_labor_time(
    operation: str,
    vehicle: Vehicle,
    retriever: Retriever,
    settings: Settings | None = None,
) -> dict:
    settings = settings or get_settings()

    profile = get_profile("labor", settings.provider_profile_dir)
    if profile.is_configured(settings.provider_env()):
        times = _from_api(operation, vehicle, settings)
        if times:
            return {
                "operation": operation,
                "vehicle": vehicle.label(),
                "labor_times": [t.model_dump(exclude_none=True) for t in times],
                "source": profile.vendor,
                "provider_verified": profile.verified,
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
        for candidate in extract_labor_times(hit.chunk.text, operation, hit.chunk.section)[:3]:
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

"""Torque specifications.

Two sources, in order:

1. A commercial specification API, when the shop has configured one.
2. Indexed OEM documentation, from which values are *extracted verbatim* — every
   returned figure carries the sentence it came from and a page citation.

There is no third path. If neither source yields a value, the caller gets an explicit
gap. A fabricated torque value is a failed fastener.
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from aad.config import Settings, get_settings
from aad.errors import NoGroundingError, ProviderError
from aad.models import TorqueSpec, Vehicle
from aad.providers.client import call_provider
from aad.providers.profiles import get_profile
from aad.rag.retriever import Retriever

# Value + unit, e.g. "22 lb-ft", "30 N·m", "106 in-lbs", "18-22 ft-lb".
_VALUE_RE = re.compile(
    r"(?P<low>\d{1,4}(?:\.\d+)?)\s*(?:(?:-|–|to)\s*(?P<high>\d{1,4}(?:\.\d+)?))?\s*"
    r"(?P<unit>n[·.\s]?m|newton[- ]met(?:er|re)s?|lb[-\s]?ft|ft[-\s]?lbs?|foot[- ]pounds?|"
    r"lb[-\s]?in|in[-\s]?lbs?|inch[- ]pounds?)\b",
    re.IGNORECASE,
)
_BOLT_RE = re.compile(r"\bM(\d{1,2})\s*[x×]\s*(\d{1,2}(?:\.\d+)?)\b|\b(\d+/\d+)\s*-\s*(\d+)\b")
_SEQUENCE_RE = re.compile(
    r"\b(in sequence|sequence shown|criss-?cross|star pattern|stage \d|step \d|"
    r"\+\s*\d{1,3}\s*(?:°|degrees?)|torque[- ]to[- ]yield)\b",
    re.IGNORECASE,
)


def _normalize_unit(raw: str) -> str:
    lowered = re.sub(r"[\s·.]", "", raw.lower())
    if lowered.startswith("nm") or "newton" in lowered:
        return "Nm"
    if "in" in lowered and ("lb" in lowered or "pound" in lowered):
        return "lb-in"
    return "lb-ft"


def _bolt_size(text: str) -> str | None:
    match = _BOLT_RE.search(text)
    if not match:
        return None
    if match.group(1):
        return f"M{match.group(1)}x{match.group(2)}"
    return f"{match.group(3)}-{match.group(4)}"


def _sentences(text: str) -> list[str]:
    # Deliberately not splitting on ':' — service manuals label values with it
    # ("Stage 1: 40 Nm", "Bank 1: 0.7 hrs"), and splitting there strands the number
    # in a fragment with nothing identifying it.
    return [s.strip() for s in re.split(r"(?<=[.;])\s+|\n+", text) if s.strip()]


# Words that carry no identifying information about *which* fastener is meant.
# A sentence matching only these is not evidence that it describes the requested
# component — every torque spec in the manual contains them.
_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "from", "into", "out", "all", "any", "its", "each", "per"}
)
_GENERIC_FASTENER = frozenset(
    {
        "bolt", "bolts", "nut", "nuts", "screw", "screws", "stud", "studs",
        "fastener", "fasteners", "torque", "spec", "specs", "specification",
        "specifications", "tighten", "tightening", "value", "values",
    }
)


def _words(text: str) -> set[str]:
    """Whole words of 3+ characters, lowercased, with trailing plurals folded.

    Queries say "cylinder head bolts"; manuals say "bolt". Folding the plural avoids
    a miss on a spec that is plainly present — a miss is safe but useless, and there
    is no safety cost to matching singular against plural.
    """
    words = set(re.findall(r"[a-z]{3,}", text.lower()))
    return {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words}


def _terms(component: str) -> tuple[set[str], set[str]]:
    """Split a component name into (distinctive, generic) term sets."""
    words = _words(component) - _STOPWORDS
    return words - _GENERIC_FASTENER, words & _GENERIC_FASTENER


def extract_torque_specs(text: str, component: str, section: str | None = None) -> list[dict]:
    """Pull candidate torque values out of retrieved text, keeping the exact sentence.

    A value is only offered as *this component's* spec when the sentence — or the
    section heading it sits under — actually names the component. Retrieval returns
    the closest chunks for the vehicle whether or not the component appears in them,
    so without this gate a component absent from the corpus picks up the nearest
    unrelated torque value and presents it with a citation.

    The gate trades misses for wrong answers deliberately. A miss is reported as an
    explicit gap; a wrong answer is a failed fastener.
    """
    distinctive, generic = _terms(component)
    # Whole words only. Substring matching silently equates "shaft" with "camshaft",
    # which is how a transfer-case query picks up a camshaft sensor's torque value.
    heading_words = _words(section or "")

    found: list[dict] = []
    for sentence in _sentences(text):
        sentence_words = _words(sentence)
        # The section heading counts as context: manuals routinely put the component
        # in a heading and the value in a bare table row beneath it.
        matched_distinctive = distinctive & (sentence_words | heading_words)
        matched_generic = generic & sentence_words

        if distinctive and not matched_distinctive:
            continue
        if not distinctive and not matched_generic:
            continue

        for match in _VALUE_RE.finditer(sentence):
            low = float(match.group("low"))
            high = match.group("high")
            found.append(
                {
                    "value": low,
                    "value_high": float(high) if high else None,
                    "unit": _normalize_unit(match.group("unit")),
                    "bolt_size": _bolt_size(sentence),
                    "sequence": (m.group(0) if (m := _SEQUENCE_RE.search(sentence)) else None),
                    "snippet": sentence,
                    "relevance": len(matched_distinctive) * 2 + len(matched_generic),
                }
            )
    found.sort(key=lambda item: (-item["relevance"], item["snippet"]))
    return found


def _from_api(component: str, vehicle: Vehicle, settings: Settings) -> list[TorqueSpec]:
    profile = get_profile("torque", settings.provider_profile_dir)
    rows = call_provider(
        profile, {**vehicle.model_dump(), "component": component}, env=settings.provider_env()
    )

    specs: list[TorqueSpec] = []
    for row in rows:
        try:
            specs.append(TorqueSpec.model_validate(row))
        except ValidationError as exc:
            # A mapping mismatch must not degrade into a partial spec. Say which
            # profile is wrong rather than handing back a torque value missing
            # its unit.
            raise ProviderError(
                f"{profile.vendor} returned a row this profile cannot map to a torque "
                f"spec ({exc.error_count()} field error(s)). Compare the profile's "
                f"field_map against the vendor's response format: {exc}"
            ) from exc
    return specs


def lookup_torque_spec(
    component: str,
    vehicle: Vehicle,
    retriever: Retriever,
    settings: Settings | None = None,
) -> dict:
    settings = settings or get_settings()

    profile = get_profile("torque", settings.provider_profile_dir)
    if profile.is_configured(settings.provider_env()):
        specs = _from_api(component, vehicle, settings)
        if specs:
            return {
                "component": component,
                "vehicle": vehicle.label(),
                "specs": [spec.model_dump(exclude_none=True) for spec in specs],
                "source": profile.vendor,
                "provider_verified": profile.verified,
            }

    query = f"{component} torque specification tightening bolt size {vehicle.label()}"
    try:
        hits = retriever.search(query, vehicle, spec_type="torque_spec")
    except NoGroundingError as exc:
        return {
            "component": component,
            "vehicle": vehicle.label(),
            "specs": [],
            "unavailable_reason": str(exc),
            "instruction": "Do not estimate a torque value. Tell the technician the spec is not "
            "available and to consult the OEM service information.",
        }

    specs: list[dict] = []
    for hit in hits:
        citation = hit.citation().model_dump()
        for candidate in extract_torque_specs(hit.chunk.text, component, hit.chunk.section)[:3]:
            specs.append(
                {
                    "component": component,
                    "value": candidate["value"],
                    "value_high": candidate["value_high"],
                    "unit": candidate["unit"],
                    "bolt_size": candidate["bolt_size"],
                    "sequence": candidate["sequence"],
                    "verbatim_source_text": candidate["snippet"],
                    "citation": citation,
                }
            )

    if not specs:
        return {
            "component": component,
            "vehicle": vehicle.label(),
            "specs": [],
            "supporting_text": [
                {"text": hit.chunk.text[:800], "citation": hit.citation().model_dump()}
                for hit in hits[:3]
            ],
            "unavailable_reason": (
                f"documentation for {vehicle.label()} was found but contains no torque value for "
                f"{component!r}"
            ),
            "instruction": "Do not estimate a torque value. Report that the specification was not "
            "found and cite what was reviewed.",
        }

    return {
        "component": component,
        "vehicle": vehicle.label(),
        "specs": specs[:8],
        "source": "indexed OEM documentation",
        "instruction": "Report values exactly as extracted, with the citation. If candidates "
        "disagree, surface all of them rather than picking one.",
    }

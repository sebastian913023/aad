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

import httpx

from aad.config import Settings, get_settings
from aad.errors import NoGroundingError, ProviderError
from aad.models import TorqueSpec, Vehicle
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
    return [s.strip() for s in re.split(r"(?<=[.;:])\s+|\n+", text) if s.strip()]


def extract_torque_specs(text: str, component: str) -> list[dict]:
    """Pull candidate torque values out of retrieved text, keeping the exact sentence.

    Sentences mentioning the requested component rank first; the raw snippet always
    accompanies the number so a tech can verify it against the source page.
    """
    terms = [t for t in re.findall(r"[a-z]{3,}", component.lower())]
    found: list[dict] = []
    for sentence in _sentences(text):
        for match in _VALUE_RE.finditer(sentence):
            low = float(match.group("low"))
            high = match.group("high")
            unit = _normalize_unit(match.group("unit"))
            relevance = sum(1 for term in terms if term in sentence.lower())
            found.append(
                {
                    "value": low,
                    "value_high": float(high) if high else None,
                    "unit": unit,
                    "bolt_size": _bolt_size(sentence),
                    "sequence": (m.group(0) if (m := _SEQUENCE_RE.search(sentence)) else None),
                    "snippet": sentence,
                    "relevance": relevance,
                }
            )
    found.sort(key=lambda item: (-item["relevance"], item["snippet"]))
    return found


def _from_api(component: str, vehicle: Vehicle, settings: Settings) -> list[TorqueSpec]:
    response = httpx.get(
        f"{settings.torque_api_base.rstrip('/')}/torque-specs",
        headers={"Authorization": f"Bearer {settings.torque_api_key}"},
        params={
            "year": vehicle.year,
            "make": vehicle.make,
            "model": vehicle.model,
            "engine": vehicle.engine,
            "vin": vehicle.vin,
            "component": component,
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise ProviderError(f"torque provider returned {response.status_code}: {response.text[:200]}")
    return [TorqueSpec.model_validate(item) for item in response.json().get("specs", [])]


def lookup_torque_spec(
    component: str,
    vehicle: Vehicle,
    retriever: Retriever,
    settings: Settings | None = None,
) -> dict:
    settings = settings or get_settings()

    if settings.torque_api_base and settings.torque_api_key:
        specs = _from_api(component, vehicle, settings)
        if specs:
            return {
                "component": component,
                "vehicle": vehicle.label(),
                "specs": [spec.model_dump(exclude_none=True) for spec in specs],
                "source": "subscription torque specification API",
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
        for candidate in extract_torque_specs(hit.chunk.text, component)[:3]:
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

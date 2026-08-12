"""Claim extraction.

A monitor that scores whole responses cannot enforce zero tolerance: one fabricated
torque value inside four correct paragraphs still averages out to a good-looking
score. So an output is decomposed into atomic *claims* — the individual assertions a
technician could act on — and each is verified independently.

Only verifiable claims are extracted. "The camshaft sensor reads engine speed" is
reasoning and is not checked here; "torque to 9 Nm" is a published value and is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

ClaimType = Literal["torque", "labor_hours", "part_number", "wire_color", "pin", "measurement"]

# Numbers carrying a unit are what a technician acts on, and what a fabrication
# most often looks like.
_TORQUE_RE = re.compile(
    r"(?P<value>\d{1,4}(?:\.\d+)?)\s*(?:(?:-|–|to)\s*(?P<high>\d{1,4}(?:\.\d+)?))?\s*"
    r"(?P<unit>n[·.\s]?m\b|newton[- ]met(?:er|re)s?|lb[-\s]?ft\b|ft[-\s]?lbs?\b|"
    r"lb[-\s]?in\b|in[-\s]?lbs?\b)",
    re.IGNORECASE,
)
_HOURS_RE = re.compile(r"(?P<value>\d{1,2}(?:\.\d{1,2})?)\s*(?:hrs?\b|hours?\b)", re.IGNORECASE)
_PART_RE = re.compile(r"\b(?:part\s*(?:no\.?|number|#)\s*)?([A-Z0-9]{3,}-[A-Z0-9-]{2,})\b")
_COLOURS = (
    "white|black|red|blue|green|yellow|brown|orange|violet|purple|grey|gray|pink|tan"
)
# Three shapes a wiring answer takes: "wire colour is green", "the wire is green",
# "the green wire". All three are actionable at the connector, so all three are claims.
_WIRE_RE = re.compile(
    rf"\bwire\s+colou?r\s+(?:is\s+)?(?P<color>(?:{_COLOURS})(?:/\w+)?)\b|"
    rf"\bwire\s+is\s+(?P<color3>(?:{_COLOURS})(?:/\w+)?)\b|"
    rf"\b(?P<color2>{_COLOURS})(?:/\w+)?\s+wire\b",
    re.IGNORECASE,
)
_PIN_RE = re.compile(r"\bpin\s+(?P<pin>\d{1,3})\b", re.IGNORECASE)
_BOLT_RE = re.compile(r"\bM(\d{1,2})\s*[x×]\s*(\d{1,2}(?:\.\d+)?)\b")


@dataclass(slots=True)
class Claim:
    """One verifiable assertion lifted out of an output."""

    text: str
    claim_type: ClaimType
    # The literal token that must appear in a cited source. This is what makes the
    # check zero-tolerance rather than fuzzy.
    value: str
    unit: str | None = None
    numeric: float | None = None
    cited_ids: list[str] = field(default_factory=list)

    def key(self) -> str:
        return f"{self.claim_type}:{self.value}{self.unit or ''}"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;!?])\s+|\n+", text) if s.strip()]


def extract_claims(output: str) -> list[Claim]:
    """Pull every verifiable claim out of a model response."""
    claims: list[Claim] = []
    seen: set[tuple[str, str]] = set()

    def add(claim: Claim) -> None:
        marker = (claim.claim_type, claim.value + (claim.unit or ""))
        if marker not in seen:
            seen.add(marker)
            claims.append(claim)

    for sentence in _sentences(output):
        for match in _TORQUE_RE.finditer(sentence):
            add(
                Claim(
                    text=sentence,
                    claim_type="torque",
                    value=match.group("value"),
                    unit=re.sub(r"[\s·.]", "", match.group("unit").lower()),
                    numeric=float(match.group("value")),
                )
            )
            if match.group("high"):
                add(
                    Claim(
                        text=sentence,
                        claim_type="torque",
                        value=match.group("high"),
                        unit=re.sub(r"[\s·.]", "", match.group("unit").lower()),
                        numeric=float(match.group("high")),
                    )
                )

        for match in _HOURS_RE.finditer(sentence):
            value = float(match.group("value"))
            if 0 < value <= 60:
                add(
                    Claim(
                        text=sentence,
                        claim_type="labor_hours",
                        value=match.group("value"),
                        unit="hrs",
                        numeric=value,
                    )
                )

        for match in _BOLT_RE.finditer(sentence):
            add(
                Claim(
                    text=sentence,
                    claim_type="measurement",
                    value=f"M{match.group(1)}x{match.group(2)}",
                )
            )

        for match in _PART_RE.finditer(sentence):
            add(Claim(text=sentence, claim_type="part_number", value=match.group(1)))

        for match in _PIN_RE.finditer(sentence):
            add(Claim(text=sentence, claim_type="pin", value=match.group("pin")))

        for match in _WIRE_RE.finditer(sentence):
            colour = match.group("color") or match.group("color2") or match.group("color3")
            if colour:
                add(Claim(text=sentence, claim_type="wire_color", value=colour.lower()))

    return claims

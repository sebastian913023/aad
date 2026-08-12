"""LSC metrics: Lexical grounding, Semantic consistency, Citation validity.

Three independent signals per claim. They are reported separately and *never*
averaged into a single pass/fail, because they fail in different ways and only one
of them is safe to be lenient about:

  L  lexical_grounding    the claim's literal value appears in a cited source
  S  semantic_consistency the claim and its source say the same thing
  C  citation_validity    the citation points at a source that was actually supplied

The composite is reported for trend-watching only. **The gate is L.** A torque value
that does not appear verbatim in a cited source is a fabrication no matter how
semantically plausible the sentence around it is — averaging would let a confident
paraphrase launder a wrong number, which is the precise failure this exists to catch.

Note on naming: "LSC" is this system's own composite, defined here. It is not a
published standard metric — if you meant a specific external definition, say which and
I will implement that instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aad.ingest.embeddings import Embedder, LocalHashEmbedder

# Unit spellings that mean the same thing, so a source saying "N·m" grounds a claim
# written "Nm". Normalisation only — no value conversion happens here, because a
# converted match would defeat the point of a verbatim check.
_UNIT_ALIASES = {
    "nm": {"nm", "n·m", "n.m", "newtonmeter", "newtonmeters", "newtonmetre", "newtonmetres"},
    "lb-ft": {"lb-ft", "lbft", "ft-lb", "ftlb", "ft-lbs", "ftlbs", "footpound", "footpounds"},
    "lb-in": {"lb-in", "lbin", "in-lb", "inlb", "in-lbs", "inlbs", "inchpound", "inchpounds"},
    "hrs": {"hr", "hrs", "hour", "hours"},
}


@dataclass(slots=True)
class LSCScore:
    lexical_grounding: float
    semantic_consistency: float
    citation_validity: float
    # Reported for trends. Never used as the gate — see the module docstring.
    composite: float
    detail: str = ""

    def to_dict(self) -> dict[str, float | str]:
        return {
            "lexical_grounding": round(self.lexical_grounding, 4),
            "semantic_consistency": round(self.semantic_consistency, 4),
            "citation_validity": round(self.citation_validity, 4),
            "composite": round(self.composite, 4),
            "detail": self.detail,
        }


def _normalise(text: str) -> str:
    return re.sub(r"[\s·]+", "", text.lower())


def _unit_variants(unit: str | None) -> set[str]:
    if not unit:
        return set()
    key = _normalise(unit).replace(".", "")
    for canonical, aliases in _UNIT_ALIASES.items():
        if key == _normalise(canonical) or key in {_normalise(a) for a in aliases}:
            return {_normalise(a) for a in aliases} | {_normalise(canonical)}
    return {key}


def value_appears_in(value: str, unit: str | None, source_text: str) -> bool:
    """Does this literal value (with a compatible unit) appear in the source?

    Numeric values are matched on word boundaries so "9" does not match inside "19",
    and a unit — when the claim carries one — must appear near the number. Proximity
    matters: a source containing "9" somewhere and "Nm" elsewhere does not ground a
    claim of "9 Nm".
    """
    if not source_text:
        return False

    haystack = source_text.lower()
    try:
        numeric = float(value)
    except ValueError:
        # Non-numeric claims (part numbers, wire colours, bolt sizes) match literally.
        return _normalise(value) in _normalise(source_text)

    # Match the number as written and without a trailing ".0".
    forms = {value.lower(), (f"{numeric:g}").lower()}
    for form in forms:
        for match in re.finditer(rf"(?<![\d.]){re.escape(form)}(?![\d])", haystack):
            if not unit:
                return True
            window = _normalise(haystack[match.end() : match.end() + 24])
            if any(window.startswith(v) for v in _unit_variants(unit)):
                return True
    return False


def lexical_grounding(claim_value: str, unit: str | None, sources: list[str]) -> float:
    """1.0 when the claim's literal value is present in any cited source, else 0.0.

    Deliberately binary. A partially-present torque value is not partially correct.
    """
    return 1.0 if any(value_appears_in(claim_value, unit, s) for s in sources) else 0.0


def _passages(source: str, limit: int = 60) -> list[str]:
    """Split a source into sentence-sized passages, plus the whole text as a fallback."""
    parts = [p.strip() for p in re.split(r"(?<=[.;!?])\s+|\n{2,}", source) if p.strip()]
    return [source, *parts[:limit]] if parts else [source]


def semantic_consistency(
    claim_text: str, sources: list[str], embedder: Embedder | None = None
) -> float:
    """Cosine similarity between the claim and the best-matching *passage* of a source.

    Passage-level, not document-level, and for a specific reason: a one-line claim
    scored against an 800-word chunk scores low no matter how exactly the chunk
    supports it, because the rest of the chunk is about something else. Measuring
    against the whole document would make correct short answers look inconsistent and
    flood the review queue — and a queue nobody can keep up with is a queue nobody
    reads.
    """
    if not sources or not claim_text.strip():
        return 0.0
    embedder = embedder or LocalHashEmbedder(dim=512)
    candidates = [p for source in sources for p in _passages(source)]
    vectors = embedder.embed([claim_text, *candidates])
    claim_vec, candidate_vecs = vectors[0], vectors[1:]
    return max(0.0, float(max(candidate_vecs @ claim_vec)))


def citation_validity(cited_ids: list[str], available_ids: set[str]) -> float:
    """Fraction of citations that point at a source actually supplied.

    A citation naming a document that was never in the context is itself a
    fabrication — a plausible-looking provenance for an unsourced claim.
    """
    if not cited_ids:
        return 0.0
    return sum(1 for c in cited_ids if c in available_ids) / len(cited_ids)


def score_claim(
    claim_text: str,
    claim_value: str,
    unit: str | None,
    cited_ids: list[str],
    sources: dict[str, str],
    embedder: Embedder | None = None,
) -> LSCScore:
    """Score one claim against the sources it cites.

    Only cited sources count toward lexical grounding. A value that happens to appear
    in some *other* document the model never cited is not grounded — provenance is
    part of the claim.
    """
    cited_texts = [sources[c] for c in cited_ids if c in sources]

    lexical = lexical_grounding(claim_value, unit, cited_texts)
    semantic = semantic_consistency(claim_text, cited_texts, embedder)
    citation = citation_validity(cited_ids, set(sources))

    composite = 0.5 * lexical + 0.3 * semantic + 0.2 * citation

    if not cited_ids:
        detail = "claim carries no citation"
    elif not cited_texts:
        detail = f"cited {cited_ids} but none were supplied as sources"
    elif lexical == 0.0:
        detail = f"value {claim_value!r} does not appear in any cited source"
    else:
        detail = "value found in a cited source"

    return LSCScore(
        lexical_grounding=lexical,
        semantic_consistency=semantic,
        citation_validity=citation,
        composite=composite,
        detail=detail,
    )

"""Accuracy evaluation against a gold set.

The headline number for this system is not accuracy. It is the **hallucination rate**:
how often a specification is returned that does not match the source. A system that
abstains on half the questions and is never wrong is usable in a bay; a system that
answers everything and is wrong 3% of the time is not, because the technician cannot
tell which 3%.

So a case has four outcomes, not two:

  correct    answered, and the value matches the gold value
  wrong      answered, and it does not match          <- the number that matters
  abstained  correctly reported the data as unavailable
  missed     abstained when the answer was in the corpus (safe, unhelpful)

`must_abstain` cases invert this: the gold answer is "not in the corpus", and any
returned specification is a hallucination.

An evaluation run over synthetic material is refused outright. An accuracy figure
computed against invented specs would read as evidence while meaning nothing.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from aad.config import Settings, get_settings
from aad.errors import AadError, NoGroundingError
from aad.models import Vehicle
from aad.providers.labor import lookup_labor_time
from aad.providers.torque import lookup_torque_spec
from aad.rag.retriever import Retriever, get_retriever

CaseKind = Literal["torque_spec", "labor_time", "retrieval"]
Outcome = Literal["correct", "wrong", "abstained", "missed", "error"]

# Unit conversion so a gold value in Nm still scores against a provider that
# answers in lb-ft. Conversion is applied for comparison only — nothing the
# technician sees is ever silently converted.
_TO_NM = {"Nm": 1.0, "lb-ft": 1.3558179483314004, "lb-in": 0.1129848290276167}


class GoldCase(BaseModel):
    """One question with a known answer, drawn from the licensed corpus."""

    id: str
    kind: CaseKind = "torque_spec"
    vehicle: Vehicle
    query: str
    # Expected specification. Omit entirely for a must_abstain case.
    expected_value: float | None = None
    expected_unit: Literal["Nm", "lb-ft", "lb-in"] | None = None
    expected_bolt_size: str | None = None
    expected_hours: float | None = None
    # The document the answer should come from, if you want to score retrieval.
    expected_source: str | None = None
    # True when the corpus genuinely does not contain this answer. These cases are
    # what prove the system abstains instead of inventing.
    must_abstain: bool = False
    # Fractional tolerance on numeric comparison (0.02 = 2%).
    tolerance: float = 0.02
    notes: str = ""


class CaseResult(BaseModel):
    id: str
    kind: CaseKind
    outcome: Outcome
    detail: str = ""
    expected: str | None = None
    actual: str | None = None
    cited: bool = False
    retrieval_hit: bool | None = None


@dataclass
class EvalReport:
    results: list[CaseResult] = field(default_factory=list)
    corpus_sources: dict[str, int] = field(default_factory=dict)
    synthetic_chunks: int = 0

    @property
    def total(self) -> int:
        return len(self.results)

    def count(self, outcome: Outcome) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    @property
    def answered(self) -> int:
        return self.count("correct") + self.count("wrong")

    @property
    def accuracy(self) -> float:
        """Correct behaviour over all cases.

        A correctly-abstained `must_abstain` case counts: saying "not in the corpus"
        when it genuinely is not there is the right answer, not a non-answer. The
        `abstained` outcome is only ever produced for those cases — an abstention on
        a question the corpus *could* answer is scored `missed`.
        """
        return (self.count("correct") + self.count("abstained")) / self.total if self.total else 0.0

    @property
    def hallucination_rate(self) -> float:
        """Fraction of all cases answered incorrectly. The gate metric."""
        return self.count("wrong") / self.total if self.total else 0.0

    @property
    def precision(self) -> float:
        """Of the questions it chose to answer, how many were right."""
        return self.count("correct") / self.answered if self.answered else 0.0

    @property
    def abstention_rate(self) -> float:
        return (self.count("abstained") + self.count("missed")) / self.total if self.total else 0.0

    @property
    def citation_coverage(self) -> float:
        answered = [r for r in self.results if r.outcome in ("correct", "wrong")]
        return sum(1 for r in answered if r.cited) / len(answered) if answered else 1.0

    @property
    def retrieval_recall(self) -> float | None:
        scored = [r for r in self.results if r.retrieval_hit is not None]
        return sum(1 for r in scored if r.retrieval_hit) / len(scored) if scored else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "outcomes": {
                o: self.count(o) for o in ("correct", "wrong", "abstained", "missed", "error")
            },
            "accuracy": round(self.accuracy, 4),
            "precision": round(self.precision, 4),
            "hallucination_rate": round(self.hallucination_rate, 4),
            "abstention_rate": round(self.abstention_rate, 4),
            "citation_coverage": round(self.citation_coverage, 4),
            "retrieval_recall": (
                round(self.retrieval_recall, 4) if self.retrieval_recall is not None else None
            ),
            "corpus_sources": self.corpus_sources,
            "synthetic_chunks": self.synthetic_chunks,
            "cases": [r.model_dump() for r in self.results],
        }


def load_goldset(path: Path | str) -> list[GoldCase]:
    """Load a gold set from JSONL (one case per line) or a JSON array."""
    path = Path(path)
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise AadError(f"gold set {path} is empty")

    if text.lstrip().startswith("["):
        raw = json.loads(text)
    else:
        raw = [json.loads(line) for line in text.splitlines() if line.strip()]

    cases = [GoldCase.model_validate(item) for item in raw]
    ids = [c.id for c in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise AadError(f"gold set {path} has duplicate case ids: {', '.join(sorted(duplicates))}")
    return cases


def _to_nm(value: float, unit: str) -> float:
    return value * _TO_NM.get(unit, 1.0)


def _values_match(
    got_value: float, got_unit: str, want_value: float, want_unit: str, tolerance: float
) -> bool:
    want_nm = _to_nm(want_value, want_unit)
    got_nm = _to_nm(got_value, got_unit)
    if want_nm == 0:
        return math.isclose(got_nm, 0.0, abs_tol=1e-9)
    return abs(got_nm - want_nm) / abs(want_nm) <= tolerance


def _score_torque(case: GoldCase, payload: dict) -> CaseResult:
    specs = payload.get("specs") or []
    cited = bool(specs) and all(s.get("citation") or s.get("citations") for s in specs)
    retrieval_hit = None
    if case.expected_source:
        sources = {(s.get("citation") or {}).get("source") for s in specs}
        sources |= {c.get("source") for s in specs for c in (s.get("citations") or [])}
        retrieval_hit = case.expected_source in sources

    if case.must_abstain:
        if specs:
            return CaseResult(
                id=case.id,
                kind=case.kind,
                outcome="wrong",
                detail="returned a specification for a question the corpus cannot answer",
                actual=f"{specs[0].get('value')} {specs[0].get('unit')}",
                cited=cited,
                retrieval_hit=retrieval_hit,
            )
        return CaseResult(
            id=case.id, kind=case.kind, outcome="abstained", detail="correctly reported unavailable"
        )

    if not specs:
        return CaseResult(
            id=case.id,
            kind=case.kind,
            outcome="missed",
            detail=payload.get("unavailable_reason", "no specification returned"),
            expected=f"{case.expected_value} {case.expected_unit}",
            retrieval_hit=retrieval_hit,
        )

    for spec in specs:
        value, unit = spec.get("value"), spec.get("unit")
        if value is None or unit is None:
            continue
        if _values_match(
            float(value), unit, case.expected_value, case.expected_unit, case.tolerance
        ):
            return CaseResult(
                id=case.id,
                kind=case.kind,
                outcome="correct",
                expected=f"{case.expected_value} {case.expected_unit}",
                actual=f"{value} {unit}",
                cited=cited,
                retrieval_hit=retrieval_hit,
            )

    first = specs[0]
    return CaseResult(
        id=case.id,
        kind=case.kind,
        outcome="wrong",
        detail=f"none of {len(specs)} returned value(s) match the gold value",
        expected=f"{case.expected_value} {case.expected_unit}",
        actual=f"{first.get('value')} {first.get('unit')}",
        cited=cited,
        retrieval_hit=retrieval_hit,
    )


def _score_labor(case: GoldCase, payload: dict) -> CaseResult:
    entries = payload.get("labor_times") or []
    cited = bool(entries) and all(e.get("citation") or e.get("citations") for e in entries)

    if case.must_abstain:
        if entries:
            return CaseResult(
                id=case.id,
                kind=case.kind,
                outcome="wrong",
                detail="returned labor hours for a question the corpus cannot answer",
                actual=str(entries[0].get("hours")),
                cited=cited,
            )
        return CaseResult(id=case.id, kind=case.kind, outcome="abstained")

    if not entries:
        return CaseResult(
            id=case.id,
            kind=case.kind,
            outcome="missed",
            detail=payload.get("unavailable_reason", "no labor time returned"),
            expected=str(case.expected_hours),
        )

    for entry in entries:
        hours = entry.get("hours")
        if hours is None:
            continue
        if abs(float(hours) - case.expected_hours) <= case.tolerance * max(case.expected_hours, 1e-9):
            return CaseResult(
                id=case.id,
                kind=case.kind,
                outcome="correct",
                expected=str(case.expected_hours),
                actual=str(hours),
                cited=cited,
            )

    return CaseResult(
        id=case.id,
        kind=case.kind,
        outcome="wrong",
        detail=f"none of {len(entries)} returned time(s) match",
        expected=str(case.expected_hours),
        actual=str(entries[0].get("hours")),
        cited=cited,
    )


def _score_retrieval(case: GoldCase, retriever: Retriever) -> CaseResult:
    try:
        hits = retriever.search(case.query, case.vehicle)
    except NoGroundingError:
        hits = []

    if case.must_abstain:
        outcome: Outcome = "wrong" if hits else "abstained"
        return CaseResult(
            id=case.id,
            kind=case.kind,
            outcome=outcome,
            detail="retrieved material for a question the corpus should not answer"
            if hits
            else "correctly retrieved nothing",
        )

    if not hits:
        return CaseResult(
            id=case.id, kind=case.kind, outcome="missed", detail="retrieval returned nothing"
        )

    sources = [h.chunk.source for h in hits]
    hit = case.expected_source in sources if case.expected_source else True
    return CaseResult(
        id=case.id,
        kind=case.kind,
        outcome="correct" if hit else "wrong",
        expected=case.expected_source,
        actual=", ".join(dict.fromkeys(sources))[:120],
        cited=True,
        retrieval_hit=hit,
    )


def run_evaluation(
    cases: list[GoldCase],
    *,
    retriever: Retriever | None = None,
    settings: Settings | None = None,
) -> EvalReport:
    settings = settings or get_settings()
    retriever = retriever or get_retriever(settings)
    report = EvalReport()

    for case in cases:
        try:
            if case.kind == "torque_spec":
                payload = lookup_torque_spec(case.query, case.vehicle, retriever, settings)
                report.results.append(_score_torque(case, payload))
            elif case.kind == "labor_time":
                payload = lookup_labor_time(case.query, case.vehicle, retriever, settings)
                report.results.append(_score_labor(case, payload))
            else:
                report.results.append(_score_retrieval(case, retriever))
        except AadError as exc:
            report.results.append(
                CaseResult(id=case.id, kind=case.kind, outcome="error", detail=str(exc))
            )

    return report

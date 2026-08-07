"""The monitoring pipeline.

Order matters and is deliberate:

  1. Extract atomic claims from the output.
  2. Score each against the sources it cites (deterministic; always runs).
  3. If no sources reached the monitor at all, abstain — an unverifiable claim is not
     a proven fabrication, and our own plumbing failures must not land in the
     hallucination rate.
  4. Zero-tolerance gate: any claim whose literal value is absent from a cited source
     is a fabrication. Blocked. No score can overturn this.
  5. Judge model (optional): can only tighten the outcome — raise an abstention, never
     clear a fabrication.
  6. Route: grounded / abstained / escalated / blocked.

The asymmetry in step 5 is the whole design. A soft judge that could clear a hard
lexical failure would turn a deterministic guarantee back into a probabilistic one.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from aad.config import Settings, get_settings
from aad.ingest.embeddings import Embedder, get_embedder
from aad.monitor.claims import Claim, extract_claims
from aad.monitor.judge import JudgeVerdict, judge_output
from aad.monitor.lsc import LSCScore, score_claim

Verdict = Literal["grounded", "abstained", "escalated", "blocked", "no_claims"]
AbstentionReason = Literal[
    "insufficient_context", "rubric_conflict", "low_semantic_consistency", "fabrication"
]

# An output that both asserts a value and declares it unavailable is internally
# inconsistent; a technician cannot tell which half to believe.
_UNAVAILABLE_RE = re.compile(
    r"\b(not available|unavailable|could not find|no (?:published |specification|spec|data)"
    r"|not (?:in|found in) the (?:manual|documentation)|consult the OEM)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ClaimReport:
    claim: Claim
    score: LSCScore
    fabricated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.claim.claim_type,
            "value": self.claim.value,
            "unit": self.claim.unit,
            "text": self.claim.text[:300],
            "cited": self.claim.cited_ids,
            "fabricated": self.fabricated,
            **self.score.to_dict(),
        }


@dataclass
class MonitorResult:
    verdict: Verdict
    task_type: str
    faithfulness: float
    citation_accuracy: float
    mean_semantic: float
    claims: list[ClaimReport] = field(default_factory=list)
    abstention_reason: AbstentionReason | None = None
    needs_human_review: bool = False
    judge: JudgeVerdict | None = None
    notes: list[str] = field(default_factory=list)
    latency_ms: float = 0.0

    @property
    def fabrications(self) -> list[ClaimReport]:
        return [c for c in self.claims if c.fabricated]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "task_type": self.task_type,
            "faithfulness": round(self.faithfulness, 4),
            "citation_accuracy": round(self.citation_accuracy, 4),
            "mean_semantic": round(self.mean_semantic, 4),
            "claim_count": len(self.claims),
            "fabrication_count": len(self.fabrications),
            "abstention_reason": self.abstention_reason,
            "needs_human_review": self.needs_human_review,
            "claims": [c.to_dict() for c in self.claims],
            "judge": self.judge.to_dict() if self.judge else None,
            "notes": self.notes,
            "latency_ms": round(self.latency_ms, 2),
        }


def monitor_output(
    *,
    question: str,
    output: str,
    sources: dict[str, str],
    cited_ids: list[str] | None = None,
    task_type: str = "general",
    use_judge: bool = False,
    judge_verdict: JudgeVerdict | None = None,
    settings: Settings | None = None,
    embedder: Embedder | None = None,
) -> MonitorResult:
    """Verify one model output against the sources it cited."""
    settings = settings or get_settings()
    started = time.perf_counter()
    embedder = embedder or get_embedder(settings)

    # Absent explicit per-claim citations, every claim inherits the output's citation
    # set. That is the generous reading — it can only make grounding easier to prove,
    # so a fabrication found under it is unambiguous.
    citations = list(cited_ids if cited_ids is not None else sources.keys())

    claims = extract_claims(output)
    for claim in claims:
        claim.cited_ids = citations

    reports: list[ClaimReport] = []
    for claim in claims:
        score = score_claim(
            claim.text, claim.value, claim.unit, claim.cited_ids, sources, embedder
        )
        reports.append(
            ClaimReport(claim=claim, score=score, fabricated=score.lexical_grounding == 0.0)
        )

    notes: list[str] = []

    if claims and (not sources or not citations):
        # Nothing was supplied to check against, so nothing can be *proved* fabricated.
        # Charging this to the model would put our own plumbing failures into the
        # hallucination rate, which is exactly the number that has to stay honest.
        # It is still unverified, so it still goes to a human.
        result = MonitorResult(
            verdict="abstained",
            task_type=task_type,
            faithfulness=0.0,
            citation_accuracy=0.0,
            mean_semantic=0.0,
            claims=[ClaimReport(claim=r.claim, score=r.score, fabricated=False) for r in reports],
            abstention_reason="insufficient_context",
            needs_human_review=True,
            notes=["claims were made with no source context supplied to the monitor"],
        )
        result.latency_ms = (time.perf_counter() - started) * 1000
        return result

    if not claims:
        result = MonitorResult(
            verdict="no_claims",
            task_type=task_type,
            faithfulness=1.0,
            citation_accuracy=1.0,
            mean_semantic=1.0,
            notes=["output contains no verifiable specification claims"],
        )
        result.latency_ms = (time.perf_counter() - started) * 1000
        return result

    faithfulness = sum(1 for r in reports if not r.fabricated) / len(reports)
    citation_accuracy = sum(r.score.citation_validity for r in reports) / len(reports)
    mean_semantic = sum(r.score.semantic_consistency for r in reports) / len(reports)

    judge = judge_verdict
    if judge is None and use_judge:
        judge = judge_output(question, output, sources, settings=settings)

    result = MonitorResult(
        verdict="grounded",
        task_type=task_type,
        faithfulness=faithfulness,
        citation_accuracy=citation_accuracy,
        mean_semantic=mean_semantic,
        claims=reports,
        judge=judge,
        notes=notes,
    )

    # --- Step 4: zero tolerance. Nothing below can overturn this. ---------
    if result.fabrications:
        result.verdict = "blocked"
        result.abstention_reason = "fabrication"
        result.needs_human_review = True
        notes.append(
            f"{len(result.fabrications)} claim(s) not present in any cited source: "
            + ", ".join(f"{c.claim.value}{c.claim.unit or ''}" for c in result.fabrications)
        )
        result.latency_ms = (time.perf_counter() - started) * 1000
        return result

    # --- Step 5: abstention triggers (judge may only tighten) -------------
    asserts_value = bool(claims)
    declares_unavailable = bool(_UNAVAILABLE_RE.search(output))

    if asserts_value and declares_unavailable:
        result.verdict = "abstained"
        result.abstention_reason = "rubric_conflict"
        notes.append("output both states a specification and declares it unavailable")
    elif judge is not None and judge.rubric_conflict:
        result.verdict = "abstained"
        result.abstention_reason = "rubric_conflict"
        notes.append(f"judge flagged a rubric conflict: {judge.reasoning[:200]}")
    elif judge is not None and judge.insufficient_context:
        result.verdict = "abstained"
        result.abstention_reason = "insufficient_context"
        notes.append(f"judge flagged insufficient context: {judge.reasoning[:200]}")
    elif mean_semantic < settings.monitor_min_semantic:
        result.verdict = "abstained"
        result.abstention_reason = "low_semantic_consistency"
        notes.append(
            f"mean semantic consistency {mean_semantic:.2f} is below "
            f"{settings.monitor_min_semantic:.2f}"
        )
    elif judge is not None and judge.semantic_consistency < settings.monitor_min_semantic:
        result.verdict = "abstained"
        result.abstention_reason = "low_semantic_consistency"
        notes.append(f"judge scored semantic consistency {judge.semantic_consistency:.2f}")
    elif judge is not None and not judge.supported:
        result.verdict = "escalated"
        notes.append(
            "judge could not confirm support for every claim: "
            + (", ".join(judge.unsupported_claims) or judge.reasoning[:200])
        )
        result.needs_human_review = True

    if citation_accuracy < 1.0 and result.verdict == "grounded":
        result.verdict = "escalated"
        result.needs_human_review = True
        notes.append(
            f"citation accuracy {citation_accuracy:.0%}: at least one citation names a "
            "source that was not supplied"
        )

    if result.abstention_reason is not None:
        result.needs_human_review = True

    result.latency_ms = (time.perf_counter() - started) * 1000
    return result

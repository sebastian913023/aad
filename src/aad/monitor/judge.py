"""Judge model.

A second Claude call that reads the output *and* the sources it cites, and returns a
structured verdict. It runs after the deterministic checks and can only ever make the
outcome stricter — it can raise an abstention, never clear a fabrication the lexical
check already found. A model's opinion does not override a value that provably is not
in the source.

The judge exists for the failures arithmetic cannot see: a value that is present in
the source but describes a different fastener, an output that contradicts its own
stated limitation, or a citation attached to a claim it does not support.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from aad.agent.client import ModelClient, build_client
from aad.config import Settings, get_settings

JUDGE_SYSTEM = """You audit an automotive diagnostic assistant's output against the \
source documents it cited. You are a verifier, not an author: you never supply a \
specification, and you never repair the output.

Judge only what is checkable against the supplied sources:

1. Is every specification in the output (torque value, bolt size, labor hours, wire \
colour, pin number, part number) actually stated in a cited source? A value that is \
present but attached to a different component is NOT supported.
2. Does the output contradict itself — for example asserting a value while also \
saying the specification is unavailable?
3. Do the cited sources conflict with each other on the value given?
4. Is there enough context in the sources to answer at all?

Diagnostic reasoning, procedure descriptions and test suggestions are not claims to \
verify — judge only published values and their provenance.

Be specific about which claim fails and why. When in doubt, escalate: a human \
reviewing a correct answer costs minutes, a technician acting on a wrong torque value \
costs an engine."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {
            "type": "boolean",
            "description": "True only if every specification is stated in a cited source.",
        },
        "insufficient_context": {
            "type": "boolean",
            "description": "The sources do not contain enough to answer the question.",
        },
        "rubric_conflict": {
            "type": "boolean",
            "description": "The output contradicts itself, or cited sources disagree.",
        },
        "semantic_consistency": {
            "type": "number",
            "description": "0.0-1.0: how well the output's meaning matches its sources.",
        },
        "unsupported_claims": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Each specification that is not stated in a cited source.",
        },
        "reasoning": {"type": "string"},
    },
    "required": [
        "supported",
        "insufficient_context",
        "rubric_conflict",
        "semantic_consistency",
        "unsupported_claims",
        "reasoning",
    ],
    "additionalProperties": False,
}


@dataclass(slots=True)
class JudgeVerdict:
    supported: bool
    insufficient_context: bool
    rubric_conflict: bool
    semantic_consistency: float
    unsupported_claims: list[str] = field(default_factory=list)
    reasoning: str = ""
    available: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "insufficient_context": self.insufficient_context,
            "rubric_conflict": self.rubric_conflict,
            "semantic_consistency": round(self.semantic_consistency, 4),
            "unsupported_claims": self.unsupported_claims,
            "reasoning": self.reasoning,
            "available": self.available,
            "error": self.error,
        }

    @classmethod
    def unavailable(cls, reason: str) -> JudgeVerdict:
        """When the judge cannot run, it must not read as approval.

        `supported=False` plus `insufficient_context=True` routes the output to human
        review, which is the correct default for an unverified answer.
        """
        return cls(
            supported=False,
            insufficient_context=True,
            rubric_conflict=False,
            semantic_consistency=0.0,
            reasoning=f"judge did not run: {reason}",
            available=False,
            error=reason,
        )


def _build_prompt(question: str, output: str, sources: dict[str, str]) -> str:
    rendered = "\n\n".join(
        f"[source: {sid}]\n{text[:4000]}" for sid, text in sources.items()
    ) or "(no sources were supplied)"
    return (
        f"QUESTION\n{question}\n\n"
        f"SOURCES THE OUTPUT CITED\n{rendered}\n\n"
        f"OUTPUT TO AUDIT\n{output}"
    )


def judge_output(
    question: str,
    output: str,
    sources: dict[str, str],
    *,
    client: ModelClient | None = None,
    settings: Settings | None = None,
) -> JudgeVerdict:
    settings = settings or get_settings()
    try:
        client = client or build_client(settings)
    except Exception as exc:  # noqa: BLE001 - missing key must not read as approval
        return JudgeVerdict.unavailable(f"{type(exc).__name__}: {exc}")

    try:
        response = client.create(
            max_tokens=4000,
            system=[{"type": "text", "text": JUDGE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"},
            output_config={
                "effort": settings.judge_effort,
                "format": {"type": "json_schema", "schema": JUDGE_SCHEMA},
            },
            messages=[{"role": "user", "content": _build_prompt(question, output, sources)}],
        )
    except Exception as exc:  # noqa: BLE001
        return JudgeVerdict.unavailable(f"{type(exc).__name__}: {exc}")

    if getattr(response, "stop_reason", None) == "refusal":
        return JudgeVerdict.unavailable("judge model declined the request")

    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    if not text:
        return JudgeVerdict.unavailable("judge returned no content")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return JudgeVerdict.unavailable(f"judge returned unparseable output: {exc}")

    return JudgeVerdict(
        supported=bool(data.get("supported", False)),
        insufficient_context=bool(data.get("insufficient_context", False)),
        rubric_conflict=bool(data.get("rubric_conflict", False)),
        semantic_consistency=float(data.get("semantic_consistency", 0.0)),
        unsupported_claims=list(data.get("unsupported_claims", []) or []),
        reasoning=str(data.get("reasoning", "")),
    )

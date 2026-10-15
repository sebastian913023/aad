"""The diagnostic agent loop.

This is a hand-written tool loop rather than the SDK tool runner because every tool
needs per-request state (the session vehicle, which `decode_vin` mutates so later
lookups inherit it) and because tool results are inspected on the way past to collect
citations for the API response. Both want the loop body, not a per-turn hook.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from aad.agent.client import ModelClient, build_client
from aad.agent.prompts import SYSTEM_PROMPT
from aad.agent.tools import TOOL_SCHEMAS, ToolContext, dispatch_tool
from aad.config import Settings, get_settings
from aad.models import Vehicle
from aad.rag.retriever import Retriever, get_retriever

MAX_TURNS = 12


@dataclass(slots=True)
class ToolCallRecord:
    name: str
    input: dict
    is_error: bool
    result: Any


@dataclass(slots=True)
class DiagnosticResult:
    answer: str
    vehicle: Vehicle
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    stop_reason: str | None = None
    usage: dict = field(default_factory=dict)
    messages: list[dict] = field(default_factory=list)
    monitor: dict | None = None
    monitor_event_id: str | None = None
    # The model's text before the monitor acted on it. Kept for the reviewer, not the
    # technician: when an answer is withheld, someone has to see what was withheld.
    unverified_answer: str | None = None

    def to_dict(self) -> dict:
        payload = {
            "answer": self.answer,
            "vehicle": self.vehicle.model_dump(exclude_none=True),
            "citations": self.citations,
            "tool_calls": [
                {"name": call.name, "input": call.input, "is_error": call.is_error}
                for call in self.tool_calls
            ],
            "stop_reason": self.stop_reason,
            "usage": self.usage,
        }
        if self.monitor is not None:
            payload["monitor"] = self.monitor
            payload["monitor_event_id"] = self.monitor_event_id
        if self.unverified_answer is not None:
            payload["unverified_answer"] = self.unverified_answer
        return payload


def _collect_citations(payload: Any, sink: list[dict], seen: set[str]) -> None:
    """Walk a tool result and pull out every citation, in first-seen order."""
    if isinstance(payload, dict):
        citation = payload.get("citation")
        if isinstance(citation, dict) and citation.get("chunk_id"):
            key = f"{citation.get('source')}#{citation['chunk_id']}"
            if key not in seen:
                seen.add(key)
                sink.append(citation)
        for key, value in payload.items():
            if key != "citation":
                _collect_citations(value, sink, seen)
    elif isinstance(payload, list):
        for item in payload:
            _collect_citations(item, sink, seen)


_BLOCKED_NOTICE = (
    "**Answer withheld.** The verification pass found {n} specification value(s) in this "
    "response that do not appear in any document the response cited: {values}. An "
    "unverifiable number is more dangerous than a missing one, so it is not being shown. "
    "This has been queued for human review{ref}. Consult the OEM service information for "
    "these values."
)

_REVIEW_BANNER = {
    "abstained": (
        "> ⚠️ **Held for human review** ({reason}). The values below were checked against "
        "their cited sources, but the verification pass was not satisfied. Confirm against "
        "the OEM service information before acting on them."
    ),
    "escalated": (
        "> ⚠️ **Flagged for human review** ({reason}). Some part of this answer could not be "
        "fully confirmed against its citations. Verify before acting on it."
    ),
}


class DiagnosticAgent:
    def __init__(
        self,
        *,
        client: ModelClient | None = None,
        retriever: Retriever | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or build_client(self.settings)
        self.retriever = retriever or get_retriever(self.settings)

    def _request(self, messages: list[dict]) -> Any:
        return self.client.create(
            max_tokens=self.settings.max_tokens,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    # Stable prefix: tools + system cache together behind this breakpoint.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            thinking={"type": "adaptive"},
            output_config={"effort": self.settings.model_effort},
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

    def _verify(self, result: DiagnosticResult, question: str) -> DiagnosticResult:
        """Run the grounding monitor over a finished answer and act on its verdict.

        Imported lazily: the judge model lives in `aad.monitor` and itself imports the
        agent's client, so a module-level import here would be a cycle.
        """
        if not self.settings.monitor_enabled or not result.answer:
            return result

        from aad.monitor.harvest import infer_task_type, sources_from_tool_results
        from aad.monitor.pipeline import monitor_output
        from aad.monitor.store import get_monitor_store

        sources = sources_from_tool_results(
            [call.result for call in result.tool_calls if not call.is_error]
        )
        cited = [c["chunk_id"] for c in result.citations if c.get("chunk_id")]
        task_type = infer_task_type([call.name for call in result.tool_calls])

        monitored = monitor_output(
            question=question,
            output=result.answer,
            sources=sources,
            # Fall back to everything retrieved when the answer carried no explicit
            # citation set — the generous reading, so a block is never an artefact of
            # citation bookkeeping.
            cited_ids=cited or None,
            task_type=task_type,
            settings=self.settings,
        )
        result.monitor = monitored.to_dict()

        try:
            result.monitor_event_id = get_monitor_store(settings=self.settings).record(
                monitored,
                question=question,
                output=result.answer,
                vehicle=result.vehicle.label(),
            )
        except Exception as exc:  # noqa: BLE001 - a logging failure must not gate the answer
            result.monitor["notes"] = [*result.monitor.get("notes", []), f"not recorded: {exc}"]

        ref = f" (reference {result.monitor_event_id})" if result.monitor_event_id else ""
        if monitored.verdict == "blocked":
            result.unverified_answer = result.answer
            result.answer = _BLOCKED_NOTICE.format(
                n=len(monitored.fabrications),
                values=", ".join(
                    f"{c.claim.value}{c.claim.unit or ''}" for c in monitored.fabrications
                ),
                ref=ref,
            )
        elif monitored.verdict in _REVIEW_BANNER:
            banner = _REVIEW_BANNER[monitored.verdict].format(
                reason=(monitored.abstention_reason or "unconfirmed").replace("_", " ")
            )
            result.answer = f"{banner}\n\n{result.answer}"
        return result

    def run(
        self,
        question: str,
        *,
        vehicle: Vehicle | None = None,
        history: list[dict] | None = None,
        max_turns: int = MAX_TURNS,
    ) -> DiagnosticResult:
        ctx = ToolContext(
            retriever=self.retriever, settings=self.settings, vehicle=vehicle or Vehicle()
        )

        opening = question
        if ctx.vehicle.is_scoped():
            known = json.dumps(ctx.vehicle.model_dump(exclude_none=True))
            opening = f"Vehicle on the lift: {known}\n\n{question}"

        messages: list[dict] = list(history or []) + [{"role": "user", "content": opening}]
        result = DiagnosticResult(answer="", vehicle=ctx.vehicle)
        seen_citations: set[str] = set()

        for _ in range(max_turns):
            response = self._request(messages)

            result.stop_reason = response.stop_reason
            usage = getattr(response, "usage", None)
            if usage is not None:
                result.usage = {
                    "input_tokens": getattr(usage, "input_tokens", 0),
                    "output_tokens": getattr(usage, "output_tokens", 0),
                    "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
                }

            # Check stop_reason before touching content: a refusal can arrive with an
            # empty content array, and the fallback chain can itself decline.
            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                result.answer = (
                    "This request was declined by the model's safety classifiers"
                    + (f" (category: {category})" if category else "")
                    + ". Rephrase the request around the diagnostic task, or consult the OEM "
                    "service information directly."
                )
                return result

            messages.append({"role": "assistant", "content": response.content})

            tool_uses = [block for block in response.content if block.type == "tool_use"]
            if not tool_uses:
                result.answer = "".join(
                    block.text for block in response.content if block.type == "text"
                ).strip()
                if response.stop_reason == "max_tokens":
                    result.answer += (
                        "\n\n[Response truncated at the output limit — ask for the remainder.]"
                    )
                result.messages = messages
                result.vehicle = ctx.vehicle
                return self._verify(result, question)

            tool_results = []
            for block in tool_uses:
                args = dict(block.input or {})
                payload, is_error = dispatch_tool(block.name, args, ctx)
                parsed = json.loads(payload)
                result.tool_calls.append(
                    ToolCallRecord(name=block.name, input=args, is_error=is_error, result=parsed)
                )
                if not is_error:
                    _collect_citations(parsed, result.citations, seen_citations)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": payload,
                        "is_error": is_error,
                    }
                )
            # All results for a parallel batch go back in one user message.
            messages.append({"role": "user", "content": tool_results})

        result.answer = (
            f"Stopped after {max_turns} tool-calling turns without reaching an answer. "
            "Narrow the question or supply the vehicle details directly."
        )
        result.messages = messages
        result.vehicle = ctx.vehicle
        return result

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

    def to_dict(self) -> dict:
        return {
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
                return result

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

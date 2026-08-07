"""Harvest the source texts an answer was allowed to draw on.

The monitor can only be zero-tolerance if it knows exactly what was in front of the
model. That set is not the whole index — it is the text that came back through tool
results during this one request. A value present somewhere in the corpus but never
retrieved is not grounded for this answer.

Sources are keyed by chunk id, which is what citations carry, so a claim's citation
can be checked against what was actually supplied rather than against a document name
the model wrote down.
"""

from __future__ import annotations

from typing import Any

# Fields that hold source text sitting next to a citation. Anything else in a tool
# payload is the system's own framing (instructions, labels, reasons) and must not
# count as evidence — grounding a claim in our own prompt would be circular.
_TEXT_FIELDS = ("verbatim_source_text", "text", "snippet", "excerpt")


def _walk(payload: Any, sources: dict[str, str]) -> None:
    if isinstance(payload, dict):
        citation = payload.get("citation")
        chunk_id = citation.get("chunk_id") if isinstance(citation, dict) else None
        if chunk_id:
            for field in _TEXT_FIELDS:
                text = payload.get(field)
                if isinstance(text, str) and text.strip():
                    # Several specs can cite the same chunk; keep every excerpt so a
                    # later claim is not judged against a truncated neighbour.
                    existing = sources.get(chunk_id, "")
                    if text not in existing:
                        sources[chunk_id] = f"{existing}\n{text}".strip()
        for value in payload.values():
            _walk(value, sources)
    elif isinstance(payload, list):
        for item in payload:
            _walk(item, sources)


def sources_from_tool_results(results: list[Any]) -> dict[str, str]:
    """Map chunk_id -> retrieved text across every successful tool result."""
    sources: dict[str, str] = {}
    for payload in results:
        _walk(payload, sources)
    return sources


def infer_task_type(tool_names: list[str]) -> str:
    """Label the request so the dashboard can break failures down by task.

    Named after the tool that did the grounding work, because that is where a
    fabrication would come from. The last spec-bearing tool wins: in a multi-step
    answer it is the one the final claim rests on.
    """
    by_tool = {
        "lookup_torque_spec": "torque_spec",
        "lookup_labor_time": "labor_time",
        "lookup_wiring": "wiring",
        "lookup_dtc": "dtc",
        "search_tsbs": "tsb",
        "search_parts": "parts",
        "build_estimate": "estimate",
        "obd2_scan": "obd2_scan",
        "search_service_info": "manual_search",
        "decode_vin": "vin_decode",
    }
    for name in reversed(tool_names):
        if name in by_tool:
            return by_tool[name]
    return "general"

"""Tool definitions and dispatch.

Every tool is a thin wrapper over a provider that already enforces grounding. Tool
descriptions state *when* to call the tool, not only what it does — that is what drives
correct triggering. Errors are returned to the model as `is_error` tool results with a
usable explanation, because a tool that fails silently is a tool the model routes
around by guessing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aad.config import Settings, get_settings
from aad.errors import AadError
from aad.estimates import build_estimate
from aad.models import Vehicle
from aad.providers import dtc as dtc_provider
from aad.providers import labor as labor_provider
from aad.providers import obd2 as obd2_provider
from aad.providers import parts as parts_provider
from aad.providers import torque as torque_provider
from aad.providers import tsb as tsb_provider
from aad.providers import vin as vin_provider
from aad.providers import wiring as wiring_provider
from aad.rag.retriever import Retriever

_VEHICLE_SCHEMA = {
    "type": "object",
    "description": "Vehicle identity. Supply a VIN, or year+make+model (plus engine when known).",
    "properties": {
        "vin": {"type": "string"},
        "year": {"type": "integer"},
        "make": {"type": "string"},
        "model": {"type": "string"},
        "engine": {"type": "string", "description": "e.g. '3.5L V6' or 'VQ35DE'"},
    },
}


@dataclass(slots=True)
class ToolContext:
    """Per-request state threaded into tool calls."""

    retriever: Retriever
    settings: Settings = field(default_factory=get_settings)
    vehicle: Vehicle = field(default_factory=Vehicle)

    def resolve(self, payload: dict | None) -> Vehicle:
        """Merge a tool's vehicle argument over the session vehicle.

        The session vehicle is populated by `decode_vin`, so later tool calls inherit
        the decoded year/make/model/engine without the model having to restate them.
        """
        if not payload:
            return self.vehicle
        merged = self.vehicle.model_dump()
        merged.update({k: v for k, v in payload.items() if v not in (None, "")})
        return Vehicle.model_validate(merged)


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "decode_vin",
        "description": (
            "Decode a 17-character VIN into year, make, model, engine and trim, and validate its "
            "check digit. Call this first whenever a VIN is available — every later lookup is "
            "scoped by the decoded vehicle, and specifications differ between engine variants."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"vin": {"type": "string", "description": "17-character VIN"}},
            "required": ["vin"],
        },
    },
    {
        "name": "lookup_dtc",
        "description": (
            "Explain a diagnostic trouble code: its system, whether it is SAE-generic or "
            "manufacturer-specific, and its definition from the generic table or indexed OEM "
            "documentation. Call this for any code the technician mentions before reasoning "
            "about causes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "e.g. P0340"},
                "vehicle": _VEHICLE_SCHEMA,
            },
            "required": ["code"],
        },
    },
    {
        "name": "obd2_scan",
        "description": (
            "Retrieve stored trouble codes, readiness monitors and live data from the connected "
            "scan service. Call this when the technician refers to a scan they have run rather "
            "than dictating codes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"vehicle": _VEHICLE_SCHEMA, "scan_id": {"type": "string"}},
        },
    },
    {
        "name": "search_service_info",
        "description": (
            "Search indexed OEM service manuals for procedures, diagnostic trees, theory of "
            "operation and specifications. Call this whenever the answer depends on what the "
            "manual actually says — removal steps, test procedures, sequences, capacities."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "vehicle": _VEHICLE_SCHEMA,
                "spec_type": {
                    "type": "string",
                    "enum": [
                        "procedure",
                        "torque_spec",
                        "labor_time",
                        "wiring_diagram",
                        "tsb",
                        "dtc",
                        "fluid_capacity",
                        "general",
                    ],
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "lookup_torque_spec",
        "description": (
            "Get the published torque specification and bolt size for a fastener or assembly. "
            "Call this before stating any torque value — you may not supply one from general "
            "knowledge, and this tool will tell you when the spec is unavailable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "component": {
                    "type": "string",
                    "description": "e.g. 'cylinder head bolts', 'oil drain plug'",
                },
                "vehicle": _VEHICLE_SCHEMA,
            },
            "required": ["component"],
        },
    },
    {
        "name": "lookup_labor_time",
        "description": (
            "Get the published labor time for a repair operation. Call this before quoting hours "
            "or building an estimate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "description": "e.g. 'replace camshaft position sensor bank 1'",
                },
                "vehicle": _VEHICLE_SCHEMA,
            },
            "required": ["operation"],
        },
    },
    {
        "name": "search_parts",
        "description": (
            "Look up part numbers, availability and pricing from the shop's supplier catalog. "
            "Call this before naming a part number or a price. Requires a configured supplier "
            "account; it will say so if none is set up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "part name or description"},
                "vehicle": _VEHICLE_SCHEMA,
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_tsbs",
        "description": (
            "Search technical service bulletins and NHTSA recalls for a symptom. Call this early "
            "in any diagnosis — a known bulletin often replaces hours of testing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symptom": {"type": "string"},
                "vehicle": _VEHICLE_SCHEMA,
            },
            "required": ["symptom"],
        },
    },
    {
        "name": "lookup_wiring",
        "description": (
            "Retrieve wiring diagrams, connector pinouts and wire colours for a circuit. Call "
            "this before describing any pin assignment or wire colour."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "circuit": {
                    "type": "string",
                    "description": "e.g. 'camshaft position sensor bank 1 signal circuit'",
                },
                "vehicle": _VEHICLE_SCHEMA,
            },
            "required": ["circuit"],
        },
    },
    {
        "name": "build_estimate",
        "description": (
            "Assemble a customer estimate from labor hours and part prices you have already "
            "retrieved. This tool does arithmetic only — pass figures that came from "
            "lookup_labor_time and search_parts, never figures you produced yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "vehicle": _VEHICLE_SCHEMA,
                "labor_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "operation": {"type": "string"},
                            "hours": {"type": "number"},
                            "citation": {"type": "object"},
                        },
                        "required": ["operation", "hours"],
                    },
                },
                "part_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "part_number": {"type": "string"},
                            "unit_price": {"type": "number"},
                            "quantity": {"type": "number"},
                        },
                        "required": ["description", "unit_price"],
                    },
                },
                "fees": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "amount": {"type": "number"},
                        },
                        "required": ["description", "amount"],
                    },
                },
                "labor_rate": {"type": "number"},
            },
        },
    },
]


# --- handlers -------------------------------------------------------------


def _decode_vin(args: dict, ctx: ToolContext) -> dict:
    result = vin_provider.decode_vin(args["vin"])
    # Adopt the decoded vehicle for the rest of the session.
    ctx.vehicle = Vehicle.model_validate(result["vehicle"])
    return result


def _lookup_dtc(args: dict, ctx: ToolContext) -> dict:
    return dtc_provider.lookup_dtc(args["code"], ctx.resolve(args.get("vehicle")), ctx.retriever)


def _obd2_scan(args: dict, ctx: ToolContext) -> dict:
    return obd2_provider.scan_vehicle(
        ctx.resolve(args.get("vehicle")),
        ctx.settings,
        scan_id=args.get("scan_id"),
        retriever=ctx.retriever,
    )


def _search_service_info(args: dict, ctx: ToolContext) -> dict:
    vehicle = ctx.resolve(args.get("vehicle"))
    hits = ctx.retriever.search(args["query"], vehicle, spec_type=args.get("spec_type"))
    return {
        "query": args["query"],
        "vehicle": vehicle.label(),
        "results": [
            {
                "text": hit.chunk.text,
                "spec_type": hit.chunk.spec_type,
                "citation": hit.citation().model_dump(),
            }
            for hit in hits
        ],
    }


def _lookup_torque_spec(args: dict, ctx: ToolContext) -> dict:
    return torque_provider.lookup_torque_spec(
        args["component"], ctx.resolve(args.get("vehicle")), ctx.retriever, ctx.settings
    )


def _lookup_labor_time(args: dict, ctx: ToolContext) -> dict:
    return labor_provider.lookup_labor_time(
        args["operation"], ctx.resolve(args.get("vehicle")), ctx.retriever, ctx.settings
    )


def _search_parts(args: dict, ctx: ToolContext) -> dict:
    return parts_provider.search_parts(args["query"], ctx.resolve(args.get("vehicle")), ctx.settings)


def _search_tsbs(args: dict, ctx: ToolContext) -> dict:
    return tsb_provider.search_tsbs(
        args["symptom"], ctx.resolve(args.get("vehicle")), ctx.retriever
    )


def _lookup_wiring(args: dict, ctx: ToolContext) -> dict:
    return wiring_provider.lookup_wiring(
        args["circuit"], ctx.resolve(args.get("vehicle")), ctx.retriever, ctx.settings
    )


def _build_estimate(args: dict, ctx: ToolContext) -> dict:
    estimate = build_estimate(
        ctx.resolve(args.get("vehicle")),
        labor_items=args.get("labor_items"),
        part_items=args.get("part_items"),
        fees=args.get("fees"),
        labor_rate=args.get("labor_rate"),
        settings=ctx.settings,
    )
    return estimate.model_dump(exclude_none=True)


HANDLERS: dict[str, Callable[[dict, ToolContext], dict]] = {
    "decode_vin": _decode_vin,
    "lookup_dtc": _lookup_dtc,
    "obd2_scan": _obd2_scan,
    "search_service_info": _search_service_info,
    "lookup_torque_spec": _lookup_torque_spec,
    "lookup_labor_time": _lookup_labor_time,
    "search_parts": _search_parts,
    "search_tsbs": _search_tsbs,
    "lookup_wiring": _lookup_wiring,
    "build_estimate": _build_estimate,
}


def dispatch_tool(name: str, args: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Run a tool. Returns (serialized result, is_error)."""
    handler = HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"unknown tool {name!r}"}), True
    try:
        return json.dumps(handler(args, ctx), default=str), False
    except AadError as exc:
        # Expected, informative failures: unconfigured providers, unscoped requests,
        # missing grounding. The model needs to see these to report the gap honestly.
        return (
            json.dumps(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "instruction": "Report this limitation to the technician. Do not substitute a "
                    "value from general knowledge.",
                }
            ),
            True,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the model, not swallowed
        return (
            json.dumps({"error": f"{type(exc).__name__}: {exc}", "error_type": "unexpected"}),
            True,
        )

"""Estimate assembly.

Arithmetic only. This module composes labor hours and part prices that other tools
already sourced and cited; it never originates a number. Any line item whose input was
unavailable is recorded as a disclaimer so the estimate is visibly incomplete rather
than quietly wrong.
"""

from __future__ import annotations

from aad.config import Settings, get_settings
from aad.models import Citation, Estimate, EstimateLine, Vehicle


def build_estimate(
    vehicle: Vehicle,
    *,
    labor_items: list[dict] | None = None,
    part_items: list[dict] | None = None,
    fees: list[dict] | None = None,
    labor_rate: float | None = None,
    settings: Settings | None = None,
) -> Estimate:
    """Compose an estimate.

    `labor_items`: {"operation": str, "hours": float, "citation": dict|None}
    `part_items`:  {"description": str, "unit_price": float, "quantity": float,
                    "part_number": str|None, "apply_markup": bool}
    `fees`:        {"description": str, "amount": float}
    """
    settings = settings or get_settings()
    rate = labor_rate if labor_rate is not None else settings.shop_labor_rate

    lines: list[EstimateLine] = []
    citations: list[Citation] = []
    disclaimers: list[str] = []

    for item in labor_items or []:
        hours = item.get("hours")
        if hours is None:
            disclaimers.append(
                f"labor for {item.get('operation', 'unnamed operation')!r} omitted: no published "
                "time was available"
            )
            continue
        lines.append(
            EstimateLine(
                kind="labor",
                description=item.get("operation", "labor"),
                quantity=float(hours),
                unit_price=rate,
            )
        )
        if item.get("citation"):
            citations.append(Citation.model_validate(item["citation"]))

    for item in part_items or []:
        price = item.get("unit_price")
        description = item.get("description", "part")
        if item.get("part_number"):
            description = f"{description} ({item['part_number']})"
        if price is None:
            disclaimers.append(f"part {description!r} omitted: no price was available")
            continue
        unit_price = float(price)
        if item.get("apply_markup", True):
            unit_price = round(unit_price * (1 + settings.parts_markup), 2)
        lines.append(
            EstimateLine(
                kind="part",
                description=description,
                quantity=float(item.get("quantity", 1)),
                unit_price=unit_price,
            )
        )

    for fee in fees or []:
        lines.append(
            EstimateLine(
                kind="fee",
                description=fee.get("description", "fee"),
                quantity=1.0,
                unit_price=float(fee.get("amount", 0.0)),
            )
        )

    labor_subtotal = round(sum(line.total for line in lines if line.kind == "labor"), 2)
    parts_subtotal = round(sum(line.total for line in lines if line.kind == "part"), 2)
    fees_subtotal = round(sum(line.total for line in lines if line.kind == "fee"), 2)
    subtotal = round(labor_subtotal + parts_subtotal + fees_subtotal, 2)
    tax = round(subtotal * settings.tax_rate, 2)

    if not lines:
        disclaimers.append("estimate is empty: no priced labor or parts were available")

    return Estimate(
        vehicle=vehicle,
        lines=lines,
        labor_rate=rate,
        labor_subtotal=labor_subtotal,
        parts_subtotal=parts_subtotal,
        tax=tax,
        total=round(subtotal + tax, 2),
        citations=citations,
        disclaimers=disclaimers,
    )

"""Request bodies for the HTTP API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from aad.models import SpecType, Vehicle


class VehicleScoped(BaseModel):
    vehicle: Vehicle = Field(default_factory=Vehicle)


class DiagnoseRequest(VehicleScoped):
    question: str
    history: list[dict] | None = None


class VinRequest(BaseModel):
    vin: str


class SearchRequest(VehicleScoped):
    query: str
    spec_type: SpecType | None = None
    top_k: int | None = None


class TorqueRequest(VehicleScoped):
    component: str


class LaborRequest(VehicleScoped):
    operation: str


class PartsRequest(VehicleScoped):
    query: str
    limit: int = 10


class TsbRequest(VehicleScoped):
    symptom: str


class WiringRequest(VehicleScoped):
    circuit: str


class DtcRequest(VehicleScoped):
    code: str


class ScanRequest(VehicleScoped):
    scan_id: str | None = None


class EstimateRequest(VehicleScoped):
    labor_items: list[dict] = Field(default_factory=list)
    part_items: list[dict] = Field(default_factory=list)
    fees: list[dict] = Field(default_factory=list)
    labor_rate: float | None = None


class VerifyRequest(BaseModel):
    """Verify an arbitrary output against the sources it claims to rest on.

    Exposed so outputs produced outside this system — another model, a copied answer,
    a draft write-up — can be run through the same grounding check.
    """

    output: str
    sources: dict[str, str] = Field(
        default_factory=dict, description="Source id -> full source text, as supplied to the model."
    )
    question: str = ""
    cited_ids: list[str] | None = Field(
        default=None, description="Ids the output cited. Defaults to every supplied source."
    )
    task_type: str = "general"
    use_judge: bool = Field(
        default=False, description="Also run the judge model. Needs API credentials."
    )
    record: bool = True


class ReviewRequest(BaseModel):
    """A human closing out a flagged output."""

    reviewer: str
    outcome: Literal["confirmed", "corrected", "rejected"]
    note: str = ""

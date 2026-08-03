"""Request bodies for the HTTP API."""

from __future__ import annotations

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

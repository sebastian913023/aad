"""Shared domain models.

`Vehicle` is the asset-scoping key used by every retrieval call. Nothing in this
system fetches unscoped service data — a torque spec for the wrong engine is a
wrong torque spec.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SpecType = Literal[
    "procedure",
    "torque_spec",
    "labor_time",
    "wiring_diagram",
    "tsb",
    "dtc",
    "fluid_capacity",
    "general",
]


class Vehicle(BaseModel):
    """Asset-scoping key. Either a VIN or year/make/model must be present."""

    vin: str | None = None
    year: int | None = None
    make: str | None = None
    model: str | None = None
    engine: str | None = None
    trim: str | None = None

    def is_scoped(self) -> bool:
        return bool(self.vin) or bool(self.year and self.make and self.model)

    def label(self) -> str:
        if self.year and self.make and self.model:
            base = f"{self.year} {self.make} {self.model}"
            if self.engine:
                base = f"{base} {self.engine}"
            return base
        return self.vin or "unknown vehicle"

    def filter_dict(self) -> dict[str, str | int]:
        """Metadata filter applied to every retrieval. VIN is deliberately excluded:
        documents are indexed by year/make/model/engine, and a VIN is resolved to
        those fields upstream by the decoder."""
        out: dict[str, str | int] = {}
        if self.year:
            out["year"] = self.year
        if self.make:
            out["make"] = self.make.lower()
        if self.model:
            out["model"] = self.model.lower()
        if self.engine:
            out["engine"] = self.engine.lower()
        return out


class Citation(BaseModel):
    """Provenance for one retrieved fact. Every spec surfaced to a tech carries one."""

    source: str
    page: int | None = None
    section: str | None = None
    chunk_id: str
    score: float = 0.0


class Chunk(BaseModel):
    """An indexed unit of source material."""

    chunk_id: str
    text: str
    source: str
    page: int | None = None
    section: str | None = None
    spec_type: SpecType = "general"
    year: int | None = None
    make: str | None = None
    model: str | None = None
    engine: str | None = None

    def metadata(self) -> dict:
        return {
            "source": self.source,
            "page": self.page,
            "section": self.section,
            "spec_type": self.spec_type,
            "year": self.year,
            "make": self.make,
            "model": self.model,
            "engine": self.engine,
        }


class RetrievedChunk(BaseModel):
    chunk: Chunk
    score: float

    def citation(self) -> Citation:
        return Citation(
            source=self.chunk.source,
            page=self.chunk.page,
            section=self.chunk.section,
            chunk_id=self.chunk.chunk_id,
            score=round(self.score, 4),
        )


class TorqueSpec(BaseModel):
    component: str
    value: float
    unit: Literal["Nm", "lb-ft", "lb-in"]
    bolt_size: str | None = None
    thread_pitch: str | None = None
    quantity: int | None = None
    sequence: str | None = None
    stage: str | None = None
    notes: str | None = None
    citations: list[Citation] = Field(default_factory=list)


class LaborTime(BaseModel):
    operation: str
    hours: float
    skill_level: str | None = None
    warranty_hours: float | None = None
    notes: str | None = None
    citations: list[Citation] = Field(default_factory=list)


class Part(BaseModel):
    part_number: str
    description: str
    brand: str | None = None
    price: float | None = None
    core_charge: float | None = None
    availability: str | None = None
    supplier: str | None = None


class EstimateLine(BaseModel):
    kind: Literal["labor", "part", "fee"]
    description: str
    quantity: float = 1.0
    unit_price: float = 0.0

    @property
    def total(self) -> float:
        return round(self.quantity * self.unit_price, 2)


class Estimate(BaseModel):
    vehicle: Vehicle
    lines: list[EstimateLine] = Field(default_factory=list)
    labor_rate: float = 0.0
    parts_subtotal: float = 0.0
    labor_subtotal: float = 0.0
    tax: float = 0.0
    total: float = 0.0
    citations: list[Citation] = Field(default_factory=list)
    disclaimers: list[str] = Field(default_factory=list)

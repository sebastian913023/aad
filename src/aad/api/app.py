"""FastAPI application.

Each data endpoint mirrors one agent tool, so the mobile client can call a specific
lookup directly (fast, cheap, deterministic) or go through `/diagnose` when it needs
reasoning across several of them.

Domain errors map to HTTP status codes with the reason intact: 422 for an unscoped
request, 501 for an unconfigured provider, 404 for missing grounding. Callers get the
gap, never a filled-in guess.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from aad.api.schemas import (
    DiagnoseRequest,
    DtcRequest,
    EstimateRequest,
    LaborRequest,
    PartsRequest,
    ScanRequest,
    SearchRequest,
    TorqueRequest,
    TsbRequest,
    VinRequest,
    WiringRequest,
)
from aad.cache.store import get_cache
from aad.config import Settings, get_settings
from aad.errors import (
    NoGroundingError,
    NotConfiguredError,
    ProviderError,
    UnscopedRequestError,
)
from aad.estimates import build_estimate
from aad.providers import dtc as dtc_provider
from aad.providers import labor as labor_provider
from aad.providers import obd2 as obd2_provider
from aad.providers import parts as parts_provider
from aad.providers import torque as torque_provider
from aad.providers import tsb as tsb_provider
from aad.providers import vin as vin_provider
from aad.providers import wiring as wiring_provider
from aad.rag.retriever import Retriever, get_retriever
from aad.rag.store import LocalVectorStore, get_vector_store

STATUS_BY_ERROR = {
    UnscopedRequestError: 422,
    NoGroundingError: 404,
    NotConfiguredError: 501,
    ProviderError: 502,
}


@lru_cache
def _retriever() -> Retriever:
    return get_retriever()


def get_retriever_dep() -> Retriever:
    return _retriever()


def guard(fn: Callable[[], Any]) -> Any:
    """Run a handler, translating domain errors into HTTP responses."""
    try:
        return fn()
    except tuple(STATUS_BY_ERROR) as exc:
        raise HTTPException(status_code=STATUS_BY_ERROR[type(exc)], detail=str(exc)) from exc


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Auto Mechanic Diagnostic API",
        version="0.1.0",
        description=(
            "Grounded diagnostic assistance. Every specification returned carries a citation; "
            "unavailable data is reported as unavailable."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "model": settings.model, "provider": settings.provider}

    @app.get("/api/v1/index/stats")
    def index_stats() -> dict:
        store = get_vector_store(settings)
        payload: dict[str, Any] = {"chunks": store.count(), "index_dir": str(settings.index_dir)}
        if isinstance(store, LocalVectorStore):
            payload["sources"] = store.sources()
        payload["cache"] = get_cache().stats()
        return payload

    # --- vehicle identity ------------------------------------------------
    @app.post("/api/v1/vin/decode")
    def decode_vin(body: VinRequest) -> dict:
        return guard(lambda: vin_provider.decode_vin(body.vin))

    # --- service information ---------------------------------------------
    @app.post("/api/v1/manuals/search")
    def search_manuals(
        body: SearchRequest, retriever: Retriever = Depends(get_retriever_dep)
    ) -> dict:
        def run() -> dict:
            hits = retriever.search(
                body.query, body.vehicle, spec_type=body.spec_type, top_k=body.top_k
            )
            return {
                "query": body.query,
                "vehicle": body.vehicle.label(),
                "results": [
                    {
                        "text": hit.chunk.text,
                        "spec_type": hit.chunk.spec_type,
                        "citation": hit.citation().model_dump(),
                    }
                    for hit in hits
                ],
            }

        return guard(run)

    @app.post("/api/v1/torque-specs")
    def torque_specs(
        body: TorqueRequest, retriever: Retriever = Depends(get_retriever_dep)
    ) -> dict:
        return guard(
            lambda: torque_provider.lookup_torque_spec(
                body.component, body.vehicle, retriever, settings
            )
        )

    @app.post("/api/v1/labor-times")
    def labor_times(body: LaborRequest, retriever: Retriever = Depends(get_retriever_dep)) -> dict:
        return guard(
            lambda: labor_provider.lookup_labor_time(
                body.operation, body.vehicle, retriever, settings
            )
        )

    @app.post("/api/v1/wiring-diagrams")
    def wiring_diagrams(
        body: WiringRequest, retriever: Retriever = Depends(get_retriever_dep)
    ) -> dict:
        return guard(
            lambda: wiring_provider.lookup_wiring(body.circuit, body.vehicle, retriever, settings)
        )

    # --- diagnostics ------------------------------------------------------
    @app.post("/api/v1/dtc/lookup")
    def dtc_lookup(body: DtcRequest, retriever: Retriever = Depends(get_retriever_dep)) -> dict:
        return guard(lambda: dtc_provider.lookup_dtc(body.code, body.vehicle, retriever))

    @app.post("/api/v1/obd2/scan")
    def obd2_scan(body: ScanRequest, retriever: Retriever = Depends(get_retriever_dep)) -> dict:
        return guard(
            lambda: obd2_provider.scan_vehicle(
                body.vehicle, settings, scan_id=body.scan_id, retriever=retriever
            )
        )

    @app.post("/api/v1/tsb/search")
    def tsb_search(body: TsbRequest, retriever: Retriever = Depends(get_retriever_dep)) -> dict:
        return guard(lambda: tsb_provider.search_tsbs(body.symptom, body.vehicle, retriever))

    # --- commerce ---------------------------------------------------------
    @app.post("/api/v1/parts/search")
    def parts_search(body: PartsRequest) -> dict:
        return guard(
            lambda: parts_provider.search_parts(
                body.query, body.vehicle, settings, limit=body.limit
            )
        )

    @app.post("/api/v1/estimates")
    def estimates(body: EstimateRequest) -> dict:
        def run() -> dict:
            estimate = build_estimate(
                body.vehicle,
                labor_items=body.labor_items,
                part_items=body.part_items,
                fees=body.fees,
                labor_rate=body.labor_rate,
                settings=settings,
            )
            return estimate.model_dump(exclude_none=True)

        return guard(run)

    # --- agent ------------------------------------------------------------
    @app.post("/api/v1/diagnose")
    def diagnose(body: DiagnoseRequest, retriever: Retriever = Depends(get_retriever_dep)) -> dict:
        from aad.agent.agent import DiagnosticAgent

        def run() -> dict:
            agent = DiagnosticAgent(retriever=retriever, settings=settings)
            return agent.run(
                body.question, vehicle=body.vehicle, history=body.history
            ).to_dict()

        return guard(run)

    return app

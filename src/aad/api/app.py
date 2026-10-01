"""FastAPI application.

Each data endpoint mirrors one agent tool, so the mobile client can call a specific
lookup directly (fast, cheap, deterministic) or go through `/diagnose` when it needs
reasoning across several of them.

Domain errors map to HTTP status codes with the reason intact: 422 for an unscoped
request, 501 for an unconfigured provider, 404 for missing grounding. Callers get the
gap, never a filled-in guess.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from aad.api.schemas import (
    DiagnoseRequest,
    DtcRequest,
    EstimateRequest,
    LaborRequest,
    PartsRequest,
    ReviewRequest,
    ScanRequest,
    SearchRequest,
    TorqueRequest,
    TsbRequest,
    VerifyRequest,
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

    @app.get("/", response_class=HTMLResponse)
    def app_shell() -> HTMLResponse:
        from aad.web import render_app

        return HTMLResponse(render_app())

    @app.get("/api/v1/index/stats")
    def index_stats() -> dict:
        store = get_vector_store(settings)
        payload: dict[str, Any] = {"chunks": store.count(), "index_dir": str(settings.index_dir)}
        if isinstance(store, LocalVectorStore):
            payload["sources"] = store.sources()
        payload["cache"] = get_cache(settings=settings).stats()
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

    # --- grounding monitor -------------------------------------------------
    @app.post("/api/v1/monitor/verify")
    def monitor_verify(body: VerifyRequest) -> dict:
        from aad.monitor.pipeline import monitor_output
        from aad.monitor.store import get_monitor_store

        def run() -> dict:
            result = monitor_output(
                question=body.question,
                output=body.output,
                sources=body.sources,
                cited_ids=body.cited_ids,
                task_type=body.task_type,
                use_judge=body.use_judge,
                settings=settings,
            )
            payload = result.to_dict()
            if body.record:
                payload["event_id"] = get_monitor_store(settings=settings).record(
                    result, question=body.question, output=body.output
                )
            return payload

        return guard(run)

    @app.get("/api/v1/monitor/summary")
    def monitor_summary(since_hours: float | None = None) -> dict:
        from aad.monitor.store import get_monitor_store

        store = get_monitor_store(settings=settings)
        since = time.time() - since_hours * 3600 if since_hours else None
        return {
            "summary": store.summary(since=since),
            "by_task_type": store.by_task_type(since=since),
            "abstention_reasons": store.abstention_reasons(since=since),
        }

    @app.get("/api/v1/monitor/events")
    def monitor_events(
        limit: int = 50,
        task_type: str | None = None,
        verdict: str | None = None,
        needs_review: bool | None = None,
    ) -> dict:
        from aad.monitor.store import get_monitor_store

        return {
            "events": get_monitor_store(settings=settings).recent(
                limit=min(limit, 500),
                task_type=task_type,
                verdict=verdict,
                needs_review=needs_review,
            )
        }

    @app.get("/api/v1/monitor/events/{event_id}")
    def monitor_event(event_id: str) -> dict:
        from aad.monitor.store import get_monitor_store

        event = get_monitor_store(settings=settings).get(event_id)
        if event is None:
            raise HTTPException(status_code=404, detail=f"no monitor event {event_id!r}")
        return event

    @app.get("/api/v1/monitor/review-queue")
    def monitor_review_queue(limit: int = 50) -> dict:
        from aad.monitor.store import get_monitor_store

        return {"queue": get_monitor_store(settings=settings).review_queue(limit=min(limit, 500))}

    @app.post("/api/v1/monitor/events/{event_id}/review")
    def monitor_review(event_id: str, body: ReviewRequest) -> dict:
        from aad.monitor.store import get_monitor_store

        ok = get_monitor_store(settings=settings).resolve(
            event_id, reviewer=body.reviewer, outcome=body.outcome, note=body.note
        )
        if not ok:
            raise HTTPException(status_code=404, detail=f"no monitor event {event_id!r}")
        return {"event_id": event_id, "outcome": body.outcome, "reviewer": body.reviewer}

    @app.get("/api/v1/monitor/dashboard.json")
    def monitor_dashboard_json(since_hours: float | None = None) -> dict:
        from aad.monitor.dashboard import dashboard_data
        from aad.monitor.store import get_monitor_store

        since = time.time() - since_hours * 3600 if since_hours else None
        return dashboard_data(get_monitor_store(settings=settings), since=since)

    @app.get("/monitor", response_class=HTMLResponse)
    def monitor_dashboard(since_hours: float | None = None) -> HTMLResponse:
        from aad.monitor.dashboard import render_from_store
        from aad.monitor.store import get_monitor_store

        since = time.time() - since_hours * 3600 if since_hours else None
        return HTMLResponse(render_from_store(get_monitor_store(settings=settings), since=since))

    return app

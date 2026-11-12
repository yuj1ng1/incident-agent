"""Independent local API. Import/startup never initializes or schedules Runtime."""
import logging
from pathlib import Path
import re
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from investigation.models import SessionStatus
from investigation.state import RuntimeErrorBase, RuntimeIntegrityError
from incident_store import IncidentStore
from models.remediation import RemediationStatus
from remediation.executor import ActionExecutor
from remediation.state import RemediationError
from remediation.store import RemediationStore, StoreError
from web_api.auth import LOCAL_ORIGINS, WebAuthorization
from web_api.observations import incident_signals
from web_api.queries import ROOT, RemediationQueries, RuntimeQueries
from web_api.remediation_views import safe_text
from web_api.schemas import (
    CompensationRequest, Health, IncidentDetail, IncidentPage, IncidentSignals, OperatorView,
    ReasonCommand, RemediationCommand, RemediationDetail, RemediationPage,
    SessionDetail, SessionPage,
)

logger = logging.getLogger(__name__)
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0, le=10000)]


def create_app(
    *,
    incident_url: str | None = None,
    incident_path: Path | None = None,
    runtime_url: str | None = None,
    include_runtime: bool = True,
    remediation_path: Path = ROOT / "data" / "remediation.json",
    authorization=None,
    executor_factory=ActionExecutor,
) -> FastAPI:
    if incident_url is None and incident_path is not None:
        incident_url = incident_path
    auth = authorization or WebAuthorization()
    app = FastAPI(title="Incident Agent 本地工作台 API", version="0.2.0", debug=False)
    # Direct browser GETs from the local workbench; no proxy or credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(LOCAL_ORIGINS),
        allow_methods=["GET", "POST"] if auth.enabled else ["GET"],
        allow_headers=["Authorization", "Content-Type"] if auth.enabled else [],
        allow_credentials=False,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    queries = RuntimeQueries(incident_url, runtime_url, include_runtime=include_runtime)
    remediations = RemediationQueries(remediation_path)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc):
        # FastAPI's default errors echo invalid input, including caller secrets.
        if request.url.path.startswith("/api/remediations"):
            return JSONResponse(status_code=422, content={"detail": "invalid_request"})
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(RemediationError)
    def remediation_failure(request: Request, exc):
        code = str(exc)
        status = 404 if code == "proposal_not_found" else 409
        if code == "store_busy" and request.method == "GET":
            status = 503
        if isinstance(exc, StoreError) and code not in {"proposal_not_found", "store_busy"}:
            status, code = 500, "remediation_store_failed"
        if not re.fullmatch(r"[a-z_]+(?::[a-z_]+)?", code):
            code = "remediation_operation_rejected"
        return JSONResponse(status_code=status, content={"detail": code})

    def query_failure(request: Request, exc: Exception):
        # Diagnostic class/errno only: never log input records or credential text.
        logger.error("Read-only API query failed category=%s errno=%s", type(exc).__name__, getattr(exc, "errno", None))
        unavailable = isinstance(exc, (FileNotFoundError, OperationalError)) or (
            isinstance(exc, RuntimeIntegrityError)
            and str(exc) in {"runtime_schema_not_migrated", "runtime_database_unavailable"}
        )
        return JSONResponse(status_code=503 if unavailable else 500,
                            content={"detail": "store_unavailable" if unavailable else "store_read_failed"})

    for error in (OSError, SQLAlchemyError, RuntimeErrorBase, ValidationError,
                  ValueError, KeyError, TypeError, ResponseValidationError):
        app.add_exception_handler(error, query_failure)

    @app.get("/healthz", response_model=Health)
    def healthz():
        return Health(read_only=not auth.enabled)

    @app.get("/readyz", response_model=Health)
    def readyz():
        try:
            queries.sessions(limit=1, offset=0, status=None, incident_id=None)
            queries.incidents(limit=1, offset=0, status=None)
        except (OSError, SQLAlchemyError, RuntimeErrorBase, RuntimeError) as exc:
            reason = str(exc) if isinstance(exc, (RuntimeErrorBase, RuntimeError)) else ""
            if reason not in {
                "database_url_not_configured", "database_url_invalid",
                "mysql_pymysql_database_url_required", "runtime_schema_not_migrated",
                "runtime_database_unavailable",
            }:
                reason = "store_unavailable"
            raise HTTPException(503, detail=reason) from None
        return healthz()

    @app.get("/api/operator", response_model=OperatorView)
    def operator_identity(request: Request):
        operator = auth.authenticate(request)
        return OperatorView(actor=operator.actor, permissions=sorted(operator.permissions))

    @app.get("/api/remediations", response_model=RemediationPage)
    def list_remediations(
        limit: PageLimit = 20,
        offset: PageOffset = 0,
        status: RemediationStatus | None = None,
        incident_id: str | None = None,
    ):
        return remediations.list(limit=limit, offset=offset, status=status, incident_id=incident_id)

    @app.get("/api/remediations/{proposal_id}", response_model=RemediationDetail)
    def get_remediation(proposal_id: str):
        result = remediations.detail(proposal_id)
        if result is None:
            raise HTTPException(404, "proposal_not_found")
        return result

    def writable_store(proposal_id):
        if not Path(remediation_path).is_file():
            raise HTTPException(503, "store_unavailable")
        store = RemediationStore(remediation_path)
        proposal = store.get(proposal_id)
        incident = IncidentStore(incident_url, read_only=True).get_incident(
            proposal.incident_id, include_history=False,
        )
        if (incident is None or incident.status not in {"investigating", "resolved"}
                or incident.service != proposal.service
                or (proposal.incident_fingerprint is not None
                    and proposal.incident_fingerprint != incident.fingerprint)):
            raise HTTPException(409, "incident_missing_archived_or_changed")
        return store

    def change(
        proposal_id,
        operator,
        operation,
    ):
        store = writable_store(proposal_id)
        try:
            operation(store)
        except RemediationError as exc:
            # Business rejections are durable when the Store is available.
            if not isinstance(exc, StoreError):
                with store.transaction() as data:
                    proposal = store.from_data(data, proposal_id)
                    store.event(data, proposal, "web_operation_denied", operator.actor,
                                reason=safe_text(str(exc), 200))
            raise
        return get_remediation(proposal_id)

    @app.post("/api/remediations/{proposal_id}/compensation-requests", response_model=RemediationDetail)
    def request_compensation(
        proposal_id: str,
        body: CompensationRequest,
        operator=Depends(auth.require("request")),
    ):
        return change(proposal_id, operator, lambda store: store.request_compensation(
            proposal_id, operator.actor, safe_text(body.reason), body.command_id,
            expected_digest=body.proposal_digest, target_id=body.target_id,
        ))

    @app.post("/api/remediations/{proposal_id}/approve", response_model=RemediationDetail)
    def approve_remediation(
        proposal_id: str,
        body: RemediationCommand,
        operator=Depends(auth.require("approve")),
    ):
        return change(proposal_id, operator, lambda store: store.approve(
            proposal_id, operator.actor, command_id=body.command_id,
            expected_digest=body.proposal_digest,
        ))

    @app.post("/api/remediations/{proposal_id}/reject", response_model=RemediationDetail)
    def reject_remediation(
        proposal_id: str,
        body: ReasonCommand,
        operator=Depends(auth.require("approve")),
    ):
        return change(proposal_id, operator, lambda store: store.reject(
            proposal_id, operator.actor, safe_text(body.reason), command_id=body.command_id,
            expected_digest=body.proposal_digest,
        ))

    @app.post("/api/remediations/{proposal_id}/execute", response_model=RemediationDetail)
    def execute_remediation(
        proposal_id: str,
        body: RemediationCommand,
        operator=Depends(auth.require("execute")),
    ):
        return change(proposal_id, operator, lambda store: executor_factory(store).execute(
            proposal_id, operator.actor, command_id=body.command_id,
            expected_digest=body.proposal_digest,
        ))

    @app.post("/api/remediations/{proposal_id}/inspect-execution", response_model=RemediationDetail)
    def inspect_remediation(
        proposal_id: str,
        body: RemediationCommand,
        operator=Depends(auth.require("execute")),
    ):
        def inspect(store):
            executor_factory(store).inspect_execution(
                proposal_id, operator.actor, command_id=body.command_id,
                expected_digest=body.proposal_digest,
            )
        return change(proposal_id, operator, inspect)

    @app.post("/api/remediations/{proposal_id}/close-execution", response_model=RemediationDetail)
    def close_remediation(
        proposal_id: str,
        body: ReasonCommand,
        operator=Depends(auth.require("execute")),
    ):
        def close(store):
            from remediation.approval import approval_digest
            if approval_digest(store.get(proposal_id)) != body.proposal_digest:
                raise RemediationError("proposal_digest_mismatch")
            executor_factory(store).close_execution(
                proposal_id, operator.actor, safe_text(body.reason), body.command_id,
            )
        return change(proposal_id, operator, close)

    @app.get("/api/incidents", response_model=IncidentPage)
    def list_incidents(
        limit: PageLimit = 20,
        offset: PageOffset = 0,
        status: Literal["investigating", "resolved"] | None = None,
        service: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    ):
        return queries.incidents(limit=limit, offset=offset, status=status, service=service)

    @app.get("/api/incidents/{incident_id}", response_model=IncidentDetail)
    def get_incident(incident_id: str):
        result = queries.incident(incident_id)
        if result is None:
            raise HTTPException(status_code=404, detail="incident_not_found")
        return result

    @app.get("/api/incidents/{incident_id}/signals", response_model=IncidentSignals)
    async def get_incident_signals(incident_id: str):
        incident = await run_in_threadpool(queries.incident_record, incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="incident_not_found")
        return await incident_signals(incident)

    @app.get("/api/sessions", response_model=SessionPage)
    def list_sessions(limit: PageLimit = 20, offset: PageOffset = 0,
                      status: SessionStatus | None = None, incident_id: str | None = None):
        return queries.sessions(limit=limit, offset=offset, status=status, incident_id=incident_id)

    @app.get("/api/sessions/{session_id}", response_model=SessionDetail)
    def get_session(session_id: str):
        result = queries.session(session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        return result

    return app


app = create_app()

"""Independent local API. Import/startup never initializes or schedules Runtime."""
import logging
from pathlib import Path
import sqlite3
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import ResponseValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from investigation.models import SessionStatus
from investigation.state import RuntimeErrorBase
from web_api.queries import ROOT, RuntimeQueries
from web_api.schemas import Health, IncidentDetail, IncidentPage, SessionDetail, SessionPage

logger = logging.getLogger(__name__)
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0, le=10000)]


def create_app(*, incident_path: Path = ROOT / "data" / "incidents.json",
               runtime_path: Path = ROOT / "data" / "runtime.sqlite3") -> FastAPI:
    app = FastAPI(title="Incident Agent 本地只读 API", version="0.1.0", debug=False)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    queries = RuntimeQueries(incident_path, runtime_path)

    def query_failure(request: Request, exc: Exception):
        # Diagnostic class/errno only: never log input records or credential text.
        logger.error("Read-only API query failed category=%s errno=%s", type(exc).__name__, getattr(exc, "errno", None))
        unavailable = isinstance(exc, FileNotFoundError) or (
            isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()
        )
        return JSONResponse(status_code=503 if unavailable else 500,
                            content={"detail": "store_unavailable" if unavailable else "store_read_failed"})

    for error in (OSError, sqlite3.Error, RuntimeErrorBase, ValidationError,
                  ValueError, KeyError, TypeError, ResponseValidationError):
        app.add_exception_handler(error, query_failure)

    @app.get("/healthz", response_model=Health)
    def healthz():
        return Health()

    @app.get("/api/incidents", response_model=IncidentPage)
    def list_incidents(limit: PageLimit = 20, offset: PageOffset = 0,
                       status: Literal["investigating", "resolved"] | None = None):
        return queries.incidents(limit=limit, offset=offset, status=status)

    @app.get("/api/incidents/{incident_id}", response_model=IncidentDetail)
    def get_incident(incident_id: str):
        result = queries.incident(incident_id)
        if result is None:
            raise HTTPException(status_code=404, detail="incident_not_found")
        return result

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

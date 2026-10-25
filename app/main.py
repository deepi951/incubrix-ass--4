import logging
import json
import os
import re
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.connectors.manifests import load_manifests
from app.connectors.routes import router as connector_router
from app.api.routes import router
from app.services.certification import (
    cleanup_interrupted_certifications,
    schedule_due_recertifications,
)
from app.storage.database import fail_stale_jobs, initialize_database


logger = logging.getLogger("incubrix")


class JsonLogFormatter(logging.Formatter):
    _context_fields = (
        "correlation_id",
        "job_id",
        "connector_key",
        "method",
        "path",
        "status_code",
        "simulation_mode",
        "error_code",
        "failure_type",
        "processed",
    )

    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for field in self._context_fields:
            value = getattr(record, field, None)
            if value is not None:
                event[field] = value
        if record.exc_info:
            event["exception"] = self.formatException(record.exc_info)
        return json.dumps(event, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        handlers=[handler],
        force=True,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    initialize_database()
    load_manifests()
    fail_stale_jobs(int(os.getenv("JOB_STALE_AFTER_SECONDS", "300")))
    cleanup_interrupted_certifications()
    scheduler = None
    if os.getenv("RECERTIFICATION_ENABLED", "").lower() == "true":
        scheduler = asyncio.create_task(recertification_loop())
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.cancel()
            try:
                await scheduler
            except asyncio.CancelledError:
                pass


async def recertification_loop() -> None:
    interval = max(60, int(os.getenv("RECERTIFICATION_INTERVAL_SECONDS", "86400")))
    while True:
        try:
            processed = await asyncio.to_thread(schedule_due_recertifications)
            if processed:
                logger.info(
                    "recertification_batch_completed",
                    extra={"processed": processed},
                )
        except Exception as exc:
            logger.error(
                "recertification_scheduler_failed",
                extra={"failure_type": type(exc).__name__},
            )
        await asyncio.sleep(interval)



app = FastAPI(
    title="IncuBrix Distribution",
    description="A configurable distribution API for long-tail destinations.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(connector_router)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID", "")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", correlation_id):
        correlation_id = str(uuid4())

    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    logger.info(
        "request_completed",
        extra={
            "correlation_id": correlation_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
        },
    )
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    if isinstance(exc.detail, dict):
        code = str(exc.detail.get("code", "HTTP_ERROR"))
        message = str(exc.detail.get("message", "The request could not be completed."))
        details = exc.detail.get("details", {})
    else:
        code = "HTTP_ERROR"
        message = str(exc.detail)
        details = {}
    if not isinstance(details, dict):
        details = {}
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": exc.status_code == 429 or exc.status_code >= 500,
                "details": details,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    validation_errors = [
        {
            "location": error.get("loc", []),
            "message": error.get("msg", "Invalid value."),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "REQUEST_VALIDATION_FAILED",
                "message": "The request contains invalid fields.",
                "retryable": False,
                "details": {"validation_errors": validation_errors},
            }
        },
    )


@app.get("/")
def read_root() -> dict[str, str]:
    return {"name": "IncuBrix Distribution", "status": "running"}


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/ready")
def readiness_check() -> dict[str, str]:
    return {"status": "ready"}

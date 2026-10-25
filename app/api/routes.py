import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from pydantic import ValidationError
from fastapi.responses import Response as HttpResponse

from app.api.models import (
    DestinationCreate,
    DestinationResponse,
    JobCreate,
    JobResponse,
    PostingRules,
    RecommendationRequest,
    TailorRequest,
)
from app.catalogue.service import export_csv, get_import_report, import_csv, list_entries
from app.connectors.manifests import load_manifests
from app.services.rules import RuleViolation, count_recent_posts, validate_content
from app.services.jobs import process_job
from app.services.certification import (
    real_certification_allowed,
    run_certification,
    simulation_enabled,
)
from app.services.content import recommend_destinations, tailor_for_catalogue
from app.services.idempotency import cache_idempotent_response
from app.storage.database import connect


router = APIRouter(prefix="/api/v1")


@router.post("/catalogue/import", status_code=status.HTTP_201_CREATED)
async def import_catalogue(
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
) -> dict:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in {"text/csv", "application/csv"}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "code": "CSV_CONTENT_TYPE_REQUIRED",
                "message": "Send the catalogue as a text/csv request body.",
            },
        )
    body = await request.body()
    max_bytes = int(os.getenv("MAX_CATALOGUE_IMPORT_BYTES", "20000000"))
    if len(body) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "CSV_TOO_LARGE",
                "message": "The catalogue CSV exceeds the configured upload limit.",
            },
        )
    try:
        csv_text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "CSV_ENCODING_INVALID",
                "message": "The catalogue CSV must be UTF-8 encoded.",
            },
        ) from exc

    report, replayed = import_csv(csv_text, idempotency_key)
    if "error" in report:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=report["error"],
        )
    if replayed:
        response.status_code = status.HTTP_200_OK
    return report


@router.get("/catalogue")
def get_catalogue(
    limit: int = 100,
    offset: int = 0,
) -> dict:
    if limit < 1 or limit > 1000 or offset < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "PAGINATION_INVALID",
                "message": "limit must be 1..1000 and offset must not be negative.",
            },
        )
    with connect() as connection:
        total = connection.execute(
            "SELECT COUNT(*) AS total FROM catalogue_destinations"
        ).fetchone()["total"]
    return {
        "items": list_entries(limit, offset),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/tailor", response_model=None)
def tailor_content(
    payload: TailorRequest,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
) -> dict:
    result, replayed = cache_idempotent_response(
        "tailor",
        idempotency_key,
        payload.model_dump_json(),
        lambda: tailor_for_catalogue(
            payload.content,
            list_entries(limit=100_000, offset=0),
            payload.destination_keys,
            payload.limit,
        ),
    )
    if replayed:
        response.status_code = status.HTTP_200_OK
    return result


@router.post("/recommendations", response_model=None)
def recommend(
    payload: RecommendationRequest,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
) -> dict:
    result, replayed = cache_idempotent_response(
        "recommendations",
        idempotency_key,
        payload.model_dump_json(),
        lambda: recommend_destinations(
            payload.content,
            payload.topics,
            list_entries(limit=100_000, offset=0),
            payload.limit,
        ),
    )
    if replayed:
        response.status_code = status.HTTP_200_OK
    return result


@router.get("/catalogue/export")
def export_catalogue() -> HttpResponse:
    return HttpResponse(
        content=export_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="destinations.csv"'},
    )


@router.get("/catalogue/imports/{import_id}")
def read_import_report(import_id: str) -> dict:
    report = get_import_report(import_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "CATALOGUE_IMPORT_NOT_FOUND",
                "message": "The requested catalogue import report does not exist.",
            },
        )
    return report


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _destination_from_row(row: sqlite3.Row) -> DestinationResponse:
    return DestinationResponse(
        key=row["key"],
        connector_key=row["connector_key"],
        rules=PostingRules.model_validate_json(row["rules_json"]),
        created_at=row["created_at"],
        enabled=bool(row["enabled"]),
        last_certified_at=row["last_certified_at"],
        certified_level=row["certified_level"],
    )


def _job_from_row(row: sqlite3.Row) -> JobResponse:
    return JobResponse(
        id=row["id"],
        destination_key=row["destination_key"],
        status=row["status"],
        progress=row["progress"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        result_url=f"/api/v1/jobs/{row['id']}/results",
        evidence_url=f"/api/v1/jobs/{row['id']}/evidence",
    )


@router.post(
    "/destinations",
    response_model=DestinationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_destination(
    payload: DestinationCreate,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
) -> DestinationResponse:
    created_at = _now().isoformat()
    request_hash = hashlib.sha256(payload.model_dump_json().encode("utf-8")).hexdigest()
    try:
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT request_hash, resource_id FROM idempotency_keys
                WHERE scope = 'destination' AND key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != request_hash:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "IDEMPOTENCY_KEY_REUSED",
                            "message": (
                                "This Idempotency-Key was already used for "
                                "a different destination request."
                            ),
                        },
                    )
                row = connection.execute(
                    "SELECT * FROM destinations WHERE key = ?",
                    (prior["resource_id"],),
                ).fetchone()
                if row is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "IDEMPOTENCY_RECORD_INVALID",
                            "message": "The idempotent destination record is unavailable.",
                        },
                    )
                response.status_code = status.HTTP_200_OK
                return _destination_from_row(row)

            if payload.connector_key not in load_manifests():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={
                        "code": "CONNECTOR_NOT_FOUND",
                        "message": "The requested connector is not registered.",
                    },
                )
            connection.execute(
                """
                INSERT INTO destinations
                    (key, connector_key, config_json, rules_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    payload.key,
                    payload.connector_key,
                    json.dumps(payload.config),
                    payload.rules.model_dump_json(),
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO idempotency_keys
                    (scope, key, request_hash, resource_id, created_at)
                VALUES ('destination', ?, ?, ?, ?)
                """,
                (idempotency_key, request_hash, payload.key, created_at),
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "DESTINATION_ALREADY_EXISTS",
                "message": "A destination with this key already exists.",
            },
        ) from exc

    return DestinationResponse(
        key=payload.key,
        connector_key=payload.connector_key,
        rules=payload.rules,
        created_at=created_at,
        enabled=True,
        last_certified_at=None,
        certified_level="L0",
    )


@router.get("/destinations", response_model=list[DestinationResponse])
def list_destinations() -> list[DestinationResponse]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM destinations ORDER BY key"
        ).fetchall()
    return [_destination_from_row(row) for row in rows]


@router.post(
    "/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_job(
    payload: JobCreate,
    background_tasks: BackgroundTasks,
    response: Response,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
) -> JobResponse:
    serialized_request = payload.model_dump_json()
    request_hash = hashlib.sha256(serialized_request.encode("utf-8")).hexdigest()
    now = _now()
    timestamp = now.isoformat()

    try:
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            destination = connection.execute(
                "SELECT rules_json, enabled FROM destinations WHERE key = ?",
                (payload.destination_key,),
            ).fetchone()
            if destination is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "code": "DESTINATION_NOT_FOUND",
                        "message": "The requested destination does not exist.",
                    },
                )
            if not destination["enabled"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "DESTINATION_DISABLED",
                        "message": "This destination is disabled pending review.",
                    },
                )

            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "IDEMPOTENCY_KEY_REUSED",
                            "message": (
                                "This Idempotency-Key was already used for "
                                "a different request."
                            ),
                        },
                    )
                response.status_code = status.HTTP_200_OK
                return _job_from_row(existing)

            try:
                rules = PostingRules.model_validate_json(destination["rules_json"])
            except (ValidationError, ValueError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={
                        "code": "DESTINATION_RULES_INVALID",
                        "message": "Destination posting rules are invalid.",
                    },
                ) from exc

            try:
                validate_content(rules, payload.content)
            except RuleViolation as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={"code": exc.code, "message": exc.message},
                ) from exc

            frequency_start = (
                now - timedelta(seconds=rules.frequency_window_seconds)
            ).isoformat()
            count = count_recent_posts(
                connection, payload.destination_key, frequency_start
            )
            if count >= rules.frequency_cap:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "code": "DEST_RULE_FREQUENCY_CAP",
                        "message": "The destination frequency cap is reached.",
                    },
                )

            job_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO jobs (
                    id, idempotency_key, request_hash, destination_key,
                    content_json, status, created_at, updated_at, correlation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    idempotency_key,
                    request_hash,
                    payload.destination_key,
                    payload.content.model_dump_json(),
                    "queued",
                    timestamp,
                    timestamp,
                    request.state.correlation_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "A job with this Idempotency-Key already exists.",
            },
        ) from exc

    background_tasks.add_task(process_job, job_id)
    return _job_from_row(row)


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "JOB_NOT_FOUND",
                "message": "The requested publication job does not exist.",
            },
        )
    return _job_from_row(row)


@router.post(
    "/jobs/{job_id}/retry",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
) -> JobResponse:
    retry_scope = f"job-retry:{job_id}"
    request_hash = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
    now = _now()
    timestamp = now.isoformat()
    try:
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT request_hash FROM idempotency_keys
                WHERE scope = ? AND key = ?
                """,
                (retry_scope, idempotency_key),
            ).fetchone()
            if prior is not None:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if prior["request_hash"] != request_hash or row is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "IDEMPOTENCY_KEY_REUSED",
                            "message": "The retry key cannot be reused for this request.",
                        },
                    )
                response.status_code = status.HTTP_200_OK
                return _job_from_row(row)

            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "code": "JOB_NOT_FOUND",
                        "message": "The requested publication job does not exist.",
                    },
                )
            if row["status"] != "failed" or not row["error_json"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "JOB_NOT_RETRYABLE",
                        "message": "Only failed jobs can be retried.",
                    },
                )
            error = json.loads(row["error_json"])
            details = error.get("details", {})
            if not error.get("retryable") or details.get("outcome_ambiguous"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "RETRY_UNSAFE",
                        "message": (
                            "The previous outcome cannot be safely retried; "
                            "reconcile it with the destination first."
                        ),
                    },
                )
            max_attempts = int(os.getenv("MAX_JOB_ATTEMPTS", "5"))
            if row["attempt_count"] >= max_attempts:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "RETRY_LIMIT_EXCEEDED",
                        "message": "The configured job retry limit has been reached.",
                    },
                )
            retry_after = max(0, int(details.get("retry_after_seconds", 0)))
            if (
                error.get("code") == "PLATFORM_QUOTA_EXHAUSTED"
                and "retry_after_seconds" not in details
            ):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "code": "PLATFORM_QUOTA_EXHAUSTED",
                        "message": (
                            "The platform quota is exhausted; retry only after "
                            "the next quota window."
                        ),
                        "details": {
                            "retry_after": "next_window",
                            "reset_time": details.get("reset_time"),
                        },
                    },
                )
            if retry_after:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "code": error.get("code", "PLATFORM_RATE_LIMIT"),
                        "message": (
                            "Wait for the platform quota reset before retrying."
                            if error.get("code") == "PLATFORM_QUOTA_EXHAUSTED"
                            else "Wait for the platform rate-limit reset before retrying."
                        ),
                        "details": {"retry_after_seconds": retry_after},
                    },
                )

            connection.execute(
                """
                UPDATE jobs SET status = 'queued', progress = 0,
                    error_json = NULL, result_json = NULL,
                    started_at = NULL, finished_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, job_id),
            )
            connection.execute(
                """
                INSERT INTO idempotency_keys
                    (scope, key, request_hash, resource_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (retry_scope, idempotency_key, request_hash, job_id, timestamp),
            )
            updated = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "This retry request has already been accepted.",
            },
        ) from exc

    background_tasks.add_task(process_job, job_id)
    return _job_from_row(updated)


@router.get("/jobs/{job_id}/results")
def get_job_results(job_id: str) -> dict:
    with connect() as connection:
        row = connection.execute(
            "SELECT status, result_json, error_json FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "JOB_NOT_FOUND",
                "message": "The requested publication job does not exist.",
            },
        )
    if row["result_json"]:
        return json.loads(row["result_json"])
    if row["error_json"]:
        return {"status": row["status"], "error": json.loads(row["error_json"])}
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "JOB_NOT_COMPLETE",
            "message": "The publication job has not produced a result yet.",
        },
    )


@router.get("/jobs/{job_id}/evidence")
def get_job_evidence(job_id: str) -> dict:
    with connect() as connection:
        row = connection.execute(
            "SELECT result_json FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "JOB_NOT_FOUND",
                "message": "The requested publication job does not exist.",
            },
        )
    if not row["result_json"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "JOB_EVIDENCE_NOT_READY",
                "message": "Publication evidence is not available yet.",
            },
        )
    result = json.loads(row["result_json"])
    return result["evidence"]


@router.post("/destinations/{destination_key}/certify", status_code=202)
def certify_destination(
    destination_key: str,
    background_tasks: BackgroundTasks,
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
) -> dict:
    now = _now()
    timestamp = now.isoformat()
    cleanup_deadline = (now + timedelta(minutes=10)).isoformat()
    scope = f"certification:{destination_key}"
    request_hash = hashlib.sha256(destination_key.encode("utf-8")).hexdigest()
    try:
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            destination = connection.execute(
                "SELECT config_json FROM destinations WHERE key = ?",
                (destination_key,),
            ).fetchone()
            if destination is None:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "DESTINATION_NOT_FOUND",
                        "message": "The requested destination does not exist.",
                    },
                )
            config = json.loads(destination["config_json"])
            if not simulation_enabled() and not real_certification_allowed(config):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "CERTIFICATION_TARGET_NOT_APPROVED",
                        "message": (
                            "Real test posts require an explicitly approved "
                            "owned instance or sandbox."
                        ),
                    },
                )
            prior = connection.execute(
                """
                SELECT request_hash, resource_id FROM idempotency_keys
                WHERE scope = ? AND key = ?
                """,
                (scope, idempotency_key),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != request_hash:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "IDEMPOTENCY_KEY_REUSED",
                            "message": "The certification key was used for another request.",
                        },
                    )
                row = connection.execute(
                    "SELECT * FROM certification_runs WHERE id = ?",
                    (prior["resource_id"],),
                ).fetchone()
                response.status_code = 200
                return _certification_response(row)

            run_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO certification_runs
                    (id, destination_key, status, cleanup_deadline,
                     correlation_id, created_at, updated_at)
                VALUES (?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    run_id,
                    destination_key,
                    cleanup_deadline,
                    request.state.correlation_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO idempotency_keys
                    (scope, key, request_hash, resource_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (scope, idempotency_key, request_hash, run_id, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM certification_runs WHERE id = ?", (run_id,)
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "This certification request has already been accepted.",
            },
        ) from exc

    background_tasks.add_task(run_certification, run_id)
    return _certification_response(row)


@router.get("/certifications/{run_id}")
def get_certification(run_id: str) -> dict:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM certification_runs WHERE id = ?", (run_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "CERTIFICATION_NOT_FOUND",
                "message": "The requested certification run does not exist.",
            },
        )
    return _certification_response(row)


@router.get("/alerts")
def list_alerts(
    unresolved_only: bool = True,
    limit: int = 100,
) -> list[dict]:
    if limit < 1 or limit > 1000:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PAGINATION_INVALID",
                "message": "limit must be between 1 and 1000.",
            },
        )
    query = (
        "SELECT id, destination_key, certification_run_id, error_code, "
        "message, created_at, resolved_at FROM recertification_alerts"
    )
    if unresolved_only:
        query += " WHERE resolved_at IS NULL"
    query += " ORDER BY created_at DESC LIMIT ?"
    with connect() as connection:
        rows = connection.execute(query, (limit,)).fetchall()
    return [dict(row) for row in rows]


def _certification_response(row: sqlite3.Row) -> dict:
    result = json.loads(row["result_json"]) if row["result_json"] else None
    error = json.loads(row["error_json"]) if row["error_json"] else None
    return {
        "id": row["id"],
        "destination_key": row["destination_key"],
        "status": row["status"],
        "cleanup_deadline": row["cleanup_deadline"],
        "result": result,
        "error": error,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

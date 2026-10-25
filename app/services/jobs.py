import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from app.api.models import PostingRules, PublicationContent
from app.connectors.errors import ConnectorError
from app.connectors.implementations import CONNECTOR_TYPES
from app.connectors.manifests import load_manifests
from app.services.credentials import resolve_connection
from app.services.content import tailor_rendition
from app.services.rules import (
    RuleViolation,
    count_recent_posts,
    validate_content,
)
from app.storage.database import connect


logger = logging.getLogger("incubrix.jobs")


def process_job(job_id: str) -> None:
    started = time.perf_counter()
    timestamp = datetime.now(timezone.utc).isoformat()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT jobs.*, destinations.connector_key, destinations.config_json,
                   destinations.rules_json, destinations.enabled,
                   destinations.last_certified_at, destinations.certified_level
            FROM jobs
            JOIN destinations ON destinations.key = jobs.destination_key
            WHERE jobs.id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None or row["status"] != "queued":
            return
        connection.execute(
            """
            UPDATE jobs SET status = 'running', progress = 5,
                attempt_count = attempt_count + 1, started_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (timestamp, timestamp, job_id),
        )

    provider_attempted = False
    try:
        if not row["enabled"]:
            raise ConnectorError(
                "DESTINATION_DISABLED",
                "This destination is disabled pending review.",
                http_status=409,
            )
        rules = PostingRules.model_validate_json(row["rules_json"])
        content = PublicationContent.model_validate_json(row["content_json"])
        destination_config = json.loads(row["config_json"])
        if not isinstance(destination_config, dict):
            raise ConnectorError(
                "DESTINATION_CONFIG_INVALID",
                "Destination configuration must be a JSON object.",
                http_status=422,
            )

        with connect() as connection:
            previous_count = count_recent_posts(
                connection,
                row["destination_key"],
                (
                    datetime.now(timezone.utc)
                    - timedelta(seconds=rules.frequency_window_seconds)
                ).isoformat(),
                exclude_job_id=job_id,
            )
        if previous_count >= rules.frequency_cap:
            raise ConnectorError(
                "DEST_RULE_FREQUENCY_CAP",
                "The destination frequency cap is reached; retry after the cap window.",
                retryable=True,
                http_status=429,
                details={
                    "frequency_cap": rules.frequency_cap,
                    "frequency_window_seconds": rules.frequency_window_seconds,
                },
            )

        validate_content(rules, content)
        manifest = load_manifests()[row["connector_key"]]
        connector = CONNECTOR_TYPES[row["connector_key"]](manifest)
        destination = {
            "key": row["destination_key"],
            "connector_key": row["connector_key"],
            **destination_config,
        }
        connection_config = resolve_connection(destination_config)
        rendition, tailoring_warnings = tailor_rendition(
            content, manifest, destination
        )
        rendition.update(
            {
                "ai_generated": content.ai_generated,
                "ai_disclosure": content.ai_disclosure,
            }
        )
        validation_started = time.perf_counter()
        connector_validation = connector.validate(
            rendition, destination, connection_config
        )
        validation_ms = int((time.perf_counter() - validation_started) * 1000)
        if not connector_validation["ok"]:
            raise ConnectorError(
                "DEST_SPEC_VIOLATION",
                "Rendition does not meet destination format requirements.",
                http_status=422,
                details={"violations": connector_validation["violations"]},
            )

        with connect() as connection:
            connection.execute(
                "UPDATE jobs SET progress = 35, updated_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )

        simulation = os.getenv("SIMULATION_MODE", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not simulation:
            last_certified = row["last_certified_at"]
            certification_days = int(os.getenv("RECERTIFICATION_DAYS", "30"))
            if not last_certified or datetime.fromisoformat(
                last_certified
            ) < datetime.now(timezone.utc) - timedelta(days=certification_days):
                raise ConnectorError(
                    "DESTINATION_NOT_CERTIFIED",
                    "A current successful certification is required before publishing.",
                    http_status=409,
                )
        publish_started = time.perf_counter()
        if row["connector_key"] == "assisted":
            connector_result = {
                "platform_post_id": None,
                "live_url": destination.get("submit_url"),
                "status": "requires_manual_action",
                "assist_pack": connector.build_assist_pack(rendition, destination),
            }
        else:
            provider_attempted = True
            connector_result = connector.publish(
                rendition,
                destination,
                connection_config,
                row["idempotency_key"],
            )
        publish_ms = int((time.perf_counter() - publish_started) * 1000)
        completed_at = datetime.now(timezone.utc).isoformat()
        result = {
            "publication": connector_result,
            "evidence": {
                "connector_key": row["connector_key"],
                "destination_key": row["destination_key"],
                "simulation_mode": simulation,
                "provider_attempted": row["connector_key"] != "assisted",
                "observed_at": completed_at,
                "is_real_publication": not simulation
                and connector_result["status"] != "requires_manual_action",
            },
            "manifest": {
                "providers": [
                    {"key": manifest.key, "version": manifest.version}
                ],
                "models": [],
                "settings": {
                    "simulation_mode": simulation,
                    "connector_timeout_seconds": os.getenv(
                        "CONNECTOR_TIMEOUT_SECONDS", "10"
                    ),
                },
                "seed": None,
                "timings_ms": {
                    "validation": validation_ms,
                    "publication": publish_ms,
                    "total": int((time.perf_counter() - started) * 1000),
                },
                "cost_estimate": 0 if simulation else None,
                "warnings": tailoring_warnings
                + (
                    ["Simulated result; no platform post was created."]
                    if simulation
                    else []
                ),
                "licence_rights": {
                    "status": "Content rights were not independently verified."
                },
            },
        }
        with connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = 'succeeded', progress = 100,
                    result_json = ?, error_json = NULL, finished_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(result, ensure_ascii=False),
                    completed_at,
                    completed_at,
                    job_id,
                ),
            )
        logger.info(
            "job_succeeded",
            extra={
                "job_id": job_id,
                "correlation_id": row["correlation_id"],
                "connector_key": row["connector_key"],
                "simulation_mode": simulation,
            },
        )
    except ConnectorError as exc:
        _fail_job(
            job_id,
            exc.code,
            exc.message,
            exc.retryable,
            exc.details,
            provider_attempted,
        )
        logger.warning(
            "job_failed",
            extra={
                "job_id": job_id,
                "correlation_id": row["correlation_id"],
                "error_code": exc.code,
            },
        )
    except RuleViolation as exc:
        _fail_job(
            job_id, exc.code, exc.message, False, {}, provider_attempted
        )
        logger.warning(
            "job_blocked_by_rule",
            extra={
                "job_id": job_id,
                "correlation_id": row["correlation_id"],
                "error_code": exc.code,
            },
        )
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        _fail_job(
            job_id,
            "DESTINATION_CONFIG_INVALID",
            "Job configuration or stored content is invalid.",
            False,
            {},
            provider_attempted,
        )
        logger.error(
            "job_configuration_failed",
            extra={
                "job_id": job_id,
                "correlation_id": row["correlation_id"],
                "failure_type": type(exc).__name__,
            },
        )
    except Exception as exc:
        _fail_job(
            job_id,
            "INTERNAL_ERROR",
            "The publication job failed unexpectedly.",
            False,
            {},
            provider_attempted,
        )
        logger.error(
            "job_unexpected_failure",
            extra={
                "job_id": job_id,
                "correlation_id": row["correlation_id"],
                "failure_type": type(exc).__name__,
            },
        )


def _fail_job(
    job_id: str,
    code: str,
    message: str,
    retryable: bool,
    details: dict[str, Any],
    provider_attempted: bool,
) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    simulation = os.getenv("SIMULATION_MODE", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    error_result = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "details": details,
    }
    result = {
        "error": error_result,
        "evidence": {
            "simulation_mode": simulation,
            "provider_attempted": provider_attempted,
            "is_real_publication": None if provider_attempted and not simulation else False,
            "observed_at": timestamp,
        },
        "manifest": {
            "providers": [],
            "models": [],
            "settings": {"simulation_mode": simulation},
            "seed": None,
            "timings_ms": {},
            "cost_estimate": 0 if simulation else None,
            "warnings": ["Job did not complete successfully."],
            "licence_rights": {
                "status": "Content rights were not independently verified."
            },
        },
    }
    with connect() as connection:
        connection.execute(
            """
            UPDATE jobs SET status = 'failed', progress = 100,
                error_json = ?, result_json = ?, finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(error_result, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                timestamp,
                timestamp,
                job_id,
            ),
        )

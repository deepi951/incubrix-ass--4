import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import ValidationError

from app.api.models import PostingRules, PublicationContent
from app.catalogue.models import normalize_url
from app.connectors.errors import ConnectorError
from app.connectors.implementations import CONNECTOR_TYPES
from app.connectors.manifests import load_manifests
from app.services.credentials import resolve_connection
from app.services.rules import (
    RuleViolation,
    count_recent_posts,
    validate_content,
)
from app.storage.database import connect


logger = logging.getLogger("incubrix.certification")


def simulation_enabled() -> bool:
    return os.getenv("SIMULATION_MODE", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def real_certification_allowed(config: dict[str, Any]) -> bool:
    if not (
        config.get("test_environment") is True
        and config.get("sandbox_confirmed") is True
        and os.getenv("AUTOMATED_TEST_POSTS_ENABLED", "").lower() == "true"
    ):
        return False
    return real_cleanup_target_allowed(config)


def real_cleanup_target_allowed(config: dict[str, Any]) -> bool:
    if not (
        config.get("test_environment") is True
        and config.get("sandbox_confirmed") is True
    ):
        return False
    base_url = str(config.get("base_url") or config.get("url") or "")
    host = urlsplit(base_url).hostname
    if not host:
        return False
    configured_hosts = {
        item.strip().lower()
        for item in os.getenv(
            "CERTIFICATION_LOCAL_HOSTS",
            "localhost,127.0.0.1,::1,lemmy,ghost",
        ).split(",")
        if item.strip()
    }
    normalized_host = host.lower()
    try:
        is_loopback = ip_address(normalized_host).is_loopback
    except ValueError:
        is_loopback = False
    if is_loopback or normalized_host in configured_hosts:
        return True
    return os.getenv("ALLOW_EXTERNAL_SANDBOX_CERTIFICATION", "").lower() == "true"


def run_certification(run_id: str) -> None:
    now = datetime.now(timezone.utc)
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        run = connection.execute(
            """
            SELECT certification_runs.*, destinations.connector_key,
                   destinations.config_json, destinations.rules_json
            FROM certification_runs
            JOIN destinations ON destinations.key =
                certification_runs.destination_key
            WHERE certification_runs.id = ?
            """,
            (run_id,),
        ).fetchone()
        if run is None or run["status"] != "queued":
            return
        connection.execute(
            "UPDATE certification_runs SET status = 'running', updated_at = ? WHERE id = ?",
            (now.isoformat(), run_id),
        )

    simulation = simulation_enabled()
    result: dict[str, Any] = {
        "classification": "simulation_only" if simulation else "local_integration",
        "connector_key": run["connector_key"],
        "destination_key": run["destination_key"],
        "started_at": now.isoformat(),
        "steps": [],
        "cleanup": {
            "required_by": run["cleanup_deadline"],
            "attempted": False,
            "deleted": False,
        },
    }
    failure: ConnectorError | None = None
    platform_post_id: str | None = None
    began_publish = False
    try:
        destination_config = json.loads(run["config_json"])
        if not isinstance(destination_config, dict):
            raise ConnectorError(
                "DESTINATION_CONFIG_INVALID",
                "Destination configuration must be a JSON object.",
                http_status=422,
            )
        if not simulation and not real_certification_allowed(destination_config):
            raise ConnectorError(
                "CERTIFICATION_TARGET_NOT_APPROVED",
                "Real certification requires an explicitly approved owned instance or sandbox.",
                http_status=403,
            )

        rules = PostingRules.model_validate_json(run["rules_json"])
        content = PublicationContent(
            title="Temporary connector certification test",
            text="Automated test post. This content is temporary and will be deleted.",
        )
        since = (
            datetime.now(timezone.utc)
            - timedelta(seconds=rules.frequency_window_seconds)
        ).isoformat()
        with connect() as connection:
            recent_posts = count_recent_posts(
                connection,
                run["destination_key"],
                since,
                exclude_certification_id=run_id,
            )
        if recent_posts >= rules.frequency_cap:
            raise ConnectorError(
                "DEST_RULE_FREQUENCY_CAP",
                "The destination frequency cap prevents certification publication.",
                retryable=True,
                http_status=429,
                details={
                    "frequency_window_seconds": rules.frequency_window_seconds
                },
            )
        validate_content(rules, content)
        manifest = load_manifests()[run["connector_key"]]
        connector = CONNECTOR_TYPES[run["connector_key"]](manifest)
        destination = {
            "key": run["destination_key"],
            "connector_key": run["connector_key"],
            **destination_config,
        }
        connection_config = resolve_connection(destination_config)
        rendition = {
            "title": content.title,
            "body": content.text,
            "text": content.text,
            "content_type": "text",
        }
        validation = connector.validate(rendition, destination, connection_config)
        result["steps"].append(
            {
                "method": "validate",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "result": validation,
            }
        )
        if not validation["ok"]:
            raise ConnectorError(
                "DEST_SPEC_VIOLATION",
                "Certification content does not meet destination requirements.",
                http_status=422,
                details={"violations": validation["violations"]},
            )

        if run["connector_key"] == "assisted":
            pack = connector.build_assist_pack(rendition, destination)
            result["steps"].append(
                {
                    "method": "build_assist_pack",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "result": pack,
                }
            )
            result["manual_action_required"] = True
            result["classification"] = "assisted_pack_simulation"
        else:
            began_publish = True
            publication = connector.publish(
                rendition,
                destination,
                connection_config,
                f"certification:{run_id}",
            )
            platform_post_id = publication.get("platform_post_id")
            if platform_post_id:
                with connect() as connection:
                    connection.execute(
                        "UPDATE certification_runs SET test_post_id = ?, "
                        "updated_at = ? WHERE id = ?",
                        (
                            str(platform_post_id),
                            datetime.now(timezone.utc).isoformat(),
                            run_id,
                        ),
                    )
            result["steps"].append(
                {
                    "method": "publish",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "result": publication,
                }
            )
            if not platform_post_id:
                raise ConnectorError(
                    "PLATFORM_UNAVAILABLE",
                    "Certification publication did not return a post ID.",
                    retryable=True,
                    details={"outcome_ambiguous": True},
                )
            status_result = connector.get_status(
                str(platform_post_id), connection_config
            )
            result["steps"].append(
                {
                    "method": "get_status",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "result": status_result,
                }
            )
            if status_result.get("status") not in {"published", "live", "succeeded"}:
                raise ConnectorError(
                    "PLATFORM_UNAVAILABLE",
                    "The test post could not be confirmed as live.",
                    retryable=True,
                    details={"outcome_ambiguous": True},
                )
            result["test_post"] = {
                "platform_post_id": str(platform_post_id),
                "live_url": status_result.get("live_url"),
                "confirmed_live_at": datetime.now(timezone.utc).isoformat(),
            }
    except ConnectorError as exc:
        failure = exc
    except RuleViolation as exc:
        failure = ConnectorError(exc.code, exc.message, http_status=422)
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        failure = ConnectorError(
            "DESTINATION_CONFIG_INVALID",
            "Certification configuration or stored rules are invalid.",
            http_status=422,
            details={"failure_type": type(exc).__name__},
        )
    except Exception as exc:
        failure = ConnectorError(
            "INTERNAL_ERROR",
            "Certification failed unexpectedly.",
            http_status=500,
            details={"failure_type": type(exc).__name__},
        )
        logger.error(
            "certification_unexpected_failure",
            extra={
                "job_id": run_id,
                "correlation_id": run["correlation_id"],
                "failure_type": type(exc).__name__,
            },
        )
    finally:
        if platform_post_id is not None:
            result["cleanup"]["attempted"] = True
            connector = CONNECTOR_TYPES[run["connector_key"]](
                load_manifests()[run["connector_key"]]
            )
            connection_config = resolve_connection(
                json.loads(run["config_json"])
            )
            cleanup_result = _delete_within_deadline(
                connector,
                str(platform_post_id),
                connection_config,
                datetime.fromisoformat(run["cleanup_deadline"]),
            )
            result["cleanup"].update(cleanup_result)
            with connect() as connection:
                connection.execute(
                    "UPDATE certification_runs SET result_json = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        json.dumps(result, ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat(),
                        run_id,
                    ),
                )
            result["steps"].append(
                {
                    "method": "delete",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "result": cleanup_result,
                }
            )
            if (
                (not cleanup_result["deleted"] or not cleanup_result["on_time"])
                and failure is None
            ):
                failure = ConnectorError(
                    "CERTIFICATION_CLEANUP_FAILED",
                    "The certification test post was not confirmed deleted within the cleanup deadline.",
                    retryable=True,
                    details=cleanup_result,
                )
        elif began_publish:
            result["cleanup"].update(
                {"attempted": False, "status": "outcome_unknown"}
            )

    completed = datetime.now(timezone.utc).isoformat()
    result["completed_at"] = completed
    result["elapsed_ms"] = int(
        (datetime.fromisoformat(completed) - now).total_seconds() * 1000
    )
    if failure:
        result["status"] = "failed"
        error_json = json.dumps(
            {
                "code": failure.code,
                "message": failure.message,
                "retryable": failure.retryable,
                "details": failure.details,
            },
            ensure_ascii=False,
        )
        status_value = "failed"
        logger.warning(
            "certification_failed",
            extra={
                "job_id": run_id,
                "correlation_id": run["correlation_id"],
                "error_code": failure.code,
            },
        )
    else:
        result["status"] = "succeeded"
        error_json = None
        status_value = "succeeded"
        logger.info(
            "certification_succeeded",
            extra={
                "job_id": run_id,
                "correlation_id": run["correlation_id"],
                "connector_key": run["connector_key"],
                "simulation_mode": simulation,
            },
        )

    with connect() as connection:
        connection.execute(
            """
            UPDATE certification_runs SET status = ?, result_json = ?,
                error_json = ?, updated_at = ? WHERE id = ?
            """,
            (status_value, json.dumps(result, ensure_ascii=False), error_json, completed, run_id),
        )
        if not simulation and run["connector_key"] != "assisted":
            if failure is None:
                level = "L3"
                connection.execute(
                    """
                    UPDATE destinations SET enabled = 1, last_certified_at = ?,
                        certified_level = ? WHERE key = ?
                    """,
                    (completed, level, run["destination_key"]),
                )
            else:
                level = "L0"
                connection.execute(
                    """
                    UPDATE destinations SET enabled = 0, certified_level = ?
                    WHERE key = ?
                    """,
                    (level, run["destination_key"]),
                )
            _update_matching_catalogue_record(
                connection,
                run["connector_key"],
                json.loads(run["config_json"]),
                level,
                completed if failure is None else None,
            )
            if failure is None:
                connection.execute(
                    """
                    UPDATE recertification_alerts SET resolved_at = ?
                    WHERE destination_key = ? AND resolved_at IS NULL
                    """,
                    (completed, run["destination_key"]),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO recertification_alerts
                        (id, destination_key, certification_run_id, error_code,
                         message, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        run["destination_key"],
                        run_id,
                        failure.code,
                        failure.message,
                        completed,
                    ),
                )


def cleanup_interrupted_certifications() -> int:
    with connect() as connection:
        runs = connection.execute(
            """
            SELECT certification_runs.*, destinations.connector_key,
                   destinations.config_json
            FROM certification_runs
            JOIN destinations ON destinations.key =
                certification_runs.destination_key
            WHERE certification_runs.status = 'running'
            """
        ).fetchall()

    cleaned = 0
    for run in runs:
        now = datetime.now(timezone.utc).isoformat()
        result = json.loads(run["result_json"]) if run["result_json"] else {
            "classification": "unknown",
            "steps": [],
            "cleanup": {},
        }
        post_id = run["test_post_id"]
        is_simulation = result.get("classification") in {
            "simulation_only",
            "assisted_pack_simulation",
        }
        cleanup = (
            {
                "attempted": False,
                "deleted": None,
                "status": "simulation_no_remote_post",
            }
            if is_simulation
            else {
                "attempted": bool(post_id),
                "deleted": False,
                "status": "outcome_unknown" if not post_id else "cleanup_failed",
            }
        )
        error_code = "CERTIFICATION_INTERRUPTED"
        error_message = (
            "The certification worker stopped; reconcile the destination before retrying."
        )
        if post_id and not is_simulation:
            try:
                config = json.loads(run["config_json"])
                if not real_cleanup_target_allowed(config):
                    raise ConnectorError(
                        "CERTIFICATION_CLEANUP_BLOCKED",
                        "Cleanup target is no longer explicitly approved.",
                        http_status=403,
                    )
                connector = CONNECTOR_TYPES[run["connector_key"]](
                    load_manifests()[run["connector_key"]]
                )
                connector_result = _delete_within_deadline(
                    connector,
                    str(post_id),
                    resolve_connection(config),
                    datetime.fromisoformat(run["cleanup_deadline"]),
                    delete_operation=lambda post_id, connection: connector._delete_real(
                        post_id, connection
                    ),
                )
                cleanup.update(connector_result)
                if cleanup["deleted"]:
                    cleaned += 1
            except Exception as exc:
                error_code = (
                    exc.code if isinstance(exc, ConnectorError) else "CERTIFICATION_CLEANUP_FAILED"
                )
                error_message = (
                    exc.message
                    if isinstance(exc, ConnectorError)
                    else "The interrupted test post could not be deleted."
                )
                cleanup["error_code"] = error_code
                logger.error(
                    "interrupted_certification_cleanup_failed",
                    extra={
                        "job_id": run["id"],
                        "correlation_id": run["correlation_id"],
                        "error_code": error_code,
                        "failure_type": type(exc).__name__,
                    },
                )

        result["cleanup"] = {**result.get("cleanup", {}), **cleanup}
        result["status"] = "failed"
        result["completed_at"] = now
        error_json = json.dumps(
            {
                "code": error_code,
                "message": error_message,
                "retryable": False,
                "details": {"cleanup": cleanup},
            },
            ensure_ascii=False,
        )
        with connect() as connection:
            connection.execute(
                """
                UPDATE certification_runs SET status = 'failed', result_json = ?,
                    error_json = ?, updated_at = ? WHERE id = ?
                """,
                (json.dumps(result, ensure_ascii=False), error_json, now, run["id"]),
            )
            if result.get("classification") == "local_integration":
                connection.execute(
                    """
                    UPDATE destinations SET enabled = 0, certified_level = 'L0'
                    WHERE key = ?
                    """,
                    (run["destination_key"],),
                )
        logger.error(
            "certification_interrupted",
            extra={
                "job_id": run["id"],
                "correlation_id": run["correlation_id"],
                "error_code": error_code,
            },
        )
    return cleaned


def schedule_due_recertifications() -> int:
    if (
        simulation_enabled()
        or os.getenv("RECERTIFICATION_ENABLED", "").lower() != "true"
    ):
        return 0
    now = datetime.now(timezone.utc)
    certification_days = max(1, int(os.getenv("RECERTIFICATION_DAYS", "30")))
    lead_days = min(
        certification_days,
        max(0, int(os.getenv("RECERTIFICATION_LEAD_DAYS", "1"))),
    )
    cutoff = (
        now - timedelta(days=max(0, certification_days - lead_days))
    ).isoformat()
    with connect() as connection:
        destinations = connection.execute(
            """
            SELECT key, connector_key, config_json FROM destinations
            WHERE enabled = 1 AND last_certified_at IS NOT NULL
              AND last_certified_at <= ?
            """,
            (cutoff,),
        ).fetchall()

    created = 0
    for destination in destinations:
        config = json.loads(destination["config_json"])
        if not real_certification_allowed(config):
            _disable_uncertified_destination(destination, config, now)
            continue
        run_id = str(uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        scope = f"recertification:{destination['key']}"
        key = now.date().isoformat()
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT 1 FROM certification_runs
                WHERE destination_key = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (destination["key"],),
            ).fetchone()
            if active:
                continue
            prior = connection.execute(
                "SELECT 1 FROM idempotency_keys WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
            if prior:
                continue
            connection.execute(
                """
                INSERT INTO certification_runs
                    (id, destination_key, status, cleanup_deadline, created_at,
                     updated_at)
                VALUES (?, ?, 'queued', ?, ?, ?)
                """,
                (
                    run_id,
                    destination["key"],
                    (now + timedelta(minutes=10)).isoformat(),
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
                (
                    scope,
                    key,
                    destination["key"],
                    run_id,
                    timestamp,
                ),
            )
        run_certification(run_id)
        created += 1
    return created


def _disable_uncertified_destination(destination, config, now: datetime) -> None:
    error_code = "RECERTIFICATION_NOT_AUTHORIZED"
    message = (
        "The destination passed its recertification due date, but no approved "
        "owned test instance or sandbox is configured."
    )
    with connect() as connection:
        connection.execute(
            """
            UPDATE destinations SET enabled = 0, certified_level = 'L0'
            WHERE key = ?
            """,
            (destination["key"],),
        )
        _update_matching_catalogue_record(
            connection,
            destination["connector_key"],
            config,
            "L0",
            None,
        )
        connection.execute(
            """
            INSERT INTO recertification_alerts
                (id, destination_key, certification_run_id, error_code,
                 message, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                destination["key"],
                None,
                error_code,
                message,
                now.isoformat(),
            ),
        )
    logger.error(
        "recertification_not_authorized",
        extra={
            "connector_key": destination["connector_key"],
            "error_code": error_code,
        },
    )


def _update_matching_catalogue_record(
    connection,
    connector_key: str,
    destination_config: dict[str, Any],
    level: str,
    certified_at: str | None,
) -> None:
    target_url = destination_config.get("base_url") or destination_config.get("url")
    if not isinstance(target_url, str):
        return
    try:
        normalized_target = normalize_url(target_url)
    except ValueError:
        return
    records = connection.execute(
        """
        SELECT normalized_url, platform_key, record_json
        FROM catalogue_destinations
        WHERE platform_key = ? OR platform_key = ?
        """,
        (connector_key, connector_key.replace("devto", "dev.to")),
    ).fetchall()
    for record in records:
        if record["normalized_url"] != normalized_target:
            continue
        catalogue_entry = json.loads(record["record_json"])
        catalogue_entry["level"] = level
        catalogue_entry["last_certified_at"] = certified_at
        catalogue_entry["automation_allowed"] = level in {"L2", "L3", "L4"}
        connection.execute(
            """
            UPDATE catalogue_destinations SET record_json = ?, updated_at = ?
            WHERE normalized_url = ? AND platform_key = ?
            """,
            (
                json.dumps(catalogue_entry, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
                record["normalized_url"],
                record["platform_key"],
            ),
        )


def _delete_within_deadline(
    connector,
    platform_post_id: str,
    connection: dict[str, Any],
    deadline: datetime,
    *,
    delete_operation=None,
) -> dict[str, Any]:
    attempts = max(1, int(os.getenv("CERTIFICATION_CLEANUP_ATTEMPTS", "3")))
    last_error: ConnectorError | None = None
    configured_timeout = float(os.getenv("CONNECTOR_TIMEOUT_SECONDS", "10"))
    delete = delete_operation or connector.delete
    for attempt in range(attempts):
        now = datetime.now(timezone.utc)
        remaining = (deadline - now).total_seconds()
        if attempt > 0 and remaining <= 0:
            break

        previous_timeout_limit = connector._request_timeout_limit_seconds
        connector._request_timeout_limit_seconds = (
            min(configured_timeout, remaining)
            if remaining > 0
            else configured_timeout
        )
        try:
            deleted = delete(platform_post_id, connection)
            if deleted.get("deleted"):
                deleted_at = datetime.now(timezone.utc)
                on_time = deleted_at <= deadline
                return {
                    "deleted": True,
                    "status": "deleted" if on_time else "deleted_after_deadline",
                    "attempts": attempt + 1,
                    "deleted_at": deleted_at.isoformat(),
                    "on_time": on_time,
                }
            return {
                "deleted": False,
                "status": "delete_not_confirmed",
                "attempts": attempt + 1,
                "on_time": False,
            }
        except ConnectorError as exc:
            last_error = exc
            if not exc.retryable or attempt + 1 == attempts:
                break
            remaining = (
                deadline - datetime.now(timezone.utc)
            ).total_seconds()
            if remaining <= 0:
                break
            retry_after = max(0, int(exc.details.get("retry_after_seconds", 0)))
            time.sleep(min(retry_after or (2**attempt), remaining))
        except Exception as exc:
            logger.error(
                "certification_cleanup_unexpected_failure",
                extra={
                    "failure_type": type(exc).__name__,
                    "error_code": "CERTIFICATION_CLEANUP_FAILED",
                },
            )
            return {
                "deleted": False,
                "status": "cleanup_failed",
                "attempts": attempt + 1,
                "error_code": "CERTIFICATION_CLEANUP_FAILED",
                "failure_type": type(exc).__name__,
                "on_time": False,
            }
        finally:
            connector._request_timeout_limit_seconds = previous_timeout_limit
    return {
        "deleted": False,
        "status": "cleanup_failed",
        "attempts": min(attempts, attempt + 1),
        "error_code": last_error.code if last_error else "CERTIFICATION_CLEANUP_FAILED",
        "cleanup_deadline_passed": datetime.now(timezone.utc) >= deadline,
        "on_time": False,
    }

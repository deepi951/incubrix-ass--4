import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def connect() -> sqlite3.Connection:
    database_path = Path(os.getenv("DATABASE_PATH", "data/incubrix.db"))
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database() -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS destinations (
                key TEXT PRIMARY KEY,
                connector_key TEXT NOT NULL,
                config_json TEXT NOT NULL,
                rules_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_certified_at TEXT,
                certified_level TEXT NOT NULL DEFAULT 'L0'
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                destination_key TEXT NOT NULL REFERENCES destinations(key),
                content_json TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                error_json TEXT,
                started_at TEXT,
                finished_at TEXT,
                correlation_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_destination_created
                ON jobs(destination_key, created_at);
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                scope TEXT NOT NULL,
                key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (scope, key)
            );
            CREATE TABLE IF NOT EXISTS catalogue_destinations (
                normalized_url TEXT NOT NULL,
                platform_key TEXT NOT NULL,
                record_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (normalized_url, platform_key)
            );
            CREATE TABLE IF NOT EXISTS catalogue_imports (
                id TEXT PRIMARY KEY,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS idempotent_responses (
                scope TEXT NOT NULL,
                key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (scope, key)
            );
            CREATE TABLE IF NOT EXISTS certification_runs (
                id TEXT PRIMARY KEY,
                destination_key TEXT NOT NULL REFERENCES destinations(key),
                status TEXT NOT NULL,
                result_json TEXT,
                error_json TEXT,
                cleanup_deadline TEXT NOT NULL,
                test_post_id TEXT,
                correlation_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recertification_alerts (
                id TEXT PRIMARY KEY,
                destination_key TEXT NOT NULL REFERENCES destinations(key),
                certification_run_id TEXT REFERENCES certification_runs(id),
                error_code TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            """
        )
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        destination_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(destinations)"
            ).fetchall()
        }
        destination_migrations = {
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "last_certified_at": "TEXT",
            "certified_level": "TEXT NOT NULL DEFAULT 'L0'",
        }
        for column, definition in destination_migrations.items():
            if column not in destination_columns:
                connection.execute(
                    f"ALTER TABLE destinations ADD COLUMN {column} {definition}"
                )
        migrations = {
            "progress": "INTEGER NOT NULL DEFAULT 0",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "result_json": "TEXT",
            "error_json": "TEXT",
            "started_at": "TEXT",
            "finished_at": "TEXT",
            "correlation_id": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in existing_columns:
                connection.execute(
                    f"ALTER TABLE jobs ADD COLUMN {column} {definition}"
                )
        certification_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(certification_runs)"
            ).fetchall()
        }
        if "test_post_id" not in certification_columns:
            connection.execute(
                "ALTER TABLE certification_runs ADD COLUMN test_post_id TEXT"
            )


def fail_stale_jobs(stale_after_seconds: int) -> int:
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=stale_after_seconds)).isoformat()
    finished_at = now.isoformat()
    error = (
        '{"code":"PLATFORM_UNAVAILABLE","message":"The worker stopped before '
        'recording a result; reconcile with the destination before retrying.",'
        '"retryable":true,"details":{"outcome_ambiguous":true}}'
    )
    with connect() as connection:
        cursor = connection.execute(
            """
            UPDATE jobs SET status = 'failed', progress = 100, error_json = ?,
                finished_at = ?, updated_at = ?
            WHERE status = 'running' AND started_at IS NOT NULL AND started_at < ?
            """,
            (error, finished_at, finished_at, cutoff),
        )
        return cursor.rowcount

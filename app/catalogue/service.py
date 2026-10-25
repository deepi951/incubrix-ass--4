import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import ValidationError

from app.catalogue.models import (
    CATALOGUE_FIELDS,
    CatalogueEntry,
    normalize_url,
)
from app.storage.database import connect


def import_csv(csv_text: str, idempotency_key: str) -> tuple[dict, bool]:
    request_hash = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    report_id = str(uuid4())
    imported = 0
    duplicates = 0
    errors: list[dict] = []
    warnings: list[dict] = []
    rows: list[tuple[int, CatalogueEntry, str]] = []

    try:
        reader = csv.DictReader(io.StringIO(csv_text, newline=""), strict=True)
        headers = reader.fieldnames or []
    except csv.Error as exc:
        headers = []
        errors.append(
            {"row": 1, "code": "CSV_PARSE_ERROR", "message": str(exc)}
        )

    missing_fields = sorted(set(CATALOGUE_FIELDS) - set(headers))
    unexpected_fields = sorted(set(headers) - set(CATALOGUE_FIELDS))
    if not headers and not errors:
        errors.append(
            {
                "row": 1,
                "code": "CSV_HEADER_MISSING",
                "message": "The CSV file must include the required header row.",
            }
        )
    if headers and missing_fields:
        errors.append(
            {
                "row": 1,
                "code": "CSV_REQUIRED_FIELDS_MISSING",
                "message": f"Missing columns: {', '.join(missing_fields)}",
            }
        )
    if unexpected_fields:
        warnings.append(
            {
                "code": "CSV_EXTRA_FIELDS_IGNORED",
                "fields": unexpected_fields,
            }
        )

    rows_total = 0
    if headers and not missing_fields:
        try:
            for row_number, raw_row in enumerate(reader, start=2):
                rows_total += 1
                if None in raw_row:
                    errors.append(
                        {
                            "row": row_number,
                            "code": "CSV_COLUMN_COUNT_MISMATCH",
                            "message": "The row has more values than the header.",
                        }
                    )
                    continue
                normalized_row = {
                    field: (raw_row.get(field) or "").strip()
                    for field in CATALOGUE_FIELDS
                }
                if not any(normalized_row.values()):
                    warnings.append(
                        {"row": row_number, "code": "EMPTY_ROW_IGNORED"}
                    )
                    continue
                if not normalized_row["last_certified_at"]:
                    normalized_row["last_certified_at"] = None
                try:
                    entry = CatalogueEntry.model_validate(normalized_row)
                    normalized = normalize_url(entry.url)
                    entry = entry.model_copy(update={"url": normalized})
                except (ValidationError, ValueError) as exc:
                    if isinstance(exc, ValidationError):
                        validation_details = [
                            {
                                "field": ".".join(
                                    str(part) for part in item["loc"]
                                ),
                                "message": item["msg"],
                                "type": item["type"],
                            }
                            for item in exc.errors()
                        ]
                    else:
                        validation_details = [
                            {"field": "row", "message": str(exc), "type": "value_error"}
                        ]
                    errors.append(
                        {
                            "row": row_number,
                            "code": "CSV_ROW_INVALID",
                            "message": "The row contains invalid fields.",
                            "details": validation_details,
                        }
                    )
                    continue
                rows.append((row_number, entry, normalized))
        except csv.Error as exc:
            errors.append(
                {
                    "row": rows_total + 2,
                    "code": "CSV_PARSE_ERROR",
                    "message": str(exc),
                }
            )

    report = {
        "import_id": report_id,
        "rows_total": rows_total,
        "imported": 0,
        "duplicates": 0,
        "error_count": len(errors),
        "errors": errors,
        "warnings": warnings,
        "reachability": "not_checked",
    }

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            """
            SELECT request_hash, resource_id FROM idempotency_keys
            WHERE scope = 'catalogue_import' AND key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if prior is not None:
            if prior["request_hash"] != request_hash:
                return {
                    "error": {
                        "code": "IDEMPOTENCY_KEY_REUSED",
                        "message": (
                            "This Idempotency-Key was already used for a "
                            "different catalogue import."
                        ),
                    }
                }, False
            existing = connection.execute(
                "SELECT report_json FROM catalogue_imports WHERE id = ?",
                (prior["resource_id"],),
            ).fetchone()
            if existing is None:
                return {
                    "error": {
                        "code": "IDEMPOTENCY_RECORD_INVALID",
                        "message": "The prior import report is unavailable.",
                    }
                }, False
            return json.loads(existing["report_json"]), True

        existing_keys = {
            (row["normalized_url"], row["platform_key"])
            for row in connection.execute(
                "SELECT normalized_url, platform_key FROM catalogue_destinations"
            ).fetchall()
        }
        batch_keys: set[tuple[str, str]] = set()
        for row_number, entry, normalized_url in rows:
            key = (normalized_url, entry.platform_key)
            if key in existing_keys or key in batch_keys:
                duplicates += 1
                errors.append(
                    {
                        "row": row_number,
                        "code": "DUPLICATE_DESTINATION",
                        "message": (
                            "A destination with this normalized URL and "
                            "platform already exists."
                        ),
                    }
                )
                continue
            batch_keys.add(key)
            connection.execute(
                """
                INSERT INTO catalogue_destinations
                    (normalized_url, platform_key, record_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalized_url,
                    entry.platform_key,
                    entry.model_dump_json(),
                    now,
                    now,
                ),
            )
            imported += 1

        report.update(
            {
                "imported": imported,
                "duplicates": duplicates,
                "error_count": len(errors),
                "errors": errors,
            }
        )
        connection.execute(
            "INSERT INTO catalogue_imports (id, report_json, created_at) VALUES (?, ?, ?)",
            (report_id, json.dumps(report, ensure_ascii=False), now),
        )
        connection.execute(
            """
            INSERT INTO idempotency_keys
                (scope, key, request_hash, resource_id, created_at)
            VALUES ('catalogue_import', ?, ?, ?, ?)
            """,
            (idempotency_key, request_hash, report_id, now),
        )
    return report, False


def list_entries(limit: int, offset: int) -> list[dict]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT record_json FROM catalogue_destinations
            ORDER BY platform_key, normalized_url LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [json.loads(row["record_json"]) for row in rows]


def get_import_report(import_id: str) -> dict | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT report_json FROM catalogue_imports WHERE id = ?",
            (import_id,),
        ).fetchone()
    return json.loads(row["report_json"]) if row else None


def export_csv() -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CATALOGUE_FIELDS, lineterminator="\r\n")
    writer.writeheader()
    for item in list_entries(limit=100_000, offset=0):
        values = dict(item)
        values["audience_tags"] = ";".join(values.get("audience_tags") or [])
        values["last_certified_at"] = values.get("last_certified_at") or ""
        writer.writerow(values)
    return output.getvalue()

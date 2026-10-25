import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from app.storage.database import connect


def cache_idempotent_response(
    scope: str,
    key: str,
    request_body: str,
    factory: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    request_hash = hashlib.sha256(request_body.encode("utf-8")).hexdigest()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            """
            SELECT request_hash, response_json FROM idempotent_responses
            WHERE scope = ? AND key = ?
            """,
            (scope, key),
        ).fetchone()
        if prior is not None:
            if prior["request_hash"] != request_hash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "IDEMPOTENCY_KEY_REUSED",
                        "message": (
                            "This Idempotency-Key was already used for a "
                            "different request."
                        ),
                    },
                )
            return json.loads(prior["response_json"]), True

        response = factory()
        connection.execute(
            """
            INSERT INTO idempotent_responses
                (scope, key, request_hash, response_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                scope,
                key,
                request_hash,
                json.dumps(response, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return response, False

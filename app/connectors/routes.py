import os
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.connectors.errors import ConnectorError
from app.connectors.implementations import CONNECTOR_TYPES, Connector
from app.connectors.manifests import ConnectorManifest, load_manifests


router = APIRouter()


@lru_cache(maxsize=1)
def _manifests() -> dict[str, ConnectorManifest]:
    return load_manifests()


@lru_cache(maxsize=6)
def _connector(connector_key: str) -> Connector:
    manifest = _manifests()[connector_key]
    connector_type = CONNECTOR_TYPES[connector_key]
    return connector_type(manifest)


def _error_response(error: ConnectorError) -> JSONResponse:
    return JSONResponse(
        status_code=error.http_status,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
                "details": error.details,
            }
        },
    )


def _required_object(body: dict[str, Any], field: str) -> dict[str, Any]:
    value = body.get(field)
    if not isinstance(value, dict):
        raise ConnectorError(
            "REQUEST_VALIDATION_FAILED",
            f"'{field}' must be a JSON object.",
            http_status=422,
            details={"field": field},
        )
    return value


@router.get("/api/v1/connectors")
def list_connectors() -> list[dict[str, Any]]:
    return [
        manifest.model_dump(mode="json")
        for _, manifest in sorted(_manifests().items())
    ]


@router.post(
    "/connector/v1/{connector_key}/{method}",
    response_model=None,
)
async def call_connector(
    connector_key: str, method: str, request: Request
) -> Any:
    manifests = _manifests()
    if connector_key not in manifests:
        return _error_response(
            ConnectorError(
                "CONNECTOR_NOT_FOUND",
                f"Connector '{connector_key}' is not registered.",
                http_status=404,
            )
        )

    disabled = {
        value.strip()
        for value in os.getenv("DISABLED_CONNECTORS", "").split(",")
        if value.strip()
    }
    if connector_key in disabled:
        return _error_response(
            ConnectorError(
                "CONNECTOR_DISABLED",
                "This connector is disabled by configuration.",
                http_status=503,
            )
        )

    try:
        body = await request.json()
    except ValueError:
        return _error_response(
            ConnectorError(
                "REQUEST_VALIDATION_FAILED",
                "A valid JSON object is required.",
                http_status=422,
            )
        )
    if not isinstance(body, dict):
        return _error_response(
            ConnectorError(
                "REQUEST_VALIDATION_FAILED",
                "A JSON object request body is required.",
                http_status=422,
            )
        )

    connector = _connector(connector_key)
    try:
        if method == "validate":
            result = connector.validate(
                _required_object(body, "rendition"),
                _required_object(body, "destination"),
                _required_object(body, "connection"),
            )
        elif method == "publish":
            idempotency_key = body.get("idempotency_key")
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise ConnectorError(
                    "REQUEST_VALIDATION_FAILED",
                    "publish requires a non-empty idempotency_key.",
                    http_status=422,
                    details={"field": "idempotency_key"},
                )
            result = connector.publish(
                _required_object(body, "rendition"),
                _required_object(body, "destination"),
                _required_object(body, "connection"),
                idempotency_key,
            )
        elif method in {"get_status", "delete", "fetch_metrics"}:
            platform_post_id = body.get("platform_post_id")
            if connector_key == "assisted" and method in {"get_status", "delete"}:
                platform_post_id = str(platform_post_id or "")
            elif not isinstance(platform_post_id, (str, int)):
                raise ConnectorError(
                    "REQUEST_VALIDATION_FAILED",
                    f"{method} requires platform_post_id.",
                    http_status=422,
                    details={"field": "platform_post_id"},
                )
            connection = _required_object(body, "connection")
            if method == "get_status":
                result = connector.get_status(str(platform_post_id), connection)
            elif method == "delete":
                result = connector.delete(str(platform_post_id), connection)
            else:
                since = body.get("since")
                if not isinstance(since, str):
                    raise ConnectorError(
                        "REQUEST_VALIDATION_FAILED",
                        "fetch_metrics requires since as a string.",
                        http_status=422,
                        details={"field": "since"},
                    )
                result = connector.fetch_metrics(
                    str(platform_post_id), connection, since
                )
        elif method == "refresh_auth":
            result = connector.refresh_auth(_required_object(body, "connection"))
        elif method == "build_assist_pack":
            result = connector.build_assist_pack(
                _required_object(body, "rendition"),
                _required_object(body, "destination"),
            )
        else:
            raise ConnectorError(
                "CONNECTOR_METHOD_UNSUPPORTED",
                f"Method '{method}' is not part of the connector contract.",
                http_status=404,
            )
        return result
    except ConnectorError as exc:
        return _error_response(exc)

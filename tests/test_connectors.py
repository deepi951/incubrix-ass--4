import hashlib
import hmac
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.connectors.errors import ConnectorError
from app.connectors.implementations import (
    CONNECTOR_TYPES,
    DiscourseConnector,
    LemmyConnector,
    WebhookConnector,
)
from app.connectors.manifests import (
    ConnectorManifest,
    RateLimitManifest,
    load_manifests,
)
from app.main import app


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("SIMULATION_MODE", "true")
    with TestClient(app) as test_client:
        yield test_client


def _contract_body(connector_key: str) -> dict:
    return {
        "rendition": {
            "title": "A test title",
            "body": "A local simulated test body.",
            "text": "A local simulated test body.",
            "content_type": "text",
        },
        "destination": {
            "key": f"{connector_key}-test-destination",
            "base_url": "http://localhost:9000",
            "submit_url": "https://example.invalid/submit",
        },
        "connection": {},
        "idempotency_key": f"test-{connector_key}",
    }


@pytest.mark.parametrize(
    "connector_key",
    ["discourse", "lemmy", "devto", "ghost", "webhook", "assisted"],
)
def test_required_connector_methods_follow_appendix_contract(
    client: TestClient, connector_key: str
) -> None:
    body = _contract_body(connector_key)

    validation = client.post(
        f"/connector/v1/{connector_key}/validate",
        json={
            "rendition": body["rendition"],
            "destination": body["destination"],
            "connection": body["connection"],
        },
    )
    assert validation.status_code == 200
    assert set(validation.json()) == {"ok", "violations"}

    publication = client.post(
        f"/connector/v1/{connector_key}/publish", json=body
    )
    assert publication.status_code == 200
    assert set(publication.json()) == {
        "platform_post_id",
        "live_url",
        "status",
    }
    if connector_key == "assisted":
        assert publication.json()["platform_post_id"] is None
        assert publication.json()["status"] == "requires_manual_action"
    else:
        assert publication.json()["platform_post_id"]
        assert publication.json()["live_url"].startswith("mock://")
        assert publication.json()["status"] == "published"

    status_result = client.post(
        f"/connector/v1/{connector_key}/get_status",
        json={
            "platform_post_id": publication.json()["platform_post_id"],
            "connection": {},
        },
    )
    assert status_result.status_code == 200
    assert set(status_result.json()) == {"status", "live_url"}

    deletion = client.post(
        f"/connector/v1/{connector_key}/delete",
        json={
            "platform_post_id": publication.json()["platform_post_id"],
            "connection": {},
        },
    )
    assert deletion.status_code == 200
    assert set(deletion.json()) == {"deleted"}
    if connector_key == "assisted":
        assert deletion.json()["deleted"] is False
    else:
        assert deletion.json()["deleted"] is True
        deleted_status = client.post(
            f"/connector/v1/{connector_key}/get_status",
            json={
                "platform_post_id": publication.json()["platform_post_id"],
                "connection": {},
            },
        )
        assert deleted_status.status_code == 200
        assert deleted_status.json()["status"] == "deleted"


def test_connector_list_returns_six_valid_manifests(client: TestClient) -> None:
    response = client.get("/api/v1/connectors")

    assert response.status_code == 200
    connectors = {item["key"]: item for item in response.json()}
    assert set(connectors) == {
        "discourse",
        "lemmy",
        "devto",
        "ghost",
        "webhook",
        "assisted",
    }
    assert all(
        {"key", "version", "tier", "auth", "surfaces", "rate_limits",
         "capabilities", "error_map", "instance_param"} <= item.keys()
        for item in connectors.values()
    )
    for key, manifest in connectors.items():
        assert manifest["rate_limits"]["per_connection"]["requests"] == 60
        assert manifest["rate_limits"]["per_connection"]["per_seconds"] == 60
        assert manifest["error_map"]["quota_exhausted"] == (
            "PLATFORM_QUOTA_EXHAUSTED"
        )
        assert {
            "DEST_SPEC_VIOLATION",
            "DEST_RULE_FREQUENCY_CAP",
            "CONNECTION_AUTH_EXPIRED",
            "PLATFORM_RATE_LIMIT",
            "PLATFORM_QUOTA_EXHAUSTED",
            "PLATFORM_CONTENT_REJECTED",
            "PLATFORM_UNAVAILABLE",
            "CONNECTOR_DISABLED",
        } <= set(manifest["error_map"].values())


def test_simulation_replays_same_publish_without_duplicate(client: TestClient) -> None:
    body = _contract_body("discourse")
    url = "/connector/v1/discourse/publish"

    first = client.post(url, json=body)
    second = client.post(url, json=body)

    assert first.json() == second.json()
    assert first.json()["live_url"].startswith("mock://")


def test_assisted_connector_returns_manual_package_not_publication(
    client: TestClient,
) -> None:
    body = _contract_body("assisted")
    result = client.post(
        "/connector/v1/assisted/build_assist_pack",
        json={
            "rendition": body["rendition"],
            "destination": body["destination"],
        },
    )
    publication = client.post("/connector/v1/assisted/publish", json=body)

    assert result.status_code == 200
    assert result.json()["files"][0]["name"] == "ready-to-post.md"
    assert "post manually" in result.json()["checklist"][2].lower()
    assert publication.json()["status"] == "requires_manual_action"
    assert publication.json()["platform_post_id"] is None
    assert set(result.json()) == {"files", "caption", "deep_link", "checklist"}


@pytest.mark.parametrize(
    "connector_key",
    ["discourse", "lemmy", "devto", "ghost", "webhook", "assisted"],
)
def test_optional_connector_methods_have_contract_shapes_or_unsupported_errors(
    client: TestClient, connector_key: str
) -> None:
    metrics = client.post(
        f"/connector/v1/{connector_key}/fetch_metrics",
        json={
            "platform_post_id": "simulated-post",
            "connection": {},
            "since": "2026-01-01T00:00:00Z",
        },
    )
    if connector_key in {"discourse", "lemmy", "devto"}:
        assert metrics.status_code == 200
        assert set(metrics.json()) == {
            "views",
            "likes",
            "comments",
            "shares",
            "saves",
            "watch_time_s",
            "clicks",
        }
    else:
        assert metrics.status_code == 501
        assert metrics.json()["error"]["code"] == "CONNECTOR_METHOD_UNSUPPORTED"

    refresh = client.post(
        f"/connector/v1/{connector_key}/refresh_auth",
        json={"connection": {}},
    )
    assert refresh.status_code == 501
    assert refresh.json()["error"]["code"] == "CONNECTOR_METHOD_UNSUPPORTED"

    assist_pack = client.post(
        f"/connector/v1/{connector_key}/build_assist_pack",
        json={
            "rendition": {"title": "Title", "body": "Body"},
            "destination": {"submit_url": "https://example.invalid/submit"},
        },
    )
    if connector_key == "assisted":
        assert assist_pack.status_code == 200
        assert set(assist_pack.json()) == {
            "files",
            "caption",
            "deep_link",
            "checklist",
        }
    else:
        assert assist_pack.status_code == 501
        assert assist_pack.json()["error"]["code"] == "CONNECTOR_METHOD_UNSUPPORTED"


def test_manifest_validation_flags_discourse_format_violation(
    client: TestClient,
) -> None:
    body = _contract_body("discourse")
    body["rendition"]["title"] = "x" * 256

    response = client.post(
        "/connector/v1/discourse/validate",
        json={
            "rendition": body["rendition"],
            "destination": body["destination"],
            "connection": {},
        },
    )

    assert response.json()["ok"] is False
    assert response.json()["violations"][0]["field"] == "rendition.title"


def test_hmac_webhook_signs_the_exact_request_bytes(monkeypatch) -> None:
    monkeypatch.setenv("SIMULATION_MODE", "false")
    manifest = load_manifests()["webhook"]
    connector = CONNECTOR_TYPES["webhook"](manifest)
    captured: dict = {}

    def fake_request(method, url, connection, **kwargs):
        captured.update(kwargs)
        return object(), {
            "platform_post_id": "mock-post-1",
            "live_url": "https://local.test/posts/1",
            "status": "published",
        }

    monkeypatch.setattr(connector, "_request", fake_request)
    rendition = {"title": "Local test", "body": "No public posting."}
    destination = {"url": "https://local.test/publish"}
    secret = "owned-local-test-secret"

    result = connector.publish(
        rendition,
        destination,
        {"hmac_secret": secret},
        "local-test-key",
    )

    sent_bytes = captured["content"]
    expected = hmac.new(secret.encode(), sent_bytes, hashlib.sha256).hexdigest()
    assert captured["headers"]["X-IncuBrix-Signature"] == f"sha256={expected}"
    assert result["platform_post_id"] == "mock-post-1"
    assert secret.encode() not in sent_bytes


def test_platform_rate_limit_maps_reset_time(monkeypatch) -> None:
    class RateLimitedClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def request(self, method, url, **kwargs):
            request = httpx.Request(method, url)
            return httpx.Response(
                429,
                headers={"Retry-After": "17"},
                request=request,
            )

    monkeypatch.setattr(
        "app.connectors.implementations.httpx.Client",
        lambda **kwargs: RateLimitedClient(),
    )
    connector = CONNECTOR_TYPES["discourse"](load_manifests()["discourse"])

    with pytest.raises(ConnectorError) as error:
        connector._request("POST", "https://local.test/posts", {})

    assert error.value.code == "PLATFORM_RATE_LIMIT"
    assert error.value.retryable is True
    assert error.value.details["retry_after_seconds"] == 17


@pytest.mark.parametrize(
    ("status_code", "body", "headers", "expected_code", "retryable"),
    [
        (
            403,
            {"code": "quota_exhausted"},
            {"Retry-After": "120"},
            "PLATFORM_QUOTA_EXHAUSTED",
            True,
        ),
        (
            401,
            {},
            {},
            "CONNECTION_AUTH_EXPIRED",
            False,
        ),
        (
            422,
            {"message": "Invalid post payload"},
            {},
            "PLATFORM_CONTENT_REJECTED",
            False,
        ),
        (
            503,
            {},
            {},
            "PLATFORM_UNAVAILABLE",
            True,
        ),
        (
            408,
            {},
            {},
            "PLATFORM_UNAVAILABLE",
            True,
        ),
    ],
)
def test_upstream_errors_map_to_shared_codes(
    monkeypatch,
    status_code: int,
    body: dict,
    headers: dict[str, str],
    expected_code: str,
    retryable: bool,
) -> None:
    class ErrorResponseClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def request(self, method, url, **_kwargs):
            request = httpx.Request(method, url)
            return httpx.Response(
                status_code,
                headers=headers,
                json=body,
                request=request,
            )

    monkeypatch.setattr(
        "app.connectors.implementations.httpx.Client",
        lambda **_kwargs: ErrorResponseClient(),
    )
    connector = CONNECTOR_TYPES["discourse"](load_manifests()["discourse"])

    with pytest.raises(ConnectorError) as error:
        connector._request(
            "POST",
            f"https://{status_code}.local.test/posts",
            {},
        )

    assert error.value.code == expected_code
    assert error.value.retryable is retryable
    if expected_code == "PLATFORM_QUOTA_EXHAUSTED":
        assert error.value.details["retry_after"] == "next_window"
        assert error.value.details["retry_after_seconds"] == 120
    if status_code == 408:
        assert error.value.details["outcome_ambiguous"] is True


def test_quota_error_uses_shared_connector_error_envelope(
    client: TestClient, monkeypatch
) -> None:
    def fail_publish(*_args, **_kwargs):
        raise ConnectorError(
            "PLATFORM_QUOTA_EXHAUSTED",
            "The platform quota is exhausted.",
            retryable=True,
            http_status=429,
            details={"retry_after": "next_window"},
        )

    monkeypatch.setattr(
        "app.connectors.routes._connector",
        lambda _connector_key: type("ConnectorStub", (), {"publish": fail_publish})(),
    )
    response = client.post(
        "/connector/v1/discourse/publish",
        json=_contract_body("discourse"),
    )

    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "code": "PLATFORM_QUOTA_EXHAUSTED",
            "message": "The platform quota is exhausted.",
            "retryable": True,
            "details": {"retry_after": "next_window"},
        }
    }


def test_configured_manifest_rate_limit_is_enforced(monkeypatch) -> None:
    class SuccessClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def request(self, method, url, **_kwargs):
            request = httpx.Request(method, url)
            return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(
        "app.connectors.implementations.httpx.Client",
        lambda **_kwargs: SuccessClient(),
    )
    manifest = ConnectorManifest.model_validate(
        {
            **load_manifests()["discourse"].model_dump(),
            "rate_limits": {
                "per_connection": {
                    "requests": 1,
                    "per_seconds": 60,
                }
            },
        }
    )
    connector = CONNECTOR_TYPES["discourse"](manifest)
    url = f"https://limited-{uuid4().hex}.local.test/posts"

    connector._request("GET", url, {"account": "test-account"})
    with pytest.raises(ConnectorError) as error:
        connector._request("GET", url, {"account": "test-account"})

    assert error.value.code == "PLATFORM_RATE_LIMIT"
    assert error.value.retryable is True
    assert error.value.details["retry_after_seconds"] > 0
    assert error.value.details["configured_limit"] == {
        "requests": 1,
        "per_seconds": 60,
    }


def test_lemmy_and_webhook_auth_headers_use_configured_bearer_tokens() -> None:
    manifests = load_manifests()
    lemmy = LemmyConnector(manifests["lemmy"])
    webhook = WebhookConnector(manifests["webhook"])

    assert lemmy._headers({"access_token": "lemmy-test-token"}) == {
        "Authorization": "Bearer lemmy-test-token"
    }
    assert webhook._auth_headers({"bearer_token": "webhook-test-token"}) == {
        "Authorization": "Bearer webhook-test-token"
    }


def test_connector_request_timeout_respects_cleanup_deadline(monkeypatch) -> None:
    captured: dict[str, float] = {}

    class ImmediateClient:
        def __init__(self, *, timeout, **_kwargs):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def request(self, method, url, **_kwargs):
            request = httpx.Request(method, url)
            return httpx.Response(200, json={}, request=request)

    monkeypatch.setenv("CONNECTOR_TIMEOUT_SECONDS", "10")
    monkeypatch.setattr(
        "app.connectors.implementations.httpx.Client",
        ImmediateClient,
    )
    connector = DiscourseConnector(load_manifests()["discourse"])
    connector._request_timeout_limit_seconds = 0.25

    connector._request("DELETE", "https://local.test/posts/1", {})

    assert captured["timeout"] == 0.25


def test_post_timeout_is_reported_as_ambiguous(monkeypatch) -> None:
    class TimeoutClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def request(self, method, url, **_kwargs):
            raise httpx.ReadTimeout(
                "timed out",
                request=httpx.Request(method, url),
            )

    monkeypatch.setenv("SIMULATION_MODE", "false")
    monkeypatch.setattr(
        "app.connectors.implementations.httpx.Client",
        lambda **_kwargs: TimeoutClient(),
    )
    connector = DiscourseConnector(load_manifests()["discourse"])

    with pytest.raises(ConnectorError) as error:
        connector._request("POST", "https://local.test/posts", {})

    assert error.value.code == "PLATFORM_UNAVAILABLE"
    assert error.value.retryable is True
    assert error.value.details["outcome_ambiguous"] is True


@pytest.mark.parametrize(
    ("connector_key", "responses", "expected"),
    [
        (
            "discourse",
            [
                {"post": {"topic_id": 12}},
                {"views": 40, "like_count": 5, "posts_count": 3},
            ],
            {"views": 40, "likes": 5, "comments": 2},
        ),
        (
            "lemmy",
            [{"post_view": {"counts": {"upvotes": 4, "comments": 2}}}],
            {"views": None, "likes": 4, "comments": 2},
        ),
        (
            "devto",
            [
                {
                    "page_views_count": 51,
                    "public_reactions_count": 7,
                    "comments_count": 3,
                }
            ],
            {"views": 51, "likes": 7, "comments": 3},
        ),
    ],
)
def test_metrics_are_normalized_without_fabricated_unknown_values(
    monkeypatch, connector_key: str, responses: list[dict], expected: dict
) -> None:
    monkeypatch.setenv("SIMULATION_MODE", "false")
    connector = CONNECTOR_TYPES[connector_key](load_manifests()[connector_key])
    iterator = iter(responses)
    monkeypatch.setattr(
        connector,
        "_request",
        lambda *args, **kwargs: (object(), next(iterator)),
    )
    connection = {
        "base_url": "https://local.test",
        "api_key": "mock-key",
        "username": "mock-user",
        "access_token": "mock-token",
    }

    result = connector.fetch_metrics("12", connection, "2026-01-01T00:00:00Z")

    assert result == {
        "views": expected["views"],
        "likes": expected["likes"],
        "comments": expected["comments"],
        "shares": None,
        "saves": None,
        "watch_time_s": None,
        "clicks": None,
    }

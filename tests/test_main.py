import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage.database import connect


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as client:
        yield client


def destination_payload(key: str = "sample") -> dict:
    return {
        "key": key,
        "connector_key": "assisted",
        "config": {},
        "rules": {
            "daily_frequency_cap": 1,
            "allow_self_promotion": False,
            "allow_links": True,
            "require_ai_disclosure": True,
        },
    }


def job_payload(**content_overrides) -> dict:
    content = {
        "text": "A useful example post.",
        "has_links": False,
        "self_promotional": False,
        "ai_generated": False,
        "ai_disclosure": False,
    }
    content.update(content_overrides)
    return {"destination_key": "sample", "content": content}


def create_destination(client: TestClient, payload: dict | None = None) -> None:
    response = client.post(
        "/api/v1/destinations",
        json=payload or destination_payload(),
        headers={"Idempotency-Key": "destination-1"},
    )
    assert response.status_code == 201


def test_starter_endpoints_and_openapi(api_client: TestClient) -> None:
    assert api_client.get("/").json() == {
        "name": "IncuBrix Distribution",
        "status": "running",
    }
    assert api_client.get("/health").json() == {"status": "healthy"}
    assert api_client.get("/ready").json() == {"status": "ready"}

    openapi = api_client.get("/openapi.json").json()
    assert openapi["openapi"] == "3.1.0"
    assert "/api/v1/catalogue" in openapi["paths"]
    assert "/api/v1/alerts" in openapi["paths"]
    assert api_client.get("/docs").status_code == 200


def test_empty_alerts_endpoint_returns_list(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/alerts?limit=20")

    assert response.status_code == 200
    assert response.json() == []


def test_destination_creation_does_not_return_config_secrets(
    api_client: TestClient,
) -> None:
    payload = destination_payload()
    payload["config"] = {"credential_env": "SAMPLE_TOKEN"}

    response = api_client.post(
        "/api/v1/destinations",
        json=payload,
        headers={"Idempotency-Key": "destination-1"},
    )

    assert response.status_code == 201
    assert "config" not in response.json()
    assert api_client.get("/api/v1/destinations").json()[0]["key"] == "sample"


def test_job_creation_is_persisted_and_idempotent(api_client: TestClient) -> None:
    create_destination(api_client)
    headers = {"Idempotency-Key": "request-1"}

    first = api_client.post("/api/v1/jobs", json=job_payload(), headers=headers)
    repeated = api_client.post("/api/v1/jobs", json=job_payload(), headers=headers)

    assert first.status_code == 202
    assert repeated.status_code == 200
    assert first.json()["id"] == repeated.json()["id"]
    job = api_client.get(f"/api/v1/jobs/{first.json()['id']}").json()
    assert job["status"] == "succeeded"
    assert job["progress"] == 100
    assert api_client.get(job["result_url"]).json()["manifest"]["settings"][
        "simulation_mode"
    ]
    assert api_client.get(job["evidence_url"]).json()["is_real_publication"] is False


def test_reusing_idempotency_key_with_different_payload_conflicts(
    api_client: TestClient,
) -> None:
    create_destination(api_client)
    headers = {"Idempotency-Key": "request-1"}
    api_client.post("/api/v1/jobs", json=job_payload(), headers=headers)

    response = api_client.post(
        "/api/v1/jobs",
        json=job_payload(text="Different content."),
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"self_promotional": True}, "SELF_PROMOTION_NOT_ALLOWED"),
        ({"has_links": True}, None),
        (
            {"ai_generated": True, "ai_disclosure": False},
            "AI_DISCLOSURE_REQUIRED",
        ),
    ],
)
def test_configured_rules_are_enforced(
    api_client: TestClient, overrides: dict, error_code: str | None
) -> None:
    create_destination(api_client)

    response = api_client.post(
        "/api/v1/jobs",
        json=job_payload(**overrides),
        headers={"Idempotency-Key": f"rule-{error_code}"},
    )

    if error_code is None:
        assert response.status_code == 202
    else:
        assert response.status_code == 422
        assert response.json()["error"]["code"] == error_code
        assert response.json()["error"]["retryable"] is False


def test_frequency_cap_prevents_second_job(api_client: TestClient) -> None:
    create_destination(api_client)
    api_client.post(
        "/api/v1/jobs",
        json=job_payload(),
        headers={"Idempotency-Key": "first"},
    )

    response = api_client.post(
        "/api/v1/jobs",
        json=job_payload(),
        headers={"Idempotency-Key": "second"},
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "DEST_RULE_FREQUENCY_CAP"
    assert response.json()["error"]["retryable"] is True


def test_retry_waits_for_rate_reset_and_replays_same_job(
    api_client: TestClient,
) -> None:
    create_destination(api_client)
    created = api_client.post(
        "/api/v1/jobs",
        json=job_payload(),
        headers={"Idempotency-Key": "original-job"},
    )
    job_id = created.json()["id"]
    rate_error = {
        "code": "PLATFORM_RATE_LIMIT",
        "message": "Rate limited.",
        "retryable": True,
        "details": {"retry_after_seconds": 60},
    }
    with connect() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'failed', error_json = ?, attempt_count = 1 "
            "WHERE id = ?",
            (json.dumps(rate_error), job_id),
        )

    too_soon = api_client.post(
        f"/api/v1/jobs/{job_id}/retry",
        headers={"Idempotency-Key": "retry-too-soon"},
    )
    assert too_soon.status_code == 429
    assert too_soon.json()["error"]["details"]["retry_after_seconds"] == 60

    rate_error["details"]["retry_after_seconds"] = 0
    with connect() as connection:
        connection.execute(
            "UPDATE jobs SET error_json = ? WHERE id = ?",
            (json.dumps(rate_error), job_id),
        )

    retry_key = {"Idempotency-Key": "retry-after-reset"}
    retried = api_client.post(f"/api/v1/jobs/{job_id}/retry", headers=retry_key)
    replayed = api_client.post(f"/api/v1/jobs/{job_id}/retry", headers=retry_key)

    assert retried.status_code == 202
    assert replayed.status_code == 200
    assert replayed.json()["id"] == job_id
    with connect() as connection:
        row = connection.execute(
            "SELECT status, attempt_count FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row["status"] == "succeeded"
    assert row["attempt_count"] == 2


def test_quota_exhaustion_retry_waits_for_next_window(
    api_client: TestClient,
) -> None:
    create_destination(api_client)
    created = api_client.post(
        "/api/v1/jobs",
        json=job_payload(),
        headers={"Idempotency-Key": "original-quota"},
    )
    job_id = created.json()["id"]
    quota_error = {
        "code": "PLATFORM_QUOTA_EXHAUSTED",
        "message": "Quota exhausted.",
        "retryable": True,
        "details": {"retry_after": "next_window"},
    }
    with connect() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'failed', error_json = ?, attempt_count = 1 "
            "WHERE id = ?",
            (json.dumps(quota_error), job_id),
        )

    before_reset = api_client.post(
        f"/api/v1/jobs/{job_id}/retry",
        headers={"Idempotency-Key": "retry-before-quota-reset"},
    )

    assert before_reset.status_code == 429
    assert before_reset.json()["error"]["code"] == "PLATFORM_QUOTA_EXHAUSTED"
    assert before_reset.json()["error"]["details"]["retry_after"] == "next_window"

    quota_error["details"]["retry_after_seconds"] = 0
    with connect() as connection:
        connection.execute(
            "UPDATE jobs SET error_json = ? WHERE id = ?",
            (json.dumps(quota_error), job_id),
        )
    after_reset = api_client.post(
        f"/api/v1/jobs/{job_id}/retry",
        headers={"Idempotency-Key": "retry-after-quota-reset"},
    )

    assert after_reset.status_code == 202
    assert after_reset.json()["id"] == job_id


def test_ambiguous_publish_outcome_cannot_be_retried(
    api_client: TestClient,
) -> None:
    create_destination(api_client)
    created = api_client.post(
        "/api/v1/jobs",
        json=job_payload(),
        headers={"Idempotency-Key": "original-ambiguous"},
    )
    job_id = created.json()["id"]
    ambiguous_error = {
        "code": "PLATFORM_UNAVAILABLE",
        "message": "Timed out.",
        "retryable": True,
        "details": {"outcome_ambiguous": True},
    }
    with connect() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'failed', error_json = ?, attempt_count = 1 "
            "WHERE id = ?",
            (json.dumps(ambiguous_error), job_id),
        )

    response = api_client.post(
        f"/api/v1/jobs/{job_id}/retry",
        headers={"Idempotency-Key": "unsafe-retry"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RETRY_UNSAFE"


def test_api_errors_have_consistent_shape_and_correlation_id(
    api_client: TestClient,
) -> None:
    response = api_client.get(
        "/api/v1/jobs/missing",
        headers={"X-Correlation-ID": "trace-test"},
    )

    assert response.status_code == 404
    assert response.headers["X-Correlation-ID"] == "trace-test"
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"
    assert set(response.json()["error"]) == {
        "code",
        "message",
        "retryable",
        "details",
    }

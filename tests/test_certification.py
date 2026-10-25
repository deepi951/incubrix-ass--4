from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.connectors.errors import ConnectorError
from app.connectors.implementations import CONNECTOR_TYPES
from app.services.certification import (
    _delete_within_deadline,
    schedule_due_recertifications,
)
from app.storage.database import connect


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "certification.db"))
    monkeypatch.setenv("SIMULATION_MODE", "true")
    with TestClient(app) as test_client:
        yield test_client


def destination_payload(connector_key: str, key: str) -> dict:
    return {
        "key": key,
        "connector_key": connector_key,
        "config": {"base_url": "http://localhost:9000"},
        "rules": {
            "frequency_cap": 10,
            "frequency_window_seconds": 604800,
            "self_promo_allowed": False,
            "link_policy": "allow",
            "disclosure_required": True,
        },
    }


@pytest.mark.parametrize(
    "connector_key",
    ["discourse", "lemmy", "devto", "ghost", "webhook", "assisted"],
)
def test_certification_workflow_and_cleanup_are_explicitly_simulated(
    client: TestClient, connector_key: str
) -> None:
    destination_key = f"cert-{connector_key}"
    created = client.post(
        "/api/v1/destinations",
        json=destination_payload(connector_key, destination_key),
        headers={"Idempotency-Key": f"destination-{connector_key}"},
    )
    assert created.status_code == 201

    response = client.post(
        f"/api/v1/destinations/{destination_key}/certify",
        headers={"Idempotency-Key": f"certification-{connector_key}"},
    )
    assert response.status_code == 202

    run = client.get(f"/api/v1/certifications/{response.json()['id']}").json()
    assert run["status"] == "succeeded"
    assert run["result"]["classification"] in {
        "simulation_only",
        "assisted_pack_simulation",
    }
    if connector_key == "assisted":
        assert run["result"]["manual_action_required"] is True
        assert "publish" not in [
            step["method"] for step in run["result"]["steps"]
        ]
    else:
        assert [
            step["method"] for step in run["result"]["steps"]
        ] == ["validate", "publish", "get_status", "delete"]
        assert run["result"]["cleanup"]["deleted"] is True
        assert run["result"]["test_post"]["live_url"].startswith("mock://")
        cleanup_deadline = datetime.fromisoformat(run["cleanup_deadline"])
        deleted_at = datetime.fromisoformat(
            run["result"]["cleanup"]["deleted_at"]
        )
        assert run["result"]["cleanup"]["on_time"] is True
        assert deleted_at <= cleanup_deadline


def test_certification_request_replay_returns_same_run(
    client: TestClient,
) -> None:
    destination_key = "cert-replay"
    client.post(
        "/api/v1/destinations",
        json=destination_payload("discourse", destination_key),
        headers={"Idempotency-Key": "destination-replay"},
    )
    url = f"/api/v1/destinations/{destination_key}/certify"
    headers = {"Idempotency-Key": "certification-replay"}

    first = client.post(url, headers=headers)
    replay = client.post(url, headers=headers)

    assert first.status_code == 202
    assert replay.status_code == 200
    assert first.json()["id"] == replay.json()["id"]
    retrieved = client.get(
        f"/api/v1/certifications/{first.json()['id']}"
    )
    assert retrieved.status_code == 200
    assert retrieved.json()["id"] == first.json()["id"]


def test_cleanup_failure_is_persisted_and_retrievable(
    client: TestClient, monkeypatch
) -> None:
    class CleanupFailureConnector:
        def __init__(self, _manifest) -> None:
            self._request_timeout_limit_seconds = None

        def validate(self, *_args):
            return {"ok": True, "violations": []}

        def publish(self, *_args):
            return {
                "platform_post_id": "test-post-1",
                "live_url": "https://local.test/post/1",
                "status": "published",
            }

        def get_status(self, *_args):
            return {
                "status": "published",
                "live_url": "https://local.test/post/1",
            }

        def delete(self, *_args):
            raise ConnectorError(
                "PLATFORM_UNAVAILABLE",
                "Delete request failed.",
                retryable=False,
            )

    monkeypatch.setitem(
        CONNECTOR_TYPES, "discourse", CleanupFailureConnector
    )
    destination_key = "cleanup-failure"
    created = client.post(
        "/api/v1/destinations",
        json=destination_payload("discourse", destination_key),
        headers={"Idempotency-Key": "destination-cleanup-failure"},
    )
    assert created.status_code == 201

    started = client.post(
        f"/api/v1/destinations/{destination_key}/certify",
        headers={"Idempotency-Key": "certification-cleanup-failure"},
    )
    run_id = started.json()["id"]
    retrieved = client.get(f"/api/v1/certifications/{run_id}")

    assert retrieved.status_code == 200
    run = retrieved.json()
    assert run["status"] == "failed"
    assert run["error"]["code"] == "CERTIFICATION_CLEANUP_FAILED"
    assert run["result"]["cleanup"]["deleted"] is False
    assert run["result"]["cleanup"]["on_time"] is False


def test_ambiguous_publish_timeout_is_failed_without_claiming_cleanup(
    client: TestClient, monkeypatch
) -> None:
    class TimeoutConnector:
        _request_timeout_limit_seconds = None

        def __init__(self, _manifest) -> None:
            pass

        def validate(self, *_args):
            return {"ok": True, "violations": []}

        def publish(self, *_args):
            raise ConnectorError(
                "PLATFORM_UNAVAILABLE",
                "The platform request timed out; its outcome may be unknown.",
                retryable=True,
                details={"outcome_ambiguous": True},
            )

        def delete(self, *_args):
            raise AssertionError("Do not delete without a confirmed post ID.")

    monkeypatch.setitem(CONNECTOR_TYPES, "discourse", TimeoutConnector)
    destination_key = "ambiguous-publish-timeout"
    created = client.post(
        "/api/v1/destinations",
        json=destination_payload("discourse", destination_key),
        headers={"Idempotency-Key": "destination-ambiguous-timeout"},
    )
    assert created.status_code == 201

    started = client.post(
        f"/api/v1/destinations/{destination_key}/certify",
        headers={"Idempotency-Key": "certification-ambiguous-timeout"},
    )
    run = client.get(
        f"/api/v1/certifications/{started.json()['id']}"
    ).json()

    assert run["status"] == "failed"
    assert run["error"]["code"] == "PLATFORM_UNAVAILABLE"
    assert run["error"]["details"]["outcome_ambiguous"] is True
    assert run["result"]["cleanup"]["deleted"] is False
    assert run["result"]["cleanup"]["status"] == "outcome_unknown"


def test_late_delete_is_recorded_as_deleted_but_outside_deadline() -> None:
    class SlowDeleteConnector:
        _request_timeout_limit_seconds = None

        def delete(self, *_args):
            return {"deleted": True}

    deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    result = _delete_within_deadline(
        SlowDeleteConnector(),
        "test-post",
        {},
        deadline,
    )

    assert result["deleted"] is True
    assert result["status"] == "deleted_after_deadline"
    assert result["on_time"] is False


def test_unexpected_delete_error_is_returned_as_cleanup_failure() -> None:
    class BrokenDeleteConnector:
        _request_timeout_limit_seconds = None

        def delete(self, *_args):
            raise RuntimeError("unexpected connector error")

    result = _delete_within_deadline(
        BrokenDeleteConnector(),
        "test-post",
        {},
        datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    assert result["deleted"] is False
    assert result["status"] == "cleanup_failed"
    assert result["error_code"] == "CERTIFICATION_CLEANUP_FAILED"
    assert result["failure_type"] == "RuntimeError"


def test_real_certification_is_blocked_without_explicit_approval(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("SIMULATION_MODE", "false")
    destination_key = "public-target"
    client.post(
        "/api/v1/destinations",
        json={
            **destination_payload("discourse", destination_key),
            "config": {
                "base_url": "https://public.example",
                "test_environment": True,
                "sandbox_confirmed": True,
            },
        },
        headers={"Idempotency-Key": "destination-public"},
    )

    response = client.post(
        f"/api/v1/destinations/{destination_key}/certify",
        headers={"Idempotency-Key": "certification-public"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == (
        "CERTIFICATION_TARGET_NOT_APPROVED"
    )


def test_due_destination_is_demoted_and_alerted_when_no_sandbox_is_approved(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("SIMULATION_MODE", "false")
    monkeypatch.setenv("RECERTIFICATION_ENABLED", "true")
    destination_key = "stale-destination"
    client.post(
        "/api/v1/destinations",
        json={
            **destination_payload("discourse", destination_key),
            "config": {"base_url": "https://public.example"},
        },
        headers={"Idempotency-Key": "destination-stale"},
    )
    last_certified = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat()
    with connect() as connection:
        connection.execute(
            "UPDATE destinations SET last_certified_at = ?, certified_level = 'L3' "
            "WHERE key = ?",
            (last_certified, destination_key),
        )

    assert schedule_due_recertifications() == 0
    destination = client.get("/api/v1/destinations").json()[0]
    assert destination["enabled"] is False
    response = client.get("/api/v1/alerts")
    assert response.status_code == 200
    alerts = response.json()
    assert alerts[0]["error_code"] == "RECERTIFICATION_NOT_AUTHORIZED"
    assert set(alerts[0]) == {
        "id",
        "destination_key",
        "certification_run_id",
        "error_code",
        "message",
        "created_at",
        "resolved_at",
    }
    assert alerts[0]["destination_key"] == destination_key
    assert alerts[0]["message"]
    assert alerts[0]["created_at"]
    assert alerts[0]["resolved_at"] is None

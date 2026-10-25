import csv
import io
import time

import pytest
from fastapi.testclient import TestClient

from app.catalogue.models import CATALOGUE_FIELDS
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "catalogue.db"))
    with TestClient(app) as test_client:
        yield test_client


def catalogue_row(number: int = 1) -> dict[str, str]:
    return {
        "name": f"Example community {number}",
        "url": f"https://community{number}.example/forum/",
        "platform_key": "discourse",
        "surface": "topic",
        "category": "community",
        "audience_tags": "AI, video, SaaS",
        "language": "en",
        "geo": "",
        "est_audience": "1200",
        "rules_url": "https://example.invalid/rules",
        "frequency_cap": "2",
        "self_promo_allowed": "false",
        "link_policy": "allow",
        "disclosure_required": "true",
        "automation_allowed": "true",
        "connector_key": "discourse",
        "relevance_score": "4",
        "relevance_reason": "The community discusses AI and SaaS.",
        "level": "L0",
        "last_certified_at": "",
    }


def csv_text(rows: list[dict[str, str]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CATALOGUE_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def test_empty_catalogue_returns_paged_response_shape(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/catalogue?limit=100")

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "total": 0,
        "limit": 100,
        "offset": 0,
    }


def test_catalogue_import_normalizes_deduplicates_and_exports(
    client: TestClient,
) -> None:
    first = catalogue_row(1)
    duplicate = catalogue_row(2)
    duplicate["url"] = "https://COMMUNITY1.example/forum?utm_source=sample#rules"
    response = client.post(
        "/api/v1/catalogue/import",
        content=csv_text([first, duplicate]),
        headers={
            "Content-Type": "text/csv",
            "Idempotency-Key": "catalogue-import-1",
        },
    )

    assert response.status_code == 201
    report = response.json()
    assert report["rows_total"] == 2
    assert report["imported"] == 1
    assert report["duplicates"] == 1
    assert report["errors"][0]["code"] == "DUPLICATE_DESTINATION"
    assert report["reachability"] == "not_checked"

    catalogue = client.get("/api/v1/catalogue").json()
    assert catalogue["total"] == 1
    assert catalogue["limit"] == 100
    assert catalogue["offset"] == 0
    assert set(catalogue["items"][0]) == set(CATALOGUE_FIELDS)
    assert catalogue["items"][0]["url"] == "https://community1.example/forum"
    assert catalogue["items"][0]["audience_tags"] == ["ai", "video", "saas"]

    exported = client.get("/api/v1/catalogue/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "Example community 1" in exported.text
    assert client.get(
        f"/api/v1/catalogue/imports/{report['import_id']}"
    ).json() == report


def test_catalogue_import_is_idempotent_and_conflicts_on_changed_body(
    client: TestClient,
) -> None:
    body = csv_text([catalogue_row()])
    headers = {
        "Content-Type": "text/csv",
        "Idempotency-Key": "catalogue-import-1",
    }

    original = client.post("/api/v1/catalogue/import", content=body, headers=headers)
    replay = client.post("/api/v1/catalogue/import", content=body, headers=headers)
    conflict = client.post(
        "/api/v1/catalogue/import",
        content=csv_text([catalogue_row(2)]),
        headers=headers,
    )

    assert original.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["import_id"] == original.json()["import_id"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_bad_rows_are_reported_without_aborting_other_rows(
    client: TestClient,
) -> None:
    good = catalogue_row()
    invalid = catalogue_row(2)
    invalid["url"] = "file:///not-a-web-destination"

    response = client.post(
        "/api/v1/catalogue/import",
        content=csv_text([good, invalid]),
        headers={
            "Content-Type": "text/csv",
            "Idempotency-Key": "catalogue-row-errors",
        },
    )

    assert response.status_code == 201
    assert response.json()["imported"] == 1
    assert response.json()["error_count"] == 1
    assert response.json()["errors"][0]["row"] == 3
    assert response.json()["errors"][0]["code"] == "CSV_ROW_INVALID"


def test_one_thousand_catalogue_rows_import_within_five_minutes(
    client: TestClient,
) -> None:
    body = csv_text([catalogue_row(index) for index in range(1000)])
    started = time.monotonic()

    response = client.post(
        "/api/v1/catalogue/import",
        content=body,
        headers={
            "Content-Type": "text/csv",
            "Idempotency-Key": "catalogue-thousand-rows",
        },
    )
    elapsed = time.monotonic() - started

    assert response.status_code == 201
    assert response.json()["imported"] == 1000
    assert elapsed < 300


def test_catalogue_requires_csv_content_type_and_fields(
    client: TestClient,
) -> None:
    wrong_type = client.post(
        "/api/v1/catalogue/import",
        content="name,url",
        headers={"Idempotency-Key": "csv-type"},
    )
    missing_fields = client.post(
        "/api/v1/catalogue/import",
        content="name,url\nExample,https://example.invalid",
        headers={
            "Content-Type": "text/csv",
            "Idempotency-Key": "csv-columns",
        },
    )

    assert wrong_type.status_code == 415
    assert missing_fields.status_code == 201
    assert missing_fields.json()["errors"][0]["code"] == (
        "CSV_REQUIRED_FIELDS_MISSING"
    )

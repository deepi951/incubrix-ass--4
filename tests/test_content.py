import csv
import io
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.catalogue.models import CATALOGUE_FIELDS
from app.main import app


def make_catalogue_csv(surface: str = "topic", connector: str = "discourse") -> str:
    row = {
        "name": "AI community",
        "url": "https://community.example",
        "platform_key": connector,
        "surface": surface,
        "category": "community",
        "audience_tags": "ai;saas",
        "language": "en",
        "geo": "",
        "est_audience": "1000",
        "rules_url": "",
        "frequency_cap": "3",
        "self_promo_allowed": "false",
        "link_policy": "allow",
        "disclosure_required": "true",
        "automation_allowed": "true",
        "connector_key": connector,
        "relevance_score": "4",
        "relevance_reason": "Discusses AI and SaaS.",
        "level": "L3",
        "last_certified_at": datetime.now(timezone.utc).isoformat(),
    }
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CATALOGUE_FIELDS)
    writer.writeheader()
    writer.writerow(row)
    return output.getvalue()


def import_sample(client: TestClient, body: str) -> None:
    response = client.post(
        "/api/v1/catalogue/import",
        content=body,
        headers={
            "Content-Type": "text/csv",
            "Idempotency-Key": "sample-catalogue",
        },
    )
    assert response.status_code == 201


def test_tailoring_applies_manifest_limits_and_keeps_review_notes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "tailor.db"))
    with TestClient(app) as client:
        import_sample(client, make_catalogue_csv())
        title = "Useful discussion " + "word " * 60
        text = "Helpful explanation " + "content " * 5000
        payload = {
            "content": {
                "title": title,
                "text": text,
                "has_links": False,
                "self_promotional": False,
                "ai_generated": False,
                "ai_disclosure": False,
            },
            "destination_keys": [],
            "limit": 10,
        }

        response = client.post(
            "/api/v1/tailor",
            json=payload,
            headers={"Idempotency-Key": "tailor-request"},
        )

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert len(item["title"]) <= 255
        assert len(item["text"]) <= 32000
        assert item["truncation"] == {"title": True, "text": True}
        assert item["discussion_suggestion"]
        assert item["method"] == "deterministic_manifest_and_rules"


def test_recommendation_scores_topics_and_does_not_trust_catalogue_certification(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "recommend.db"))
    with TestClient(app) as client:
        import_sample(client, make_catalogue_csv())
        payload = {
            "content": {
                "title": "An AI product",
                "text": "How SaaS teams use AI.",
            },
            "topics": ["AI", "SaaS"],
            "limit": 10,
        }
        headers = {"Idempotency-Key": "recommend-request"}

        first = client.post("/api/v1/recommendations", json=payload, headers=headers)
        replay = client.post("/api/v1/recommendations", json=payload, headers=headers)

        assert first.status_code == 200
        assert replay.status_code == 200
        assert first.json() == replay.json()
        item = first.json()["items"][0]
        assert item["matched_topics"] == ["ai", "saas"]
        assert item["eligible_for_automatic_publication"] is False
        assert item["decision"] == "needs_configuration_and_certification"
        assert first.json()["precision_at_10"] is None


def test_tailoring_blocks_content_violating_catalogue_rules(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "rules.db"))
    with TestClient(app) as client:
        body = make_catalogue_csv()
        row = body.splitlines()
        row[1] = row[1].replace(",allow,", ",disallow,")
        import_sample(client, "\n".join(row) + "\n")

        response = client.post(
            "/api/v1/tailor",
            json={
                "content": {
                    "title": "A post",
                    "text": "Read https://example.com/about",
                    "has_links": False,
                }
            },
            headers={"Idempotency-Key": "tailor-blocked"},
        )

        assert response.status_code == 200
        assert response.json()["items"][0]["rule_status"] == "blocked"
        assert response.json()["items"][0]["warnings"][0].startswith(
            "LINKS_NOT_ALLOWED"
        )

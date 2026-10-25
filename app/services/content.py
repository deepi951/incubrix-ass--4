import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from app.api.models import PostingRules, PublicationContent
from app.catalogue.models import CatalogueEntry, normalize_url
from app.connectors.manifests import ConnectorManifest
from app.connectors.implementations import CONNECTOR_TYPES
from app.connectors.manifests import load_manifests
from app.services.rules import RuleViolation, count_recent_posts, validate_content
from app.storage.database import connect


def tailor_for_catalogue(
    content: PublicationContent,
    records: list[dict[str, Any]],
    destination_keys: list[str],
    limit: int,
) -> dict[str, Any]:
    requested = set(destination_keys)
    tailored: list[dict[str, Any]] = []
    for record in records:
        key = f"{record['platform_key']}|{record['url']}"
        if requested and key not in requested:
            continue
        entry = CatalogueEntry.model_validate(record)
        manifest = load_manifests()[entry.connector_key]
        surface = next(
            (item for item in manifest.surfaces if item.key == entry.surface),
            None,
        )
        if surface is None:
            tailored.append(
                {
                    "destination_key": key,
                    "name": entry.name,
                    "status": "unsupported_surface",
                    "warnings": ["The connector manifest does not define this surface."],
                }
            )
            continue

        title, title_truncated = _truncate_at_word_boundary(
            content.title, surface.spec.get("title_max_chars")
        )
        text, text_truncated = _truncate_at_word_boundary(
            content.text, surface.spec.get("body_max_chars")
        )
        tailored_content = content.model_copy(
            update={"title": title, "text": text}
        )
        warnings: list[str] = []
        try:
            validate_content(_rules_from_entry(entry), tailored_content)
            rule_status = "allowed"
        except RuleViolation as exc:
            rule_status = "blocked"
            warnings.append(f"{exc.code}: {exc.message}")

        suggestion = None
        if surface.key in {"topic", "community_post"}:
            suggestion = (
                "Consider opening with a focused question that invites community discussion."
            )
        tailored.append(
            {
                "destination_key": key,
                "name": entry.name,
                "connector_key": entry.connector_key,
                "surface": surface.key,
                "format": _format_for_surface(surface.key),
                "title": title,
                "text": text,
                "canonical_url": content.canonical_url
                if surface.key == "article"
                else None,
                "discussion_suggestion": suggestion,
                "rule_status": rule_status,
                "warnings": warnings,
                "truncation": {
                    "title": title_truncated,
                    "text": text_truncated,
                },
                "method": "deterministic_manifest_and_rules",
            }
        )
        if len(tailored) >= limit:
            break
    return {
        "method": "deterministic_manifest_and_rules",
        "items": tailored,
        "total": len(tailored),
    }


def tailor_rendition(
    content: PublicationContent,
    manifest: ConnectorManifest,
    destination: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    surface_key = destination.get("surface") or manifest.surfaces[0].key
    surface = next(
        (item for item in manifest.surfaces if item.key == surface_key),
        None,
    )
    if surface is None:
        return content.model_dump(), []
    title, title_truncated = _truncate_at_word_boundary(
        content.title, surface.spec.get("title_max_chars")
    )
    text, text_truncated = _truncate_at_word_boundary(
        content.text, surface.spec.get("body_max_chars")
    )
    rendition = {
        "title": title,
        "body": text,
        "text": text,
        "content_type": "text",
        "canonical_url": content.canonical_url,
        "tags": [],
    }
    warnings = []
    if title_truncated:
        warnings.append(
            f"Title was truncated at a word boundary to "
            f"{len(title)} characters for surface '{surface.key}'."
        )
    if text_truncated:
        warnings.append(
            f"Text was truncated at a word boundary to "
            f"{len(text)} characters for surface '{surface.key}'."
        )
    return rendition, warnings


def recommend_destinations(
    content: PublicationContent,
    topics: list[str],
    records: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    normalized_topics = {topic.strip().lower() for topic in topics if topic.strip()}
    recommendations: list[dict[str, Any]] = []
    for record in records:
        entry = CatalogueEntry.model_validate(record)
        matched = sorted(normalized_topics & set(entry.audience_tags))
        topic_fit = (
            1 + 4 * len(matched) / len(normalized_topics)
            if normalized_topics
            else 1
        )
        score = (entry.relevance_score + topic_fit) / 2
        reasons = [
            f"Catalogue relevance {entry.relevance_score}/5: {entry.relevance_reason}"
        ]
        if matched:
            reasons.append(f"Audience topic match: {', '.join(matched)}.")
        else:
            reasons.append("No supplied topic matched this destination's audience tags.")

        eligible, decision, rule_reasons, destination_key = _publication_eligibility(
            entry, content
        )
        reasons.extend(rule_reasons)
        history = _real_publication_history(destination_key)
        history_adjustment = 0.0
        if history["real_attempts"] >= 3:
            success_rate = history["real_successes"] / history["real_attempts"]
            history_adjustment = 0.25 if success_rate >= 0.8 else (
                -0.5 if success_rate < 0.5 else 0.0
            )
            score += history_adjustment
            reasons.append(
                f"Past verified real publication success rate: {success_rate:.0%}."
            )
        else:
            reasons.append("Insufficient verified real publication history to adjust rank.")

        recommendations.append(
            {
                "name": entry.name,
                "url": entry.url,
                "platform_key": entry.platform_key,
                "connector_key": entry.connector_key,
                "score": round(max(1.0, min(5.0, score)), 2),
                "matched_topics": matched,
                "decision": decision,
                "eligible_for_automatic_publication": eligible,
                "manual_action_required": entry.connector_key == "assisted"
                or not entry.automation_allowed,
                "history": history,
                "reasons": reasons,
            }
        )

    recommendations.sort(
        key=lambda item: (
            item["eligible_for_automatic_publication"],
            item["score"],
            item["name"].casefold(),
        ),
        reverse=True,
    )
    selected = recommendations[:limit]
    for rank, item in enumerate(selected, start=1):
        item["rank"] = rank
    return {
        "ranking_method": "deterministic_rules_topics_and_verified_history",
        "topics": sorted(normalized_topics),
        "items": selected,
        "total": len(selected),
        "precision_at_10": None,
        "precision_note": "No labelled evaluation set has been supplied.",
    }


def _publication_eligibility(
    entry: CatalogueEntry, content: PublicationContent
) -> tuple[bool, str, list[str], str | None]:
    reasons: list[str] = []
    try:
        validate_content(_rules_from_entry(entry), content)
    except RuleViolation as exc:
        reasons.append(f"{exc.code}: {exc.message}")
        return False, "blocked_by_rules", reasons, None

    destination_key = _registered_destination_key(entry)
    if entry.connector_key == "assisted":
        return False, "manual_only", ["A person must publish this destination."], destination_key
    if not entry.automation_allowed:
        return (
            False,
            "manual_only",
            ["The destination catalogue does not permit automated posting."],
            None,
        )
    if entry.level not in {"L3", "L4"} or not entry.last_certified_at:
        return (
            False,
            "needs_certification",
            ["The catalogue record is not marked as a natively certified destination."],
            None,
        )
    if not destination_key:
        return (
            False,
            "needs_configuration_and_certification",
            ["No matching configured destination with current real certification was found."],
            None,
        )
    with connect() as connection:
        destination = connection.execute(
            """
            SELECT enabled, last_certified_at FROM destinations WHERE key = ?
            """,
            (destination_key,),
        ).fetchone()
    if not destination or not destination["enabled"]:
        return (
            False,
            "destination_disabled",
            ["Destination is disabled or not configured."],
            destination_key,
        )
    certified_at = destination["last_certified_at"]
    days = int(os.getenv("RECERTIFICATION_DAYS", "30"))
    if not certified_at or datetime.fromisoformat(
        certified_at
    ) < datetime.now(timezone.utc) - timedelta(days=days):
        return (
            False,
            "needs_certification",
            ["No current real-platform/local-instance certification is recorded."],
            destination_key,
        )

    rules = _rules_from_entry(entry)
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(seconds=rules.frequency_window_seconds)
    ).isoformat()
    with connect() as connection:
        recent = count_recent_posts(connection, destination_key, cutoff)
    if recent >= rules.frequency_cap:
        return (
            False,
            "frequency_cap_reached",
            ["Destination frequency cap is currently reached."],
            destination_key,
        )
    return True, "eligible", reasons, destination_key


def _registered_destination_key(entry: CatalogueEntry) -> str | None:
    with connect() as connection:
        destinations = connection.execute(
            """
            SELECT key, connector_key, config_json FROM destinations
            WHERE connector_key = ?
            """,
            (entry.connector_key,),
        ).fetchall()
    for destination in destinations:
        config = json.loads(destination["config_json"])
        url = config.get("base_url") or config.get("url")
        if not isinstance(url, str):
            continue
        try:
            if normalize_url(url) == normalize_url(entry.url):
                return destination["key"]
        except ValueError:
            continue
    return None


def _real_publication_history(destination_key: str | None) -> dict[str, int]:
    if destination_key is None:
        return {"real_attempts": 0, "real_successes": 0}
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT status, result_json FROM jobs
            WHERE destination_key = ? AND status IN ('succeeded', 'failed')
            """,
            (destination_key,),
        ).fetchall()
    real_attempts = 0
    real_successes = 0
    for row in rows:
        if not row["result_json"]:
            continue
        evidence = json.loads(row["result_json"]).get("evidence", {})
        if (
            evidence.get("simulation_mode") is False
            and evidence.get("provider_attempted") is True
        ):
            real_attempts += 1
            if row["status"] == "succeeded":
                real_successes += 1
    return {"real_attempts": real_attempts, "real_successes": real_successes}


def _rules_from_entry(entry: CatalogueEntry) -> PostingRules:
    return PostingRules(
        frequency_cap=entry.frequency_cap,
        frequency_window_seconds=int(
            os.getenv("DEFAULT_FREQUENCY_WINDOW_SECONDS", "604800")
        ),
        self_promo_allowed=entry.self_promo_allowed,
        link_policy=entry.link_policy,
        disclosure_required=entry.disclosure_required,
    )


def _truncate_at_word_boundary(
    value: str, maximum: int | None
) -> tuple[str, bool]:
    if maximum is None or len(value) <= maximum:
        return value, False
    if maximum <= 0:
        return "", True
    prefix = value[:maximum]
    if len(value) > maximum and not value[maximum].isspace():
        boundary = prefix.rfind(" ")
        if boundary > 0:
            prefix = prefix[:boundary]
    return prefix.rstrip(), True


def _format_for_surface(surface: str) -> str:
    if surface in {"topic", "community_post"}:
        return "discussion"
    if surface == "article":
        return "article"
    if surface == "post":
        return "blog_post"
    return "plain_text"

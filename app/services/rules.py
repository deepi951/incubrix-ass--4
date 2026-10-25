import re
import sqlite3
from urllib.parse import urlsplit

from app.api.models import PostingRules, PublicationContent


class RuleViolation(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def validate_content(rules: PostingRules, content: PublicationContent) -> None:
    if content.self_promotional and not rules.self_promo_allowed:
        raise RuleViolation(
            "SELF_PROMOTION_NOT_ALLOWED",
            "This destination does not allow self-promotional content.",
        )
    links = re.findall(r"https?://[^\s<>()]+", content.text)
    if content.canonical_url:
        links.append(content.canonical_url)
    has_links = content.has_links or bool(links)
    if has_links and rules.link_policy == "disallow":
        raise RuleViolation(
            "LINKS_NOT_ALLOWED",
            "This destination does not allow links.",
        )
    if has_links and rules.link_policy == "canonical_only":
        canonical = content.canonical_url
        if not canonical or not links or any(
            _normalize_url(link) != _normalize_url(canonical) for link in links
        ):
            raise RuleViolation(
                "NON_CANONICAL_LINK",
                "Only the configured canonical URL is allowed.",
            )
    if (
        rules.disclosure_required
        and content.ai_generated
        and not content.ai_disclosure
    ):
        raise RuleViolation(
            "AI_DISCLOSURE_REQUIRED",
            "AI-generated content must include an AI disclosure.",
        )


def _normalize_url(value: str) -> str:
    parsed = urlsplit(value.rstrip(".,;!?"))
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def count_recent_posts(
    connection: sqlite3.Connection,
    destination_key: str,
    since: str,
    *,
    exclude_job_id: str | None = None,
    exclude_certification_id: str | None = None,
) -> int:
    jobs = connection.execute(
        """
        SELECT COUNT(*) AS total FROM jobs
        WHERE destination_key = ? AND created_at >= ?
          AND status IN ('queued', 'running', 'succeeded')
          AND (? IS NULL OR id != ?)
        """,
        (destination_key, since, exclude_job_id, exclude_job_id),
    ).fetchone()["total"]
    certifications = connection.execute(
        """
        SELECT COUNT(*) AS total FROM certification_runs
        WHERE destination_key = ? AND created_at >= ?
          AND status IN ('queued', 'running', 'succeeded')
          AND (? IS NULL OR id != ?)
        """,
        (
            destination_key,
            since,
            exclude_certification_id,
            exclude_certification_id,
        ),
    ).fetchone()["total"]
    return jobs + certifications

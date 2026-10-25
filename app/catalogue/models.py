import re
from datetime import datetime
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CatalogueEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    url: str
    platform_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    surface: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=128)
    audience_tags: list[str] = Field(min_length=1)
    language: str = Field(min_length=2, max_length=35)
    geo: str | None = Field(default=None, max_length=128)
    est_audience: int | None = Field(default=None, ge=0)
    rules_url: str | None = None
    frequency_cap: int = Field(ge=1)
    self_promo_allowed: bool
    link_policy: Literal["allow", "disallow", "canonical_only"]
    disclosure_required: bool
    automation_allowed: bool
    connector_key: Literal[
        "discourse", "lemmy", "devto", "ghost", "webhook", "assisted"
    ]
    relevance_score: int = Field(ge=1, le=5)
    relevance_reason: str = Field(min_length=1, max_length=1000)
    level: Literal["L0", "L1", "L2", "L3", "L4"]
    last_certified_at: datetime | None = None

    @field_validator("audience_tags", mode="before")
    @classmethod
    def parse_audience_tags(cls, value):
        if isinstance(value, str):
            value = re.split(r"[,;|]", value)
        if isinstance(value, list):
            return [str(tag).strip().lower() for tag in value if str(tag).strip()]
        return value

    @field_validator("self_promo_allowed", "disclosure_required", "automation_allowed", mode="before")
    @classmethod
    def parse_boolean_fields(cls, value):
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "1"}:
                return True
            if normalized in {"false", "no", "0"}:
                return False
        raise ValueError("Must be true or false.")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        normalize_url(value)
        return value.strip()

    @field_validator("rules_url")
    @classmethod
    def validate_rules_url(cls, value: str | None) -> str | None:
        if value:
            normalize_url(value)
            return value.strip()
        return None


def normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https and include a hostname.")
    if parsed.username or parsed.password:
        raise ValueError("URL must not contain embedded credentials.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL contains an invalid port.") from exc
    hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if port and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"

    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    return urlunsplit(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path.rstrip("/"),
            urlencode(sorted(query)),
            "",
        )
    )


CATALOGUE_FIELDS = tuple(CatalogueEntry.model_fields)

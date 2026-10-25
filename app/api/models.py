from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class PostingRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frequency_cap: int = Field(
        ge=1,
        validation_alias=AliasChoices("frequency_cap", "daily_frequency_cap"),
    )
    frequency_window_seconds: int = Field(default=604800, ge=1)
    self_promo_allowed: bool = Field(
        validation_alias=AliasChoices(
            "self_promo_allowed", "allow_self_promotion"
        )
    )
    link_policy: Literal["allow", "disallow", "canonical_only"]
    disclosure_required: bool = Field(
        validation_alias=AliasChoices(
            "disclosure_required", "require_ai_disclosure"
        )
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_rule_names(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "link_policy" not in normalized and "allow_links" in normalized:
            normalized["link_policy"] = (
                "allow" if normalized.pop("allow_links") else "disallow"
            )
        return normalized


class DestinationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    connector_key: str = Field(min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)
    rules: PostingRules

    @model_validator(mode="after")
    def reject_inline_credentials(self) -> "DestinationCreate":
        credential_fragments = (
            "token",
            "secret",
            "password",
            "api_key",
            "authorization",
        )

        def inspect(value: Any, path: str) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalized_key = str(key).lower()
                    if (
                        any(fragment in normalized_key for fragment in credential_fragments)
                        and not normalized_key.endswith("_env")
                    ):
                        raise ValueError(
                            f"Credential value at config.{path}{key} is not allowed; "
                            "store an environment-variable reference instead."
                        )
                    inspect(nested, f"{path}{key}.")
            elif isinstance(value, list):
                for index, nested in enumerate(value):
                    inspect(nested, f"{path}{index}.")

        inspect(self.config, "")
        return self


class DestinationResponse(BaseModel):
    key: str
    connector_key: str
    rules: PostingRules
    created_at: str
    enabled: bool
    last_certified_at: str | None
    certified_level: str


class PublicationContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=100_000)
    title: str = Field(default="", max_length=10_000)
    has_links: bool = False
    canonical_url: str | None = None
    self_promotional: bool = False
    ai_generated: bool = False
    ai_disclosure: bool = False


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination_key: str
    content: PublicationContent


class TailorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: PublicationContent
    destination_keys: list[str] = Field(default_factory=list, max_length=100)
    limit: int = Field(default=20, ge=1, le=100)


class RecommendationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: PublicationContent
    topics: list[str] = Field(min_length=1, max_length=50)
    limit: int = Field(default=10, ge=1, le=100)


class JobResponse(BaseModel):
    id: str
    destination_key: str
    status: str
    progress: int
    created_at: str
    updated_at: str
    result_url: str
    evidence_url: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail

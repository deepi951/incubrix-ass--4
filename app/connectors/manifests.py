import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.connectors.errors import ConnectorError


class SurfaceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    content_types: list[str] = Field(min_length=1)
    spec: dict[str, Any]


class RateLimitManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: int = Field(gt=0)
    per_seconds: int = Field(gt=0)


class ConnectorManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    version: str
    tier: Literal["native", "protocol", "bridge", "feed", "assisted"]
    auth: Literal["oauth2", "api_key", "bot_token", "none"]
    surfaces: list[SurfaceManifest] = Field(min_length=1)
    rate_limits: dict[str, RateLimitManifest]
    capabilities: list[str]
    error_map: dict[str, str]
    instance_param: str


def load_manifests() -> dict[str, ConnectorManifest]:
    directory = Path(
        os.getenv("CONNECTOR_MANIFEST_DIR", str(Path(__file__).parents[2] / "manifests"))
    )
    if not directory.is_dir():
        raise RuntimeError(f"Connector manifest directory does not exist: {directory}")

    manifests: dict[str, ConnectorManifest] = {}
    for path in sorted(directory.glob("*.yaml")):
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
            manifest = ConnectorManifest.model_validate(value)
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise RuntimeError(f"Invalid connector manifest: {path.name}") from exc
        if manifest.key in manifests:
            raise RuntimeError(f"Duplicate connector manifest key: {manifest.key}")
        manifests[manifest.key] = manifest

    required = {"discourse", "lemmy", "devto", "ghost", "webhook", "assisted"}
    missing = required - manifests.keys()
    if missing:
        raise RuntimeError(f"Missing required connector manifests: {sorted(missing)}")
    return manifests

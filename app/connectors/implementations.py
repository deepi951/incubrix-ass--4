import base64
import hashlib
import hmac
import html
import json
import os
import re
import threading
import time
from collections import deque
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from app.connectors.errors import ConnectorError
from app.connectors.manifests import ConnectorManifest
from app.connectors.simulation import simulated_platform


class Connector:
    _rate_limit_lock = threading.Lock()
    _request_history: dict[str, deque[float]] = {}

    def __init__(self, manifest: ConnectorManifest) -> None:
        self.manifest = manifest
        self.key = manifest.key
        self._request_timeout_limit_seconds: float | None = None

    def validate(
        self,
        rendition: dict[str, Any],
        destination: dict[str, Any],
        connection: dict[str, Any],
    ) -> dict[str, Any]:
        violations: list[dict[str, str]] = []
        surface_key = destination.get("surface") or self.manifest.surfaces[0].key
        surface = next(
            (item for item in self.manifest.surfaces if item.key == surface_key),
            None,
        )
        if surface is None:
            violations.append(
                {
                    "field": "destination.surface",
                    "code": "DEST_SPEC_VIOLATION",
                    "reason": "The requested surface is not supported.",
                }
            )
            return {"ok": False, "violations": violations}

        content_type = str(rendition.get("content_type", "text"))
        if content_type not in surface.content_types:
            violations.append(
                {
                    "field": "rendition.content_type",
                    "code": "DEST_SPEC_VIOLATION",
                    "reason": f"Content type '{content_type}' is not supported.",
                }
            )

        spec = surface.spec
        title = str(rendition.get("title", ""))
        body = str(rendition.get("body", rendition.get("text", "")))
        if len(title) > spec.get("title_max_chars", float("inf")):
            violations.append(
                {
                    "field": "rendition.title",
                    "code": "DEST_SPEC_VIOLATION",
                    "reason": (
                        f"Title exceeds {spec['title_max_chars']} characters."
                    ),
                }
            )
        if len(body) > spec.get("body_max_chars", float("inf")):
            violations.append(
                {
                    "field": "rendition.body",
                    "code": "DEST_SPEC_VIOLATION",
                    "reason": f"Body exceeds {spec['body_max_chars']} characters.",
                }
            )
        links_allowed = spec.get("links_allowed")
        if links_allowed is False and re.search(r"https?://\S+", body):
            violations.append(
                {
                    "field": "rendition.body",
                    "code": "DEST_SPEC_VIOLATION",
                    "reason": "Links are not allowed on this destination surface.",
                }
            )
        return {"ok": not violations, "violations": violations}

    def publish(
        self,
        rendition: dict[str, Any],
        destination: dict[str, Any],
        connection: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        validation = self.validate(rendition, destination, connection)
        if not validation["ok"]:
            raise ConnectorError(
                "DEST_SPEC_VIOLATION",
                "Rendition does not meet destination format requirements.",
                http_status=422,
                details={"violations": validation["violations"]},
            )
        if self._simulation_enabled(connection):
            return simulated_platform.publish(
                self.key, destination, rendition, idempotency_key
            )
        return self._publish_real(rendition, destination, connection, idempotency_key)

    def get_status(
        self, platform_post_id: str, connection: dict[str, Any]
    ) -> dict[str, Any]:
        if self._simulation_enabled(connection):
            return simulated_platform.get_status(platform_post_id)
        return self._get_status_real(platform_post_id, connection)

    def delete(
        self, platform_post_id: str, connection: dict[str, Any]
    ) -> dict[str, bool]:
        if self._simulation_enabled(connection):
            return simulated_platform.delete(platform_post_id)
        return self._delete_real(platform_post_id, connection)

    def fetch_metrics(
        self, platform_post_id: str, connection: dict[str, Any], since: str
    ) -> dict[str, int | None]:
        raise ConnectorError(
            "CONNECTOR_METHOD_UNSUPPORTED",
            f"Connector '{self.key}' does not provide metrics.",
            http_status=501,
        )

    def refresh_auth(self, connection: dict[str, Any]) -> dict[str, Any]:
        raise ConnectorError(
            "CONNECTOR_METHOD_UNSUPPORTED",
            f"Connector '{self.key}' does not refresh authentication.",
            http_status=501,
        )

    def build_assist_pack(
        self, rendition: dict[str, Any], destination: dict[str, Any]
    ) -> dict[str, Any]:
        raise ConnectorError(
            "CONNECTOR_METHOD_UNSUPPORTED",
            f"Connector '{self.key}' does not build assisted posting packs.",
            http_status=501,
        )

    @staticmethod
    def _simulation_enabled(connection: dict[str, Any]) -> bool:
        return os.getenv("SIMULATION_MODE", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _base_url(
        destination: dict[str, Any] | None = None,
        connection: dict[str, Any] | None = None,
    ) -> str:
        candidate = ""
        if destination:
            candidate = str(destination.get("base_url") or destination.get("url") or "")
        if not candidate and connection:
            candidate = str(connection.get("base_url") or "")
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConnectorError(
                "PLATFORM_UNAVAILABLE",
                "A valid HTTP or HTTPS destination base URL is required.",
                retryable=False,
                http_status=422,
            )
        if parsed.username or parsed.password:
            raise ConnectorError(
                "PLATFORM_UNAVAILABLE",
                "Credentials must not be embedded in destination URLs.",
                retryable=False,
                http_status=422,
            )
        return candidate.rstrip("/") + "/"

    def _request(
        self,
        method: str,
        url: str,
        connection: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[httpx.Response, dict[str, Any]]:
        self._enforce_configured_rate_limit(url, connection)
        request_headers = dict(headers or {})
        if idempotency_key:
            request_headers["Idempotency-Key"] = idempotency_key
        timeout = float(os.getenv("CONNECTOR_TIMEOUT_SECONDS", "10"))
        if self._request_timeout_limit_seconds is not None:
            timeout = min(timeout, self._request_timeout_limit_seconds)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                response = client.request(
                    method,
                    url,
                    headers=request_headers,
                    json=json_body if content is None else None,
                    content=content,
                )
        except httpx.TimeoutException as exc:
            raise ConnectorError(
                "PLATFORM_UNAVAILABLE",
                "The platform request timed out; its outcome may be unknown.",
                retryable=True,
                details={"outcome_ambiguous": method.upper() in {"POST", "PUT"}},
            ) from exc
        except httpx.RequestError as exc:
            raise ConnectorError(
                "PLATFORM_UNAVAILABLE",
                "The platform could not be reached.",
                retryable=True,
                details={
                    "failure_type": type(exc).__name__,
                    "outcome_ambiguous": method.upper() in {"POST", "PUT"},
                },
            ) from exc

        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}

        upstream_code = self._upstream_error_code(response, data)
        if upstream_code == "PLATFORM_QUOTA_EXHAUSTED":
            reset = (
                response.headers.get("Retry-After")
                or response.headers.get("X-RateLimit-Reset")
                or response.headers.get("X-Quota-Reset")
            )
            retry_after = self._retry_after_seconds(reset)
            details: dict[str, Any] = {"retry_after": "next_window"}
            if reset:
                details["reset_time"] = reset
            if retry_after is not None:
                details["retry_after_seconds"] = retry_after
            raise ConnectorError(
                "PLATFORM_QUOTA_EXHAUSTED",
                "The platform quota is exhausted; retry in the next quota window.",
                retryable=True,
                http_status=429,
                details=details,
            )

        if response.status_code == 401:
            raise ConnectorError(
                "CONNECTION_AUTH_EXPIRED",
                "The platform rejected the connection credentials.",
                http_status=401,
            )
        if response.status_code == 429:
            reset = response.headers.get("Retry-After") or response.headers.get(
                "X-RateLimit-Reset"
            )
            retry_after = self._retry_after_seconds(reset)
            details = {"reset_time": reset} if reset else {}
            if retry_after is not None:
                details["retry_after_seconds"] = retry_after
            raise ConnectorError(
                "PLATFORM_RATE_LIMIT",
                "The platform rate limit was reached.",
                retryable=True,
                http_status=429,
                details=details,
            )
        if response.status_code == 422:
            reason = self._safe_platform_message(data, connection)
            raise ConnectorError(
                "PLATFORM_CONTENT_REJECTED",
                reason or "The platform rejected the submitted content.",
                http_status=422,
            )
        if response.status_code == 408 or response.status_code >= 500:
            details = {}
            if response.status_code == 408 and method.upper() in {"POST", "PUT"}:
                details["outcome_ambiguous"] = True
            raise ConnectorError(
                "PLATFORM_UNAVAILABLE",
                (
                    "The platform request timed out; its outcome may be unknown."
                    if response.status_code == 408
                    else "The platform is temporarily unavailable."
                ),
                retryable=True,
                http_status=502,
                details=details,
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ConnectorError(
                "PLATFORM_CONTENT_REJECTED",
                "The platform rejected the request.",
                http_status=response.status_code,
                details={"platform_status": response.status_code},
            ) from exc
        return response, data if isinstance(data, dict) else {}

    def _enforce_configured_rate_limit(
        self, url: str, connection: dict[str, Any]
    ) -> None:
        policy = self.manifest.rate_limits.get("per_connection")
        if policy is None:
            return

        endpoint = urlparse(url)
        connection_identity = hashlib.sha256(
            json.dumps(connection, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        bucket_key = f"{self.key}:{endpoint.scheme}://{endpoint.netloc}:{connection_identity}"
        now = time.monotonic()
        window_start = now - policy.per_seconds
        with self._rate_limit_lock:
            history = self._request_history.setdefault(bucket_key, deque())
            while history and history[0] <= window_start:
                history.popleft()
            if len(history) >= policy.requests:
                retry_after = max(
                    1,
                    int(policy.per_seconds - (now - history[0]) + 0.999),
                )
                raise ConnectorError(
                    "PLATFORM_RATE_LIMIT",
                    "The configured connector request limit was reached.",
                    retryable=True,
                    http_status=429,
                    details={
                        "retry_after_seconds": retry_after,
                        "configured_limit": {
                            "requests": policy.requests,
                            "per_seconds": policy.per_seconds,
                        },
                    },
                )
            history.append(now)

    def _upstream_error_code(
        self, response: httpx.Response, data: dict[str, Any]
    ) -> str | None:
        candidates = [
            response.headers.get("X-Platform-Error-Code"),
            response.headers.get("X-Error-Code"),
            data.get("code"),
            data.get("error_code"),
        ]
        error = data.get("error")
        if isinstance(error, dict):
            candidates.extend((error.get("code"), error.get("error_code")))
        elif isinstance(error, str):
            candidates.append(error)

        error_map = self.manifest.error_map
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            normalized = candidate.strip().upper().replace("-", "_").replace(" ", "_")
            if normalized == "PLATFORM_QUOTA_EXHAUSTED":
                return normalized
            for upstream, shared in error_map.items():
                if (
                    upstream.strip().upper().replace("-", "_") == normalized
                    and shared == "PLATFORM_QUOTA_EXHAUSTED"
                ):
                    return shared
        return None

    @staticmethod
    def _retry_after_seconds(value: str | None) -> int | None:
        if not value:
            return None
        try:
            numeric_value = float(value)
            if numeric_value > 1_000_000_000:
                return max(0, int(numeric_value - time.time()))
            return max(0, int(numeric_value))
        except ValueError:
            try:
                reset_at = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if reset_at.tzinfo is None:
                reset_at = reset_at.replace(tzinfo=timezone.utc)
            return max(
                0,
                int((reset_at - datetime.now(timezone.utc)).total_seconds()),
            )

    @staticmethod
    def _safe_platform_message(
        data: dict[str, Any], connection: dict[str, Any]
    ) -> str:
        message = data.get("message") or data.get("error") or data.get("errors")
        if not isinstance(message, str):
            return ""
        safe_message = message[:500]
        for key, value in connection.items():
            if any(term in key.lower() for term in ("token", "secret", "key", "password")):
                if isinstance(value, str) and value:
                    safe_message = safe_message.replace(value, "[REDACTED]")
        return safe_message

    def _publish_real(
        self,
        rendition: dict[str, Any],
        destination: dict[str, Any],
        connection: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _get_status_real(
        self, platform_post_id: str, connection: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _delete_real(
        self, platform_post_id: str, connection: dict[str, Any]
    ) -> dict[str, bool]:
        raise NotImplementedError


class DiscourseConnector(Connector):
    def _publish_real(self, rendition, destination, connection, idempotency_key):
        base = self._base_url(destination, connection)
        api_key = str(connection.get("api_key") or "")
        username = str(connection.get("username") or "")
        if not api_key or not username:
            raise ConnectorError(
                "CONNECTION_AUTH_EXPIRED",
                "Discourse requires an API key and username.",
                http_status=401,
            )
        headers = {"Api-Key": api_key, "Api-Username": username}
        _, data = self._request(
            "POST",
            urljoin(base, "posts.json"),
            connection,
            headers=headers,
            json_body={
                "title": rendition.get("title", ""),
                "raw": rendition.get("body", rendition.get("text", "")),
                "category": destination.get("category_id"),
            },
            idempotency_key=idempotency_key,
        )
        post_id = data.get("id")
        if post_id is None:
            raise ConnectorError(
                "PLATFORM_UNAVAILABLE",
                "Discourse response did not include a post ID.",
                retryable=True,
            )
        slug = data.get("topic_slug") or "t"
        topic_id = data.get("topic_id") or post_id
        return {
            "platform_post_id": str(post_id),
            "live_url": urljoin(base, f"{slug}/{topic_id}"),
            "status": "published",
        }

    def _get_status_real(self, platform_post_id, connection):
        base = self._base_url(connection=connection)
        headers = {
            "Api-Key": str(connection.get("api_key") or ""),
            "Api-Username": str(connection.get("username") or ""),
        }
        _, data = self._request(
            "GET",
            urljoin(base, f"posts/{quote(platform_post_id, safe='')}.json"),
            connection,
            headers=headers,
        )
        post = data.get("post", data)
        return {
            "status": "published" if post.get("id") else "not_found",
            "live_url": post.get("url"),
        }

    def _delete_real(self, platform_post_id, connection):
        base = self._base_url(connection=connection)
        headers = {
            "Api-Key": str(connection.get("api_key") or ""),
            "Api-Username": str(connection.get("username") or ""),
        }
        self._request(
            "DELETE",
            urljoin(base, f"posts/{quote(platform_post_id, safe='')}.json"),
            connection,
            headers=headers,
        )
        return {"deleted": True}

    def fetch_metrics(self, platform_post_id, connection, since):
        if self._simulation_enabled(connection):
            return _empty_metrics()
        base = self._base_url(connection=connection)
        headers = {
            "Api-Key": str(connection.get("api_key") or ""),
            "Api-Username": str(connection.get("username") or ""),
        }
        _, post_data = self._request(
            "GET",
            urljoin(base, f"posts/{quote(platform_post_id, safe='')}.json"),
            connection,
            headers=headers,
        )
        post = post_data.get("post", post_data)
        topic_id = post.get("topic_id")
        if topic_id is None:
            return _empty_metrics()
        _, topic_data = self._request(
            "GET",
            urljoin(base, f"t/{quote(str(topic_id), safe='')}.json"),
            connection,
            headers=headers,
        )
        return {
            "views": _integer_or_none(topic_data.get("views")),
            "likes": _integer_or_none(topic_data.get("like_count")),
            "comments": max(0, _integer_or_none(topic_data.get("posts_count")) - 1)
            if _integer_or_none(topic_data.get("posts_count")) is not None
            else None,
            "shares": None,
            "saves": None,
            "watch_time_s": None,
            "clicks": None,
        }


class LemmyConnector(Connector):
    def _headers(self, connection):
        token = str(connection.get("access_token") or "")
        if not token:
            raise ConnectorError(
                "CONNECTION_AUTH_EXPIRED",
                "Lemmy requires a valid access token.",
                http_status=401,
            )
        return {"Authorization": f"Bearer {token}"}

    def _publish_real(self, rendition, destination, connection, idempotency_key):
        community_id = destination.get("community_id")
        if community_id is None:
            raise ConnectorError(
                "DEST_SPEC_VIOLATION",
                "Lemmy destination requires community_id.",
                http_status=422,
                details={"field": "destination.community_id"},
            )
        base = self._base_url(destination, connection)
        _, data = self._request(
            "POST",
            urljoin(base, "api/v3/post"),
            connection,
            headers=self._headers(connection),
            json_body={
                "community_id": community_id,
                "name": rendition.get("title") or rendition.get("text", "")[:200],
                "body": rendition.get("body", rendition.get("text", "")),
                "url": rendition.get("url"),
            },
            idempotency_key=idempotency_key,
        )
        post_view = data.get("post_view", {})
        post = post_view.get("post", {}) if isinstance(post_view, dict) else {}
        post_id = post.get("id") or data.get("post_id")
        if post_id is None:
            raise ConnectorError(
                "PLATFORM_UNAVAILABLE",
                "Lemmy response did not include a post ID.",
                retryable=True,
            )
        return {
            "platform_post_id": str(post_id),
            "live_url": urljoin(base, f"post/{post_id}"),
            "status": "published",
        }

    def _get_status_real(self, platform_post_id, connection):
        base = self._base_url(connection=connection)
        _, data = self._request(
            "GET",
            urljoin(base, f"api/v3/post?id={quote(platform_post_id, safe='')}"),
            connection,
            headers=self._headers(connection),
        )
        post_view = data.get("post_view", {})
        post = post_view.get("post", {}) if isinstance(post_view, dict) else {}
        return {
            "status": "published" if post.get("id") else "not_found",
            "live_url": urljoin(base, f"post/{platform_post_id}")
            if post.get("id")
            else None,
        }

    def _delete_real(self, platform_post_id, connection):
        base = self._base_url(connection=connection)
        self._request(
            "POST",
            urljoin(base, "api/v3/post/delete"),
            connection,
            headers=self._headers(connection),
            json_body={"post_id": int(platform_post_id)},
        )
        return {"deleted": True}

    def fetch_metrics(self, platform_post_id, connection, since):
        if self._simulation_enabled(connection):
            return _empty_metrics()
        base = self._base_url(connection=connection)
        _, data = self._request(
            "GET",
            urljoin(base, f"api/v3/post?id={quote(platform_post_id, safe='')}"),
            connection,
            headers=self._headers(connection),
        )
        post_view = data.get("post_view", {})
        counts = post_view.get("counts", {}) if isinstance(post_view, dict) else {}
        return {
            "views": _integer_or_none(counts.get("views")),
            "likes": _integer_or_none(counts.get("upvotes")),
            "comments": _integer_or_none(counts.get("comments")),
            "shares": None,
            "saves": None,
            "watch_time_s": None,
            "clicks": None,
        }


class DevtoConnector(Connector):
    def _headers(self, connection):
        api_key = str(connection.get("api_key") or "")
        if not api_key:
            raise ConnectorError(
                "CONNECTION_AUTH_EXPIRED",
                "Dev.to requires an API key.",
                http_status=401,
            )
        return {"api-key": api_key}

    def _publish_real(self, rendition, destination, connection, idempotency_key):
        base = self._base_url(destination, connection)
        article = {
            "title": rendition.get("title", ""),
            "body_markdown": rendition.get("body", rendition.get("text", "")),
            "published": True,
        }
        if rendition.get("tags"):
            article["tags"] = rendition["tags"]
        _, data = self._request(
            "POST",
            urljoin(base, "api/articles"),
            connection,
            headers=self._headers(connection),
            json_body={"article": article},
            idempotency_key=idempotency_key,
        )
        if data.get("id") is None:
            raise ConnectorError(
                "PLATFORM_UNAVAILABLE",
                "Dev.to response did not include an article ID.",
                retryable=True,
            )
        return {
            "platform_post_id": str(data["id"]),
            "live_url": data.get("url"),
            "status": "published",
        }

    def _get_status_real(self, platform_post_id, connection):
        base = self._base_url(connection=connection)
        _, data = self._request(
            "GET",
            urljoin(base, f"api/articles/{quote(platform_post_id, safe='')}"),
            connection,
            headers=self._headers(connection),
        )
        return {
            "status": "published" if data.get("id") else "not_found",
            "live_url": data.get("url"),
        }

    def _delete_real(self, platform_post_id, connection):
        base = self._base_url(connection=connection)
        self._request(
            "DELETE",
            urljoin(base, f"api/articles/{quote(platform_post_id, safe='')}"),
            connection,
            headers=self._headers(connection),
        )
        return {"deleted": True}

    def fetch_metrics(self, platform_post_id, connection, since):
        if self._simulation_enabled(connection):
            return {
                "views": None,
                "likes": None,
                "comments": None,
                "shares": None,
                "saves": None,
                "watch_time_s": None,
                "clicks": None,
            }
        base = self._base_url(connection=connection)
        _, data = self._request(
            "GET",
            urljoin(base, f"api/articles/{quote(platform_post_id, safe='')}"),
            connection,
            headers=self._headers(connection),
        )
        return {
            "views": data.get("page_views_count"),
            "likes": data.get("public_reactions_count"),
            "comments": data.get("comments_count"),
            "shares": None,
            "saves": None,
            "watch_time_s": None,
            "clicks": None,
        }


class GhostConnector(Connector):
    @staticmethod
    def _ghost_token(connection: dict[str, Any]) -> str:
        admin_api_key = str(connection.get("admin_api_key") or "")
        try:
            key_id, secret = admin_api_key.split(":", maxsplit=1)
            secret_bytes = bytes.fromhex(secret)
        except ValueError as exc:
            raise ConnectorError(
                "CONNECTION_AUTH_EXPIRED",
                "Ghost admin_api_key must contain a key ID and hex secret.",
                http_status=401,
            ) from exc

        header = {"alg": "HS256", "typ": "JWT", "kid": key_id}
        now = int(time.time())
        payload = {"iat": now, "exp": now + 300, "aud": "/admin/"}
        encoded_header = base64.urlsafe_b64encode(
            json.dumps(header, separators=(",", ":")).encode()
        ).rstrip(b"=")
        encoded_payload = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).rstrip(b"=")
        signing_input = encoded_header + b"." + encoded_payload
        signature = hmac.new(secret_bytes, signing_input, hashlib.sha256).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
        return (signing_input + b"." + encoded_signature).decode("ascii")

    def _headers(self, connection):
        return {"Authorization": f"Ghost {self._ghost_token(connection)}"}

    def _publish_real(self, rendition, destination, connection, idempotency_key):
        base = self._base_url(destination, connection)
        body = str(rendition.get("body", rendition.get("text", "")))
        post = {
            "title": rendition.get("title", ""),
            "html": rendition.get("html") or f"<p>{html.escape(body)}</p>",
            "status": "published",
        }
        _, data = self._request(
            "POST",
            urljoin(base, "ghost/api/admin/posts/?source=html"),
            connection,
            headers=self._headers(connection),
            json_body={"posts": [post]},
            idempotency_key=idempotency_key,
        )
        posts = data.get("posts", [])
        if not posts or posts[0].get("id") is None:
            raise ConnectorError(
                "PLATFORM_UNAVAILABLE",
                "Ghost response did not include a post ID.",
                retryable=True,
            )
        created = posts[0]
        return {
            "platform_post_id": str(created["id"]),
            "live_url": created.get("url"),
            "status": created.get("status", "published"),
        }

    def _get_status_real(self, platform_post_id, connection):
        base = self._base_url(connection=connection)
        _, data = self._request(
            "GET",
            urljoin(
                base,
                f"ghost/api/admin/posts/{quote(platform_post_id, safe='')}/",
            ),
            connection,
            headers=self._headers(connection),
        )
        posts = data.get("posts", [])
        post = posts[0] if posts else {}
        return {"status": post.get("status", "not_found"), "live_url": post.get("url")}

    def _delete_real(self, platform_post_id, connection):
        base = self._base_url(connection=connection)
        self._request(
            "DELETE",
            urljoin(
                base,
                f"ghost/api/admin/posts/{quote(platform_post_id, safe='')}/",
            ),
            connection,
            headers=self._headers(connection),
        )
        return {"deleted": True}


class WebhookConnector(Connector):
    def _publish_real(self, rendition, destination, connection, idempotency_key):
        url = str(destination.get("url") or destination.get("base_url") or "")
        secret = str(connection.get("hmac_secret") or "")
        if not secret:
            raise ConnectorError(
                "CONNECTION_AUTH_EXPIRED",
                "A webhook HMAC secret is required.",
                http_status=401,
            )
        payload = {
            "rendition": rendition,
            "destination": {
                key: value
                for key, value in destination.items()
                if key not in {"secret", "token", "password"}
            },
            "idempotency_key": idempotency_key,
        }
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        response, data = self._request(
            "POST",
            url,
            connection,
            headers={
                "Content-Type": "application/json",
                "X-IncuBrix-Signature": f"sha256={signature}",
            },
            content=body,
            idempotency_key=idempotency_key,
        )
        post_id = data.get("platform_post_id")
        if not post_id:
            raise ConnectorError(
                "PLATFORM_CONTENT_REJECTED",
                "Webhook response must include platform_post_id.",
                http_status=422,
            )
        return {
            "platform_post_id": str(post_id),
            "live_url": data.get("live_url"),
            "status": data.get("status", "published"),
        }

    def _get_status_real(self, platform_post_id, connection):
        url_template = connection.get("status_url")
        if not url_template:
            raise ConnectorError(
                "CONNECTOR_METHOD_UNSUPPORTED",
                "Configure status_url to retrieve webhook publication status.",
                http_status=501,
            )
        url = str(url_template).replace(
            "{platform_post_id}", quote(platform_post_id, safe="")
        )
        _, data = self._request("GET", url, connection, headers=self._auth_headers(connection))
        return {"status": data.get("status", "unknown"), "live_url": data.get("live_url")}

    def _delete_real(self, platform_post_id, connection):
        url_template = connection.get("delete_url")
        if not url_template:
            raise ConnectorError(
                "CONNECTOR_METHOD_UNSUPPORTED",
                "Configure delete_url to delete webhook publications.",
                http_status=501,
            )
        url = str(url_template).replace(
            "{platform_post_id}", quote(platform_post_id, safe="")
        )
        self._request(
            "DELETE",
            url,
            connection,
            headers=self._auth_headers(connection),
        )
        return {"deleted": True}

    @staticmethod
    def _auth_headers(connection):
        token = connection.get("bearer_token")
        return {"Authorization": f"Bearer {token}"} if token else {}


class AssistedConnector(Connector):
    def publish(self, rendition, destination, connection, idempotency_key):
        pack = self.build_assist_pack(rendition, destination)
        return {
            "platform_post_id": None,
            "live_url": pack["deep_link"],
            "status": "requires_manual_action",
        }

    def get_status(self, platform_post_id, connection):
        return {"status": "requires_manual_action", "live_url": None}

    def delete(self, platform_post_id, connection):
        return {"deleted": False}

    def build_assist_pack(self, rendition, destination):
        title = str(rendition.get("title", "")).strip()
        body = str(rendition.get("body", rendition.get("text", ""))).strip()
        markdown = "\n\n".join(part for part in (f"# {title}" if title else "", body) if part)
        checklist = [
            "Review the destination's current posting rules before submitting.",
            "Confirm the content and any links are permitted.",
            "Post manually; this connector does not publish automatically.",
        ]
        if destination.get("disclosure_required"):
            checklist.append("Add the required AI-content disclosure.")
        return {
            "files": [
                {
                    "name": "ready-to-post.md",
                    "content_type": "text/markdown",
                    "content": markdown,
                }
            ],
            "caption": body,
            "deep_link": destination.get("submit_url"),
            "checklist": checklist,
        }


CONNECTOR_TYPES = {
    "discourse": DiscourseConnector,
    "lemmy": LemmyConnector,
    "devto": DevtoConnector,
    "ghost": GhostConnector,
    "webhook": WebhookConnector,
    "assisted": AssistedConnector,
}


def _integer_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _empty_metrics() -> dict[str, int | None]:
    return {
        "views": None,
        "likes": None,
        "comments": None,
        "shares": None,
        "saves": None,
        "watch_time_s": None,
        "clicks": None,
    }

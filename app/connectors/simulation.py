import hashlib
import threading
from typing import Any


class SimulatedPlatform:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._posts: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[tuple[str, str, str], str] = {}

    def publish(
        self,
        connector_key: str,
        destination: dict[str, Any],
        rendition: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        destination_id = str(
            destination.get("key")
            or destination.get("url")
            or destination.get("base_url")
            or ""
        )
        key = (connector_key, destination_id, idempotency_key)
        with self._lock:
            existing_id = self._idempotency.get(key)
            if existing_id:
                return dict(self._posts[existing_id]["result"])

            digest = hashlib.sha256(
                f"{connector_key}:{destination_id}:{idempotency_key}".encode()
            ).hexdigest()[:20]
            post_id = f"sim-{digest}"
            result = {
                "platform_post_id": post_id,
                "live_url": f"mock://{connector_key}/{post_id}",
                "status": "published",
            }
            self._posts[post_id] = {
                "connector_key": connector_key,
                "destination": destination,
                "rendition": rendition,
                "result": result,
                "status": "published",
            }
            self._idempotency[key] = post_id
            return dict(result)

    def get_status(self, platform_post_id: str) -> dict[str, Any]:
        with self._lock:
            post = self._posts.get(platform_post_id)
            if post is None:
                return {"status": "not_found", "live_url": None}
            return {
                "status": post["status"],
                "live_url": post["result"]["live_url"],
            }

    def delete(self, platform_post_id: str) -> dict[str, bool]:
        with self._lock:
            post = self._posts.get(platform_post_id)
            if post is None:
                return {"deleted": False}
            post["status"] = "deleted"
            return {"deleted": True}


simulated_platform = SimulatedPlatform()

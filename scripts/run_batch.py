"""Submit JSON publication inputs and save their status, result, and evidence."""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


TERMINAL_STATES = {"succeeded", "failed"}


def request_json(url: str, *, method: str = "GET", payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
        headers["Idempotency-Key"] = str(uuid4())
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            response_body = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            response_body = {"message": exc.reason}
        raise RuntimeError(
            f"HTTP {exc.code} from {url}: "
            f"{json.dumps(response_body, ensure_ascii=False)}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach API at {url}: {exc.reason}") from exc


def read_input(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON input {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Input must be a JSON object: {path}")
    if not isinstance(payload.get("destination_key"), str) or not payload["destination_key"]:
        raise ValueError(f"Input requires a non-empty destination_key: {path}")
    if not isinstance(payload.get("content"), dict):
        raise ValueError(f"Input requires a content object: {path}")
    if not isinstance(payload["content"].get("text"), str) or not payload["content"]["text"].strip():
        raise ValueError(f"Input content requires non-empty text: {path}")
    return {"destination_key": payload["destination_key"], "content": payload["content"]}


def run_one(input_path: Path, output_dir: Path, api_url: str, poll_seconds: float, timeout_seconds: int) -> bool:
    payload = read_input(input_path)
    base_url = api_url.rstrip("/")
    submitted = request_json(f"{base_url}/api/v1/jobs", method="POST", payload=payload)
    job_id = submitted.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError(f"API response for {input_path.name} did not include a job id.")

    logging.info("job_accepted input=%s job_id=%s", input_path.name, job_id)
    deadline = time.monotonic() + timeout_seconds
    latest = submitted
    while latest.get("status") not in TERMINAL_STATES:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Job {job_id} did not finish within {timeout_seconds} seconds.")
        time.sleep(poll_seconds)
        latest = request_json(f"{base_url}/api/v1/jobs/{job_id}")

    output = {"input": input_path.name, "job": latest}
    for field in ("result_url", "evidence_url"):
        endpoint = latest.get(field)
        if isinstance(endpoint, str):
            output[field.removesuffix("_url")] = request_json(f"{base_url}{endpoint}")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{job_id}.json"
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logging.info("job_finished input=%s job_id=%s status=%s output=%s", input_path.name, job_id, latest["status"], output_path)
    return latest["status"] == "succeeded"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON input file or directory of JSON files")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000", help="FastAPI base URL")
    parser.add_argument("--output-dir", type=Path, default=Path("test_pack/outputs"))
    parser.add_argument("--log-file", type=Path, default=Path("test_pack/logs/batch-runner.log"))
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args()

    if args.poll_seconds <= 0 or args.timeout_seconds <= 0:
        parser.error("poll interval and timeout must be positive.")
    inputs = sorted(args.input.glob("*.json")) if args.input.is_dir() else [args.input]
    if not inputs or any(not path.is_file() for path in inputs):
        parser.error(f"No readable JSON input files found at {args.input}.")

    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(args.log_file, encoding="utf-8"), logging.StreamHandler()],
    )
    succeeded = 0
    for input_path in inputs:
        try:
            succeeded += run_one(
                input_path,
                args.output_dir,
                args.api_url,
                args.poll_seconds,
                args.timeout_seconds,
            )
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            logging.error("batch_item_failed input=%s error=%s", input_path.name, exc)

    logging.info("batch_finished total=%d succeeded=%d failed=%d", len(inputs), succeeded, len(inputs) - succeeded)
    return 0 if succeeded == len(inputs) else 1


if __name__ == "__main__":
    raise SystemExit(main())

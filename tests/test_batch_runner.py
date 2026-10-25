import json
from pathlib import Path

import pytest

from scripts.run_batch import read_input, run_one


def test_read_input_requires_destination_and_content(tmp_path: Path) -> None:
    input_path = tmp_path / "missing.json"
    input_path.write_text(json.dumps({"content": {"text": "hello"}}), encoding="utf-8")

    with pytest.raises(ValueError, match="destination_key"):
        read_input(input_path)


def test_run_one_persists_terminal_status_result_and_evidence(tmp_path: Path, monkeypatch) -> None:
    input_path = tmp_path / "publish.json"
    input_path.write_text(
        json.dumps({"destination_key": "community", "content": {"text": "A useful post."}}),
        encoding="utf-8",
    )
    responses = iter(
        [
            {"id": "job-1", "status": "queued", "result_url": "/result", "evidence_url": "/evidence"},
            {
                "id": "job-1",
                "status": "succeeded",
                "progress": 100,
                "result_url": "/result",
                "evidence_url": "/evidence",
            },
            {"url": "mock://post"},
            {"created": True},
        ]
    )
    requested: list[str] = []

    def fake_request(url: str, **_kwargs) -> dict:
        requested.append(url)
        return next(responses)

    monkeypatch.setattr("scripts.run_batch.request_json", fake_request)
    monkeypatch.setattr("scripts.run_batch.time.sleep", lambda _seconds: None)

    assert run_one(input_path, tmp_path / "outputs", "http://api", 0.01, 5) is True
    output = json.loads((tmp_path / "outputs" / "job-1.json").read_text(encoding="utf-8"))
    assert output["job"]["status"] == "succeeded"
    assert output["result"] == {"url": "mock://post"}
    assert output["evidence"] == {"created": True}
    assert requested == [
        "http://api/api/v1/jobs",
        "http://api/api/v1/jobs/job-1",
        "http://api/result",
        "http://api/evidence",
    ]

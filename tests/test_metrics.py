import json

from utils.metrics import classify_error, log_event


def test_generation_metric_is_append_only_jsonl(monkeypatch, tmp_path):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("METRICS_PATH", str(path))
    monkeypatch.setenv("APP_VERSION", "test-version")

    assert log_event(
        "generation_completed",
        job_id="job-1",
        operator_id="operator",
        products_count=2,
        variants_count=20,
        skus_count=20,
        duration_ms=125,
    )

    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["event_type"] == "generation_completed"
    assert row["job_id"] == "job-1"
    assert row["products_count"] == 2
    assert row["app_version"] == "test-version"


def test_duplicate_and_rate_limit_classification():
    assert classify_error("SKU already used duplicate") == "duplicate"
    assert classify_error("HTTP 429") == "rate_limit"

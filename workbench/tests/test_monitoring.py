from autotriager import monitoring


def source(series, expected):
    return {
        "series": series,
        "expected_latest_bin_start": expected,
        "service": "shop",
        "metric": "latency",
        "unit": "ms",
        "bin_seconds": 300,
        "threshold": 19,
    }


def test_live_forecasts_are_prefix_stable_and_alert_types_distinct():
    prefix = [(index * 300, 10 + index) for index in range(10)]
    first = monitoring._live_monitoring(source(prefix, 2700))["cases"][0]["frames"]
    extended = monitoring._live_monitoring(source(prefix + [(3000, 200)], 3000))["cases"][0]["frames"]
    assert extended[:-1] == first
    assert first[0]["forecast"] is None
    assert first[-1]["forecast"] == 20
    assert first[-1]["state"] == "forecast_risk"
    assert extended[-1]["state"] == "observed_breach"


def test_stale_or_gapped_live_series_pauses_current_forecast():
    prefix = [(index * 300, 10 + index) for index in range(10)]
    stale = monitoring._live_monitoring(source(prefix, 3000))
    gapped = monitoring._live_monitoring(source(prefix + [(3300, 22)], 3300))
    assert stale["freshness"] == "stale"
    assert gapped["freshness"] == "gap"
    for payload in (stale, gapped):
        assert payload["cases"][0]["frames"][-1]["forecast"] is None
        assert payload["cases"][0]["frames"][0]["value"] == 10


def test_configured_source_failure_does_not_silently_switch_to_replay(tmp_path, monkeypatch):
    def unavailable():
        raise ValueError("private source detail")

    monkeypatch.setattr(monitoring, "live_metrics", unavailable)
    assert monitoring.monitoring_data(tmp_path) == {
        "schema_version": "autotriager.api.v1", "mode": "live_unavailable", "cases": [],
    }


def test_replay_archive_has_explicit_missing_and_corrupt_states(tmp_path, monkeypatch):
    monkeypatch.setattr(monitoring, "live_metrics", lambda: None)
    assert monitoring.monitoring_data(tmp_path)["mode"] == "replay_unavailable"
    archive = tmp_path / monitoring.ARCHIVE_NAME
    archive.parent.mkdir()
    archive.write_bytes(b"not the public archive")
    assert monitoring.monitoring_data(tmp_path)["mode"] == "replay_invalid"

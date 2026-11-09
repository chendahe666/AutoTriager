"""Causal, one-bin forecasting for the read-only monitoring console."""

import csv
import hashlib
import io
import math
import statistics
import zipfile
from datetime import datetime, timezone
from functools import lru_cache

from .contracts import ROOT
from .monitoring_prometheus import live_metrics


ARCHIVE_NAME = ".local-proactive-petshop/petshop-2e96f93.zip"
REVISION = "2e96f937c4c044b8b4aad03217592cd52e66db5d"
ARCHIVE_SHA256 = "d3b9b19118c0fd7f062c7adf6b0176a98781acfe7e96752f672cb216b486964a"
REPOSITORY = "https://github.com/amazon-science/petshop-root-cause-analysis"
DATASET = "dataset/temporal_traffic1"
CASES = ("issue_0", "issue_1", "issue_2")
BIN_SECONDS = 300
SCHEMA = "autotriager.api.v1"


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def _read_series(content):
    rows = csv.reader(io.StringIO(content.decode("utf-8")))
    headers = [next(rows) for _ in range(4)]
    if [row[0] for row in headers] != ["microservice", "metric", "statistic", "unix_timestamp"]:
        raise ValueError("Unexpected PetShop headers")
    column = list(zip(*headers[:3], strict=True)).index(("PetSite", "latency", "Average"))
    series = {}
    for row in rows:
        timestamp = float(row[0])
        if timestamp % BIN_SECONDS or timestamp in series:
            raise ValueError("Invalid or duplicate timestamp")
        if row[column]:
            latency = float(row[column]) * 1000
            if not math.isfinite(latency) or latency < 0:
                raise ValueError("Invalid latency")
            series[int(timestamp)] = latency
    return sorted(series.items())


def _fit(series, bin_seconds):
    pairs = [(a[1], b[1]) for a, b in zip(series, series[1:]) if b[0] - a[0] == bin_seconds]
    if len(pairs) < 8:
        return None
    previous, following = zip(*pairs)
    previous_mean, following_mean = statistics.mean(previous), statistics.mean(following)
    variance = sum((value - previous_mean) ** 2 for value in previous)
    covariance = sum((x - previous_mean) * (y - following_mean) for x, y in pairs)
    slope = max(0.0, min(1.0, covariance / variance)) if variance else 0.0
    return (following_mean - slope * previous_mean, slope)


def _forecast(model, latest):
    return max(0.0, model[0] + model[1] * latest) if model is not None else None


def _frames(series, model, threshold, bin_seconds):
    frames = []
    for start, observed in series:
        forecast = _forecast(model, observed)
        state = "below_threshold"
        if observed > threshold:
            state = "observed_breach"
        elif forecast is not None and forecast > threshold:
            state = "forecast_risk"
        frames.append({
            "start": utc(start),
            "closed_at": utc(start + bin_seconds),
            "target_end": utc(start + 2 * bin_seconds),
            "value": observed,
            "forecast": forecast,
            "state": state,
        })
    return frames


def _score(series_groups, model, threshold):
    errors = {"ar1": [], "persistence": []}
    eligible = warnings = hits = false_warnings = 0
    for series in series_groups:
        for a, b in zip(series, series[1:]):
            if b[0] - a[0] != BIN_SECONDS:
                continue
            latest, actual = a[1], b[1]
            forecast = _forecast(model, latest)
            errors["ar1"].append(abs(forecast - actual))
            errors["persistence"].append(abs(latest - actual))
            if latest <= threshold:
                eligible += actual > threshold
                warnings += forecast > threshold
                hits += forecast > threshold and actual > threshold
                false_warnings += forecast > threshold and actual <= threshold
    return {
        "pairs": len(errors["ar1"]),
        "mae": {method: statistics.mean(values) if values else None for method, values in errors.items()},
        "eligible_crossings": eligible,
        "forecast_warnings": warnings,
        "crossings_warned": hits,
        "false_forecast_warnings": false_warnings,
    }


@lru_cache(maxsize=2)
def _load_replay(path, fingerprint):
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("PetShop source digest mismatch")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        prefix = f"petshop-root-cause-analysis-{REVISION}/{DATASET}"
        normal = _read_series(archive.read(f"{prefix}/noissue/metrics.csv"))
        groups = {case: _read_series(archive.read(f"{prefix}/test/{case}/metrics.csv")) for case in CASES}
    split = len(normal) // 2
    training, holdout = normal[:split], normal[split:]
    if not training or not holdout or any(not group or group[0][0] < normal[-1][0] + BIN_SECONDS for group in groups.values()):
        raise ValueError("PetShop calibration must precede replay cases")
    model = _fit(training, BIN_SECONDS)
    if model is None:
        raise ValueError("PetShop calibration is too short")
    threshold = sorted(value for _, value in training)[math.ceil(0.95 * len(training)) - 1]
    return {
        "mode": "replay",
        "service": "PetSite",
        "metric": "latency",
        "unit": "ms",
        "bin_seconds": BIN_SECONDS,
        "threshold": threshold,
        "source_url": f"{REPOSITORY}/tree/{REVISION}/{DATASET}",
        "source_paper": "https://proceedings.mlr.press/v236/hardt24a.html",
        "cases": [{
            "id": case,
            "source_url": f"{REPOSITORY}/blob/{REVISION}/{DATASET}/test/{case}/metrics.csv",
            "frames": _frames(series, model, threshold, BIN_SECONDS),
        } for case, series in groups.items()],
        "evaluation": {
            "training_bins": len(training),
            "healthy_holdout": _score([holdout], model, threshold),
            "incident_replay": _score(groups.values(), model, threshold),
        },
    }


def _live_monitoring(source):
    series, bin_seconds = source["series"], source["bin_seconds"]
    threshold = source["threshold"]
    frames = []
    latest = series[-1][0]
    freshness = "fresh"
    if latest != source["expected_latest_bin_start"]:
        freshness = "stale"
    elif len(series) < 2 or latest - series[-2][0] != bin_seconds:
        freshness = "gap"
    for index, sample in enumerate(series):
        adjacent = index > 0 and sample[0] - series[index - 1][0] == bin_seconds
        current = index == len(series) - 1
        model = _fit(series[:index + 1], bin_seconds) if adjacent and (not current or freshness == "fresh") else None
        frames.extend(_frames([sample], model, threshold, bin_seconds))
    return {
        "mode": "live",
        "freshness": freshness,
        "service": source["service"],
        "metric": source["metric"],
        "unit": source["unit"],
        "bin_seconds": bin_seconds,
        "threshold": threshold,
        "cases": [{"id": "live", "frames": frames[-36:]}],
    }


def monitoring_data(root=ROOT):
    """Read configured live telemetry, or load the optional public replay archive."""
    base = {"schema_version": SCHEMA, "cases": []}
    try:
        source = live_metrics()
    except ValueError:
        return {**base, "mode": "live_unavailable"}
    if source is not None:
        return {**base, **_live_monitoring(source)}
    try:
        path = root / ARCHIVE_NAME
        stat = path.stat()
        replay = _load_replay(path, (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    except FileNotFoundError:
        return {**base, "mode": "replay_unavailable"}
    except (OSError, ValueError, KeyError, IndexError, StopIteration, zipfile.BadZipFile):
        return {**base, "mode": "replay_invalid"}
    return {**base, **replay}

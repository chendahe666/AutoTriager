"""Bounded range reads for one explicitly configured local Prometheus series."""
from __future__ import annotations

import http.client
import json
import math
import os
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

PREFIX = "AUTOTRIAGER_MONITORING_"
STEP = 300
MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class Source:
    host: str
    port: int
    query: str
    service: str
    metric: str
    unit: str
    threshold: float
    step: int


def read_source():
    endpoint = os.environ.get(PREFIX + "PROMETHEUS", "").strip()
    if not endpoint:
        return None
    try:
        url = urlsplit(endpoint)
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1"}
                or url.username is not None or url.password is not None
                or url.path not in {"", "/"} or url.query or url.fragment):
            raise ValueError()
        query, service, metric, unit = [os.environ[PREFIX + key].strip()
                                        for key in ("QUERY", "SERVICE", "METRIC", "UNIT")]
        if not query or len(query) > 1024 or any(not v or len(v) > 120 for v in (service, metric, unit)):
            raise ValueError()
        threshold = float(os.environ[PREFIX + "THRESHOLD"])
        if not math.isfinite(threshold) or threshold <= 0:
            raise ValueError()
        step = int(os.environ.get(PREFIX + "BIN_SECONDS", STEP))
        if not 2 <= step <= 300:
            raise ValueError()
        return Source(url.hostname, url.port or 9090, query, service, metric, unit, threshold, step)
    except (KeyError, ValueError) as exc:
        raise ValueError("Configured monitoring source is unavailable.") from exc


def read_api(source, route, params):
    client = http.client.HTTPConnection(source.host, source.port, timeout=3)
    try:
        client.request("GET", route + "?" + urlencode(params), headers={"Accept": "application/json"})
        response = client.getresponse()
        if response.status != 200:
            raise ValueError()
        body = response.read(MAX_BYTES + 1)
    finally:
        client.close()
    if len(body) > MAX_BYTES:
        raise ValueError()
    payload = json.loads(body)
    if payload["status"] != "success" or payload.get("warnings"):
        raise ValueError()
    return payload["data"]


def live_metrics():
    source = read_source()
    if source is None:
        return None
    try:
        step = source.step
        end = int(time.time() // step * step)
        start = end - 287 * step
        data = read_api(source, "/api/v1/query_range", {"query": source.query, "start": start, "end": end,
                        "step": step, "timeout": "1s", "lookback_delta": f"{min(step, 60)}s"})
        if data["resultType"] != "matrix" or len(data["result"]) != 1:
            raise ValueError()
        series = []
        last = start - step
        for raw_time, raw_value in data["result"][0]["values"]:
            if (type(raw_time) not in (int, float) or not math.isfinite(raw_time)
                    or not start <= raw_time <= end or raw_time % step or raw_time <= last):
                raise ValueError()
            last = raw_time
            value = float(raw_value)
            if math.isfinite(value):
                if value < 0:
                    raise ValueError()
                # Prometheus evaluates at the right edge of the completed interval.
                series.append((int(raw_time) - step, value))
        if not series:
            raise ValueError()
        return {"service": source.service, "metric": source.metric, "unit": source.unit,
                "bin_seconds": step, "threshold": source.threshold, "series": series,
                "expected_latest_bin_start": end - step}
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, OverflowError) as exc:
        raise ValueError("Configured monitoring source is unavailable.") from exc

"""Read incident-scoped monitoring samples without reconstructing missing data."""

from datetime import datetime, timezone
import http.client
import math
import re
import time

from .monitoring_prometheus import read_api
from .incident_metrics_archive import archive_incident_metrics, recorded_live_incident_metrics


def _utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds")


def _timestamp(value):
    date = datetime.fromisoformat(value)
    if date.utcoffset() is None:
        raise ValueError("Incident timestamp must include its timezone")
    return date.timestamp()


def _expressions(lab_id):
    labels = '{service="checkout",lab_id="' + lab_id + '"}'

    def gauge(name):
        return name + labels

    def rate(name):
        return f"rate({gauge(name)}[30s])"

    requests = rate("autotriager_checkout_probe_requests_total")
    errors = rate("autotriager_checkout_probe_errors_total")
    return (
        ("checkout_latency", "Checkout request latency", "requests", "ms",
         "Checkout probe · latest recorded request", gauge("autotriager_checkout_lab_latency_ms")),
        ("probe_throughput", "Checkout probe throughput", "requests", "req/s",
         "Checkout probe · 30 s rate", requests),
        ("probe_error_rate", "Checkout probe error rate", "requests", "%",
         "Checkout probe · error share over 30 s", f"100 * ({errors} / {requests})"),
        ("lab_process_cpu", "Lab process CPU usage", "resources", "% of one core",
         "Local checkout and payment lab process · 30 s CPU rate",
         f"100 * {rate('autotriager_checkout_lab_process_cpu_seconds_total')}"),
        ("lab_process_memory", "Lab process resident memory", "resources", "MiB",
         "Local checkout and payment lab process", f"{gauge('autotriager_checkout_lab_process_resident_memory_bytes')} / 1048576"),
        ("lab_host_cpu", "Host CPU utilization", "resources", "%",
         "Local test host · all CPU cores", gauge("autotriager_checkout_lab_host_cpu_utilization_percent")),
        ("lab_host_memory", "Host memory utilization", "resources", "%",
         "Local test host · physical memory", gauge("autotriager_checkout_lab_host_memory_utilization_percent")),
    )


def _points(values, start, end):
    points = []
    previous = None
    for at, raw in values:
        if type(at) not in (int, float) or not math.isfinite(at) or not start <= at <= end or (previous is not None and at <= previous):
            raise ValueError("Unexpected Prometheus time coordinates")
        previous = at
        value = float(raw)
        if math.isfinite(value) and value >= 0:
            points.append({"at": _utc(at), "value": value})
    return points


def _read_scoped(source, lab_id, start, end, threshold):
    definitions = _expressions(lab_id)
    # Each expression retains the same service/lab labels. A fixed output label
    # keeps union operands separate even for derived rates without metric names.
    query = " or ".join(
        f'label_replace(({expr}), "workbench_series", "{series_id}", "service", ".+")'
        for series_id, _, _, _, _, expr in definitions
    )
    step = max(1, math.ceil((end - start) / 180))
    data = read_api(source, "/api/v1/query_range", {
        "query": query, "start": start, "end": end, "step": step, "timeout": "2s", "lookback_delta": "3s",
    })
    if data["resultType"] != "matrix" or len(data["result"]) > len(definitions):
        raise ValueError("Unexpected Prometheus result")
    result = {}
    for item in data["result"]:
        labels = item["metric"]
        if labels.get("service") != "checkout" or labels.get("lab_id") != lab_id:
            raise ValueError("Unrelated Prometheus series")
        series_id = labels["workbench_series"]
        if series_id in result:
            raise ValueError("Ambiguous Prometheus series")
        result[series_id] = _points(item["values"], start, end)
    series = []
    for series_id, title, group, unit, scope, _ in definitions:
        points = result.get(series_id)
        if points:
            row = {"id": series_id, "title": title, "group": group, "unit": unit,
                   "source": "Prometheus", "scope": scope, "points": points}
            if series_id == "checkout_latency":
                row["threshold"] = threshold
            series.append(row)
    return series


def _recorded_live_response(response, case, request_log):
    series = recorded_live_incident_metrics(case, request_log=request_log)
    if series:
        timestamps = [_timestamp(point["at"]) for row in series for point in row["points"]]
        start = min(_timestamp(response["start"]), min(timestamps))
        end = max(_timestamp(response["end"]), max(timestamps))
        response.update(start=_utc(start), end=_utc(end), series=series)
    return response


def incident_metrics(app, case_id):
    from .triage import load_baro_replay
    if replay := load_baro_replay(case_id):
        return replay["metrics"]
    case = app.triage_case(case_id)["case"]
    if case.get("source") != "live":
        return archive_incident_metrics(case)

    measurement = case["metric_evidence"]
    triggered = _timestamp(case["triggered_at"])
    observed = _timestamp(measurement.get("observed_at", measurement["end"]))
    response = {"incident_id": case_id, "alert_at": _utc(triggered), "start": case["start"],
                "end": case["end"], "status": "partial", "series": [{
                    "id": "checkout_latency", "title": measurement["metric"], "group": "requests",
                    "unit": measurement["unit"], "source": "Recorded Prometheus alert", "threshold": measurement["threshold"],
                    "points": [{"at": _utc(observed), "value": measurement["value"]}],
                }]}
    lab_id = (case.get("matched_request") or {}).get("lab_id")
    source = app.live_alerts.source if app.live_alerts else None
    request_log = app.live_alerts.request_log if app.live_alerts else None
    if (not source or not isinstance(lab_id, str) or not re.fullmatch(r"[0-9a-f]{32}", lab_id)
            or (case.get("alert_event") or {}).get("scope") != f"{source.host}:{source.port}"):
        return _recorded_live_response(response, case, request_log)

    action = app.lifecycle.action(case_id) if app.lifecycle else None
    samples = ((action or {}).get("recovery") or {}).get("samples", [])
    target_end = max([observed, *(sample["observed_at_unix"] for sample in samples)]) + 15 if samples else time.time()
    start = math.floor(triggered - 60)
    end = math.floor(min(time.time(), target_end, triggered + 1800))
    try:
        series = _read_scoped(source, lab_id, start, end, measurement["threshold"])
    except (OSError, http.client.HTTPException, KeyError, TypeError, ValueError, OverflowError):
        series = []
    if not series:
        return _recorded_live_response(response, case, request_log)
    complete = len(series) == len(_expressions(lab_id))
    if not any(row["id"] == "checkout_latency" for row in series):
        series = response["series"] + series
    response.update(start=_utc(start), end=_utc(end), series=series, status="available" if complete else "partial")
    return response

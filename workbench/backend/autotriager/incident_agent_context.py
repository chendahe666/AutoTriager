"""Label-free incident snapshots and bounded evidence retrieval for Agent chat."""

from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime
from functools import lru_cache
import hashlib
import json
import re
from statistics import fmean, median
from zipfile import BadZipFile, ZipFile

from .contracts import ROOT
from .incident_metrics import incident_metrics
from .incident_metrics_archive import ARCHIVE, _archive_matches, _evidenced_members


MAX_EVIDENCE_BYTES = 24_000
MAX_EVIDENCE_ROWS = 24
MAX_DIRECTORY_ROWS = 64
MAX_ARCHIVE_MEMBER_BYTES = 50_000_000
REFERENCE_WINDOW = re.compile(r"reference contains \d+ samples in \[([^,]+), ([^)]+)\)")


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _window(start, end):
    return {"start": start, "end": end}


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def _evidence(case_id, key, modality, window, label, detail, source, *, service=None, unit=None, data=None):
    reference = hashlib.sha256(f"{case_id}:{key}".encode()).hexdigest()[:24]
    return {"evidence_id": f"ev_{reference}", "service_id": service, "modality": modality,
            "kind": "incident_observation", "observed_window": window, "unit": unit,
            "record": {"label": label, "detail": detail, "source": source, **(data or {})},
            "derivation": None, "limitations": []}


def _original_evidence(case, window):
    candidates = {ref: item["service"] for item in case["candidates"] for ref in item["evidence_ids"]}
    evidence = []
    for row in case["evidence"]:
        source_id = row["id"]
        suffix = source_id.rsplit("-", 1)[-1]
        modality = "trace" if source_id == "lab-request" else suffix
        if modality not in {"trace", "metric", "log"}:
            modality = "timeline"
        detail = row["detail"]
        service = candidates.get(source_id)
        if source_id == "prom-metric":
            alert = case["metric_evidence"]
            detail = (f"{alert['metric']} measured {alert['value']:.6g} {alert['unit']} at "
                      f"{alert.get('observed_at', alert['end'])}, above the {alert['threshold']:.6g} {alert['unit']} threshold.")
            service = alert["service"]
        elif source_id == "prom-rule":
            detail = f"Prometheus recorded a firing rule at {case['triggered_at']}."
            service = case["metric_evidence"]["service"]
        evidence.append(_evidence(case["id"], source_id, modality, window, row["title"], detail,
                                  row.get("source_url", "Published incident evidence"), service=service,
                                  data={"source_evidence_id": source_id}))
    return evidence


def _metric_evidence(case_id, result):
    evidence = []
    for series in result["series"]:
        points = series["points"]
        if not points:
            continue
        values = [point["value"] for point in points]
        details = {"series_id": series["id"], "samples": len(values), "first": points[0], "last": points[-1],
                   "minimum": min(values), "maximum": max(values), "mean": fmean(values)}
        if "threshold" in series:
            details["threshold"] = series["threshold"]
        title = series["title"]
        service = None
        if title.startswith("ts-"):
            service = title.split(" · ", 1)[0]
        elif series["id"] == "payment_latency":
            service = "payment"
        elif series["group"] == "requests":
            service = "checkout"
        observed = _window(points[0]["at"], points[-1]["at"])
        evidence.append(_evidence(case_id, "series/" + series["id"], "metric", observed, title,
                                  f"{len(values)} observed samples, mean {details['mean']:.6g}, "
                                  f"range {details['minimum']:.6g} to {details['maximum']:.6g}.", series["source"],
                                  service=service, unit=series["unit"], data=details))
    return evidence


def _live_details(app, case, window):
    alert = case["metric_evidence"]
    alert_fields = ("service", "metric", "value", "unit", "threshold", "start", "end", "observed_at", "source")
    details = {"alert": {key: alert[key] for key in alert_fields if key in alert}}
    edges, evidence = [], []
    request = case.get("matched_request")
    if request:
        keys = ("request_id", "service", "dependency", "route", "dependency_route", "observed_at_unix",
                "latency_ms", "dependency_latency_ms", "http_status")
        observation = {key: request[key] for key in keys if key in request}
        edge = {"caller": request["service"], "callee": request["dependency"], **observation}
        at = _iso(request["observed_at_unix"])
        row = _evidence(case["id"], "matched-call", "trace", _window(at, at), f"{edge['caller']} → {edge['callee']}",
                        f"Recorded request {request['request_id']} at {at}. HTTP {request['http_status']}. "
                        f"Request {request['latency_ms']:.6g} ms, dependency call {request['dependency_latency_ms']:.6g} ms.",
                        "Recorded local checkout request", service=edge["callee"], unit="ms", data=edge)
        evidence.append(row)
        edges.append({"caller": edge["caller"], "callee": edge["callee"], "evidence_id": row["evidence_id"]})
    alert_source = getattr(app, "live_alerts", None)
    if alert_source and hasattr(alert_source, "routing"):
        routes = alert_source.routing(case["id"])
        route_fields = ("decision", "event_id", "triggered_at", "service", "rule")
        details["alert_routing"] = {"recorded_count": len(routes), "latest": [
            {key: route[key] for key in route_fields if key in route} for route in routes[-12:]
        ]}
        for route in details["alert_routing"]["latest"]:
            if route.get("triggered_at") and route.get("event_id"):
                at = route["triggered_at"]
                evidence.append(_evidence(case["id"], "route/" + route["event_id"], "timeline", _window(at, at),
                                          "Alert routing receipt", f"Recorded routing decision: {route['decision']}.",
                                          "Local alert routing history", service=route.get("service"), data=route))
    action = app.lifecycle.action(case["id"]) if app.lifecycle else None
    details["resolution"] = "resolved" if action and action["status"] == "verified" else "unresolved"
    if action:
        recovery = action.get("recovery") or {}
        sample_fields = ("request_id", "observed_at_unix", "latency_ms", "http_status")
        samples = [{key: row[key] for key in sample_fields if key in row} for row in recovery.get("samples", [])]
        details["recovery"] = {"action_status": action["status"], "verification_status": recovery.get("status"),
                               "sample_count": len(samples), "recorded_sample_count": min(len(samples), 12)}
        if samples:
            times = [sample["observed_at_unix"] for sample in samples if "observed_at_unix" in sample]
            observed = _window(_iso(min(times)), _iso(max(times))) if times else window
            sample_detail = ", ".join(f"HTTP {sample['http_status']} at {sample['latency_ms']:.6g} ms"
                                      for sample in samples[:3] if "http_status" in sample and "latency_ms" in sample)
            description = f"Stored verification status: {recovery.get('status')}. {len(samples)} recovery samples."
            if sample_detail:
                description += f" First recorded results: {sample_detail}."
            evidence.append(_evidence(case["id"], "recorded-recovery", "timeline", observed, "Recovery verification",
                                      description, "Incident action receipt",
                                      unit="ms", data={**details["recovery"], "samples": samples[:12]}))
    return details, edges, evidence


def _archive_coordinates(case):
    members = _evidenced_members(case)
    directories = {member.rsplit("/metrics/", 1)[0] for member in members.values()}
    references = {match.groups() for row in case["evidence"] if (match := REFERENCE_WINDOW.search(row["detail"]))}
    if len(directories) == len(references) == 1:
        return next(iter(directories)), next(iter(references))
    return None


@lru_cache(maxsize=8)
def _archive_rows(path, _size, _modified_ns, directory, reference_times, observed_times):
    from tools.eadro_telemetry import Window, read_logs, read_spans

    reference, observed = (Window(*(datetime.fromisoformat(t).timestamp() for t in times))
                           for times in (reference_times, observed_times))
    with ZipFile(path) as archive:
        for name in ("logs.json", "spans.json"):
            if archive.getinfo(f"{directory}/{name}").file_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError("Archive member exceeds the context read bound")
        spans = read_spans(archive, directory, Window(reference.start, observed.end))
        logs = read_logs(archive, directory)

    edge_samples = defaultdict(lambda: ([], []))
    for child in spans.values():
        parent = spans.get((child.trace, child.parent))
        if not parent or parent.service == child.service:
            continue
        for phase, window in enumerate((reference, observed)):
            if window.contains_span(parent) and window.contains_span(child):
                edge_samples[parent.service, child.service][phase].append((child, parent))
    log_counts = defaultdict(lambda: {"reference": 0, "incident": 0})
    log_warnings = defaultdict(lambda: {"reference": 0, "incident": 0})
    log_examples = defaultdict(dict)
    for log in logs:
        for phase, window in (("reference", reference), ("incident", observed)):
            if window.contains(log.time):
                log_counts[log.service][phase] += 1
                warned = log.severity in {"WARN", "ERROR"}
                log_warnings[log.service][phase] += warned
                example = log_examples[log.service].get(phase)
                priority = (warned, -log.time, -log.index)
                if example is None or priority > example[0]:
                    match = spans.get(log.span_key)
                    coordinate = {"at": _iso(log.time), "level": log.severity, "index_zero_based": log.index}
                    if match and match.service == log.service and window.contains_span(match):
                        coordinate.update(trace_id=match.trace, span_id=match.span)
                    log_examples[log.service][phase] = (priority, coordinate)
    logs_by_service = {service: {"reference_count": counts["reference"], "incident_count": counts["incident"],
                                 "reference_warn_error_count": log_warnings[service]["reference"],
                                 "incident_warn_error_count": log_warnings[service]["incident"],
                                 "examples": {phase: row[1] for phase, row in log_examples[service].items()}}
                       for service, counts in log_counts.items()}
    return dict(edge_samples), logs_by_service


def _archived_details(case, window):
    coordinates = _archive_coordinates(case)
    if coordinates is None:
        return {}, [], []
    path = ROOT / ARCHIVE
    directory, reference_times = coordinates
    try:
        stat = path.stat()
        if not _archive_matches(path, stat.st_size, stat.st_mtime_ns):
            return {}, [], []
        edges, log_counts = _archive_rows(path, stat.st_size, stat.st_mtime_ns, directory,
                                         reference_times, (case["start"], case["end"]))
    except (BadZipFile, OSError, KeyError, TypeError, ValueError, OverflowError):
        return {}, [], []
    edge_index, evidence = [], []
    for (caller, callee), (before, after) in sorted(edges.items()):
        data = {"caller": caller, "callee": callee, "reference_count": len(before), "incident_count": len(after)}
        for phase, rows in (("reference", before), ("incident", after)):
            if rows:
                data[f"{phase}_median_ms"] = median(child.duration * 1000 for child, _ in rows)
                data[f"{phase}_error_span_count"] = sum(child.error for child, _ in rows)
        child, parent = min(after or before, key=lambda row: (row[0].start, row[0].trace, row[0].span))
        data["example"] = {"trace_id": child.trace, "parent_span_id": parent.span, "child_span_id": child.span,
                           "at": _iso(child.start), "phase": "incident" if after else "reference"}
        before_detail = f", median child duration {data['reference_median_ms']:.6g} ms" if before else ""
        after_detail = f", median child duration {data['incident_median_ms']:.6g} ms" if after else ""
        row = _evidence(case["id"], f"edge/{caller}/{callee}", "trace", window, f"{caller} → {callee}",
                        f"Reference {len(before)} matched calls{before_detail}. Incident {len(after)} matched calls"
                        f"{after_detail}. Example trace {child.trace}, parent {parent.span}, child {child.span}.",
                        f"Eadro TrainTicket · {directory}/spans.json", service=callee, unit="ms", data=data)
        evidence.append(row)
        edge_index.append({"caller": caller, "callee": callee, "evidence_id": row["evidence_id"]})
    for service, data in sorted(log_counts.items()):
        evidence.append(_evidence(case["id"], f"log-count/{service}", "log", window, f"{service} log counts",
                                  f"Reference {data['reference_count']} log entries and {data['reference_warn_error_count']} WARN/ERROR. "
                                  f"Incident {data['incident_count']} log entries and {data['incident_warn_error_count']} WARN/ERROR.",
                                  f"Eadro TrainTicket · {directory}/logs.json", service=service,
                                  data=data))
    return {"reference_window": _window(*reference_times)}, edge_index, evidence


def _describe(case, original):
    return {"title": case["title"], "source": "live" if case.get("source") == "live" else "recorded",
            "start": case["start"], "end": case["end"], "evidence_count": len(original),
            "available_modalities": sorted({row["modality"] for row in original})}


def describe_incident_agent_context(app, case_id):
    """Describe the published incident snapshot without scanning or querying telemetry."""
    case = app.triage_case(case_id)["case"]
    return _describe(case, _original_evidence(case, _window(case["start"], case["end"])))


def load_incident_agent_context(app, case_id):
    """Resolve only the requested published incident and its approved source windows."""
    case = app.triage_case(case_id)["case"]
    window = _window(case["start"], case["end"])
    live = case.get("source") == "live"
    detail, edges, supplemental = (_live_details(app, case, window) if live else _archived_details(case, window))
    metrics = incident_metrics(app, case_id)
    original = _original_evidence(case, window)
    evidence = original + supplemental + _metric_evidence(case_id, metrics)
    run_id = "incident-agent-" + hashlib.sha256(case_id.encode()).hexdigest()[:20]
    modalities = {name: sum(row["modality"] == name for row in evidence)
                  for name in ("metric", "log", "trace", "timeline")}
    original_ids = {row["record"]["source_evidence_id"]: row["evidence_id"] for row in original}
    candidates = [{**row, "evidence_ids": [original_ids[ref] for ref in row["evidence_ids"] if ref in original_ids]}
                  for row in case["candidates"]]
    warnings = [*case["limits"], "Evidence covers recorded observations within the declared source windows. "
                "An unobserved dependency or missing signal is unknown."]
    if live:
        warnings.append("This is a local alarm snapshot. No current production status is asserted.")
    if metrics["status"] != "available":
        warnings.append("Metric series coverage is partial or unavailable for this incident.")
    if not live and detail:
        warnings.append("Archive log evidence includes counts and source coordinates. Log message bodies are withheld.")
    scope = {**_describe(case, original), "mode": "incident_agent",
             "triggered_at": case.get("triggered_at"), "time_window": window,
             "recorded_evidence_count": len(evidence), "modalities": modalities,
             "metric_series_availability": metrics["status"], "observed_cross_service_edges": edges,
             "coverage_warnings": warnings, "previous_candidate_hypotheses": candidates,
             "observation_type": "recorded_local_alarm" if live else "recorded_archive", **detail}
    return {"incident_id": case_id, "context_run_id": run_id, "scope": scope,
            "evidence": evidence, "evidence_ids": [row["evidence_id"] for row in evidence]}


def _query_priority(row, question):
    record = row["record"]
    services = (row["service_id"], record.get("caller"), record.get("callee"))
    names = {name.casefold() for name in services if name}
    mentioned = sum(
        any(re.search(r"(?<![a-z0-9_-])" + re.escape(alias) + r"(?![a-z0-9_-])", question)
            for alias in {name, name.removeprefix("ts-").removesuffix("-service")} if alias)
        for name in names
    )
    modality = row["modality"]
    focus = {"metric": r"metric|cpu|latency|p\d\d|指标|延迟|内存", "log": r"logs?|日志|异常|exception",
             "trace": r"trac|edge|call|dependency|dependenc|调用|链路|依赖",
             "timeline": r"recov|resolv|恢复|处置"}
    requested = bool(re.search(focus[modality], question))
    direct_reference = re.search(r"(?<![a-z0-9_])" + row["evidence_id"] + r"(?![a-z0-9_])", question) is not None
    return (direct_reference, mentioned, requested)


def _priority(row, question, previous_question):
    record = row["record"]
    before, after = record.get("reference_median_ms"), record.get("incident_median_ms")
    relative = abs(after - before) / max(1, before) if before is not None and after is not None else 0
    return (*_query_priority(row, question), *_query_priority(row, previous_question),
            bool(record.get("caller")), relative, "source_evidence_id" in record)


def select_incident_agent_context(context, question, *, previous_question=""):
    """Keep the incident envelope and select evidence from this incident only."""
    question, previous_question = question.casefold(), previous_question.casefold()
    priorities = {row["evidence_id"]: _priority(row, question, previous_question) for row in context["evidence"]}
    groups = defaultdict(list)
    for row in sorted(context["evidence"], key=lambda row: priorities[row["evidence_id"]], reverse=True):
        groups[row["modality"]].append(row)
    selected = []
    group_order = sorted(groups, key=lambda name: priorities[groups[name][0]["evidence_id"]], reverse=True)
    for index in range(MAX_EVIDENCE_ROWS):
        for name in group_order:
            if index >= len(groups[name]):
                continue
            row = groups[name][index]
            if len(selected) >= MAX_EVIDENCE_ROWS:
                break
            if len(_canonical([*selected, row])) <= MAX_EVIDENCE_BYTES:
                selected.append(row)
    scope = deepcopy(context["scope"])
    selected_ids = {row["evidence_id"] for row in selected}
    scope["evidence_directory"] = [
        {"evidence_id": row["evidence_id"], "modality": row["modality"], "service_id": row["service_id"],
         "label": row["record"]["label"][:140], "loaded": row["evidence_id"] in selected_ids}
        for row in context["evidence"][:MAX_DIRECTORY_ROWS]
    ]
    scope.update(selected_count=len(selected), sampled=len(selected) < scope["recorded_evidence_count"],
                 directory_truncated=len(context["evidence"]) > MAX_DIRECTORY_ROWS,
                 selection="Question-scoped and interleaved by evidence modality. Directory entries with loaded=false "
                           "only identify records and may not be cited as evidence for this answer.")
    return {**context, "scope": scope, "evidence": selected,
            "evidence_ids": [row["evidence_id"] for row in selected]}

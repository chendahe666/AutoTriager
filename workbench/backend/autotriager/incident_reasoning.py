"""Explainable alert routing and evidence-bound checkout diagnosis."""

import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone


_IDENTITY = ("source", "scope", "service", "rule", "target")
_OPEN_STATES = {"open", "investigating", "mitigating", "verifying"}
_FINGERPRINT = "checkout.payment.delay"
_LAB_EVIDENCE = "lab-request"
_METRIC_EVIDENCE = ("prom-rule", "prom-metric")
_HEX_ID = re.compile(r"[0-9a-f]{32}")


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _identifier(value):
    return isinstance(value, str) and bool(value.strip())


def _lab_id(value):
    return isinstance(value, str) and bool(_HEX_ID.fullmatch(value))


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is not None:
            return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    return None


def route_alert(event, incidents, *, correlation_window_seconds=300):
    """Route a normalized event within one source, deployment, and alert identity."""
    if not _number(correlation_window_seconds):
        raise ValueError("Correlation window must be a nonnegative finite number")

    event_id = event.get("event_id")
    event_time = _timestamp(event.get("triggered_at"))
    identity = tuple(event.get(key) for key in _IDENTITY)
    if not _identifier(event_id) or event_time is None or not all(map(_identifier, identity)):
        return {"decision": "skip", "incident_id": None, "reason": "invalid_event"}
    if event.get("status", "firing") != "firing":
        return {"decision": "skip", "incident_id": None, "reason": "not_firing"}
    if event.get("suppressed") is True:
        return {"decision": "skip", "incident_id": None, "reason": "explicitly_suppressed"}

    candidates = []
    for incident in incidents:
        incident_id = incident.get("incident_id")
        if not _identifier(incident_id):
            continue
        same_identity = tuple(incident.get(key) for key in _IDENTITY) == identity
        event_ids = incident.get("event_ids", ())
        if isinstance(event_ids, (tuple, list, set, frozenset)) and event_id in event_ids:
            return {"decision": "skip", "incident_id": incident_id if same_identity else None,
                    "reason": "duplicate_delivery" if same_identity else "event_identity_conflict"}
        if not same_identity or incident.get("state") not in _OPEN_STATES:
            continue
        previous_time = _timestamp(incident.get("last_event_at"))
        if previous_time is not None and 0 <= (event_time - previous_time).total_seconds() <= correlation_window_seconds:
            candidates.append((previous_time, incident_id))

    if candidates:
        return {"decision": "link", "incident_id": max(candidates)[1],
                "reason": "matching_open_incident_in_window"}
    return {"decision": "new", "incident_id": None, "reason": "no_matching_open_incident"}


def _matching_request(request, metric, observed_at):
    elapsed = request.get("latency_ms")
    observed = request.get("observed_at_unix")
    dependency_ms = request.get("dependency_latency_ms")
    status = request.get("http_status")
    return (
        observed_at is not None
        and _lab_id(request.get("request_id"))
        and request.get("service") == metric.get("service") == "checkout"
        and request.get("dependency") == "payment"
        and request.get("route") == "/checkout"
        and request.get("dependency_route") == "/authorize"
        and _number(elapsed)
        and math.isclose(elapsed, metric["value"], rel_tol=0, abs_tol=1e-8)
        and _number(observed)
        and 0 <= observed_at.timestamp() - observed <= 3
        and _number(dependency_ms)
        and dependency_ms <= elapsed
        and type(status) is int
        and 100 <= status <= 599
    )


def _verified_recovery(case, request, metric, recovery):
    """Confirm the mechanism only when the controlled intervention and samples agree."""
    plan = _mapping(recovery.get("plan"))
    action = _mapping(plan.get("action"))
    receipt = _mapping(recovery.get("action_receipt"))
    lab_id = request.get("lab_id")
    generation = request.get("control_generation")
    configured_delay = request.get("configured_delay_ms")
    applied_at = receipt.get("applied_at_unix")
    baseline = receipt.get("baseline_delay_ms")
    samples = recovery.get("samples")

    if (
        recovery.get("status") != "verified"
        or not _lab_id(lab_id)
        or type(generation) is not int
        or generation < 0
        or not _number(configured_delay)
        or not metric["threshold"] < configured_delay <= request["dependency_latency_ms"]
        or plan.get("incident_id") != case.get("id")
        or not _identifier(plan.get("plan_id"))
        or recovery.get("plan_id") != plan["plan_id"]
        or action.get("operation") != "set_payment_delay_ms"
        or action.get("expected_lab_id") != lab_id
        or action.get("expected_generation") != generation
        or action.get("expected_delay_ms") != configured_delay
        or not _number(baseline)
        or not baseline < metric["threshold"]
        or action.get("delay_ms") != baseline
        or receipt.get("status") != "applied"
        or receipt.get("lab_id") != lab_id
        or receipt.get("generation") != generation + 1
        or receipt.get("delay_ms") != baseline
        or not _number(applied_at)
        or not request["observed_at_unix"] < applied_at
        or recovery.get("lab_id") != lab_id
        or recovery.get("generation") != generation + 1
        or recovery.get("applied_at_unix") != applied_at
        or not isinstance(samples, list)
        or len(samples) < 2
    ):
        return []

    sample_ids = []
    for sample in samples:
        row = _mapping(sample)
        sample_id = row.get("request_id")
        time = row.get("observed_at_unix")
        elapsed = row.get("latency_ms")
        if (
            not _lab_id(sample_id)
            or sample_id == request["request_id"]
            or sample_id in sample_ids
            or not _number(time)
            or time <= applied_at
            or not _number(elapsed)
            or elapsed > metric["threshold"]
            or row.get("http_status") != 200
        ):
            return []
        sample_ids.append(sample_id)
    return sample_ids if sample_ids == recovery.get("evidence_ids") else []


def diagnose_incident(case, *, matched_request=None, recovery=None):
    """Return a measured symptom, a bounded finding, and a discriminating next check."""
    evidence = case.get("evidence")
    evidence_ids = [
        entry["id"] for entry in evidence
        if isinstance(entry, Mapping) and _identifier(entry.get("id"))
    ] if isinstance(evidence, list) else []
    metric = _mapping(case.get("metric_evidence"))
    observed_at = _timestamp(metric.get("observed_at"))
    triggered_at = _timestamp(case.get("triggered_at"))
    value, threshold = metric.get("value"), metric.get("threshold")
    result = {
        "observation": None,
        "finding": {"status": "insufficient", "summary": "The cause remains unknown.",
                    "evidence_ids": [], "fingerprint": None, "control_generation": None, "lab_id": None},
        "limitations": [],
        "next_checks": [],
    }
    if (
        case.get("source") != "live"
        or not all(evidence_ids.count(key) == 1 for key in _METRIC_EVIDENCE)
        or not _number(value)
        or not _number(threshold)
        or not 0 < threshold < value
        or observed_at is None
        or triggered_at is None
        or triggered_at > observed_at
        or not all(_identifier(metric.get(key)) for key in ("service", "metric", "unit"))
    ):
        result["limitations"] = ["The alert and metric do not form a complete, consistent observation."]
        result["next_checks"] = ["Retrieve the firing rule and its original metric evaluation."]
        return result

    result["observation"] = {
        "status": "observed", "service": metric["service"], "metric": metric["metric"],
        "value": value, "threshold": threshold, "unit": metric["unit"],
        "observed_at": metric["observed_at"], "evidence_ids": list(_METRIC_EVIDENCE),
    }
    request = _mapping(case.get("matched_request") if matched_request is None else matched_request)
    if metric["unit"] != "ms" or evidence_ids.count(_LAB_EVIDENCE) != 1 or not _matching_request(request, metric, observed_at):
        result["limitations"] = ["A matching, consistent checkout request is unavailable."]
        result["next_checks"] = ["Collect a checkout request and match its timing and latency to the alert."]
        return result

    dependency_ms = request["dependency_latency_ms"]
    references = [*_METRIC_EVIDENCE, _LAB_EVIDENCE]
    result["finding"]["evidence_ids"] = references
    if dependency_ms <= threshold:
        result["finding"]["summary"] = "The matched request does not isolate the latency to the payment call."
        result["limitations"] = ["Other portions of checkout latency could have contributed to the breach."]
        result["next_checks"] = ["Compare the payment call and the rest of checkout against a healthy request."]
        return result

    generation, lab_id = request.get("control_generation"), request.get("lab_id")
    result["finding"].update({
        "status": "hypothesis",
        "summary": f"The matched payment call took {dependency_ms:.2f} ms and exceeded the checkout threshold on its own.",
        "fingerprint": _FINGERPRINT,
        "control_generation": generation if type(generation) is int and generation >= 0 else None,
        "lab_id": lab_id if _lab_id(lab_id) else None,
    })
    recovery_ids = _verified_recovery(case, request, metric, _mapping(recovery))
    if recovery_ids:
        result["finding"].update({
            "status": "confirmed_in_lab",
            "summary": "Restoring the injected payment delay to its lab baseline brought measured checkout latency below threshold.",
            "evidence_ids": [*references, *recovery_ids],
        })
        result["limitations"] = ["The intervention establishes the cause for this controlled lab incident."]
        result["next_checks"] = ["Track whether the same controlled failure mechanism recurs."]
    else:
        result["limitations"] = ["The matched request localizes the observed wait. The reason payment slowed remains unverified."]
        result["next_checks"] = ["Check the bound lab control state and compare checkout latency after an approved restoration."]
    return result

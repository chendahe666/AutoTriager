"""Draft bounded lab actions and turn verified repeat incidents into follow-ups."""

from hashlib import sha256
from json import dumps
from math import isfinite
from re import fullmatch


FINGERPRINT = "checkout.payment.delay"
OPERATION = "set_payment_delay_ms"
BASELINE_DELAY_MS = 15
INJECTED_DELAY_MS = 240
CHECKOUT_THRESHOLD_MS = 100.0
RECOVERY_SAMPLES = 3
RECOVERY_SECONDS = 15


def _lab_id(value):
    return isinstance(value, str) and fullmatch(r"[0-9a-f]{32}", value) is not None


def _valid_generation(value):
    return type(value) is int and value >= 0


def _finite_number(value):
    return type(value) in (float, int) and isfinite(value)


def _finding(diagnosis):
    if not isinstance(diagnosis, dict):
        return None
    finding = diagnosis.get("finding")
    if not isinstance(finding, dict):
        return None
    evidence = finding.get("evidence_ids")
    if (finding.get("status") in {"hypothesis", "confirmed_in_lab"}
            and finding.get("fingerprint") == FINGERPRINT
            and _valid_generation(finding.get("control_generation"))
            and _lab_id(finding.get("lab_id"))
            and isinstance(evidence, list) and evidence
            and all(isinstance(evidence_id, str) and evidence_id for evidence_id in evidence)):
        return finding
    return None


def _operation(lab_id, generation, expected_delay, delay):
    return {
        "operation": OPERATION, "expected_lab_id": lab_id,
        "expected_generation": generation, "expected_delay_ms": expected_delay, "delay_ms": delay,
    }


def draft_mitigation(incident_id, diagnosis, lab_state):
    """Draft one lab reset from a bound diagnosis and an authenticated state read."""
    finding = _finding(diagnosis)
    if finding is None or not isinstance(incident_id, str) or not incident_id:
        return None
    lab_id = lab_state.get("lab_id")
    generation = lab_state.get("generation")
    current_delay = lab_state.get("delay_ms")
    baseline_delay = lab_state.get("baseline_delay_ms")
    if (lab_id != finding["lab_id"] or not _valid_generation(generation)
            or generation != finding["control_generation"]
            or type(current_delay) is not int or current_delay != INJECTED_DELAY_MS
            or type(baseline_delay) is not int or baseline_delay != BASELINE_DELAY_MS):
        return None
    action = _operation(lab_id, generation, current_delay, baseline_delay)
    plan_id = "plan-" + sha256(dumps([incident_id, action], sort_keys=True).encode()).hexdigest()[:24]
    return {
        "plan_id": plan_id, "incident_id": incident_id,
        "summary": "Restore the local lab payment delay to its baseline.",
        "scope": "checkout/payment lab", "fingerprint": FINGERPRINT,
        "requires_approval": True, "evidence_ids": list(dict.fromkeys(finding["evidence_ids"])),
        "action": action,
        "rollback": _operation(lab_id, generation + 1, baseline_delay, current_delay),
        "verification": {
            "min_samples": RECOVERY_SAMPLES, "max_seconds": RECOVERY_SECONDS,
            "checkout_threshold_ms": CHECKOUT_THRESHOLD_MS, "expected_http_status": 200,
        },
    }


def assess_recovery(plan, samples, *, applied_at_unix, now_unix=None):
    """Score distinct, consecutive post-change checkout samples from the applied generation."""
    if not _finite_number(applied_at_unix) or (now_unix is not None and not _finite_number(now_unix)):
        raise ValueError("Recovery times must be finite Unix seconds")
    action = plan["action"]
    lab_id = action["expected_lab_id"]
    generation = action["expected_generation"] + 1
    deadline = applied_at_unix + RECOVERY_SECONDS
    limit = deadline if now_unix is None else min(deadline, now_unix)
    observations = {}
    for sample in samples:
        observed_at = sample.get("observed_at_unix")
        request_id = sample.get("request_id")
        if (sample.get("lab_id") == lab_id and _valid_generation(sample.get("control_generation"))
                and sample["control_generation"] == generation and _finite_number(observed_at)
                and applied_at_unix < observed_at <= limit and isinstance(request_id, str) and request_id):
            if request_id in observations and observations[request_id] != sample:
                raise ValueError("A request ID has conflicting recovery samples")
            observations[request_id] = sample

    consecutive = []
    last_seen = applied_at_unix
    for sample in sorted(observations.values(), key=lambda row: row["observed_at_unix"]):
        last_seen = sample["observed_at_unix"]
        latency = sample.get("latency_ms")
        if sample.get("http_status") == 200 and _finite_number(latency) and 0 <= latency <= CHECKOUT_THRESHOLD_MS:
            consecutive.append({key: sample[key] for key in ("request_id", "observed_at_unix", "latency_ms", "http_status")})
            if len(consecutive) == RECOVERY_SAMPLES:
                break
        else:
            consecutive.clear()
    clock = last_seen if now_unix is None else now_unix
    status = "failed" if clock >= deadline else "pending"
    if len(consecutive) == RECOVERY_SAMPLES:
        status = "verified"
    return {
        "status": status, "plan_id": plan["plan_id"], "lab_id": lab_id, "generation": generation,
        "applied_at_unix": applied_at_unix, "deadline_unix": deadline, "samples": consecutive,
        "evidence_ids": [sample["request_id"] for sample in consecutive],
    }


def _verified_episode(record):
    finding = _finding(record.get("diagnosis", {}))
    action = record.get("action_receipt")
    recovery = record.get("recovery")
    incident_id = record.get("incident_id")
    if (finding is None or not isinstance(incident_id, str) or not incident_id
            or not isinstance(action, dict) or not isinstance(recovery, dict)
            or action.get("status") != "applied" or recovery.get("status") != "verified"):
        return None
    lab_id, generation = finding["lab_id"], finding["control_generation"]
    if (action.get("lab_id") != lab_id or not _valid_generation(action.get("generation"))
            or action["generation"] != generation + 1 or not _finite_number(action.get("applied_at_unix"))):
        return None
    expected_plan = draft_mitigation(incident_id, record["diagnosis"], {
        "lab_id": lab_id, "generation": generation,
        "delay_ms": INJECTED_DELAY_MS, "baseline_delay_ms": BASELINE_DELAY_MS,
    })
    if (action.get("plan_id") != expected_plan["plan_id"] or recovery.get("plan_id") != expected_plan["plan_id"]
            or recovery.get("lab_id") != lab_id or recovery.get("generation") != generation + 1
            or recovery.get("applied_at_unix") != action["applied_at_unix"]):
        return None
    samples = recovery.get("samples")
    if not isinstance(samples, list) or len(samples) != RECOVERY_SAMPLES or not all(isinstance(sample, dict) for sample in samples):
        return None
    samples = [{**sample, "lab_id": lab_id, "control_generation": generation + 1} for sample in samples]
    try:
        replay = assess_recovery(expected_plan, samples, applied_at_unix=action["applied_at_unix"])
    except ValueError:
        return None
    if replay["status"] != "verified" or replay["evidence_ids"] != recovery.get("evidence_ids"):
        return None
    return (lab_id, generation), {
        "incident_id": incident_id, "plan_id": expected_plan["plan_id"],
        "diagnosis_evidence_ids": finding["evidence_ids"], "recovery_evidence_ids": replay["evidence_ids"],
    }


def prevention_recommendations(records):
    """Recommend a recurrence-control experiment after two independently verified lab episodes."""
    distinct = {}
    seen_incidents = set()
    for record in records:
        episode = _verified_episode(record)
        if episode is None:
            continue
        identity, evidence = episode
        incident_id = evidence["incident_id"]
        if identity not in distinct and incident_id not in seen_incidents:
            distinct[identity] = evidence
            seen_incidents.add(incident_id)
    if len(distinct) < 2:
        return []
    return [{
        "recommendation_id": "checkout-payment-delay-resilience",
        "fingerprint": FINGERPRINT, "scope": "checkout/payment lab",
        "category": "observability_and_resilience", "status": "proposed",
        "title": "Add a payment degradation check and rehearse containment.",
        "summary": "Distinct payment delay episodes exceeded the checkout latency threshold and recovered after the lab delay was reset.",
        "steps": [
            "Measure the payment dependency latency alongside checkout latency and expose both in the investigation.",
            "Exercise slow payment and healthy control runs under matched load before changing alert routing or timeouts.",
        ],
        "verification": "Compare checkout threshold breaches, time to detection, and healthy-run false alerts in a held-out repeat experiment.",
        "limitation": "Observed recovery supports the lab mechanism. Whether the proposed changes reduce future incidents requires a separate comparison.",
        "incident_ids": sorted(seen_incidents),
        "evidence": sorted(distinct.values(), key=lambda row: row["incident_id"]),
    }]

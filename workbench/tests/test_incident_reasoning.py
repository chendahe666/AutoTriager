"""Check incident routing boundaries and evidence-linked confirmation."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from autotriager.incident_reasoning import diagnose_incident, route_alert
from autotriager.live_alerts import LiveAlerts


def stamp(second):
    return datetime.fromtimestamp(second, timezone.utc).isoformat()


def test_queue_keeps_older_unresolved_incidents_before_newer_recoveries(tmp_path):
    alerts = object.__new__(LiveAlerts)
    alerts.path = tmp_path / "alerts.sqlite3"
    with sqlite3.connect(alerts.path) as db:
        db.execute("CREATE TABLE alerts (id TEXT PRIMARY KEY, triggered_at TEXT, snapshot TEXT)")
        db.execute("CREATE TABLE incident_actions (incident_id TEXT PRIMARY KEY, result TEXT)")
        for n in range(103):
            case_id = f"case-{n}"
            when = stamp(n)
            case = {"id": case_id, "title": "Checkout latency", "start": when, "end": stamp(n + 1),
                    "source": "live", "triggered_at": when}
            db.execute("INSERT INTO alerts VALUES (?, ?, ?)", (case_id, when, json.dumps(case)))
            if n:
                status = "verification_failed" if n == 1 else "verified"
                db.execute("INSERT INTO incident_actions VALUES (?, ?)", (case_id, json.dumps({"status": status})))
    queue = alerts.queue()
    assert [(item["id"], item["resolution"]) for item in queue[:3]] == [
        ("case-1", "unresolved"), ("case-0", "unresolved"), ("case-102", "resolved"),
    ]
    assert len(queue) == 100


def alert(**overrides):
    return {
        "event_id": "new-event", "source": "prometheus", "scope": "lab-a",
        "service": "checkout", "rule": "checkout-latency", "target": "instance-a",
        "triggered_at": stamp(120), **overrides,
    }


def prior(**overrides):
    return {
        **{key: value for key, value in alert().items() if key not in ("event_id", "triggered_at")},
        "incident_id": "existing", "last_event_at": stamp(100), "event_ids": ["previous-event"],
        "state": "investigating", **overrides,
    }


def test_related_firing_alert_links_and_duplicate_does_not_restart_work():
    assert route_alert(alert(), [prior()]) == {
        "decision": "link", "incident_id": "existing", "reason": "matching_open_incident_in_window",
    }
    assert route_alert(alert(event_id="previous-event"), [prior(state="closed")]) == {
        "decision": "skip", "incident_id": "existing", "reason": "duplicate_delivery",
    }
    conflict = route_alert(alert(event_id="previous-event", service="payments"), [prior()])
    assert conflict == {"decision": "skip", "incident_id": None, "reason": "event_identity_conflict"}


@pytest.mark.parametrize("overrides", [
    {"service": "payment"}, {"scope": "lab-b"}, {"target": "instance-b"},
    {"rule": "checkout-errors"}, {"source": "another-receiver"},
    {"triggered_at": stamp(99)}, {"triggered_at": stamp(401)},
])
def test_identity_or_time_mismatch_starts_separate_investigation(overrides):
    assert route_alert(alert(**overrides), [prior()])["decision"] == "new"


def test_skips_approved_suppression_and_non_firing_without_linking():
    assert route_alert(alert(suppressed=True), [prior()])["reason"] == "explicitly_suppressed"
    assert route_alert(alert(status="resolved"), [prior()])["reason"] == "not_firing"
    assert route_alert(alert(triggered_at="2026-11-06T12:00:00"), [prior()])["reason"] == "invalid_event"
    assert route_alert(alert(), [prior(state="closed")])["decision"] == "new"


@pytest.fixture
def measured_case():
    return {
        "id": "incident-a", "source": "live", "triggered_at": stamp(100),
        "metric_evidence": {"service": "checkout", "metric": "checkout latency", "value": 244.0,
                            "threshold": 100.0, "unit": "ms", "observed_at": stamp(101)},
        "matched_request": {"request_id": "0" * 32, "service": "checkout", "dependency": "payment",
                            "route": "/checkout", "dependency_route": "/authorize",
                            "observed_at_unix": 100.5, "latency_ms": 244.0, "dependency_latency_ms": 241.0,
                            "http_status": 200, "control_generation": 5, "lab_id": "1" * 32,
                            "configured_delay_ms": 240},
        "evidence": [{"id": identifier} for identifier in ("prom-rule", "prom-metric", "lab-request")],
    }


@pytest.fixture
def recovery(measured_case):
    return {
        "status": "verified", "plan_id": "plan-a", "lab_id": "1" * 32, "generation": 6,
        "applied_at_unix": 110,
        "plan": {
            "plan_id": "plan-a", "incident_id": measured_case["id"],
            "action": {"operation": "set_payment_delay_ms", "expected_lab_id": "1" * 32,
                       "expected_generation": 5, "expected_delay_ms": 240, "delay_ms": 15},
        },
        "action_receipt": {"status": "applied", "lab_id": "1" * 32, "generation": 6,
                           "delay_ms": 15, "baseline_delay_ms": 15, "applied_at_unix": 110},
        "samples": [
            {"request_id": "2" * 32, "observed_at_unix": 111, "latency_ms": 19.5, "http_status": 200},
            {"request_id": "3" * 32, "observed_at_unix": 112, "latency_ms": 20.5, "http_status": 200},
            {"request_id": "4" * 32, "observed_at_unix": 113, "latency_ms": 21.0, "http_status": 200},
        ],
        "evidence_ids": [digit * 32 for digit in ("2", "3", "4")],
    }


def test_matched_payment_alone_supports_only_a_hypothesis(measured_case):
    result = diagnose_incident(measured_case)
    assert result["observation"]["value"] == 244.0
    assert result["finding"]["status"] == "hypothesis"
    assert result["finding"]["fingerprint"] == "checkout.payment.delay"
    assert result["finding"]["control_generation"] == 5
    assert set(result["finding"]["evidence_ids"]) == {"prom-rule", "prom-metric", "lab-request"}


@pytest.mark.parametrize(("field", "value"), [
    ("latency_ms", 245), ("observed_at_unix", 102), ("observed_at_unix", 97),
    ("dependency_latency_ms", 245), ("dependency_latency_ms", 80), ("service", "inventory"),
])
def test_conflicting_or_nondiscriminating_request_cannot_establish_hypothesis(measured_case, field, value):
    measured_case["matched_request"][field] = value
    result = diagnose_incident(measured_case)
    assert result["observation"]["status"] == "observed"
    assert result["finding"]["status"] == "insufficient"
    assert result["finding"]["fingerprint"] is None
    assert result["next_checks"]


def test_no_symptom_claim_when_evidence_ids_do_not_bind(measured_case):
    measured_case["evidence"] = [{"id": "prom-rule"}, {"id": "unrelated"}]
    result = diagnose_incident(measured_case)
    assert result["observation"] is None
    assert result["finding"]["status"] == "insufficient"


def test_confirm_only_when_matching_restoration_and_recovery_samples_agree(measured_case, recovery):
    result = diagnose_incident(measured_case, recovery=recovery)
    assert result["finding"]["status"] == "confirmed_in_lab"
    assert set(result["finding"]["evidence_ids"]).issuperset(recovery["evidence_ids"])

    tampered = deepcopy(recovery)
    tampered["plan"]["action"]["expected_generation"] = 4
    assert diagnose_incident(measured_case, recovery=tampered)["finding"]["status"] == "hypothesis"

    tampered = deepcopy(recovery)
    tampered["samples"][0]["observed_at_unix"] = 110
    assert diagnose_incident(measured_case, recovery=tampered)["finding"]["status"] == "hypothesis"

    tampered = deepcopy(recovery)
    tampered["samples"][0]["latency_ms"] = 101
    assert diagnose_incident(measured_case, recovery=tampered)["finding"]["status"] == "hypothesis"

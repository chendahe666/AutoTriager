from copy import deepcopy

from autotriager.incident_actions import assess_recovery, draft_mitigation, prevention_recommendations


LAB = "a" * 32


def diagnosis(generation=1):
    return {"finding": {"status": "hypothesis", "fingerprint": "checkout.payment.delay",
                        "evidence_ids": ["prom-metric", "lab-request"],
                        "control_generation": generation, "lab_id": LAB}}


def state(generation=1, **changes):
    return {"generation": generation, "lab_id": LAB, "delay_ms": 240, "baseline_delay_ms": 15, **changes}


def sample(index, *, generation=2, observed_at=None, latency=40, http_status=200):
    return {"request_id": f"request-{generation}-{index}", "lab_id": LAB,
            "control_generation": generation, "observed_at_unix": 100 + index if observed_at is None else observed_at,
            "http_status": http_status, "latency_ms": latency}


def test_draft_binds_approval_and_rollback_to_the_exact_lab_state():
    plan = draft_mitigation("incident-1", diagnosis(), state())
    assert plan["requires_approval"] is True
    assert plan["action"] == {"operation": "set_payment_delay_ms", "expected_lab_id": LAB,
                              "expected_generation": 1, "expected_delay_ms": 240, "delay_ms": 15}
    assert plan["rollback"] == {"operation": "set_payment_delay_ms", "expected_lab_id": LAB,
                                "expected_generation": 2, "expected_delay_ms": 15, "delay_ms": 240}
    assert draft_mitigation("incident-1", diagnosis(), state(2)) is None
    assert draft_mitigation("incident-1", diagnosis(), state(lab_id="b" * 32)) is None
    assert draft_mitigation("incident-1", diagnosis(), state(delay_ms=200)) is None
    unbound = diagnosis()
    del unbound["finding"]["control_generation"]
    assert draft_mitigation("incident-1", unbound, state()) is None
    assert draft_mitigation("incident-1", diagnosis(), state(delay_ms=15)) is None


def test_recovery_filters_stale_samples_and_requires_consecutive_distinct_passes():
    plan = draft_mitigation("incident-1", diagnosis(), state())
    samples = [sample(1), sample(1), sample(2, generation=1), sample(3, latency=120),
               sample(4), sample(5), sample(0)]
    assert assess_recovery(plan, samples, applied_at_unix=100, now_unix=107)["status"] == "pending"
    assert assess_recovery(plan, samples, applied_at_unix=100, now_unix=115)["status"] == "failed"
    final = assess_recovery(plan, samples + [sample(6)], applied_at_unix=100, now_unix=107)
    assert final["status"] == "verified"
    assert final["evidence_ids"] == ["request-2-4", "request-2-5", "request-2-6"]
    assert assess_recovery(plan, [sample(16), sample(17), sample(18)], applied_at_unix=100,
                           now_unix=119)["status"] == "failed"


def record(incident_id, generation):
    finding = diagnosis(generation)
    plan = draft_mitigation(incident_id, finding, state(generation))
    recovery = assess_recovery(plan, [sample(index, generation=generation + 1) for index in range(1, 4)],
                               applied_at_unix=100)
    receipt = {"status": "applied", "plan_id": plan["plan_id"], "lab_id": LAB,
               "generation": generation + 1, "applied_at_unix": 100}
    return {"incident_id": incident_id, "diagnosis": finding, "action_receipt": receipt, "recovery": recovery}


def test_recurrence_requires_two_independent_verified_episodes():
    first = record("incident-1", 1)
    duplicate_generation = record("incident-duplicate", 1)
    second = record("incident-2", 3)
    assert prevention_recommendations([first, duplicate_generation]) == []
    assert prevention_recommendations([first, {**second, "recovery": None}]) == []
    wrong_receipt = deepcopy(second)
    wrong_receipt["action_receipt"]["plan_id"] = first["action_receipt"]["plan_id"]
    assert prevention_recommendations([first, wrong_receipt]) == []
    wrong_sample = deepcopy(second)
    wrong_sample["recovery"]["samples"][0]["latency_ms"] = 400
    assert prevention_recommendations([first, wrong_sample]) == []
    recommendation, = prevention_recommendations([first, second, duplicate_generation])
    assert recommendation["incident_ids"] == ["incident-1", "incident-2"]
    assert recommendation["status"] == "proposed"
    assert "separate comparison" in recommendation["limitation"]

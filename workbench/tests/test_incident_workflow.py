"""Exercise actual state transitions with deterministic providers and tool receipts."""
import copy
import time
from types import SimpleNamespace

import pytest

from autotriager import incident_workflow as module
from autotriager.errors import APIError
from autotriager.store import Store


def evidence(eid, case="case-a"):
    return {"evidence_id": eid, "service_id": "service", "modality": "trace", "unit": "ms",
            "derivation": "Original source observation",
            "record": {"label": eid, "detail": "Measured source value", "source": case + "/spans.json"},
            "observed_window": {"start": "2022-01-01T00:00:00Z", "end": "2022-01-01T00:01:00Z"}}


class Provider:
    def __init__(self):
        self.calls = []
        self.fail = False

    def capabilities(self):
        return {"enabled": True, "disabled_reason": None}

    def plan_check(self, context, workflow, question, call_id, *, allow_checks):
        self.calls.append(copy.deepcopy(context))
        if self.fail and len(self.calls) == 2:
            raise ValueError("Injected local provider failure")
        checks = [c for c in workflow["checks"] if c["status"] == "proposed" and not c["verification_only"]]
        return {"text": "Observed a delayed call; cause is not proven.", "focus": "Compare paths",
                "hypotheses": [{"title": "Path-specific slowdown", "statement": "A candidate explanation",
                                "status": "candidate", "supporting_evidence_ids": [], "contradicting_evidence_ids": []}],
                "next_check_id": checks[0]["check_id"] if allow_checks and checks else "NONE",
                "selection_reason": "Check a competing path" if checks and allow_checks else "Review limitations",
                "citations": [], "limitations": ["Sparse observations"], "next_checks": []}


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    store = Store(tmp_path / "workflow.sqlite3")
    provider = Provider()
    app = SimpleNamespace(store=store, incident_agent=SimpleNamespace(provider=provider))
    def load(_, case):
        return {"incident_id": case, "context_run_id": case,
                "scope": {"title": case, "source": "recorded", "start": "2022-01-01T00:00:00Z",
                          "end": "2022-01-01T00:01:00Z", "recorded_evidence_count": 1},
                "evidence": [evidence("ev_initial", case)], "evidence_ids": ["ev_initial"]}
    monkeypatch.setattr(module, "load_incident_agent_context", load)
    monkeypatch.setattr(module, "select_incident_agent_context", lambda context, q: context)
    specs = [{"check_id": name, "kind": name, "title": name, "purpose": "Read source",
              "service": "service", "verification_only": name == "verify", "parameters": {"bounded": True}}
             for name in ("trace", "logs", "third", "verify")]
    monkeypatch.setattr(module, "build_check_catalog", lambda *_: copy.deepcopy(specs))
    receipts = []
    def execute(_, case, spec):
        receipts.append(spec["check_id"])
        row = evidence("ev_" + spec["check_id"], case)
        row["derivation"] = {"check_id": spec["check_id"]}
        return {"check_id": spec["check_id"], "status": "completed", "summary": "Actual local test receipt",
                "evidence": [row], "limitations": [], "observed_window": row["observed_window"],
                "recovery_assessment": {"status": "observed_latency_normalization", "summary": "Later samples only"}}
    monkeypatch.setattr(module, "execute_check", execute)
    workflow = module.IncidentWorkflow(app)
    yield workflow, provider, receipts, app
    workflow.close()
    store.close()


def finish(workflow, actor="owner", case="case-a"):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        state = workflow.get(actor, case)
        if state["status"] != "running":
            return state
        time.sleep(.01)
    pytest.fail("Test workflow did not finish")


def test_bounded_model_check_reassessment_and_separate_recovery(fixture):
    workflow, provider, receipts, _ = fixture
    turn = workflow.submit("owner", "case-a", {"question": "Investigate"}, "one")
    state = finish(workflow)
    assert state["status"] == "waiting_for_review"
    assert receipts == ["trace", "logs"]
    assert len(provider.calls) == 3
    assert "ev_trace" in provider.calls[1]["evidence_ids"]
    assert "ev_logs" in provider.calls[2]["evidence_ids"]
    assert state["recovery"]["status"] == "not_checked"
    assert state["cause"]["status"] == "unconfirmed"
    assert len([e for e in state["events"] if e["kind"] == "check_finished"]) == 2
    assert all(e["at"].startswith("20") and not e["at"].startswith("2022") for e in state["events"])
    assert workflow.submit("owner", "case-a", {"question": "Investigate"}, "one") == turn
    assert len(provider.calls) == 3
    with pytest.raises(APIError, match="different operation"):
        workflow.submit("owner", "case-a", {"question": "Different"}, "one")
    verified = workflow.run_check("owner", "case-a", None, "verify-key", verify=True)
    assert verified["recovery"]["status"] == "observed_latency_normalization"
    assert verified["recovery"]["executed_action"] is False
    assert verified["cause"]["status"] == "unconfirmed"
    assert verified["status"] != "resolved"
    workflow.run_check("owner", "case-a", None, "verify-key", verify=True)
    assert receipts.count("verify") == 1
    assert workflow.get("someone-else", "case-a")["checks"][0]["status"] == "proposed"
    assert workflow.get("owner", "case-b")["checks"][0]["status"] == "proposed"


def test_failure_retains_tool_result_and_prevents_fake_completion(fixture):
    workflow, provider, receipts, _ = fixture
    provider.fail = True
    workflow.submit("owner", "case-a", {"question": "Investigate"}, "one")
    state = finish(workflow)
    assert state["status"] == "failed"
    assert receipts == ["trace"]
    assert len(provider.calls) == 2
    assert state["checks"][0]["status"] == "completed"
    assert state["checks"][1]["status"] == "proposed"
    assert state["events"][-1]["kind"] == "failed"
    assert state["recovery"]["status"] == "not_checked"


def test_unknown_check_rejected_and_manual_receipts_shared_with_chat(fixture):
    workflow, _, receipts, _ = fixture
    with pytest.raises(APIError):
        workflow.run_check("owner", "case-a", "arbitrary-command", "unknown")
    assert receipts == []
    workflow.run_check("owner", "case-a", "logs", "known")
    base = module.load_incident_agent_context(None, "case-a")
    enriched = workflow.enrich("owner", "case-a", base)
    assert "ev_logs" in enriched["evidence_ids"]
    assert workflow.enrich("other", "case-a", base) == base


def test_restart_retains_completed_receipts_and_marks_interruption(fixture):
    workflow, _, _, app = fixture
    state = workflow.run_check("owner", "case-a", "logs", "logs")
    state["status"] = "running"
    state["checks"][0]["status"] = "running"
    workflow._save("owner", state)
    reopened = module.IncidentWorkflow(app)
    try:
        restored = reopened.get("owner", "case-a")
        assert restored["status"] == "failed"
        assert restored["checks"][0]["status"] == "failed"
        assert restored["checks"][1]["status"] == "completed"
        assert any(r["evidence_id"] == "ev_logs" for r in restored["evidence"])
    finally:
        reopened.close()


def test_verification_only_records_cannot_enter_diagnostic_model(fixture):
    workflow, _, _, _ = fixture
    state = workflow.get("owner", "case-a")
    later = evidence("ev_later")
    later["derivation"] = {"check_id": "verify", "verification_only": True}
    state["evidence"].append(later)
    context = workflow._model_context(state, "Investigate")
    assert context["evidence_ids"] == ["ev_initial"]


def test_model_supported_status_does_not_verify_causal_mechanism(fixture):
    workflow, _, _, _ = fixture
    h = {"title": "Internal bottleneck", "statement": "Model claims a cause", "status": "supported",
         "supporting_evidence_ids": ["ev_initial"], "contradicting_evidence_ids": []}
    scoped = workflow._unverified_hypothesis(h)
    assert scoped["status"] == "candidate"
    assert scoped["model_assessment"] == "supported"
    assert scoped["verification_status"] == "unverified"
    assert scoped["statement"] == h["statement"]
    state = workflow.get("owner", "case-a")
    state["hypotheses"] = [h]
    workflow._save("owner", state)
    restored = workflow.get("owner", "case-a")
    assert restored["hypotheses"][0]["verification_status"] == "unverified"
    assert restored["cause"]["status"] == "unconfirmed"
    assert restored["events"][-1]["kind"] == "policy_refinement"

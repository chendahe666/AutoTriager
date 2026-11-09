"""Whitelisted read-only aggregate receipts; never read benchmark labels.

These are historical CLI scores, not an evaluation of the conversation feature.
"""
import hashlib
import json
import math
from .contracts import ROOT

# Fixed published aggregate pair for this dated view, not arbitrary same-shape
# receipts. Updating the campaign requires an explicit new projection/version.
EXPECTED_HASHES = (
    "b264e5450fd186f59ae8a28281009c7ee9e55d88d85af91452f6b6d67f3bf080",
    "b5bba9acb27ad3d968109c328259a7bab9e5a8603d2ecc0d48b39d0967d812ea",
)
COMPARISON_RECEIPT = "verification/re2ob-multisource-baro-20261108/receipt.json"
COMPARISON_SHA256 = "e7f65214ba4b0f1547b54ec91c58c75b25d7ee737e2e9dff07c23f1a2bc2cf91"


def rcaeval_comparison(root=ROOT):
    """Publish only service-ranking aggregates from the frozen comparison."""
    try:
        with (root / COMPARISON_RECEIPT).open("rb") as source:
            raw = source.read(16_385)
        if hashlib.sha256(raw).hexdigest() != COMPARISON_SHA256:
            raise ValueError("Comparison receipt changed")
        receipt = json.loads(raw)
        methods = []
        for label in ("Metric BARO", "Multi-source BARO", "B1"):
            score = receipt["scores"][label]
            methods.append({"label": label, "case_count": score["planned"],
                            "top_hits": [round(score[f"AC@{k}"] * score["planned"]) for k in (1, 3, 5)],
                            "avg_at_5": score["Avg@5"]})
        return {"available": True, "cohort": "RE2 Online Boutique", "methods": methods,
                "receipt_sha256": COMPARISON_SHA256}
    except (OSError, ValueError, KeyError, TypeError):
        return {"available": False}


def evaluation_summary(root=ROOT):
    paths = [
        "verification/full-r1-real-gemini-20261104-scores/receipt.json",
        "verification/full-r1-real-gemini-20261104/usage-summary.json",
    ]
    try:
        raw = [(root / path).read_bytes() for path in paths]
    except FileNotFoundError:
        return {"available": False, "reason": "Retained R1 aggregate receipts are not installed on this machine. No experiment was started."}
    try:
        if tuple(hashlib.sha256(value).hexdigest() for value in raw) != EXPECTED_HASHES:
            raise ValueError()
        scores, usage = (json.loads(value) for value in raw)
        if scores["kind"] != "real_full_re2ob_service_localization_not_causal_certification" or usage["kind"] != "observed_token_derived_nominal_fees_not_invoice":
            raise ValueError()
        arms = []
        for name in ("B1", "B2"):
            result, arm_usage = scores["results"][name], usage["arms"][name]
            planned, attempted = result["planned"], result["attempted"]
            completed = arm_usage["status_counts"].get("completed", 0)
            failed = arm_usage["status_counts"].get("failed", 0)
            if any(type(v) is not int or v < 0 for v in (planned, attempted, completed, failed)) or not 0 < planned == scores["all_cases"] or attempted > planned or completed + failed != attempted:
                raise ValueError()
            metrics = [result[field] for field in ("AC@1", "AC@3", "AC@5", "Avg@5")]
            if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or not 0 <= v <= 1 for v in metrics):
                raise ValueError()
            if scores["all_cases"] != 90 or not metrics[0] <= metrics[1] <= metrics[2]:
                raise ValueError()
            for value in (arm_usage["nominal_known_fee_usd"], arm_usage["median_workflow_wall_seconds"]):
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                    raise ValueError()
            arms.append({"id": name, "planned": planned, "attempted": attempted, "completed": completed, "failed": failed,
                         "ac_at_1": metrics[0], "ac_at_3": metrics[1], "ac_at_5": metrics[2], "avg_at_5": metrics[3],
                         "nominal_usd": arm_usage["nominal_known_fee_usd"], "median_seconds": arm_usage["median_workflow_wall_seconds"]})
        return {"available": True, "model_id": "gemini-3.5-flash-lite", "cohort_size": scores["all_cases"],
                "completed_at": scores["scored_at"], "arms": arms,
                "usage": {"generations": usage["generation_responses_with_valid_usage"], "input_tokens": usage["input_tokens"],
                          "output_and_thinking_tokens": usage["output_and_thought_tokens"], "nominal_usd": usage["known_nominal_fee_usd"],
                          "unresolved_calls": usage["unresolved_attempts"]},
                "limitations": ["Historical CLI experiment on 90 exposed RE2-OB development/audit cases, not held-out validation or the current conversation feature.",
                                "Failed answers remain misses in the common 90-case denominator. Correct service labels do not validate causal mechanisms or safe repair.",
                                "B2 Holmes did not improve service localization relative to B1 fixed-context Gemini. Compute budgets differ between the arms.",
                                "Nominal token costs precede credits/tax/cache discounts and are not account billing. Workflow duration is not human investigation time or MTTR.",
                                "The browser still loads one recorded 60-second preview, not all 90 benchmark cases."],
                "provenance": [{"path": path, "sha256": hashlib.sha256(value).hexdigest()} for path, value in zip(paths, raw)]}
    except (ValueError, KeyError, TypeError):
        return {"available": False, "reason": "The retained aggregate receipts failed their format checks; no scores or new experiment are substituted."}

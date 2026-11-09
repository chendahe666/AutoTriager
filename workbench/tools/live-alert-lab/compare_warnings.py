"""Compare warning methods on the same timed requests from the checkout lab."""

import argparse
import json
import statistics
import time
from contextlib import ExitStack
from pathlib import Path

from checkout_lab import (
    BASE_MS, CADENCE, FAULT_MS, THRESHOLD_MS, Lab, checkout_handler, payment_handler, serve,
)


TRIALS = (
    ("step-01", "step", 0.0), ("ramp-01", "ramp", 2.2),
    ("control-ramp", "control_ramp", 1.5), ("step-02", "step", 0.0),
    ("ramp-02", "ramp", 3.0), ("control-pulse", "control_pulse", 0.0),
    ("step-03", "step", 0.0), ("ramp-03", "ramp", 3.8),
)
PRELUDE_SECONDS = 2
REQUESTS_PER_TRIAL = 15


def timed_samples(lab, count):
    start = time.monotonic()
    for step in range(count):
        time.sleep(max(0, start + step * CADENCE - time.monotonic()))
        yield lab.sample()


def payment_delay(profile, elapsed, ramp_seconds):
    if elapsed < 0:
        return BASE_MS
    if profile == "step":
        return FAULT_MS
    if profile == "ramp":
        return BASE_MS + (FAULT_MS - BASE_MS) * min(1, elapsed / ramp_seconds)
    if profile == "control_ramp":
        return BASE_MS + (87 - BASE_MS) * min(1, elapsed / ramp_seconds)
    return 85 if elapsed < 0.9 else BASE_MS


def adjacent(first, second):
    return 0 < second["observed_at_unix"] - first["observed_at_unix"] <= 1.5 * CADENCE


def forecast_ar1(calibration, prefix):
    pairs = [(a["latency_ms"], b["latency_ms"]) for segment in (calibration, prefix)
             for a, b in zip(segment, segment[1:]) if adjacent(a, b)]
    if len(pairs) < 8:
        return None
    prior, after = zip(*pairs)
    prior_mean = statistics.mean(prior)
    after_mean = statistics.mean(after)
    variance = sum((value - prior_mean) ** 2 for value in prior)
    covariance = sum((a - prior_mean) * (b - after_mean) for a, b in zip(prior, after))
    slope = max(0.0, min(1.0, covariance / variance)) if variance else 0.0
    return max(0.0, after_mean - slope * prior_mean + slope * prefix[-1]["latency_ms"])


def score(trials, calibration, method):
    cases = []
    leads = []
    false_warnings = 0
    noncrossing_decisions = 0
    eligible_faults = 0
    early_hits = 0
    control_trials_warned = 0
    for trial in trials:
        rows = trial["samples"]
        onset = trial["fault_onset_unix"]
        times = [row["observed_at_unix"] for row in rows]
        values = [row["latency_ms"] for row in rows]
        cross = next((i for i, value in enumerate(values) if onset is not None
                      and times[i] >= onset and value > THRESHOLD_MS), None)
        eligible_faults += cross is not None
        false_count = 0
        warn_indices = []
        matched = None
        for i in range(1, len(values) - 1):
            current = values[i]
            following = values[i + 1]
            if not adjacent(rows[i - 1], rows[i]) or not adjacent(rows[i], rows[i + 1]) or current > THRESHOLD_MS:
                continue
            if method == "trend":
                prediction = current + (current - values[i - 1])
            elif method == "ar1":
                prediction = forecast_ar1(calibration, rows[:i + 1])
            else:
                prediction = None
            warning = prediction is not None and prediction > THRESHOLD_MS
            noncrossing_decisions += following <= THRESHOLD_MS
            if warning:
                warn_indices.append(i)
                false_count += following <= THRESHOLD_MS
                if i + 1 == cross:
                    matched = i
        if method == "reactive" and cross is not None:
            matched = cross
        lead = round(times[cross] - times[matched], 6) if matched is not None else None
        if lead is not None:
            leads.append(lead)
            early_hits += lead > 0
        false_warnings += false_count
        trial_alerted = any(v > THRESHOLD_MS for v in values) if method == "reactive" else bool(warn_indices)
        control_warned = onset is None and trial_alerted
        control_trials_warned += control_warned
        cases.append({"case": trial["case"], "first_cross_unix": times[cross] if cross is not None else None,
                      "matched_warning_unix": times[matched] if matched is not None else None,
                      "lead_seconds": lead, "false_warning_count": false_count,
                      "control_trial_warned": control_warned})
    return {"eligible_fault_crossings": eligible_faults, "matched_crossings": len(leads),
            "missed_crossings": eligible_faults - len(leads), "advance_hits": early_hits,
            "advance_misses": eligible_faults - early_hits,
            "median_lead_seconds_for_hits": statistics.median(leads) if leads else None,
            "false_next_step_warnings": false_warnings,
            "noncrossing_decision_windows": noncrossing_decisions,
            "control_trials_warned": control_trials_warned, "cases": cases}


def capture(port, payment_port):
    lab = Lab(port, payment_port, None)
    with ExitStack() as stack:
        stack.enter_context(serve(payment_port, payment_handler(lab)))
        stack.enter_context(serve(port, checkout_handler(lab)))
        calibration = list(timed_samples(lab, 12))
        trials = []
        for case, profile, ramp_seconds in TRIALS:
            onset_monotonic = time.monotonic() + PRELUDE_SECONDS
            onset_unix = time.time() + PRELUDE_SECONDS
            lab.delay = lambda now: payment_delay(profile, now - onset_monotonic, ramp_seconds)
            rows = list(timed_samples(lab, REQUESTS_PER_TRIAL))
            trials.append({"case": case, "profile": profile,
                           "fault_onset_unix": None if profile.startswith("control_") else onset_unix,
                           "samples": rows})
            print(f"{case}: {len(rows)} measured checkout requests", flush=True)
    return calibration, trials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rescore", type=Path, help="Use the retained requests from a previous lab run")
    parser.add_argument("--port", type=int, default=9474)
    parser.add_argument("--payment-port", type=int, default=9475)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    if args.rescore:
        original = json.loads(args.rescore.read_text(encoding="utf-8"))
        calibration, trials = original["calibration"], original["trials"]
    else:
        calibration, trials = capture(args.port, args.payment_port)
    if any(row["http_status"] != 200 or row["dependency_status"] != 200
           for row in calibration + [row for trial in trials for row in trial["samples"]]):
        raise SystemExit("The comparison requires completed checkout and payment requests")
    results = {method: score(trials, calibration, method) for method in ("reactive", "ar1", "trend")}
    report = {"source": "local controlled checkout-to-payment HTTP testbed",
              "threshold_ms": THRESHOLD_MS, "cadence_seconds": CADENCE,
              "scoring": "Methods share the same valid windows with contiguous previous, current, and next observations. A forecast matches only if the next completed request is the first post-injection crossing. A reactive alarm has zero lead. False forecasts target an at-or-below-threshold request. Control alarms are counted by trial. AR(1) never joins a pair across the separate calibration and trial segments.",
              "calibration": calibration, "trials": trials, "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps({method: {key: val for key, val in result.items() if key != "cases"}
                      for method, result in results.items()}, indent=2))


if __name__ == "__main__":
    main()

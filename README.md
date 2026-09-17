# AutoTriager

**Evidence-grounded incident triage for microservices.**

AutoTriager is a capstone project developing an AI copilot for on-call engineers. It aims to connect an alert with relevant logs, metric anomalies, recent code changes, and historical incidents, then produce a traceable explanation of the most likely cause and a recommended next investigation step.

The central question is: **What most likely caused this incident, what evidence supports that conclusion, and when is the evidence insufficient to blame a recent change?**

> **Status: project planning and repository setup.** Implementation, datasets, runnable demonstrations, and reproducible evaluation results have not yet been published in this repository. The capabilities and milestones below describe the planned system; performance targets are not measured results.

CS 5588 — Data Science Capstone · University of Missouri–Kansas City · Fall 2026

## The problem

During a microservice incident, engineers must reconstruct a timeline across alerts, logs, dashboards, deployment history, and code reviews. A recent deployment may be relevant, but temporal proximity alone does not establish causation. The actual cause may lie in another service, an external dependency, or missing operational context.

AutoTriager is designed to reduce the work required to assemble and inspect this evidence. Its output should help an engineer distinguish an actionable hypothesis from an unsupported guess.

## Planned user experience

1. **Select or replay an incident.** Start with an alert, affected service, and time window.
2. **Inspect the evidence.** View abnormal log patterns, metric changes, candidate deployments, and relevant historical incidents.
3. **Review ranked hypotheses.** See supporting and contradictory evidence, source references, and unresolved uncertainty.
4. **Choose the next step.** Review a proposed investigation or mitigation step and record whether the recommendation was useful.

A triage report will include the incident timeline, suspected service and fault category, candidate code change where supported, evidence references, alternative explanations, and missing information. If evidence is insufficient, the system should abstain from PR attribution and explain what additional evidence is needed.

Mitigation remains a human decision. Automatic production rollback is outside the initial scope.

## How it is designed to work

The core retrieval unit is an **incident evidence bundle**: a structured collection of observations linked by incident ID, service, time window, and source references.

```text
Alert + incident time window
            |
            v
Evidence preparation
  - Parse relevant logs
  - Calculate metric anomalies
  - Identify candidate code changes
            |
            v
Incident evidence bundle <--- Retrieve similar historical incidents
            |
            v
Evidence-based hypothesis ranking and explanation
            |
            v
Triage report ---> Engineer review and feedback
```

Deterministic tools perform parsing and numerical calculations. Retrieval selects relevant evidence. The language model compares and summarizes that evidence. Logs, time series, and code retain their native structure and provenance rather than being reduced to unsupported prose.

Deployment metadata may be captured before an incident to narrow the candidate change set. This provides context for post-incident investigation; it is not a claim that the system can predict whether a PR will fail.

## Roadmap: progressively harder incident-triage problems

Each milestone will deliver a complete path from **data → analysis → working application feature → evaluation**. Technical components are selected to address demonstrated failure modes.

Dates follow the course syllabus. These are proposed AutoTriager milestones, subject to the corresponding challenge handouts and Canvas deadlines. Challenge 1 is the separate course kickoff exercise and is not presented here as completed AutoTriager work.

| Milestone | Incident-triage problem | Data and solution | Working feature | Evaluation |
|---|---|---|---|---|
| **Challenge 2 · Sep 7–25** | Explain a controlled incident in one service with a known injected cause. | Build a small evidence bundle connecting the alert, logs, metric anomaly, candidate change, and a separate historical precedent where available. Use a simple evidence-ranking baseline and grounded explanation. | A minimal incident replay/report view with inspectable evidence links. | 5–20 cases with expected evidence and explicit pass/fail criteria; test source validity, supported conclusions, and missing-evidence handling. |
| **Challenge 3 · Sep 28–Oct 16** | Identify the relevant change when several deployments and similar-looking incidents compete. | Introduce distractor changes and confusable historical cases; combine time/service filtering with retrieval and hypothesis comparison. | A candidate-comparison view explaining why one hypothesis ranks above alternatives. | Held-out candidate-ranking accuracy, retrieval relevance, citation support, and comparison against a latest-deployment heuristic. |
| **Challenge 4 · Oct 19–Nov 6** | Distinguish the initiating failure from downstream symptoms, including external-dependency faults. | Use multi-service incidents, dependency context, conflicting observations, and examples without a causal PR. | A cross-service incident timeline with alternatives and explicit abstention from unsupported attribution. | Root-service accuracy, false PR attribution on external faults, and accuracy versus the fraction of cases receiving an attribution. |
| **Challenge 5 · Nov 9–13 · Hack-A-Roo** | Produce useful triage during a live testbed incident while evidence arrives late or is incomplete. | Replay or inject controlled faults; collect evidence within a fixed time budget and expose missing inputs. | A live or replay dashboard showing evidence availability, the triage report, and an engineer review action. | Repeated end-to-end demonstrations; report completion rate, evidence correctness, latency, and model cost. |
| **Challenge 6 · Nov 16–Dec 11** | Preserve trustworthy behavior on unseen incidents and degraded telemetry. | Freeze an evaluation set with unseen incident runs, noisy/missing evidence, and matched variants for controlled comparisons. | A reproducible release with benchmark results and visible limitations or insufficient-evidence states. | Held-out evaluation, failure analysis, component ablations, and a clean-environment reproduction check. |

RAG, alternative models, and additional agents are possible implementation choices. An added component must improve an observed weakness enough to justify its complexity, cost, and latency.

## Data and evaluation plan

The proposed evaluation combines selected **RCAEval** cases with controlled fault injections in a **Google Online Boutique** testbed. Availability of logs, metrics, traces, code changes, and labels will be checked for each case before it enters an experiment.

- **Keep prediction tasks distinct.** Root-service identification, fault-category classification, and causal-PR attribution require different labels and metrics. PR attribution will only be evaluated where the causal change is explicitly recorded.
- **Preserve provenance.** Evidence references should resolve to source files, log lines, timestamps, metric windows, or commits.
- **Avoid leakage.** Keep the current incident's ground truth and completed postmortem out of its runtime evidence. Separate evaluation incidents and repeated runs of the same injected change from development examples and retrieval material that would reveal the answer.
- **Compare fairly.** Use the same cases and available evidence for simple heuristics, an LLM baseline, and the proposed system. Include external faults and insufficient-evidence cases.
- **Measure support, not just citation presence.** Check whether cited evidence actually supports a claim. Record both incorrect answers and appropriate abstentions.
- **Publish reproducible measurements.** Report case counts, splits, configuration, failures, latency, and cost alongside aggregate scores.

Initial research targets from the proposal are listed below. They will be revisited after the first reproducible baseline and are not course-wide requirements or achieved results.

| Metric | Initial target | Measurement boundary |
|---|---|---|
| Top-1 causal-PR attribution accuracy | ≥80% | Controlled code-change incidents with known causal PRs; an abstention does not count as a correct attribution. |
| External-fault non-attribution rate | ≥85% | Labeled external faults where the system correctly avoids blaming an internal PR; also report internal-case attribution coverage to detect trivial always-abstain behavior. |
| End-to-end triage latency | <60 seconds | Measure from accepted alert to validated report, including evidence collection/retrieval; report the distribution and failures. |
| Model API cost per triage | <$0.10 | Record all model calls and token costs; report testbed and hosting costs separately. |

## Development and reproducibility

Every challenge will preserve four kinds of evidence:

- **Human design:** the initial question, dataset understanding, baseline, and acceptance criteria.
- **AI-assisted design:** prompts, proposed architecture, assumptions, and generated changes.
- **Human review:** accepted, rejected, or corrected suggestions with reasons.
- **Verification and reflection:** executed tests, outputs, failure cases, and the next improvement.

Planned tools include Python, log parsing and metric-analysis utilities, retrieval components, and a lightweight application interface. Specific models, databases, and orchestration libraries will be chosen and documented as the evidence warrants them.

**Getting started:** no runnable application or installation procedure is published yet. Setup instructions, sample data, dependencies, and demonstration commands will accompany the first working release.

## References

- [RCAEval](https://github.com/phamquiluan/RCAEval) — candidate root-cause analysis benchmark.
- [Google Online Boutique](https://github.com/GoogleCloudPlatform/microservices-demo) — proposed microservice testbed.

## Author

[Dahe (Mike) Chen](https://github.com/chendahe666)

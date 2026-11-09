"""Keep historical memory separate from current incident evidence."""
from __future__ import annotations
import copy
import re
import json


def historical_question(question):
    return bool(re.search(r'\b(?:history|historical)\b|\b(?:earlier|previous|prior|past|similar)(?:\s+[a-z-]+){0,4}\s+(?:incidents?|cases?|observations?|records?|failures?|outages?|patterns?)\b|历史|类似|(?:以前|过去|之前|曾经).{0,12}(?:案例|事故|记录|观测|故障)', question, re.I))


def memory_trace(snapshot):
    return {"snapshot_id": snapshot.get("snapshot_id"),
            "items": [{"memory_id": item["memory_id"], "revision": item["revision"],
                       "title": item.get("title", ""), "summary": item.get("summary", ""),
                       "claim_kind": "recorded_observation", "causal_status": "not_assessed",
                       "source": {key: item["source"].get(key) for key in ("kind", "incident_id", "run_id", "investigation_id", "data_revision", "digest")},
                       "review": copy.deepcopy(item.get("review")), "match_reason": copy.deepcopy(item.get("match_reason"))}
                      for item in snapshot.get("items", [])],
            "limitations": list(snapshot.get("limitations", []))}


def model_memory(snapshot):
    """Exclude evaluator group identifiers and internal actor fields from egress."""
    result = memory_trace(snapshot)
    for output, item in zip(result["items"], snapshot.get("items", [])):
        output["facts"] = [{key: copy.deepcopy(fact.get(key)) for key in ("fact_id", "modality", "service_id", "record", "unit")}
                           for fact in item.get("facts", [])]
        if isinstance(output.get("review"), dict):
            output["review"] = {key: output["review"].get(key) for key in ("actor_type", "at", "basis")}
    return result


def enrich_from_memory(memory, store, actor, context, question):
    prior_question = None
    if hasattr(store, '_db'):
        with store._db() as db:
            previous = db.execute("SELECT question,answer_json FROM conversation_turns WHERE context_run_id=? AND status='COMPLETED' ORDER BY sequence DESC LIMIT 12", (context['context_run_id'],)).fetchall()
        if previous and json.loads(previous[0]['answer_json']).get('memory_citations'):
            # Carry the explicit historical topic through bounded generic
            # follow-ups, rather than replacing it with 'tell me more'.
            prior_question = next((row['question'] for row in previous if historical_question(row['question'])), previous[0]['question'])
    refuse_history = bool(re.search(r'do not use (?:history|historical)|without (?:history|memory)|不要(?:使用|参考)?历史|不使用历史', question, re.I))
    if refuse_history or (not historical_question(question) and not prior_question):
        return context
    incident = store.get_incident(context["incident_id"])
    artifacts = set()
    for eid in context["evidence_ids"]:
        evidence = store.get_evidence(actor, context["context_run_id"], eid)
        artifacts.add(evidence["source_artifact_digest"])
    # Repetition labels are unavailable at deployment time. Do not look up the
    # evaluator's answer/group mapping to steer the live retrieval.
    query = question if historical_question(question) else (question + ' ' + prior_question)[-4000:]
    snapshot = memory.retrieve(actor, query,
        {"system_id": incident["system_id"], "service_ids": incident["service_ids"],
         "exclude_artifact_digests": sorted(artifacts)},
        exclude_incident_id=context["incident_id"], exclude_run_id=context["context_run_id"])
    snapshot.setdefault("limitations", []).append("Historical observations do not establish the current cause. Deployment-version compatibility is unknown; live retrieval does not use evaluator repetition groups.")
    context["memory_retrieval"] = snapshot
    return context

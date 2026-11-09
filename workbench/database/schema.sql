-- AutoTriager planned SQLite persistence contract. Author: Dahe Chen.
-- Executable DDL and constraints; this file does not implement an API/worker.
PRAGMA foreign_keys = ON;

CREATE TABLE schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE local_sessions (
  session_digest TEXT PRIMARY KEY CHECK(length(session_digest) = 64),
  actor_scope TEXT NOT NULL,
  csrf_nonce_digest TEXT NOT NULL CHECK(length(csrf_nonce_digest) = 64),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT
);
-- Raw cookie, CSRF token, model API keys, bearer tokens and gold labels are absent.

CREATE TABLE incidents (
  incident_id TEXT PRIMARY KEY,
  system_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  mode TEXT NOT NULL CHECK(mode IN ('fixture','offline_replay')),
  data_revision TEXT NOT NULL,
  manifest_digest TEXT NOT NULL CHECK(length(manifest_digest) = 64),
  window_start TEXT NOT NULL,
  window_end TEXT NOT NULL CHECK(window_end >= window_start),
  manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json))
);
CREATE TABLE incident_services (
  incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
  service_id TEXT NOT NULL,
  PRIMARY KEY(incident_id, service_id)
);
CREATE TABLE investigations (
  investigation_id TEXT PRIMARY KEY,
  incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
  actor_scope TEXT NOT NULL,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(investigation_id, incident_id)
);

CREATE TABLE inference_grants (
  grant_id TEXT PRIMARY KEY,
  actor_scope TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('DRAFT','ACTIVE','REVOKED','EXPIRED')),
  permitted_mode TEXT NOT NULL CHECK(permitted_mode='offline_replay'),
  permitted_data_revision TEXT NOT NULL,
  permitted_manifest_digest TEXT NOT NULL CHECK(length(permitted_manifest_digest)=64),
  permitted_model_key TEXT NOT NULL,
  resolved_model_id TEXT NOT NULL,
  decision_reference TEXT NOT NULL,
  grant_digest TEXT NOT NULL CHECK(length(grant_digest)=64),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  max_provider_calls_total INTEGER NOT NULL CHECK(max_provider_calls_total >= 0),
  used_provider_calls INTEGER NOT NULL DEFAULT 0 CHECK(used_provider_calls >= 0),
  reserved_provider_calls INTEGER NOT NULL DEFAULT 0 CHECK(reserved_provider_calls >= 0),
  max_total_provider_tokens INTEGER NOT NULL CHECK(max_total_provider_tokens >= 0),
  used_provider_tokens INTEGER NOT NULL DEFAULT 0 CHECK(used_provider_tokens >= 0),
  reserved_provider_tokens INTEGER NOT NULL DEFAULT 0 CHECK(reserved_provider_tokens >= 0),
  max_paid_cost_usd REAL NOT NULL DEFAULT 0 CHECK(max_paid_cost_usd=0),
  paid_fallback_allowed INTEGER NOT NULL DEFAULT 0 CHECK(paid_fallback_allowed=0),
  CHECK(used_provider_calls + reserved_provider_calls <= max_provider_calls_total),
  CHECK(used_provider_tokens + reserved_provider_tokens <= max_total_provider_tokens)
);
CREATE TRIGGER grant_scope_ceiling_immutable BEFORE UPDATE ON inference_grants
WHEN NEW.grant_id != OLD.grant_id OR NEW.actor_scope != OLD.actor_scope
 OR NEW.permitted_mode != OLD.permitted_mode OR NEW.permitted_data_revision != OLD.permitted_data_revision
 OR NEW.permitted_manifest_digest != OLD.permitted_manifest_digest
 OR NEW.permitted_model_key != OLD.permitted_model_key OR NEW.resolved_model_id != OLD.resolved_model_id
 OR NEW.max_provider_calls_total != OLD.max_provider_calls_total
 OR NEW.max_total_provider_tokens != OLD.max_total_provider_tokens
 OR NEW.expires_at != OLD.expires_at OR NEW.decision_reference != OLD.decision_reference
 OR NEW.grant_digest != OLD.grant_digest OR NEW.created_at != OLD.created_at
BEGIN SELECT RAISE(ABORT, 'immutable_grant_scope_and_ceiling'); END;

CREATE TABLE run_configs (
  config_digest TEXT PRIMARY KEY CHECK(length(config_digest) = 64),
  run_id TEXT NOT NULL UNIQUE,
  incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
  mode TEXT NOT NULL CHECK(mode IN ('fixture','offline_replay')),
  policy_digest TEXT NOT NULL CHECK(length(policy_digest) = 64),
  engine_source_commit TEXT NOT NULL CHECK(length(engine_source_commit) = 40),
  model_key TEXT,
  resolved_model_id TEXT,
  inference_authorized INTEGER NOT NULL CHECK(inference_authorized IN (0,1)),
  grant_id TEXT REFERENCES inference_grants(grant_id),
  grant_expires_at TEXT,
  config_json TEXT NOT NULL CHECK(json_valid(config_json)),
  CHECK((inference_authorized = 0) OR
        (mode = 'offline_replay' AND model_key IS NOT NULL AND
         resolved_model_id IS NOT NULL AND grant_id IS NOT NULL AND grant_expires_at IS NOT NULL))
);
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  investigation_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  parent_run_id TEXT,
  question TEXT NOT NULL CHECK(length(question) BETWEEN 1 AND 8000),
  config_digest TEXT NOT NULL UNIQUE REFERENCES run_configs(config_digest),
  state TEXT NOT NULL CHECK(state IN
    ('CREATED','QUEUED','RUNNING','VALIDATING','COMPLETED','FAILED','TIMED_OUT',
     'BUDGET_EXHAUSTED','CANCEL_REQUESTED','CANCELLED')),
  revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
  run_generation INTEGER NOT NULL DEFAULT 1 CHECK(run_generation >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  termination_code TEXT,
  UNIQUE(run_id, incident_id),
  FOREIGN KEY(investigation_id, incident_id)
    REFERENCES investigations(investigation_id, incident_id),
  FOREIGN KEY(parent_run_id, incident_id) REFERENCES runs(run_id, incident_id)
);
CREATE TRIGGER runs_initial_state BEFORE INSERT ON runs
WHEN NEW.state != 'CREATED' OR NEW.revision != 0
BEGIN SELECT RAISE(ABORT, 'initial_run_state_must_be_created'); END;
CREATE TRIGGER runs_config_scope BEFORE INSERT ON runs
WHEN NOT EXISTS(SELECT 1 FROM run_configs c WHERE c.config_digest=NEW.config_digest
  AND c.run_id=NEW.run_id AND c.incident_id=NEW.incident_id)
BEGIN SELECT RAISE(ABORT, 'run_config_scope_mismatch'); END;
CREATE TRIGGER runs_parent_scope BEFORE INSERT ON runs
WHEN NEW.parent_run_id IS NOT NULL AND NOT EXISTS(
  SELECT 1 FROM runs p WHERE p.run_id=NEW.parent_run_id
  AND p.investigation_id=NEW.investigation_id AND p.incident_id=NEW.incident_id
  AND p.state IN ('COMPLETED','FAILED','TIMED_OUT','BUDGET_EXHAUSTED','CANCELLED'))
BEGIN SELECT RAISE(ABORT, 'parent_must_be_terminal_in_same_investigation'); END;
CREATE TRIGGER runs_identity_immutable BEFORE UPDATE ON runs
WHEN NEW.run_id != OLD.run_id OR NEW.investigation_id != OLD.investigation_id
 OR NEW.incident_id != OLD.incident_id OR NEW.parent_run_id IS NOT OLD.parent_run_id
 OR NEW.question != OLD.question OR NEW.config_digest != OLD.config_digest
 OR NEW.created_at != OLD.created_at OR NEW.run_generation != OLD.run_generation
BEGIN SELECT RAISE(ABORT, 'immutable_run_identity'); END;
CREATE TRIGGER runs_revision_cas BEFORE UPDATE ON runs
WHEN NEW.revision != OLD.revision + 1
BEGIN SELECT RAISE(ABORT, 'revision_must_increment_once'); END;
CREATE TRIGGER runs_terminal_immutable BEFORE UPDATE ON runs
WHEN OLD.state IN ('COMPLETED','FAILED','TIMED_OUT','BUDGET_EXHAUSTED','CANCELLED')
BEGIN SELECT RAISE(ABORT, 'terminal_run_is_immutable'); END;
CREATE TRIGGER runs_state_transition BEFORE UPDATE OF state ON runs
WHEN NEW.state != OLD.state AND NOT (
 (OLD.state='CREATED' AND NEW.state IN ('QUEUED','FAILED','CANCEL_REQUESTED')) OR
 (OLD.state='QUEUED' AND NEW.state IN ('RUNNING','FAILED','TIMED_OUT','BUDGET_EXHAUSTED','CANCEL_REQUESTED')) OR
 (OLD.state='RUNNING' AND NEW.state IN ('VALIDATING','FAILED','TIMED_OUT','BUDGET_EXHAUSTED','CANCEL_REQUESTED')) OR
 (OLD.state='VALIDATING' AND NEW.state IN ('COMPLETED','FAILED','TIMED_OUT','BUDGET_EXHAUSTED','CANCEL_REQUESTED')) OR
 (OLD.state='CANCEL_REQUESTED' AND NEW.state='CANCELLED'))
BEGIN SELECT RAISE(ABORT, 'illegal_state_transition'); END;
CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs
BEGIN SELECT RAISE(ABORT, 'run_deletion_requires_future_retention_migration'); END;
CREATE TRIGGER configs_no_update BEFORE UPDATE ON run_configs
BEGIN SELECT RAISE(ABORT, 'immutable_config'); END;
CREATE TRIGGER configs_no_delete BEFORE DELETE ON run_configs
BEGIN SELECT RAISE(ABORT, 'immutable_config'); END;

CREATE TABLE run_budgets (
  run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
  max_model_calls INTEGER NOT NULL CHECK(max_model_calls >= 0),
  used_model_calls INTEGER NOT NULL DEFAULT 0 CHECK(used_model_calls >= 0),
  reserved_model_calls INTEGER NOT NULL DEFAULT 0 CHECK(reserved_model_calls >= 0),
  max_tool_calls INTEGER NOT NULL CHECK(max_tool_calls >= 0),
  used_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK(used_tool_calls >= 0),
  reserved_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK(reserved_tool_calls >= 0),
  max_source_bytes INTEGER NOT NULL CHECK(max_source_bytes >= 0),
  used_source_bytes INTEGER NOT NULL DEFAULT 0 CHECK(used_source_bytes >= 0),
  reserved_source_bytes INTEGER NOT NULL DEFAULT 0 CHECK(reserved_source_bytes >= 0),
  max_total_rows INTEGER NOT NULL CHECK(max_total_rows >= 0),
  used_rows INTEGER NOT NULL DEFAULT 0 CHECK(used_rows >= 0),
  reserved_rows INTEGER NOT NULL DEFAULT 0 CHECK(reserved_rows >= 0),
  max_total_input_tokens INTEGER NOT NULL CHECK(max_total_input_tokens >= 0),
  used_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(used_input_tokens >= 0),
  reserved_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(reserved_input_tokens >= 0),
  max_total_output_tokens INTEGER NOT NULL CHECK(max_total_output_tokens >= 0),
  used_output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(used_output_tokens >= 0),
  reserved_output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(reserved_output_tokens >= 0),
  max_output_tokens INTEGER NOT NULL CHECK(max_output_tokens >= 0),
  max_wall_time_seconds INTEGER NOT NULL CHECK(max_wall_time_seconds >= 0),
  deadline_at TEXT NOT NULL,
  paid_fallback_allowed INTEGER NOT NULL DEFAULT 0 CHECK(paid_fallback_allowed=0),
  CHECK(used_model_calls + reserved_model_calls <= max_model_calls),
  CHECK(used_tool_calls + reserved_tool_calls <= max_tool_calls),
  CHECK(used_source_bytes + reserved_source_bytes <= max_source_bytes),
  CHECK(used_rows + reserved_rows <= max_total_rows),
  CHECK(used_input_tokens + reserved_input_tokens <= max_total_input_tokens),
  CHECK(used_output_tokens + reserved_output_tokens <= max_total_output_tokens)
);
CREATE TABLE gateway_capabilities (
  capability_id TEXT PRIMARY KEY,
  token_digest TEXT NOT NULL UNIQUE CHECK(length(token_digest)=64),
  run_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  run_generation INTEGER NOT NULL CHECK(run_generation >= 1),
  tool_allowlist_json TEXT NOT NULL CHECK(json_valid(tool_allowlist_json)),
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  FOREIGN KEY(run_id, incident_id) REFERENCES runs(run_id, incident_id)
);
CREATE TRIGGER budget_ceiling_immutable BEFORE UPDATE ON run_budgets
WHEN NEW.max_model_calls != OLD.max_model_calls OR NEW.max_tool_calls != OLD.max_tool_calls
 OR NEW.max_source_bytes != OLD.max_source_bytes OR NEW.max_total_rows != OLD.max_total_rows
 OR NEW.max_total_input_tokens != OLD.max_total_input_tokens
 OR NEW.max_total_output_tokens != OLD.max_total_output_tokens
 OR NEW.max_output_tokens != OLD.max_output_tokens
 OR NEW.max_wall_time_seconds != OLD.max_wall_time_seconds OR NEW.deadline_at != OLD.deadline_at
 OR NEW.run_id != OLD.run_id
BEGIN SELECT RAISE(ABORT, 'immutable_budget_ceiling'); END;

CREATE TABLE dispatch_reservations (
  dispatch_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  kind TEXT NOT NULL CHECK(kind IN ('engine_request','provider_request')),
  parent_dispatch_id TEXT REFERENCES dispatch_reservations(dispatch_id),
  grant_id TEXT REFERENCES inference_grants(grant_id),
  slot_name TEXT NOT NULL CHECK(slot_name IN ('engine_investigation','model_provider')),
  state TEXT NOT NULL CHECK(state IN
    ('RESERVED','DISPATCHED','UNRESOLVED','COMPLETION_OBSERVED')),
  calls_reserved INTEGER NOT NULL CHECK(calls_reserved IN (0,1)),
  reserved_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(reserved_input_tokens >= 0),
  reserved_output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(reserved_output_tokens >= 0),
  created_at TEXT NOT NULL,
  dispatched_at TEXT,
  completed_at TEXT,
  provider_request_id TEXT,
  budget_digest TEXT NOT NULL CHECK(length(budget_digest)=64),
  CHECK((kind='engine_request' AND slot_name='engine_investigation' AND
         calls_reserved=0 AND parent_dispatch_id IS NULL AND grant_id IS NULL) OR
        (kind='provider_request' AND slot_name='model_provider' AND
         calls_reserved=1 AND parent_dispatch_id IS NOT NULL AND grant_id IS NOT NULL)),
  CHECK(state != 'COMPLETION_OBSERVED' OR completed_at IS NOT NULL)
);
CREATE UNIQUE INDEX one_active_dispatch_per_slot ON dispatch_reservations(slot_name)
WHERE state IN ('RESERVED','DISPATCHED','UNRESOLVED');
CREATE TRIGGER dispatch_parent_scope BEFORE INSERT ON dispatch_reservations
WHEN NEW.kind='provider_request' AND NOT EXISTS(
 SELECT 1 FROM dispatch_reservations p WHERE p.dispatch_id=NEW.parent_dispatch_id
 AND p.run_id=NEW.run_id AND p.kind='engine_request')
BEGIN SELECT RAISE(ABORT, 'provider_parent_scope_mismatch'); END;
CREATE TRIGGER dispatch_grant_scope BEFORE INSERT ON dispatch_reservations
WHEN NEW.kind='provider_request' AND NOT EXISTS(
 SELECT 1 FROM inference_grants g JOIN run_configs c ON c.grant_id=g.grant_id
 JOIN runs r ON r.run_id=c.run_id JOIN investigations i ON i.investigation_id=r.investigation_id
 JOIN incidents inc ON inc.incident_id=r.incident_id
 WHERE g.grant_id=NEW.grant_id AND c.run_id=NEW.run_id AND c.inference_authorized=1
 AND g.status='ACTIVE' AND NEW.created_at < g.expires_at
 AND g.actor_scope=i.actor_scope AND c.mode=g.permitted_mode
 AND inc.manifest_digest=g.permitted_manifest_digest AND inc.data_revision=g.permitted_data_revision
 AND c.model_key=g.permitted_model_key AND c.resolved_model_id=g.resolved_model_id)
BEGIN SELECT RAISE(ABORT, 'provider_grant_scope_or_authorization_denied'); END;
CREATE TRIGGER dispatch_terminal_immutable BEFORE UPDATE ON dispatch_reservations
WHEN OLD.state='COMPLETION_OBSERVED'
BEGIN SELECT RAISE(ABORT, 'dispatch_completion_is_immutable'); END;
CREATE TRIGGER dispatch_state_transition BEFORE UPDATE ON dispatch_reservations
WHEN NEW.dispatch_id != OLD.dispatch_id OR NEW.run_id != OLD.run_id OR
 NEW.kind != OLD.kind OR NEW.slot_name != OLD.slot_name OR
 NEW.parent_dispatch_id IS NOT OLD.parent_dispatch_id OR NEW.grant_id IS NOT OLD.grant_id OR NEW.calls_reserved != OLD.calls_reserved OR
 NEW.reserved_input_tokens != OLD.reserved_input_tokens OR NEW.reserved_output_tokens != OLD.reserved_output_tokens OR
 NEW.created_at != OLD.created_at OR NEW.budget_digest != OLD.budget_digest OR
 NOT ((OLD.state='RESERVED' AND NEW.state IN ('DISPATCHED','UNRESOLVED','COMPLETION_OBSERVED')) OR
      (OLD.state='DISPATCHED' AND NEW.state IN ('UNRESOLVED','COMPLETION_OBSERVED')) OR
      (OLD.state='UNRESOLVED' AND NEW.state='COMPLETION_OBSERVED'))
BEGIN SELECT RAISE(ABORT, 'illegal_dispatch_mutation'); END;

CREATE TABLE queries (
  query_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  tool_name TEXT NOT NULL CHECK(tool_name IN
    ('incident_manifest','incident_services','incident_metrics','incident_logs','incident_traces')),
  modality TEXT CHECK(modality IN ('metrics','logs','traces')),
  window_start TEXT NOT NULL,
  window_end TEXT NOT NULL CHECK(window_end >= window_start),
  max_records INTEGER NOT NULL CHECK(max_records BETWEEN 1 AND 2000),
  max_bytes INTEGER NOT NULL CHECK(max_bytes BETWEEN 1 AND 1048576),
  parameters_json TEXT NOT NULL CHECK(json_valid(parameters_json)),
  UNIQUE(query_id,run_id,incident_id),
  FOREIGN KEY(run_id,incident_id) REFERENCES runs(run_id,incident_id)
);
CREATE TABLE query_results (
  query_id TEXT PRIMARY KEY REFERENCES queries(query_id),
  status TEXT NOT NULL CHECK(status IN
    ('success','modality_absent','no_match','truncated','error','scope_denied','budget_exhausted','cancelled')),
  availability TEXT NOT NULL CHECK(availability IN ('present','absent','unknown')),
  returned_count INTEGER NOT NULL CHECK(returned_count >= 0),
  matched_count INTEGER CHECK(matched_count >= 0),
  truncated INTEGER NOT NULL CHECK(truncated IN (0,1)),
  result_complete INTEGER NOT NULL CHECK(result_complete IN (0,1)),
  result_json TEXT NOT NULL CHECK(json_valid(result_json)),
  CHECK(status != 'modality_absent' OR
    (availability='absent' AND returned_count=0 AND truncated=0 AND result_complete=0)),
  CHECK(status != 'no_match' OR
    (availability='present' AND returned_count=0 AND truncated=0 AND result_complete=1)),
  CHECK(status != 'truncated' OR (truncated=1 AND result_complete=0)),
  CHECK(status != 'error' OR result_complete=0),
  CHECK(status != 'success' OR (availability='present' AND truncated=0 AND result_complete=1))
);
CREATE TRIGGER query_requires_running BEFORE INSERT ON queries
WHEN (SELECT state FROM runs WHERE run_id=NEW.run_id) != 'RUNNING'
BEGIN SELECT RAISE(ABORT, 'query_requires_running_run'); END;
CREATE TRIGGER query_no_update BEFORE UPDATE ON queries
BEGIN SELECT RAISE(ABORT, 'immutable_query'); END;
CREATE TRIGGER query_result_no_update BEFORE UPDATE ON query_results
BEGIN SELECT RAISE(ABORT, 'immutable_query_result'); END;
CREATE TABLE evidence (
  evidence_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  query_id TEXT NOT NULL,
  service_id TEXT,
  modality TEXT NOT NULL CHECK(modality IN ('metrics','logs','traces')),
  kind TEXT NOT NULL CHECK(kind IN ('raw','derived')),
  source_artifact_digest TEXT NOT NULL CHECK(length(source_artifact_digest)=64),
  record_locator TEXT NOT NULL CHECK(record_locator NOT LIKE '%..%'),
  content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
  observed_start TEXT NOT NULL,
  observed_end TEXT NOT NULL CHECK(observed_end >= observed_start),
  preserved_at TEXT NOT NULL,
  unit TEXT NOT NULL,
  record_json TEXT NOT NULL CHECK(json_valid(record_json)),
  derivation_json TEXT CHECK(derivation_json IS NULL OR json_valid(derivation_json)),
  limitations_json TEXT NOT NULL CHECK(json_valid(limitations_json)),
  UNIQUE(evidence_id,run_id,incident_id),
  CHECK((kind='raw' AND derivation_json IS NULL) OR
        (kind='derived' AND derivation_json IS NOT NULL)),
  FOREIGN KEY(run_id,incident_id) REFERENCES runs(run_id,incident_id),
  FOREIGN KEY(query_id,run_id,incident_id) REFERENCES queries(query_id,run_id,incident_id),
  FOREIGN KEY(incident_id,service_id) REFERENCES incident_services(incident_id,service_id)
);
CREATE TABLE evidence_parents (
  evidence_id TEXT NOT NULL,
  parent_evidence_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  PRIMARY KEY(evidence_id,parent_evidence_id),
  CHECK(evidence_id != parent_evidence_id),
  FOREIGN KEY(evidence_id,run_id,incident_id) REFERENCES evidence(evidence_id,run_id,incident_id),
  FOREIGN KEY(parent_evidence_id,run_id,incident_id) REFERENCES evidence(evidence_id,run_id,incident_id)
);
CREATE TRIGGER evidence_parent_derived BEFORE INSERT ON evidence_parents
WHEN (SELECT kind FROM evidence WHERE evidence_id=NEW.evidence_id) != 'derived'
BEGIN SELECT RAISE(ABORT, 'raw_evidence_cannot_have_parents'); END;
CREATE TRIGGER evidence_parent_cycle BEFORE INSERT ON evidence_parents
WHEN EXISTS(
 WITH RECURSIVE ancestors(id) AS (
   SELECT NEW.parent_evidence_id UNION
   SELECT p.parent_evidence_id FROM evidence_parents p JOIN ancestors a ON p.evidence_id=a.id)
 SELECT 1 FROM ancestors WHERE id=NEW.evidence_id)
BEGIN SELECT RAISE(ABORT, 'evidence_derivation_cycle'); END;
CREATE TRIGGER evidence_parent_running BEFORE INSERT ON evidence_parents
WHEN (SELECT state FROM runs WHERE run_id=NEW.run_id) != 'RUNNING'
BEGIN SELECT RAISE(ABORT, 'lineage_requires_running_run'); END;
CREATE TRIGGER evidence_parent_no_update BEFORE UPDATE ON evidence_parents
BEGIN SELECT RAISE(ABORT, 'immutable_lineage'); END;
CREATE TRIGGER evidence_parent_no_delete BEFORE DELETE ON evidence_parents
BEGIN SELECT RAISE(ABORT, 'immutable_lineage'); END;
CREATE TRIGGER evidence_no_update BEFORE UPDATE ON evidence
BEGIN SELECT RAISE(ABORT, 'immutable_evidence'); END;
CREATE TRIGGER evidence_running_fence BEFORE INSERT ON evidence
WHEN (SELECT state FROM runs WHERE run_id=NEW.run_id) != 'RUNNING'
BEGIN SELECT RAISE(ABORT, 'evidence_requires_running_run'); END;
CREATE TRIGGER evidence_no_delete BEFORE DELETE ON evidence
BEGIN SELECT RAISE(ABORT, 'immutable_evidence'); END;

CREATE TABLE answers (
  answer_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL UNIQUE,
  incident_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ranked_candidates','insufficient_evidence')),
  raw_output_digest TEXT NOT NULL CHECK(length(raw_output_digest)=64),
  structure_valid INTEGER NOT NULL CHECK(structure_valid=1),
  reference_valid INTEGER NOT NULL CHECK(reference_valid=1),
  semantic_support_status TEXT NOT NULL DEFAULT 'not_assessed' CHECK(semantic_support_status='not_assessed'),
  answer_json TEXT NOT NULL CHECK(json_valid(answer_json)),
  created_at TEXT NOT NULL,
  UNIQUE(answer_id,run_id,incident_id),
  FOREIGN KEY(run_id,incident_id) REFERENCES runs(run_id,incident_id)
);
CREATE TABLE answer_candidates (
  answer_id TEXT NOT NULL,
  rank INTEGER NOT NULL CHECK(rank >= 1),
  run_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  service_id TEXT NOT NULL,
  hypothesis TEXT NOT NULL,
  PRIMARY KEY(answer_id,rank),
  UNIQUE(answer_id,service_id),
  FOREIGN KEY(answer_id,run_id,incident_id) REFERENCES answers(answer_id,run_id,incident_id),
  FOREIGN KEY(incident_id,service_id) REFERENCES incident_services(incident_id,service_id)
);
CREATE TABLE answer_evidence_links (
  answer_id TEXT NOT NULL,
  candidate_rank INTEGER NOT NULL,
  evidence_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  relation TEXT NOT NULL CHECK(relation IN ('support','contradict')),
  PRIMARY KEY(answer_id,candidate_rank,evidence_id,relation),
  FOREIGN KEY(answer_id,candidate_rank) REFERENCES answer_candidates(answer_id,rank),
  FOREIGN KEY(answer_id,run_id,incident_id) REFERENCES answers(answer_id,run_id,incident_id),
  FOREIGN KEY(evidence_id,run_id,incident_id) REFERENCES evidence(evidence_id,run_id,incident_id)
);
CREATE TRIGGER candidates_validating BEFORE INSERT ON answer_candidates
WHEN (SELECT state FROM runs WHERE run_id=NEW.run_id) != 'VALIDATING'
BEGIN SELECT RAISE(ABORT, 'candidate_requires_validating_run'); END;
CREATE TRIGGER candidates_no_update BEFORE UPDATE ON answer_candidates
BEGIN SELECT RAISE(ABORT, 'immutable_candidate'); END;
CREATE TRIGGER candidates_no_delete BEFORE DELETE ON answer_candidates
BEGIN SELECT RAISE(ABORT, 'immutable_candidate'); END;
CREATE TRIGGER answer_links_validating BEFORE INSERT ON answer_evidence_links
WHEN (SELECT state FROM runs WHERE run_id=NEW.run_id) != 'VALIDATING'
BEGIN SELECT RAISE(ABORT, 'answer_link_requires_validating_run'); END;
CREATE TRIGGER answer_links_no_update BEFORE UPDATE ON answer_evidence_links
BEGIN SELECT RAISE(ABORT, 'immutable_answer_link'); END;
CREATE TRIGGER answer_links_no_delete BEFORE DELETE ON answer_evidence_links
BEGIN SELECT RAISE(ABORT, 'immutable_answer_link'); END;
CREATE TRIGGER answers_run_validating BEFORE INSERT ON answers
WHEN (SELECT state FROM runs WHERE run_id=NEW.run_id) != 'VALIDATING'
BEGIN SELECT RAISE(ABORT, 'answer_requires_validating_state'); END;
CREATE TRIGGER answers_no_update BEFORE UPDATE ON answers
BEGIN SELECT RAISE(ABORT, 'immutable_answer'); END;
CREATE TRIGGER answers_no_delete BEFORE DELETE ON answers
BEGIN SELECT RAISE(ABORT, 'immutable_answer'); END;
CREATE TRIGGER completion_requires_answer BEFORE UPDATE OF state ON runs
WHEN NEW.state='COMPLETED' AND NOT EXISTS(SELECT 1 FROM answers WHERE run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT, 'completion_requires_valid_answer'); END;

CREATE TABLE events (
  event_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  sequence INTEGER NOT NULL CHECK(sequence >= 1),
  schema_version TEXT NOT NULL CHECK(schema_version='autotriager.api.v1'),
  received_at TEXT NOT NULL,
  upstream_observed_at TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN
    ('run.state_changed','tool.started','tool.finished','evidence.added','answer.received',
     'validation.finished','usage.observed','warning','late_observation')),
  upstream_call_id TEXT,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  PRIMARY KEY(run_id,sequence)
);
CREATE TRIGGER events_contiguous BEFORE INSERT ON events
WHEN NEW.sequence != COALESCE((SELECT MAX(sequence)+1 FROM events WHERE run_id=NEW.run_id),1)
BEGIN SELECT RAISE(ABORT, 'event_sequence_must_be_contiguous'); END;
CREATE TRIGGER events_terminal_fence BEFORE INSERT ON events
WHEN (SELECT state FROM runs WHERE run_id=NEW.run_id) IN
 ('COMPLETED','FAILED','TIMED_OUT','BUDGET_EXHAUSTED','CANCELLED') AND
 NEW.event_type NOT IN ('late_observation','warning','usage.observed')
BEGIN SELECT RAISE(ABORT, 'terminal_event_fence'); END;
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'append_only_event'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'append_only_event'); END;

CREATE TABLE reviews (
  review_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  answer_id TEXT,
  actor_scope TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK(verdict IN ('supported','needs_revision','cannot_judge')),
  notes TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(answer_id,run_id) REFERENCES answers(answer_id,run_id)
);
-- A composite parent-key table is unnecessary; use a matching UNIQUE index below.
CREATE UNIQUE INDEX answers_review_scope ON answers(answer_id,run_id);

CREATE TABLE idempotency (
  actor_scope TEXT NOT NULL,
  route TEXT NOT NULL,
  key TEXT NOT NULL,
  request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
  resource_type TEXT NOT NULL CHECK(resource_type IN ('investigation','run')),
  resource_id TEXT NOT NULL,
  response_status INTEGER NOT NULL CHECK(response_status IN (201,202)),
  response_json TEXT NOT NULL CHECK(json_valid(response_json)),
  created_at TEXT NOT NULL,
  PRIMARY KEY(actor_scope,route,key)
);
CREATE TRIGGER idempotency_resource BEFORE INSERT ON idempotency
WHEN (NEW.resource_type='investigation' AND NOT EXISTS(
 SELECT 1 FROM investigations WHERE investigation_id=NEW.resource_id AND actor_scope=NEW.actor_scope))
 OR (NEW.resource_type='run' AND NOT EXISTS(
 SELECT 1 FROM runs r JOIN investigations i ON i.investigation_id=r.investigation_id
 WHERE r.run_id=NEW.resource_id AND i.actor_scope=NEW.actor_scope))
BEGIN SELECT RAISE(ABORT, 'idempotency_resource_scope_mismatch'); END;
CREATE TRIGGER idempotency_no_update BEFORE UPDATE ON idempotency
BEGIN SELECT RAISE(ABORT, 'immutable_idempotency_record'); END;
CREATE TRIGGER idempotency_no_delete BEFORE DELETE ON idempotency
BEGIN SELECT RAISE(ABORT, 'idempotency_retained_for_mvp'); END;
CREATE TRIGGER reviews_no_update BEFORE UPDATE ON reviews
BEGIN SELECT RAISE(ABORT, 'append_only_review'); END;
CREATE TRIGGER reviews_no_delete BEFORE DELETE ON reviews
BEGIN SELECT RAISE(ABORT, 'append_only_review'); END;

CREATE VIEW run_journal_positions AS
 SELECT r.run_id, COALESCE(MAX(e.sequence),0) AS last_sequence
 FROM runs r LEFT JOIN events e ON e.run_id=r.run_id GROUP BY r.run_id;
CREATE INDEX events_run_order ON events(run_id,sequence);
CREATE INDEX evidence_query_scope ON evidence(run_id,query_id);
CREATE INDEX runs_investigation_order ON runs(investigation_id,created_at,run_id);
CREATE INDEX investigations_actor_order ON investigations(actor_scope,created_at,investigation_id);

-- SQL guards are a subset of the protocol: schema validates payloads; worker uses
-- BEGIN IMMEDIATE + revision CAS + journal/answer/ledger atomic commit. No worker
-- exists here. Gold evaluation data belongs in a separate file/process, not a table.

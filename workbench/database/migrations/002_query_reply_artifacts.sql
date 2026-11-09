-- Additive local upgrade. Original runs, evidence and source digests are retained.
CREATE TABLE IF NOT EXISTS gateway_capabilities (
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
CREATE TABLE IF NOT EXISTS query_reply_artifacts (
  query_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  artifact_digest TEXT NOT NULL CHECK(length(artifact_digest)=64),
  byte_length INTEGER NOT NULL CHECK(byte_length>=0),
  created_at TEXT NOT NULL,
  FOREIGN KEY(query_id,run_id,incident_id) REFERENCES queries(query_id,run_id,incident_id)
);
CREATE TRIGGER IF NOT EXISTS query_reply_artifact_no_update BEFORE UPDATE ON query_reply_artifacts
BEGIN SELECT RAISE(ABORT,'immutable_query_reply_artifact'); END;
CREATE TRIGGER IF NOT EXISTS query_reply_artifact_no_delete BEFORE DELETE ON query_reply_artifacts
BEGIN SELECT RAISE(ABORT,'immutable_query_reply_artifact'); END;

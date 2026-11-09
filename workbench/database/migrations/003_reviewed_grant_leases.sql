-- One reviewed R1 technical lease; original grant identity/expiry stay immutable.
CREATE TABLE IF NOT EXISTS reviewed_grant_leases (
  grant_id TEXT PRIMARY KEY REFERENCES inference_grants(grant_id),
  parent_run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
  incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
  manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64),
  resolved_model_id TEXT NOT NULL,
  original_expires_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  review_reference TEXT NOT NULL,
  reason TEXT NOT NULL CHECK(reason='R1 bounded repair of rejected model query arguments'),
  provider_snapshot_digest TEXT NOT NULL CHECK(length(provider_snapshot_digest)=64),
  receipt_digest TEXT NOT NULL CHECK(length(receipt_digest)=64),
  grant_digest TEXT NOT NULL CHECK(length(grant_digest)=64),
  prior_used_provider_calls INTEGER NOT NULL CHECK(prior_used_provider_calls=2),
  prior_reserved_provider_calls INTEGER NOT NULL CHECK(prior_reserved_provider_calls=0),
  prior_used_provider_tokens INTEGER NOT NULL CHECK(prior_used_provider_tokens>0),
  prior_reserved_provider_tokens INTEGER NOT NULL CHECK(prior_reserved_provider_tokens>=0),
  lease_digest TEXT NOT NULL UNIQUE CHECK(length(lease_digest)=64),
  CHECK(length(created_at)=27 AND substr(created_at,20,1)='.' AND substr(created_at,27,1)='Z'),
  CHECK(length(expires_at)=27 AND substr(expires_at,20,1)='.' AND substr(expires_at,27,1)='Z'),
  CHECK(strftime('%Y-%m-%dT%H:%M:%S',substr(created_at,1,19)||'Z','+1 hour') IS NOT NULL),
  CHECK(expires_at>original_expires_at),
  CHECK(expires_at>created_at),
  -- Avoid julianday floating-point rounding at an exact +1h microsecond bound.
  CHECK(expires_at<=strftime('%Y-%m-%dT%H:%M:%S',substr(created_at,1,19)||'Z','+1 hour')||substr(created_at,20))
);
CREATE TRIGGER IF NOT EXISTS reviewed_lease_scope BEFORE INSERT ON reviewed_grant_leases
WHEN NOT EXISTS(
  SELECT 1 FROM inference_grants g JOIN run_configs c ON c.grant_id=g.grant_id
  JOIN runs r ON r.run_id=c.run_id JOIN incidents i ON i.incident_id=r.incident_id
  WHERE g.grant_id=NEW.grant_id AND g.status='REVOKED'
  AND r.run_id=NEW.parent_run_id AND r.state='FAILED' AND r.termination_code='QUALIFICATION_STOPPED'
  AND r.incident_id=NEW.incident_id AND i.manifest_digest=NEW.manifest_digest
  AND g.permitted_manifest_digest=NEW.manifest_digest AND g.resolved_model_id=NEW.resolved_model_id
  AND g.expires_at=NEW.original_expires_at AND g.grant_digest=NEW.grant_digest
  AND g.used_provider_calls=NEW.prior_used_provider_calls
  AND g.reserved_provider_calls=NEW.prior_reserved_provider_calls
  AND g.used_provider_tokens=NEW.prior_used_provider_tokens
  AND g.reserved_provider_tokens=NEW.prior_reserved_provider_tokens)
BEGIN SELECT RAISE(ABORT, 'reviewed_lease_scope_or_credit_mismatch'); END;
CREATE TRIGGER IF NOT EXISTS reviewed_lease_no_update BEFORE UPDATE ON reviewed_grant_leases
BEGIN SELECT RAISE(ABORT, 'reviewed_lease_is_immutable'); END;
CREATE TRIGGER IF NOT EXISTS reviewed_lease_no_delete BEFORE DELETE ON reviewed_grant_leases
BEGIN SELECT RAISE(ABORT, 'reviewed_lease_is_immutable'); END;

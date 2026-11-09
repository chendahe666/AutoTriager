-- Final routine owned-template repair under the same ten-call approval.
CREATE TABLE IF NOT EXISTS final_template_repairs (
  grant_id TEXT PRIMARY KEY REFERENCES inference_grants(grant_id),
  parent_run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
  lease_digest TEXT NOT NULL REFERENCES reviewed_grant_leases(lease_digest),
  service503_recovery_digest TEXT NOT NULL REFERENCES service503_recoveries(recovery_digest),
  review_reference TEXT NOT NULL,
  reason TEXT NOT NULL CHECK(reason='Final routine repair of literal schema version and empty metric filter'),
  provider_snapshot_digest TEXT NOT NULL CHECK(length(provider_snapshot_digest)=64),
  receipt_digest TEXT NOT NULL CHECK(length(receipt_digest)=64),
  created_at TEXT NOT NULL,
  prior_used_provider_calls INTEGER NOT NULL CHECK(prior_used_provider_calls=6),
  prior_reserved_provider_calls INTEGER NOT NULL CHECK(prior_reserved_provider_calls=0),
  prior_used_provider_tokens INTEGER NOT NULL CHECK(prior_used_provider_tokens>0),
  prior_reserved_provider_tokens INTEGER NOT NULL CHECK(prior_reserved_provider_tokens>=9024),
  repair_digest TEXT NOT NULL UNIQUE CHECK(length(repair_digest)=64)
);
CREATE TRIGGER IF NOT EXISTS final_template_repair_scope BEFORE INSERT ON final_template_repairs
WHEN NOT EXISTS(
  SELECT 1 FROM inference_grants g JOIN reviewed_grant_leases l ON l.grant_id=g.grant_id
  JOIN service503_recoveries s ON s.grant_id=g.grant_id JOIN run_configs c ON c.grant_id=g.grant_id
  JOIN runs r ON r.run_id=c.run_id
  WHERE g.grant_id=NEW.grant_id AND g.status='REVOKED'
  AND r.run_id=NEW.parent_run_id AND r.state='FAILED' AND r.termination_code='QUALIFICATION_STOPPED'
  AND r.parent_run_id=s.parent_run_id AND c.grant_expires_at=l.expires_at
  AND l.lease_digest=NEW.lease_digest AND s.recovery_digest=NEW.service503_recovery_digest
  AND NEW.created_at<l.expires_at AND NEW.review_reference!=l.review_reference AND NEW.review_reference!=s.review_reference
  AND g.used_provider_calls=NEW.prior_used_provider_calls AND g.reserved_provider_calls=NEW.prior_reserved_provider_calls
  AND g.used_provider_tokens=NEW.prior_used_provider_tokens AND g.reserved_provider_tokens=NEW.prior_reserved_provider_tokens)
BEGIN SELECT RAISE(ABORT, 'final_template_repair_scope_or_credit_mismatch'); END;
CREATE TRIGGER IF NOT EXISTS final_template_repair_no_update BEFORE UPDATE ON final_template_repairs
BEGIN SELECT RAISE(ABORT, 'final_template_repair_is_immutable'); END;
CREATE TRIGGER IF NOT EXISTS final_template_repair_no_delete BEFORE DELETE ON final_template_repairs
BEGIN SELECT RAISE(ABORT, 'final_template_repair_is_immutable'); END;

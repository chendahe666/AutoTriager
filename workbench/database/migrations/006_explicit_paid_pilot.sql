-- One separately authorized fixed-paid development profile. Legacy grant
-- max_paid_cost_usd=0 describes the old Free Tier contract; it is not the
-- authority for this explicitly named paid policy. No fallback is enabled.
CREATE TABLE IF NOT EXISTS explicit_paid_authorizations (
  grant_id TEXT PRIMARY KEY REFERENCES inference_grants(grant_id),
  profile_digest TEXT NOT NULL UNIQUE CHECK(length(profile_digest)=64),
  profile_json TEXT NOT NULL CHECK(json_valid(profile_json)),
  max_cost_nano_usd INTEGER NOT NULL CHECK(max_cost_nano_usd=2000000000),
  input_nano_usd_per_token INTEGER NOT NULL CHECK(input_nano_usd_per_token=300),
  output_nano_usd_per_token INTEGER NOT NULL CHECK(output_nano_usd_per_token=2500),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS explicit_paid_scope BEFORE INSERT ON explicit_paid_authorizations
WHEN NOT EXISTS(SELECT 1 FROM inference_grants g WHERE g.grant_id=NEW.grant_id
  AND g.status='ACTIVE' AND g.resolved_model_id='gemini-3.5-flash-lite'
  AND g.permitted_model_key='autotriager-gemini-paid-pilot'
  AND g.decision_reference='human:2026-11-03:small-scale-end-to-end-after-usd2-pilot-estimate'
  AND g.max_provider_calls_total=10 AND g.max_total_provider_tokens=90240
  AND g.expires_at=NEW.expires_at)
BEGIN SELECT RAISE(ABORT, 'paid_pilot_scope_mismatch'); END;
CREATE TRIGGER IF NOT EXISTS explicit_paid_no_update BEFORE UPDATE ON explicit_paid_authorizations
BEGIN SELECT RAISE(ABORT, 'explicit_paid_authorization_is_immutable'); END;
CREATE TRIGGER IF NOT EXISTS explicit_paid_no_delete BEFORE DELETE ON explicit_paid_authorizations
BEGIN SELECT RAISE(ABORT, 'explicit_paid_authorization_is_immutable'); END;

CREATE TABLE IF NOT EXISTS paid_run_costs (
  run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
  grant_id TEXT NOT NULL REFERENCES explicit_paid_authorizations(grant_id),
  metadata_requests INTEGER NOT NULL CHECK(metadata_requests BETWEEN 0 AND 20),
  money_reserved_nano_usd INTEGER NOT NULL CHECK(money_reserved_nano_usd BETWEEN 0 AND 2000000000),
  known_fee_nano_usd INTEGER NOT NULL CHECK(known_fee_nano_usd BETWEEN 0 AND money_reserved_nano_usd)
);
CREATE TRIGGER IF NOT EXISTS paid_run_costs_no_delete BEFORE DELETE ON paid_run_costs
BEGIN SELECT RAISE(ABORT, 'paid_usage_requires_preservation'); END;

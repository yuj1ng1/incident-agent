// These public fields follow web_api/schemas.py. Internal checkpoints stay in Python.
export const SESSION_STATUSES = [
  'CREATED', 'QUEUED', 'RUNNING', 'WAITING_RETRY', 'WAITING_HUMAN',
  'PAUSED', 'COMPLETED', 'EXHAUSTED', 'FAILED', 'CANCELLED',
] as const;
export type SessionStatus = typeof SESSION_STATUSES[number];
export type InvestigationStatus = 'completed' | 'max_round_reached' | 'model_incomplete';
export type HumanRequestStatus = 'pending' | 'answered' | 'consumed' | 'expired' | 'cancelled';
export type RecoveryStatus = 'alert_firing' | 'pending' | 'running' | 'verified' | 'failed'
  | 'insufficient' | 'superseded' | 'not_recorded' | 'legacy_only';
export type RecoveryReason = 'conditions_met' | 'recovery_capability_not_configured' | 'clear_time_invalid'
  | 'http_status_mismatch' | 'business_condition_mismatch' | 'redirect_refused' | 'timeout'
  | 'connection_failed' | 'invalid_response' | 'response_too_large' | 'content_encoding_refused'
  | 'invalid_evidence' | 'lease_expired' | 'alert_changed' | 'configuration_changed' | 'internal_probe_error';

export interface RecoveryCheckView {
  kind: 'http_status' | 'json_equals' | 'nonempty_object';
  field: string;
  matched: boolean;
}

export interface RecoveryAttemptView {
  number: number;
  started_at: string;
  completed_at: string | null;
  outcome: 'running' | 'success' | 'failed' | 'insufficient' | 'abandoned';
  reason: RecoveryReason | null;
  http_status: number | null;
  checks: RecoveryCheckView[];
}

export interface RecoveryVerificationView {
  status: RecoveryStatus;
  target_id: string | null;
  attempt_count: number;
  max_attempts: number | null;
  last_attempt_at: string | null;
  next_retry_at: string | null;
  reason: RecoveryReason | null;
  last_http_status: number | null;
  verified_at: string | null;
  attempts: RecoveryAttemptView[];
}

export interface Page<T> {
  items: T[];
  limit: number;
  offset: number;
  has_more: boolean;
}
export type IncidentPage = Page<IncidentSummary>;
export type SessionPage = Page<SessionSummary>;

export interface IncidentSummary {
  incident_id: string;
  service: string;
  severity: string;
  status: string;
  alert_state: string;
  last_investigated_at: string | null;
  cleared_at: string | null;
  recovery_verified_at: string | null;
  consecutive_investigation_failures: number;
  investigation_suspended_reason: string | null;
}

export interface ResultTrust {
  mechanical_status: 'not_checked' | 'passed' | 'failed';
  mechanical_scope: 'not_checked' | 'result_schema_only' | 'frozen_references_and_hashes';
  semantic_status: 'pending_review' | 'rejected' | 'accepted';
  trusted: false;
  human_approval: 'not_recorded';
  answer_sha256: string | null;
  review_origin: 'none' | 'caller_supplied_agent_review';
  rejection_reasons: string[];
}

export interface InvestigationAnswer {
  success: boolean | null;
  status: InvestigationStatus;
  answer: string;
  answer_truncated: boolean;
  model_rounds: number;
  tool_calls: number;
  result_trust: ResultTrust;
}

export interface IncidentDetail extends IncidentSummary {
  recovery_verification: RecoveryVerificationView;
  recent_alerts: {
    alertname: string; state: string; metric: string | null;
    value: number | null; threshold: number | null; observed_at: string | null;
  }[];
  recent_investigations: (InvestigationAnswer & {
    session_id: string | null; investigated_at: string;
  })[];
  evidence_count: number;
  archived_session_count: number;
  history_included: false;
}

export interface SessionSummary {
  session_id: string;
  incident_id: string;
  service: string;
  status: SessionStatus;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  state_version: number;
  attempt_count: number;
  current_attempt_rounds: number;
  current_attempt_tool_calls: number;
  total_model_requests: number;
  total_tool_executions: number;
  resume_count: number;
  last_error_category: string | null;
  next_retry_at: string | null;
}

export interface SessionDetail {
  session: SessionSummary;
  limits: {
    max_attempts: number; max_model_requests: number; max_tool_executions: number;
    max_resumes: number; max_duration_seconds: number; max_human_requests: number;
    lease_seconds: number; model_timeout_seconds: number; retry_base_seconds: number;
    human_timeout_seconds: number; starvation_seconds: number; checkpoints_to_keep: number;
  };
  incident_budget_usage: {
    attempts: number; model_requests: number; tool_executions: number;
    resumes: number; human_requests: number; started_at: string;
  };
  investigation_result: InvestigationAnswer | null;
  projection: {
    projected: boolean; blocked: boolean; error_category: string | null; retry_at: string | null;
  };
  proposal_generation: {
    status: 'not_pending' | 'waiting_retry' | 'blocked';
    pending: boolean; attempts: number; blocked: boolean; blocked_reason: string | null;
    error_category: string | null; retry_at: string | null; evidence_gap: string | null;
  };
  human_requests: {
    request_id: string; status: HumanRequestStatus; question: string; created_at: string; expires_at: string;
  }[];
}

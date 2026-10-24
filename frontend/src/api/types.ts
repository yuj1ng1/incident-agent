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

export type RemediationStatus = 'pending' | 'approved' | 'rejected' | 'executing' | 'succeeded' | 'failed' | 'verification_failed' | 'closed';
export interface OperatorView { actor: string; permissions: ('request' | 'approve' | 'execute')[] }
export interface CompensationTargetView {
  target_id: string; service: string; commit_sha: string; artifact_sha256: string;
  configuration_digest: string; preconditions: string; side_effects: string;
  verification_policy: string; data_compatibility: string;
}
export interface CompensationView {
  status: 'blocked' | 'approval_required' | 'already_requested'; reason: string;
  target: CompensationTargetView | null; compensation_proposal_id: string | null;
}
export interface CompensationRequestView extends CompensationView {
  assessment_id: string; command_id: string; requested_by: string; request_reason: string;
  evaluated_at: string; trigger: 'verification_failed' | 'operator_request';
}
export interface RemediationSummary {
  proposal_id: string; incident_id: string; service: string; summary: string; action_type: string;
  risk: 'low' | 'medium' | 'high'; status: RemediationStatus; created_at: string;
  execution_outcome: 'not_executed' | 'executed' | 'unknown'; original_proposal_id: string | null;
}
export type RemediationPage = Page<RemediationSummary>;
export interface RemediationDetail extends RemediationSummary {
  demo_recovery?: {
    environment: 'isolated_real_demo'; artifact_source: string; artifact_sha256: string;
    source_commit: string; target_commit: string; release_id: string;
    source_release_id?: string | null;
    instance_id: string; startup_configuration: string;
  } | null;
  reason: string; parameters: Record<string, string>; hidden_parameter_count: number;
  proposal_digest: string; expected_commit_sha: string; approved_by: string | null;
  approved_at: string | null; approval_valid: boolean; rejected_by: string | null;
  rejected_at: string | null; rejection_reason: string | null; closure_reason: string | null;
  compensation: CompensationView; target: CompensationTargetView | null;
  related_proposals: RemediationSummary[];
  actions: { action_id: string; started_at: string; finished_at: string;
    success: boolean; status: string; execution_outcome: 'not_executed' | 'executed' | 'unknown';
    reason?: string | null; resulting_commit_sha?: string | null;
    resulting_configuration_digest?: string | null; release_id?: string | null; pid?: number | null }[];
  verifications: { verification_id: string; verified_at: string; purpose: 'post_action' | 'manual_check';
    outcome: 'passed' | 'failed' | 'no_data' | 'timeout' | 'stale' | 'mismatch' | 'unknown';
    evidence_status: 'bound' | 'legacy' | 'stale' | 'mismatch' | 'manual_observation';
    historical_success: boolean; action_id: string | null;
    checks: { name: string; success: boolean | null; http_status: number | null;
      reason?: string | null;
      release_id?: string | null; artifact_sha256?: string | null; pid?: number | null }[] }[];
  requests: CompensationRequestView[];
  audit: { event_id: string; occurred_at: string; event: string; actor: string; reason: string | null }[];
}

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

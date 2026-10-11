import type { IncidentDetail, SessionDetail, SessionSummary } from '../src/api/types';

// Synthetic records belong only to tests. The application has no fixture fallback.
export const session: SessionSummary = {
  session_id: 'ses-fixture', incident_id: 'inc-fixture', service: 'fixture-service',
  status: 'COMPLETED', created_at: '2026-10-11T01:00:00Z', updated_at: '2026-10-11T01:01:00Z',
  completed_at: '2026-10-11T01:01:00Z', state_version: 3, attempt_count: 1,
  current_attempt_rounds: 2, current_attempt_tool_calls: 4, total_model_requests: 2,
  total_tool_executions: 4, resume_count: 0, last_error_category: null, next_retry_at: null,
};
export const incident: IncidentDetail = {
  incident_id: 'inc-fixture', service: 'fixture-service', severity: 'warning', status: 'investigating',
  alert_state: 'firing', last_investigated_at: '2026-10-11T01:01:00Z', cleared_at: null,
  recovery_verified_at: null, consecutive_investigation_failures: 0, investigation_suspended_reason: null,
  recent_alerts: [{ alertname: 'FixtureAlert', state: 'firing', metric: 'fixture_metric', value: null, threshold: 0, observed_at: null }],
  recent_investigations: [{ session_id: session.session_id, investigated_at: session.updated_at, success: null,
    status: 'completed', answer: '测试调查回答', answer_truncated: true, model_rounds: 2, tool_calls: 4 }],
  evidence_count: 0, archived_session_count: 2, history_included: false,
};
export const detail: SessionDetail = {
  session,
  limits: { max_attempts: 3, max_model_requests: 11, max_tool_executions: 39, max_resumes: 8,
    max_duration_seconds: 3600, max_human_requests: 0, lease_seconds: 120, model_timeout_seconds: 60,
    retry_base_seconds: 5, human_timeout_seconds: 1800, starvation_seconds: 120, checkpoints_to_keep: 4 },
  incident_budget_usage: { attempts: 2, model_requests: 7, tool_executions: 13, resumes: 1,
    human_requests: 0, started_at: session.created_at },
  investigation_result: { success: true, status: 'completed', answer: '<img src=x onerror=alert(1)> 纯文本调查结论',
    answer_truncated: true, model_rounds: 2, tool_calls: 4 },
  projection: { projected: false, blocked: true, error_category: 'fixture_projection_error', retry_at: null },
  proposal_generation: { status: 'not_pending', pending: false, attempts: 0, blocked: false,
    blocked_reason: null, error_category: null, retry_at: null, evidence_gap: 'fixture_evidence_gap' },
  human_requests: [],
};

export function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
}
export function page(items: unknown[], hasMore = false, offset = 0, limit = 20) {
  return { items, has_more: hasMore, offset, limit };
}

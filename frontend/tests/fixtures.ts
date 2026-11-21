import type {
  IncidentDetail,
  KnowledgeDocumentPage,
  KnowledgeDocumentSummary,
  KnowledgeImportResponse,
  KnowledgeSearchResponse,
  KnowledgeVersionDetail,
  ResultTrust,
  RunbookCitationView,
  SessionDetail,
  SessionSummary,
} from '../src/api/types';

// Synthetic records belong only to tests. The application has no fixture fallback.
const resultTrust: ResultTrust = {
  mechanical_status: 'passed', mechanical_scope: 'result_schema_only',
  semantic_status: 'pending_review', trusted: false, human_approval: 'not_recorded',
  answer_sha256: null, review_origin: 'none', rejection_reasons: [],
};
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
  recovery_verification: {
    status: 'alert_firing', target_id: null, attempt_count: 0, max_attempts: null,
    last_attempt_at: null, next_retry_at: null, reason: null, last_http_status: null,
    verified_at: null, attempts: [],
  },
  recent_alerts: [{ alertname: 'FixtureAlert', state: 'firing', metric: 'fixture_metric', value: null, threshold: 0, observed_at: null,
    origin_status: 'unconfirmed', evidence_origin: null, origin_note: null }],
  recent_investigations: [{ session_id: session.session_id, investigated_at: session.updated_at, success: null,
    status: 'completed', answer: '测试调查回答', answer_truncated: true, model_rounds: 2, tool_calls: 4,
    result_trust: resultTrust, origin_status: 'unconfirmed', evidence_origin: null, origin_note: null }],
  evidence_count: 0, archived_session_count: 2, history_included: false,
};
export const detail: SessionDetail = {
  session,
  limits_source: 'session_checkpoint',
  limits: { max_attempts: 3, max_model_requests: 11, max_tool_executions: 39, max_resumes: 8,
    max_duration_seconds: 3600, max_human_requests: 0, lease_seconds: 120, model_timeout_seconds: 60,
    retry_base_seconds: 5, human_timeout_seconds: 1800, starvation_seconds: 120, checkpoints_to_keep: 4 },
  incident_budget_usage: { attempts: 2, model_requests: 7, tool_executions: 13, resumes: 1,
    human_requests: 0, started_at: session.created_at },
  origin_status: 'unconfirmed',
  evidence_origin: null,
  origin_note: null,
  investigation_result: { success: true, status: 'completed', answer: '<img src=x onerror=alert(1)> 纯文本调查结论',
    answer_truncated: true, model_rounds: 2, tool_calls: 4, result_trust: resultTrust,
    origin_status: 'unconfirmed', evidence_origin: null, origin_note: null },
  projection: { projected: false, blocked: true, error_category: 'fixture_projection_error', retry_at: null },
  proposal_generation: { status: 'not_pending', pending: false, attempts: 0, blocked: false,
    blocked_reason: null, error_category: null, retry_at: null, evidence_gap: 'fixture_evidence_gap' },
  human_requests: [],
  runbook_citations: [],
};

export const runbookCitation: RunbookCitationView = {
  document_id: 'doc-order-5xx-001',
  version_id: 'ver-order-5xx-v2',
  chunk_id: 'chk-order-5xx-02',
  service: 'order-service',
  title: '订单服务 HTTP 5xx 排障手册',
  heading_path: '订单服务 HTTP 5xx 排障手册 > 常见错误码 > HighHttp5xxRate',
  start_line: 4,
  end_line: 7,
  citation: 'doc-order-5xx-001@ver-order-5xx-v2#L4-L7',
  snippet: '检查近期发布与 simulate_code_regression 开关，并核对 /order 实时指标。',
  score: 0.0328,
};

export const knowledgeDocument: KnowledgeDocumentSummary = {
  document_id: 'doc-order-5xx-001',
  service: 'order-service',
  title: '订单服务 HTTP 5xx 排障手册',
  source: 'order_service_5xx_runbook.md',
  published_version_id: 'ver-order-5xx-v2',
  published_status: 'published',
  version_count: 2,
  versions: [
    {
      version_id: 'ver-order-5xx-v1',
      status: 'archived',
      content_sha256: '1'.repeat(64),
      created_at: '2026-11-18T02:00:00Z',
    },
    {
      version_id: 'ver-order-5xx-v2',
      status: 'published',
      content_sha256: '2'.repeat(64),
      created_at: '2026-11-20T04:00:00Z',
    },
  ],
  created_at: '2026-11-18T02:00:00Z',
  updated_at: '2026-11-20T04:00:00Z',
};

export const knowledgeDocumentPage: KnowledgeDocumentPage = {
  items: [knowledgeDocument],
  total: 1,
  limit: 20,
  offset: 0,
  has_more: false,
};

export const knowledgeVersionDetail: KnowledgeVersionDetail = {
  document_id: 'doc-order-5xx-001',
  service: 'order-service',
  title: '订单服务 HTTP 5xx 排障手册',
  source: 'order_service_5xx_runbook.md',
  published_version_id: 'ver-order-5xx-v2',
  version_id: 'ver-order-5xx-v2',
  status: 'published',
  content_sha256: '2'.repeat(64),
  created_at: '2026-11-20T04:00:00Z',
  raw_markdown: [
    '# 订单服务 HTTP 5xx 排障手册',
    '',
    '## 常见错误码',
    '### HighHttp5xxRate',
    '当 order-service 出现 5xx 飙升时，先查询实时指标与 Loki 日志。',
    '排查 simulate_code_regression 与下游 order-db 连通性。',
    '切勿仅凭手册判断当前根因，需以实时观测为准。',
    '',
    '## 恢复核对',
    '确认 /order 业务探针返回 HTTP 200 且无错误日志。',
  ].join('\n'),
  chunks: [
    {
      chunk_id: 'chk-order-5xx-01',
      ordinal: 0,
      heading_path: '订单服务 HTTP 5xx 排障手册',
      start_line: 1,
      end_line: 3,
      chunk_text: '# 订单服务 HTTP 5xx 排障手册\n\n## 常见错误码',
      embedding_model: 'offline-ngram-v1',
      embedding_dim: 256,
    },
    {
      chunk_id: 'chk-order-5xx-02',
      ordinal: 1,
      heading_path: '订单服务 HTTP 5xx 排障手册 > 常见错误码 > HighHttp5xxRate',
      start_line: 4,
      end_line: 7,
      chunk_text: '### HighHttp5xxRate\n当 order-service 出现 5xx 飙升时，先查询实时指标与 Loki 日志。',
      embedding_model: 'offline-ngram-v1',
      embedding_dim: 256,
    },
    {
      chunk_id: 'chk-order-5xx-03',
      ordinal: 2,
      heading_path: '订单服务 HTTP 5xx 排障手册 > 恢复核对',
      start_line: 9,
      end_line: 10,
      chunk_text: '## 恢复核对\n确认 /order 业务探针返回 HTTP 200 且无错误日志。',
      embedding_model: 'offline-ngram-v1',
      embedding_dim: 256,
    },
  ],
};

export const knowledgeSearchOk: KnowledgeSearchResponse = {
  status: 'ok',
  service: 'order-service',
  query: 'HighHttp5xxRate',
  method: 'hybrid_rrf',
  candidate_count: 12,
  reason: null,
  hits: [
    {
      document_id: 'doc-order-5xx-001',
      version_id: 'ver-order-5xx-v2',
      chunk_id: 'chk-order-5xx-02',
      service: 'order-service',
      title: '订单服务 HTTP 5xx 排障手册',
      source: 'order_service_5xx_runbook.md',
      heading_path: '订单服务 HTTP 5xx 排障手册 > 常见错误码 > HighHttp5xxRate',
      start_line: 4,
      end_line: 7,
      snippet: '当 order-service 出现 5xx 飙升时，先查询实时指标与 Loki 日志。',
      score: 0.0528,
      lexical_rank: 1,
      vector_rank: 1,
      exact_match: true,
      citation: 'doc-order-5xx-001@ver-order-5xx-v2#L4-L7',
    },
  ],
};

export const knowledgeImportResponse: KnowledgeImportResponse = {
  document: knowledgeDocument,
  version: knowledgeDocument.versions[1],
  chunk_count: 3,
  idempotent: false,
};

export function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
}
export function page(items: unknown[], hasMore = false, offset = 0, limit = 20) {
  return { items, has_more: hasMore, offset, limit };
}

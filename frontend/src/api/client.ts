export class ApiError extends Error {
  constructor(message: string, readonly status?: number) { super(message); }
}

export function apiBaseUrl(value = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8010'): string {
  if (value === '/') return '';
  let url: URL;
  try { url = new URL(value); } catch { throw new ApiError('API 地址格式无效。'); }
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname)
      || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new ApiError('第一版工作台只允许连接本地 HTTP API。');
  }
  return url.origin;
}

const errorMessages: Record<number, string> = {
  401: '操作凭据无效或未提供，请重新连接操作身份。',
  403: '当前身份没有此操作的权限，或请求来源不受信任。',
  409: '当前状态或审批摘要已变化，操作被阻断。请刷新记录后核查。',
  404: '没有找到这条记录，它可能已被移除，或地址中的 ID 不正确。',
  422: '查询参数无效，请重新选择筛选条件。',
  500: '存储读取失败。请检查 FastAPI 终端中的诊断信息。',
  503: '存储暂不可用。请核对后端数据文件和访问权限，然后重试。',
};

const operationErrors: Record<string, string> = {
  store_busy: '存储正在处理其他请求，请稍后刷新核查。',
  write_auth_not_configured: '后端尚未配置操作授权，写操作已关闭。',
  operator_permission_denied: '当前身份没有此操作的权限。',
  local_operator_required: '请求必须来自受信任的本地工作台。',
  proposal_digest_mismatch: '提案内容已变化，审批摘要不匹配。请刷新后重新核对。',
  verification_stale: '验证证据已过期，本次操作被阻断；需要可信的新证据。',
  compensation_requester_cannot_approve: '申请身份不能审批自己的补偿提案，请切换到独立审批身份。',
  compensation_requester_cannot_execute: '申请身份不能执行自己的补偿提案，请切换到独立执行身份。',
  compensation_approver_cannot_execute: '审批身份不能执行其批准的补偿提案，请切换到独立执行身份。',
  compensation_requester_unconfirmed: '无法确认原申请身份，补偿审批与执行已关闭，请核查审计记录。',
  safe_compensation_target_not_registered: '没有注册经过验证的安全补偿目标，本次操作被阻断。',
  original_execution_unknown: '原动作结果未知，请先核查，不能再次执行或盲目补偿。',
  command_id_conflict: '操作标识与已保存的请求冲突，请刷新核查已有记录。',
  incident_missing_archived_or_changed: '关联 Incident 不存在、已归档或身份已变化，操作被阻断。',
  knowledge_unavailable: '运维知识库暂不可用，请检查 MySQL 连接状态后重试。',
  knowledge_database_unavailable: '运维知识库数据库暂不可用，请检查 MySQL 连接状态后重试。',
  knowledge_schema_not_migrated: '运维知识库数据表尚未完成迁移，请先执行 Alembic 迁移。',
  knowledge_not_found: '没有找到指定的知识库文档或版本。',
  document_not_found: '没有找到指定的知识库文档。',
  version_not_found: '没有找到指定的知识库文档版本。',
  published_version_not_found: '该文档尚未发布可用版本。',
  knowledge_validation_failed: '知识库请求参数或 Markdown 内容校验未通过，请核对后重试。',
  embedding_unavailable: '向量化服务暂不可用，导入或语义检索已中止；已发布版本不受影响。',
  embedding_provider_not_configured: '后端尚未配置可用的向量化服务。',
  embedding_request_timeout: '向量化服务请求超时，请稍后重试；已发布版本不受影响。',
  unknown_service: '所选服务未在 ServiceRegistry 中注册，请求已被拒绝。',
  invalid_service_name: '服务名称无效，请选择已注册的服务。',
  document_service_mismatch: '文档所属服务与请求中的服务不一致，禁止跨服务修改。',
  markdown_exceeds_max_bytes: 'Markdown 文件超过大小上限（512 KB），请精简后重试。',
  markdown_invalid_utf8: 'Markdown 文件不是有效的 UTF-8 文本，请重新保存编码后重试。',
  markdown_contains_nul_byte: 'Markdown 文件包含非法空字节，已被拒绝。',
  markdown_content_empty: 'Markdown 内容为空，请选择有效的手册文件。',
  markdown_produced_no_chunks: 'Markdown 未能切分出有效正文切片，请检查内容。',
  knowledge_store_read_only: '知识库存储当前为只读模式，无法写入。',
  knowledge_integrity_conflict: '知识库写入发生并发冲突，请刷新文档列表后核对。',
  query_cannot_be_empty: '请输入检索关键词或故障现象。',
  query_exceeds_max_length: '检索文本过长（上限 2000 字符），请精简后重试。',
  invalid_document_id: '文档 ID 格式无效，请核对后重试。',
  invalid_version_id: '版本 ID 格式无效，请核对后重试。',
  invalid_source_name: '文档来源标识不能为空或超过长度限制。',
};

async function requestJson<T>(path: string, signal: AbortSignal, token?: string, body?: unknown): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const cancel = () => controller.abort();
  signal.addEventListener('abort', cancel, { once: true });
  if (signal.aborted) controller.abort();
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 10_000);
  try {
    const response = await fetch(`${apiBaseUrl()}${path}`, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'omit', cache: 'no-store',
      headers: { Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), referrerPolicy: 'no-referrer',
      signal: controller.signal,
    });
    if (!response.ok) {
      let message: string | undefined;
      // Only known codes become UI text; never echo a raw backend error/body.
      try {
        const failure = await response.json();
        if (typeof failure?.detail === 'string' && Object.hasOwn(operationErrors, failure.detail)) message = operationErrors[failure.detail];
      } catch { /* Use a safe HTTP fallback for non-JSON failures. */ }
      throw new ApiError(message || (response.status === 422 && body !== undefined
        ? '操作参数无效，请核对理由和当前提案后重试。'
        : response.status >= 500 && body !== undefined
          ? '操作响应异常，结果尚未确认。请刷新核查，不要重复执行。'
          : errorMessages[response.status] || `API 请求失败（HTTP ${response.status}）。`), response.status);
    }
    if (!response.headers.get('content-type')?.includes('application/json')) {
      throw new ApiError('API 返回了非 JSON 内容，请确认 API 地址和端口。');
    }
    let data: unknown;
    try { data = await response.json(); } catch { throw new ApiError('API 返回的 JSON 无法解析。'); }
    if (!data || typeof data !== 'object') throw new ApiError('API 响应格式不正确。');
    const routePath = path.split('?')[0];
    if (body === undefined && ['/api/incidents', '/api/sessions', '/api/remediations', '/api/knowledge/documents'].includes(routePath)) {
      const page = data as Record<string, unknown>;
      if (!Array.isArray(page.items) || typeof page.has_more !== 'boolean'
          || !Number.isInteger(page.offset) || !Number.isInteger(page.limit)
          || (routePath === '/api/knowledge/documents' && !Number.isInteger(page.total))) {
        throw new ApiError('API 分页响应格式不正确。');
      }
    }
    return data as T;
  } catch (error) {
    if (signal.aborted) throw error;
    if (error instanceof ApiError) throw error;
    if (timedOut) throw new ApiError(body === undefined ? '请求超过 10 秒，请稍后重试。'
      : '请求超过 10 秒，操作结果尚未确认。请刷新核查，不要重复执行。');
    throw new ApiError('无法连接本地 API。请确认 FastAPI 已启动，且前端使用 5173 端口。');
  } finally {
    clearTimeout(timer);
    signal.removeEventListener('abort', cancel);
  }
}

export const getJson = <T>(path: string, signal: AbortSignal) => requestJson<T>(path, signal);
export const operatorJson = <T>(path: string, token: string, signal: AbortSignal) => requestJson<T>(path, signal, token);
export const postJson = <T>(path: string, token: string, body: unknown, signal: AbortSignal) => requestJson<T>(path, signal, token, body);

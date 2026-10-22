export class ApiError extends Error {
  constructor(message: string, readonly status?: number) { super(message); }
}

export function apiBaseUrl(value = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8010'): string {
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
  503: '存储暂不可用。请确认 Runtime 已生成数据文件，然后重试。',
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
    if (!response.ok) throw new ApiError(
      response.status === 503 && token ? '写操作未启用或存储暂不可用。请核对后端操作授权配置。'
        : errorMessages[response.status] || `API 请求失败（HTTP ${response.status}）。`, response.status,
    );
    if (!response.headers.get('content-type')?.includes('application/json')) {
      throw new ApiError('API 返回了非 JSON 内容，请确认 API 地址和端口。');
    }
    let data: unknown;
    try { data = await response.json(); } catch { throw new ApiError('API 返回的 JSON 无法解析。'); }
    if (!data || typeof data !== 'object') throw new ApiError('API 响应格式不正确。');
    if (['/api/incidents', '/api/sessions', '/api/remediations'].includes(path.split('?')[0])) {
      const page = data as Record<string, unknown>;
      if (!Array.isArray(page.items) || typeof page.has_more !== 'boolean'
          || !Number.isInteger(page.offset) || !Number.isInteger(page.limit)) {
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

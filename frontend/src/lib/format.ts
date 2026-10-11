export function formatTime(value: string | null | undefined): string {
  if (!value) return '暂无记录';
  // Legacy naive Runtime times are Beijing time, matching Python's query semantics.
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}+08:00`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return '时间无效';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
    year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date);
}

export function recordPath(kind: 'incidents' | 'sessions', id: string): string {
  return `/${kind}/${encodeURIComponent(id)}`;
}

export function pageQuery(search: URLSearchParams, allowedStatuses: readonly string[]) {
  const raw = Number(search.get('offset') || 0);
  const offset = Number.isInteger(raw) && raw >= 0 && raw <= 10000 && raw % 20 === 0 ? raw : 0;
  const status = allowedStatuses.includes(search.get('status') || '') ? search.get('status')! : '';
  return { offset, status };
}

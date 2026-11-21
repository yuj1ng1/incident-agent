export const KNOWLEDGE_SERVICES = [
  'order-service',
  'order-db',
  'inventory-service',
] as const;

export const MAX_MARKDOWN_BYTES = 512 * 1024;

export const retrievalMethodLabels: Record<string, string> = {
  hybrid_rrf: '混合检索（全文 + 向量 RRF）',
  mysql_ngram: 'MySQL 全文检索（ngram）',
  vector_cosine: '语义向量检索（余弦相似度）',
};

const knowledgeReasons: Record<string, string> = {
  knowledge_unavailable: '运维知识库暂不可用，请检查 MySQL 连接状态后重试。',
  knowledge_database_unavailable: '运维知识库数据库暂不可用，请检查 MySQL 连接状态后重试。',
  knowledge_schema_not_migrated: '运维知识库数据表尚未完成迁移，请先执行 Alembic 迁移。',
  embedding_unavailable: '向量化服务暂不可用，可稍后重试或切换为 MySQL 全文检索。',
  embedding_provider_not_configured: '后端尚未配置可用的向量化服务，可切换为 MySQL 全文检索。',
  embedding_request_timeout: '向量化服务请求超时，请稍后重试。',
  unknown_service: '所选服务未在 ServiceRegistry 中注册。',
};

export function knowledgeReasonLabel(reason: string | null | undefined): string {
  if (!reason) return '知识库服务暂不可用，请检查后端数据库与向量化配置。';
  return knowledgeReasons[reason] || `知识库服务不可用（${reason}）`;
}

export function knowledgeVersionPath(
  documentId: string,
  versionId: string,
  range?: { start?: number | null; end?: number | null; chunk?: string | null },
): string {
  const base = `/knowledge/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(versionId)}`;
  if (!range) return base;
  const params = new URLSearchParams();
  if (typeof range.start === 'number' && Number.isInteger(range.start) && range.start > 0) {
    params.set('start', String(range.start));
  }
  if (typeof range.end === 'number' && Number.isInteger(range.end) && range.end > 0) {
    params.set('end', String(range.end));
  }
  if (range.chunk) {
    params.set('chunk', range.chunk);
  }
  const query = params.toString();
  return query ? `${base}?${query}` : base;
}

export function extractMarkdownTitle(markdown: string, fallbackFilename?: string): string {
  for (const line of markdown.split(/\r?\n/)) {
    const match = /^\s{0,3}#\s+(.+?)(?:\s+#+\s*)?$/.exec(line);
    if (match?.[1]?.trim()) {
      return match[1].trim().slice(0, 300);
    }
  }
  if (fallbackFilename) {
    return fallbackFilename.replace(/\.[^.]+$/, '').slice(0, 300);
  }
  return '';
}

export async function readMarkdownFile(file: File): Promise<string> {
  if (typeof file.text === 'function') {
    return file.text();
  }
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error || new Error('文件读取失败。'));
    reader.readAsText(file, 'utf-8');
  });
}

export function parseHighlightRange(
  search: URLSearchParams,
  chunks?: readonly { chunk_id: string; start_line: number; end_line: number }[],
): { start: number | null; end: number | null; chunkId: string | null } {
  const chunkId = search.get('chunk')?.trim() || null;
  const rawStart = Number(search.get('start'));
  const rawEnd = Number(search.get('end'));
  if (Number.isInteger(rawStart) && rawStart > 0) {
    const end = Number.isInteger(rawEnd) && rawEnd >= rawStart ? rawEnd : rawStart;
    return { start: rawStart, end, chunkId };
  }
  if (chunkId && chunks) {
    const matched = chunks.find(item => item.chunk_id === chunkId);
    if (matched) {
      return { start: matched.start_line, end: matched.end_line, chunkId };
    }
  }
  return { start: null, end: null, chunkId };
}

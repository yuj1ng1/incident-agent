import { useEffect } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import type { KnowledgeVersionDetail } from '../api/types';
import { DataState, EmptyState, Fields, PageHeader, Panel, StatusBadge, TechnicalValue } from '../components/ui';
import { useApi } from '../hooks/useApi';
import { formatTime } from '../lib/format';
import { knowledgeVersionPath, parseHighlightRange } from '../lib/knowledge';

export function KnowledgeVersionPage() {
  const { documentId = '', versionId = '' } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const path = `/api/knowledge/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(versionId)}`;
  const result = useApi<KnowledgeVersionDetail>(path);
  const detail = result.data;

  const { start, end, chunkId } = parseHighlightRange(searchParams, detail?.chunks);
  const lines = detail ? detail.raw_markdown.replace(/\r\n?/g, '\n').split('\n') : [];

  useEffect(() => {
    if (!detail || start === null) return;
    const target = document.getElementById(`kb-line-${start}`);
    if (target && typeof target.scrollIntoView === 'function') {
      target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }, [detail, start, end]);

  const selectRange = (nextStart: number, nextEnd: number, nextChunkId?: string) => {
    const next = new URLSearchParams();
    next.set('start', String(nextStart));
    next.set('end', String(nextEnd));
    if (nextChunkId) next.set('chunk', nextChunkId);
    setSearchParams(next, { replace: true });
  };

  const clearRange = () => {
    setSearchParams(new URLSearchParams(), { replace: true });
  };

  const isCurrentPublished = Boolean(detail && detail.published_version_id === detail.version_id);

  return <>
    <PageHeader
      title={detail?.title || '手册版本详情'}
      description="查看原始 Markdown 内容、章节切片目录与行号定位；手册内容仅作辅助排查线索。"
      back={{ to: '/knowledge', label: '返回知识库' }}
      loading={result.loading}
      reload={result.reload}
      fetchedAt={result.fetchedAt}
    />
    <DataState loading={result.loading} error={result.error} retry={result.reload}>
      {detail && <>
        <div className="detail-banner">
          <div>
            <span className="eyebrow">KNOWLEDGE VERSION</span>
            <h2>{detail.title}</h2>
            <span className="full-id">{detail.document_id} · {detail.version_id}</span>
          </div>
          <div className="badge-group">
            <StatusBadge value={detail.service} label={detail.service} tone="blue" />
            <StatusBadge value={detail.status} />
            {isCurrentPublished && <StatusBadge value="current_published" label="当前发布版本" tone="green" />}
          </div>
        </div>

        {detail.published_version_id && detail.published_version_id !== detail.version_id && (
          <div className="kb-warning" role="note" style={{ marginBottom: '24px' }}>
            当前查看的是历史或草稿版本（<code>{detail.version_id}</code>）。该文档当前已发布版本为{' '}
            <Link className="text-link" to={knowledgeVersionPath(detail.document_id, detail.published_version_id)}>
              {detail.published_version_id}
            </Link>
            。
          </div>
        )}

        <Panel title="版本元数据" subtitle="记录文档归属服务、版本哈希与切片统计">
          <Fields items={[
            ['服务范围', detail.service],
            ['文档 ID', <TechnicalValue value={detail.document_id} />],
            ['版本 ID', <TechnicalValue value={detail.version_id} />],
            ['版本状态', <StatusBadge value={detail.status} />],
            ['当前发布版本', <TechnicalValue value={detail.published_version_id} />],
            ['文档来源', detail.source],
            ['内容 SHA-256', <TechnicalValue value={detail.content_sha256} />],
            ['创建时间', formatTime(detail.created_at)],
            ['切片总数', `${detail.chunks.length} 个`],
            ['原文行数', `${lines.length} 行`],
          ]} />
        </Panel>

        <div className="kb-version-layout">
          <Panel title="切片目录" subtitle="点击任意切片可高亮并定位原文行范围">
            {detail.chunks.length ? (
              <ul className="kb-outline-list" aria-label="文档切片目录">
                {detail.chunks.map(chunk => {
                  const active = chunkId === chunk.chunk_id
                    || (chunkId === null && start === chunk.start_line && end === chunk.end_line);
                  return (
                    <li key={chunk.chunk_id}>
                      <button
                        type="button"
                        className={active ? 'kb-outline-item is-active' : 'kb-outline-item'}
                        onClick={() => selectRange(chunk.start_line, chunk.end_line, chunk.chunk_id)}
                      >
                        <span className="kb-outline-heading">{chunk.heading_path || '(文档开头)'}</span>
                        <span className="kb-outline-range">
                          L{chunk.start_line}-L{chunk.end_line} · {chunk.chunk_id}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            ) : <EmptyState title="暂无切片" description="该版本未记录切片信息。" />}
          </Panel>

          <Panel title="Markdown 原文" subtitle={`按 1..${lines.length} 行号展示；以纯文本渲染，不执行任何 HTML`}>
            {start !== null && end !== null && (
              <div className="kb-highlight-bar" role="status">
                <span>
                  已定位行范围：<strong>L{start}-L{end}</strong>
                  {chunkId ? <> · 切片 <code>{chunkId}</code></> : null}
                </span>
                <button type="button" className="button" onClick={clearRange}>清除定位</button>
              </div>
            )}
            <div className="kb-source-viewer" tabIndex={0} role="region" aria-label="Markdown 原文与行号">
              <ol className="kb-source-lines">
                {lines.map((line, index) => {
                  const lineNo = index + 1;
                  const highlighted = start !== null && end !== null && lineNo >= start && lineNo <= end;
                  return (
                    <li
                      key={lineNo}
                      id={`kb-line-${lineNo}`}
                      data-line={lineNo}
                      className={highlighted ? 'kb-line is-highlighted' : 'kb-line'}
                    >
                      <span className="kb-line-num" aria-hidden="true">{lineNo}</span>
                      <code className="kb-line-text">{line || ' '}</code>
                    </li>
                  );
                })}
              </ol>
            </div>
          </Panel>
        </div>
      </>}
    </DataState>
  </>;
}

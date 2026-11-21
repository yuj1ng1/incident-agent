import { ArrowUpRight, FileText, Search, Upload } from 'lucide-react';
import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { getJson, postJson } from '../api/client';
import type {
  KnowledgeDocumentPage,
  KnowledgeDocumentSummary,
  KnowledgeImportResponse,
  KnowledgeSearchMethod,
  KnowledgeSearchResponse,
} from '../api/types';
import { OperatorAccess, useOperator } from '../components/OperatorAccess';
import { DataState, EmptyState, PageHeader, Pagination, Panel, StatusBadge, TimeValue } from '../components/ui';
import { useApi } from '../hooks/useApi';
import { pageQuery } from '../lib/format';
import {
  extractMarkdownTitle,
  KNOWLEDGE_SERVICES,
  knowledgeReasonLabel,
  knowledgeVersionPath,
  MAX_MARKDOWN_BYTES,
  readMarkdownFile,
  retrievalMethodLabels,
} from '../lib/knowledge';

type ImportMode = 'new' | 'version' | 'switch';

const DOC_STATUSES = ['published', 'unpublished'] as const;

export function KnowledgePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { offset, status } = pageQuery(searchParams, DOC_STATUSES);
  const serviceFilter = (searchParams.get('service') || '').trim();
  const validServiceFilter = KNOWLEDGE_SERVICES.includes(serviceFilter as typeof KNOWLEDGE_SERVICES[number])
    ? serviceFilter
    : '';

  const listParams = new URLSearchParams({ limit: '20', offset: String(offset) });
  if (validServiceFilter) listParams.set('service', validServiceFilter);
  if (status) listParams.set('status', status);
  const docsResult = useApi<KnowledgeDocumentPage>(`/api/knowledge/documents?${listParams}`);
  const documents = docsResult.data?.items || [];

  const updateListParams = (nextOffset: number, nextService = validServiceFilter, nextStatus = status) => {
    const next = new URLSearchParams();
    if (nextService) next.set('service', nextService);
    if (nextStatus) next.set('status', nextStatus);
    if (nextOffset) next.set('offset', String(nextOffset));
    setSearchParams(next);
  };

  // Search panel state (uses GET /api/knowledge/search so read-only CORS mode works smoothly)
  const [searchService, setSearchService] = useState<string>(validServiceFilter || KNOWLEDGE_SERVICES[0]);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchLimit, setSearchLimit] = useState(5);
  const [searchMethod, setSearchMethod] = useState<KnowledgeSearchMethod>('hybrid_rrf');
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState('');
  const [searchResponse, setSearchResponse] = useState<KnowledgeSearchResponse | null>(null);
  const searchController = useRef<AbortController | null>(null);

  useEffect(() => () => searchController.current?.abort(), []);

  async function handleSearch(event: FormEvent) {
    event.preventDefault();
    const cleaned = searchQuery.trim();
    if (!cleaned) {
      setSearchError('请输入检索关键词、错误码或故障现象。');
      return;
    }
    const boundedLimit = Math.min(20, Math.max(1, Math.trunc(Number(searchLimit) || 5)));
    searchController.current?.abort();
    const controller = new AbortController();
    searchController.current = controller;
    setSearching(true);
    setSearchError('');
    setSearchResponse(null);
    const params = new URLSearchParams({
      service: searchService,
      query: cleaned,
      limit: String(boundedLimit),
      method: searchMethod,
    });
    try {
      const response = await getJson<KnowledgeSearchResponse>(`/api/knowledge/search?${params}`, controller.signal);
      if (!controller.signal.aborted) {
        setSearchResponse(response);
      }
    } catch (failure) {
      if (!controller.signal.aborted) {
        setSearchError((failure as Error).message);
      }
    } finally {
      if (!controller.signal.aborted) {
        setSearching(false);
      }
    }
  }

  // Import & version management state (gated by knowledge_write)
  const { token, operator } = useOperator();
  const canWrite = Boolean(operator?.permissions.includes('knowledge_write'));
  const [mode, setMode] = useState<ImportMode>('new');
  const [importService, setImportService] = useState<string>(KNOWLEDGE_SERVICES[0]);
  const [importTitle, setImportTitle] = useState('');
  const [importSource, setImportSource] = useState('');
  const [importDocId, setImportDocId] = useState('');
  const [targetDocId, setTargetDocId] = useState('');
  const [switchVersionId, setSwitchVersionId] = useState('');
  const [publishNow, setPublishNow] = useState(true);
  const [selectedFileName, setSelectedFileName] = useState('');
  const [fileSizeBytes, setFileSizeBytes] = useState(0);
  const [rawMarkdown, setRawMarkdown] = useState('');
  const [fileError, setFileError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState('');
  const [submitResult, setSubmitResult] = useState<KnowledgeImportResponse | null>(null);
  const writeController = useRef<AbortController | null>(null);
  const writeInFlight = useRef(false);
  const identityGen = useRef(0);

  useEffect(() => {
    identityGen.current++;
    writeController.current?.abort();
    if (writeInFlight.current) {
      setSubmitError('操作身份已变更，前一次提交结果尚未确认，请刷新文档列表核对。');
    }
    writeInFlight.current = false;
    setSubmitting(false);
  }, [token]);

  useEffect(() => () => writeController.current?.abort(), []);

  const selectedDoc: KnowledgeDocumentSummary | undefined = documents.find(doc => doc.document_id === targetDocId);

  useEffect(() => {
    if (!targetDocId && documents.length > 0) {
      setTargetDocId(documents[0].document_id);
    }
  }, [documents, targetDocId]);

  useEffect(() => {
    if (mode === 'switch' && selectedDoc?.versions?.length) {
      const candidate = selectedDoc.versions.find(v => v.version_id !== selectedDoc.published_version_id)
        || selectedDoc.versions[0];
      setSwitchVersionId(candidate?.version_id || '');
    }
  }, [mode, selectedDoc]);

  async function handleFileSelect(event: ChangeEvent<HTMLInputElement>) {
    setFileError('');
    setSubmitError('');
    setSubmitResult(null);
    const file = event.target.files?.[0];
    if (!file) {
      setSelectedFileName('');
      setFileSizeBytes(0);
      setRawMarkdown('');
      return;
    }
    if (file.size > MAX_MARKDOWN_BYTES) {
      setFileError('文件超过 512 KB 大小限制，请精简后再选择。');
      setSelectedFileName(file.name);
      setFileSizeBytes(file.size);
      setRawMarkdown('');
      return;
    }
    try {
      const text = await readMarkdownFile(file);
      if (!text.trim()) {
        setFileError('所选 Markdown 文件内容为空。');
        setSelectedFileName(file.name);
        setFileSizeBytes(file.size);
        setRawMarkdown('');
        return;
      }
      const bytes = new TextEncoder().encode(text).length;
      if (bytes > MAX_MARKDOWN_BYTES) {
        setFileError('文件内容 UTF-8 字节数超过 512 KB 上限。');
        setRawMarkdown('');
        return;
      }
      setSelectedFileName(file.name);
      setFileSizeBytes(bytes);
      setRawMarkdown(text);
      if (mode === 'new') {
        if (!importSource.trim()) setImportSource(file.name);
        if (!importTitle.trim()) setImportTitle(extractMarkdownTitle(text, file.name));
      }
    } catch {
      setFileError('读取本地 Markdown 文件失败，请确认文件为 UTF-8 编码。');
      setRawMarkdown('');
    }
  }

  function startVersionUploadFor(doc: KnowledgeDocumentSummary) {
    setMode('version');
    setTargetDocId(doc.document_id);
    setImportTitle(doc.title);
    setImportSource(doc.source);
    setSubmitError('');
    setSubmitResult(null);
  }

  async function handleImportSubmit(event: FormEvent) {
    event.preventDefault();
    if (!canWrite || !token || writeInFlight.current) return;
    setSubmitError('');
    setSubmitResult(null);

    let endpoint = '';
    let payload: Record<string, unknown> = {};

    if (mode === 'new') {
      if (!rawMarkdown.trim()) {
        setSubmitError('请先选择有效的本地 .md 文件并确认预览内容。');
        return;
      }
      endpoint = '/api/knowledge/documents';
      payload = {
        service: importService,
        title: (importTitle.trim() || extractMarkdownTitle(rawMarkdown, selectedFileName) || '未命名手册'),
        source: (importSource.trim() || selectedFileName || 'manual-upload.md'),
        raw_markdown: rawMarkdown,
        publish: publishNow,
        ...(importDocId.trim() ? { document_id: importDocId.trim() } : {}),
      };
    } else if (mode === 'version') {
      if (!targetDocId) {
        setSubmitError('请先选择目标文档。');
        return;
      }
      if (!rawMarkdown.trim()) {
        setSubmitError('请先选择新版本的本地 .md 文件并确认预览内容。');
        return;
      }
      endpoint = `/api/knowledge/documents/${encodeURIComponent(targetDocId)}/versions`;
      payload = {
        raw_markdown: rawMarkdown,
        publish: publishNow,
        ...(importTitle.trim() ? { title: importTitle.trim() } : {}),
        ...(importSource.trim() ? { source: importSource.trim() } : {}),
      };
    } else {
      if (!targetDocId || !switchVersionId.trim()) {
        setSubmitError('请选择目标文档与待发布版本 ID。');
        return;
      }
      endpoint = `/api/knowledge/documents/${encodeURIComponent(targetDocId)}/versions`;
      payload = {
        publish_version_id: switchVersionId.trim(),
      };
    }

    writeInFlight.current = true;
    setSubmitting(true);
    const generation = identityGen.current;
    const controller = new AbortController();
    writeController.current = controller;
    try {
      const response = await postJson<KnowledgeImportResponse>(endpoint, token, payload, controller.signal);
      if (generation !== identityGen.current) return;
      setSubmitResult(response);
      docsResult.reload();
    } catch (failure) {
      if (generation !== identityGen.current) return;
      setSubmitError((failure as Error).message);
    } finally {
      if (generation === identityGen.current) {
        writeInFlight.current = false;
        setSubmitting(false);
      }
    }
  }

  const previewLinesCount = rawMarkdown ? rawMarkdown.replace(/\r\n?/g, '\n').split('\n').length : 0;
  const canSubmitWrite = canWrite && !submitting && (
    mode === 'new'
      ? Boolean(rawMarkdown.trim() && importService)
      : mode === 'version'
        ? Boolean(targetDocId && rawMarkdown.trim())
        : Boolean(targetDocId && switchVersionId.trim())
  );

  return <>
    <PageHeader
      title="运维知识库"
      description="按服务管理版本化 Markdown 运维手册（Runbook），支持关键词与向量混合检索及原文行号定位。"
      loading={docsResult.loading}
      reload={docsResult.reload}
      fetchedAt={docsResult.fetchedAt}
    />

    <div className="kb-grid">
      <div className="kb-grid-main">
        <Panel title="知识库检索" subtitle="在指定服务的已发布版本范围内检索手册切片；只读模式可直接使用">
          <form className="kb-search-form" onSubmit={handleSearch}>
            <div className="kb-search-row">
              <label className="kb-field">
                检索服务
                <select aria-label="检索服务" value={searchService} onChange={e => setSearchService(e.target.value)}>
                  {KNOWLEDGE_SERVICES.map(svc => <option key={svc} value={svc}>{svc}</option>)}
                </select>
              </label>
              <label className="kb-field kb-field-grow">
                查询内容
                <input
                  type="search"
                  aria-label="查询内容"
                  value={searchQuery}
                  onChange={e => setSearchQuery(e.target.value)}
                  maxLength={2000}
                  placeholder="输入故障现象、中文描述或错误码（如 HighHttp5xxRate、PG_CONN_REFUSED）"
                />
              </label>
            </div>
            <div className="kb-search-row">
              <label className="kb-field">
                检索方式
                <select
                  aria-label="检索方式"
                  value={searchMethod}
                  onChange={e => setSearchMethod(e.target.value as KnowledgeSearchMethod)}
                >
                  <option value="hybrid_rrf">{retrievalMethodLabels.hybrid_rrf}</option>
                  <option value="mysql_ngram">{retrievalMethodLabels.mysql_ngram}</option>
                  <option value="vector_cosine">{retrievalMethodLabels.vector_cosine}</option>
                </select>
              </label>
              <label className="kb-field" style={{ width: '110px' }}>
                返回条数
                <input
                  type="number"
                  aria-label="返回条数"
                  min={1}
                  max={20}
                  value={searchLimit}
                  onChange={e => setSearchLimit(Number(e.target.value))}
                />
              </label>
              <button type="submit" className="button kb-button-primary" disabled={searching || !searchQuery.trim()}>
                <Search size={15} aria-hidden="true" />
                {searching ? '正在检索…' : '检索手册'}
              </button>
            </div>
          </form>

          <div className="kb-search-results">
            {searchError && <p role="alert" className="operation-error">{searchError}</p>}

            {searchResponse?.status === 'unavailable' && (
              <div className="kb-warning" role="alert">
                <strong>知识库检索暂不可用</strong>
                <p style={{ margin: '4px 0 0' }}>{knowledgeReasonLabel(searchResponse.reason)}</p>
              </div>
            )}

            {searchResponse?.status === 'no_match' && (
              <EmptyState
                title="未命中匹配手册"
                description={`在服务 ${searchResponse.service} 的已发布手册中未检索到与当前查询匹配的切片。请尝试调整关键词或确认目标服务。`}
              />
            )}

            {searchResponse?.status === 'ok' && <>
              <div className="kb-search-summary" role="status">
                <span>
                  命中 <strong>{searchResponse.hits.length}</strong> 条切片 · 候选数 {searchResponse.candidate_count} · 策略 {retrievalMethodLabels[searchResponse.method] || searchResponse.method}
                </span>
                <span>手册仅作排查线索，不能替代实时监控观测</span>
              </div>
              <div className="kb-hit-list">
                {searchResponse.hits.map(hit => (
                  <article className="kb-hit-card" key={`${hit.version_id}-${hit.chunk_id}`}>
                    <div className="kb-hit-header">
                      <h3>{hit.title}</h3>
                      <div className="badge-group">
                        <StatusBadge value={hit.service} label={hit.service} tone="blue" />
                        {hit.exact_match && <StatusBadge value="exact_match" label="精确码命中" tone="green" />}
                      </div>
                    </div>
                    <div className="kb-hit-meta">
                      <span>章节：{hit.heading_path}</span>
                      <span>行范围：<code>L{hit.start_line}-L{hit.end_line}</code></span>
                      <span>得分：{hit.score.toFixed(4)}</span>
                      <span>版本：<code>{hit.version_id}</code></span>
                    </div>
                    <pre className="kb-hit-snippet">{hit.snippet}</pre>
                    <div className="kb-hit-actions">
                      <span className="full-id">{hit.citation}</span>
                      <Link
                        className="text-link"
                        to={knowledgeVersionPath(hit.document_id, hit.version_id, {
                          start: hit.start_line,
                          end: hit.end_line,
                          chunk: hit.chunk_id,
                        })}
                      >
                        查看原文定位
                        <ArrowUpRight size={14} aria-hidden="true" />
                      </Link>
                    </div>
                  </article>
                ))}
              </div>
            </>}
          </div>
        </Panel>

        <Panel title="手册文档列表" subtitle="展示各服务 Runbook 文档及其当前发布版本状态">
          <div className="kb-toolbar">
            <label className="status-filter">
              服务筛选
              <select
                aria-label="服务筛选"
                value={validServiceFilter}
                onChange={e => updateListParams(0, e.target.value, status)}
              >
                <option value="">全部</option>
                {KNOWLEDGE_SERVICES.map(svc => <option key={svc} value={svc}>{svc}</option>)}
              </select>
            </label>
            <label className="status-filter">
              发布状态
              <select
                aria-label="发布状态"
                value={status}
                onChange={e => updateListParams(0, validServiceFilter, e.target.value)}
              >
                <option value="">全部状态</option>
                <option value="published">已发布</option>
                <option value="unpublished">未发布</option>
              </select>
            </label>
            {docsResult.data && (
              <span className="search-hint" style={{ marginLeft: 'auto' }}>
                共 {docsResult.data.total} 份文档
              </span>
            )}
          </div>

          <DataState loading={docsResult.loading} error={docsResult.error} retry={docsResult.reload}>
            {documents.length ? (
              <div className="table-scroll" tabIndex={0} role="region" aria-label="知识库文档列表，可横向滚动">
                <table>
                  <caption className="sr-only">运维知识库文档列表</caption>
                  <thead>
                    <tr>
                      <th scope="col">标题 / 文档 ID</th>
                      <th scope="col">服务</th>
                      <th scope="col">来源</th>
                      <th scope="col">发布状态 / 版本</th>
                      <th scope="col">更新时间 · 北京时间</th>
                      <th scope="col">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {documents.map(doc => (
                      <tr key={doc.document_id}>
                        <td>
                          {doc.published_version_id ? (
                            <Link
                              className="record-link"
                              to={knowledgeVersionPath(doc.document_id, doc.published_version_id)}
                            >
                              {doc.title}
                            </Link>
                          ) : (
                            <span className="record-link">{doc.title}</span>
                          )}
                          <span className="record-id" title={doc.document_id}>{doc.document_id}</span>
                        </td>
                        <td><StatusBadge value={doc.service} label={doc.service} tone="blue" /></td>
                        <td><span className="full-id">{doc.source}</span></td>
                        <td>
                          <StatusBadge value={doc.published_status} />
                          <span className="record-id" title={doc.published_version_id || '尚未发布'}>
                            {doc.published_version_id || '无发布版本'}
                          </span>
                        </td>
                        <td className="time-cell"><TimeValue value={doc.updated_at} /></td>
                        <td>
                          <div className="kb-version-actions">
                            {doc.published_version_id && (
                              <Link
                                className="text-link"
                                to={knowledgeVersionPath(doc.document_id, doc.published_version_id)}
                              >
                                查看原文
                              </Link>
                            )}
                            <button
                              type="button"
                              className="button"
                              onClick={() => startVersionUploadFor(doc)}
                            >
                              更新版本
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyState
                title="暂无知识库文档"
                description="当前筛选条件下没有已保存的 Runbook 文档，可在右侧授权后导入 Markdown 手册。"
              />
            )}
          </DataState>
          {!docsResult.error && (
            <Pagination
              offset={offset}
              visibleCount={documents.length}
              loadedCount={documents.length}
              hasMore={docsResult.data?.has_more || false}
              loading={docsResult.loading}
              onPage={nextOffset => updateListParams(nextOffset)}
            />
          )}
        </Panel>
      </div>

      <aside className="kb-grid-side" aria-label="Markdown 导入与版本发布">
        <Panel title="Markdown 导入与版本管理" subtitle="写入与版本切换需后端授权 knowledge_write 权限">
          <OperatorAccess />
          <div className="kb-import-body">
            {!operator && (
              <p className="kb-permission-hint" role="note">
                当前处于只读模式。请先连接具备 <code>knowledge_write</code> 权限的操作身份后再导入或发布版本。
              </p>
            )}
            {operator && !canWrite && (
              <p className="kb-permission-hint" role="note">
                当前身份（{operator.actor}）缺少 <code>knowledge_write</code> 权限（Remediation 权限不可替代知识库写权限），写入操作已禁用。
              </p>
            )}

            <div className="kb-mode-tabs" role="tablist" aria-label="知识库写入模式">
              <button
                type="button"
                role="tab"
                aria-selected={mode === 'new'}
                className={mode === 'new' ? 'kb-mode-tab is-active' : 'kb-mode-tab'}
                onClick={() => { setMode('new'); setSubmitError(''); setSubmitResult(null); }}
              >
                导入新文档
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={mode === 'version'}
                className={mode === 'version' ? 'kb-mode-tab is-active' : 'kb-mode-tab'}
                onClick={() => { setMode('version'); setSubmitError(''); setSubmitResult(null); }}
              >
                上传新版本
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={mode === 'switch'}
                className={mode === 'switch' ? 'kb-mode-tab is-active' : 'kb-mode-tab'}
                onClick={() => { setMode('switch'); setSubmitError(''); setSubmitResult(null); }}
              >
                切换发布版本
              </button>
            </div>

            <form className="remediation-controls" style={{ padding: 0 }} onSubmit={handleImportSubmit}>
              {mode === 'new' && <>
                <label className="kb-field">
                  归属服务
                  <select aria-label="归属服务" value={importService} onChange={e => setImportService(e.target.value)}>
                    {KNOWLEDGE_SERVICES.map(svc => <option key={svc} value={svc}>{svc}</option>)}
                  </select>
                </label>
                <label className="kb-field">
                  文档标题
                  <input
                    type="text"
                    aria-label="文档标题"
                    maxLength={300}
                    value={importTitle}
                    onChange={e => setImportTitle(e.target.value)}
                    placeholder="选择文件后自动提取一级标题，也可手动填写"
                  />
                </label>
                <label className="kb-field">
                  来源标识
                  <input
                    type="text"
                    aria-label="来源标识"
                    maxLength={300}
                    value={importSource}
                    onChange={e => setImportSource(e.target.value)}
                    placeholder="例如 order_5xx_runbook.md"
                  />
                </label>
                <label className="kb-field">
                  文档 ID（可选）
                  <input
                    type="text"
                    aria-label="文档 ID（可选）"
                    maxLength={120}
                    value={importDocId}
                    onChange={e => setImportDocId(e.target.value)}
                    placeholder="留空则按服务与来源自动生成稳定 ID"
                  />
                </label>
              </>}

              {(mode === 'version' || mode === 'switch') && (
                <label className="kb-field">
                  目标文档
                  <select
                    aria-label="目标文档"
                    value={targetDocId}
                    onChange={e => {
                      const nextId = e.target.value;
                      setTargetDocId(nextId);
                      const doc = documents.find(item => item.document_id === nextId);
                      if (doc) {
                        setImportTitle(doc.title);
                        setImportSource(doc.source);
                      }
                    }}
                  >
                    {documents.length === 0 && <option value="">当前页暂无可选文档</option>}
                    {documents.map(doc => (
                      <option key={doc.document_id} value={doc.document_id}>
                        [{doc.service}] {doc.title} ({doc.document_id})
                      </option>
                    ))}
                  </select>
                </label>
              )}

              {mode === 'switch' && (
                <label className="kb-field">
                  选择发布版本
                  {selectedDoc?.versions?.length ? (
                    <select
                      aria-label="选择发布版本"
                      value={switchVersionId}
                      onChange={e => setSwitchVersionId(e.target.value)}
                    >
                      {selectedDoc.versions.map(ver => (
                        <option key={ver.version_id} value={ver.version_id}>
                          {ver.version_id} ({ver.status})
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      type="text"
                      aria-label="选择发布版本"
                      value={switchVersionId}
                      onChange={e => setSwitchVersionId(e.target.value)}
                      placeholder="输入待发布的 version_id"
                    />
                  )}
                </label>
              )}

              {(mode === 'new' || mode === 'version') && <>
                <label className="kb-field">
                  选择 Markdown 文件
                  <input
                    type="file"
                    aria-label="选择 Markdown 文件"
                    accept=".md,text/markdown"
                    onChange={handleFileSelect}
                  />
                </label>

                {fileError && <p role="alert" className="operation-error">{fileError}</p>}

                {rawMarkdown && (
                  <div className="kb-preview-box" aria-label="Markdown 预览">
                    <div className="kb-preview-header">
                      <span><FileText size={13} aria-hidden="true" /> 预览：{selectedFileName || '已选内容'}</span>
                      <span>{previewLinesCount} 行 · {fileSizeBytes} 字节</span>
                    </div>
                    <pre className="kb-preview-code">{rawMarkdown}</pre>
                  </div>
                )}

                <label className="kb-checkbox">
                  <input
                    type="checkbox"
                    checked={publishNow}
                    onChange={e => setPublishNow(e.target.checked)}
                  />
                  导入完成后立即设为当前发布版本
                </label>
              </>}

              <button type="submit" className="button kb-button-primary" disabled={!canSubmitWrite}>
                <Upload size={15} aria-hidden="true" />
                {submitting
                  ? '正在提交…'
                  : mode === 'new'
                    ? (publishNow ? '导入并发布文档' : '导入为草稿')
                    : mode === 'version'
                      ? (publishNow ? '上传并发布新版本' : '上传为草稿版本')
                      : '切换为当前发布版本'}
              </button>
            </form>

            {submitError && <p role="alert" className="operation-error">{submitError}</p>}

            {submitResult && (
              <div className="kb-notice" role="status">
                <strong>{submitResult.idempotent ? '幂等命中已有版本（内容未变化）' : '知识库版本写入成功'}</strong>
                <p style={{ margin: '4px 0 0' }}>
                  文档 <code>{submitResult.document.document_id}</code> · 版本 <code>{submitResult.version.version_id}</code> · 共 {submitResult.chunk_count} 个切片。{' '}
                  <Link to={knowledgeVersionPath(submitResult.document.document_id, submitResult.version.version_id)}>
                    查看版本原文
                  </Link>
                </p>
              </div>
            )}
          </div>
        </Panel>
      </aside>
    </div>
  </>;
}

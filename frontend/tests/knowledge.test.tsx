import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { getJson, postJson } from '../src/api/client';
import {
  detail,
  json,
  knowledgeDocument,
  knowledgeDocumentPage,
  knowledgeImportResponse,
  knowledgeSearchOk,
  knowledgeVersionDetail,
  runbookCitation,
} from './fixtures';

function open(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

async function connectOperator(token = 'knowledge-operator-token-0123456789abcdef') {
  fireEvent.change(await screen.findByLabelText('本地操作凭据'), { target: { value: token } });
  fireEvent.click(screen.getByRole('button', { name: '连接操作身份' }));
  await screen.findByRole('button', { name: '断开操作身份' });
}

it('navigates to knowledge workbench, lists documents, filters by service, and links published version', async () => {
  const fetchMock = vi.fn(async (url: string) => {
    const parsed = new URL(url);
    if (parsed.pathname === '/api/knowledge/documents') {
      const svc = parsed.searchParams.get('service');
      if (svc === 'order-db') {
        return json({ items: [], total: 0, limit: 20, offset: 0, has_more: false });
      }
      return json(knowledgeDocumentPage);
    }
    return json({ detail: 'not_found' }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);

  open('/knowledge');

  expect(screen.getByRole('link', { name: '知识库' })).toHaveAttribute('href', '/knowledge');
  expect(screen.getByText('写操作需后端授权')).toBeVisible();

  const table = await screen.findByRole('table');
  expect(within(table).getByText('订单服务 HTTP 5xx 排障手册')).toBeVisible();
  expect(within(table).getByText('doc-order-5xx-001')).toBeVisible();
  expect(within(table).getByText('order_service_5xx_runbook.md')).toBeVisible();
  expect(within(table).getByText('ver-order-5xx-v2')).toBeVisible();
  expect(within(table).getByRole('link', { name: '查看原文' })).toHaveAttribute(
    'href',
    '/knowledge/documents/doc-order-5xx-001/versions/ver-order-5xx-v2',
  );

  fireEvent.change(screen.getByRole('combobox', { name: '服务筛选' }), {
    target: { value: 'order-db' },
  });
  expect(await screen.findByText('暂无知识库文档')).toBeVisible();
  expect(fetchMock.mock.calls.at(-1)?.[0]).toContain('/api/knowledge/documents?limit=20&offset=0&service=order-db');
});

it('previews selected local .md file safely before submit and enforces knowledge_write permission gating', async () => {
  let operatorPayload = { actor: 'remediation-only', permissions: ['request', 'approve', 'execute'] };
  const fetchMock = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/api/operator')) return json(operatorPayload);
    if (url.includes('/api/knowledge/documents') && options?.method === 'POST') {
      return json(knowledgeImportResponse);
    }
    if (url.includes('/api/knowledge/documents')) return json(knowledgeDocumentPage);
    return json({ detail: 'not_found' }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);

  const { container } = open('/knowledge');
  await screen.findByText('订单服务 HTTP 5xx 排障手册');

  // Read-only before connecting operator
  expect(screen.getByText(/当前处于只读模式/)).toBeVisible();
  const submitButton = screen.getByRole('button', { name: '导入并发布文档' });
  expect(submitButton).toBeDisabled();

  // Select a local .md file and verify preview renders inertly before submitting
  const mdText = '# 订单超时排障指南\n\n<script>alert(1)</script>\n\n排查 ERR_ORDER_TIMEOUT 原因。';
  const file = new File([mdText], 'order_timeout.md', { type: 'text/markdown' });
  const fileInput = screen.getByLabelText('选择 Markdown 文件');
  fireEvent.change(fileInput, { target: { files: [file] } });

  const preview = await screen.findByLabelText('Markdown 预览');
  expect(within(preview).getByText(/order_timeout\.md/)).toBeVisible();
  expect(within(preview).getByText(/排查 ERR_ORDER_TIMEOUT 原因。/)).toBeVisible();
  expect(within(preview).getByText(/<script>alert\(1\)<\/script>/)).toBeVisible();
  expect(container.querySelector('script')).toBeNull();
  expect(screen.getByLabelText('文档标题')).toHaveValue('订单超时排障指南');
  expect(screen.getByLabelText('来源标识')).toHaveValue('order_timeout.md');
  expect(submitButton).toBeDisabled();

  // Operator with Remediation permissions only is still blocked from writing knowledge
  await connectOperator();
  expect(screen.getByText(/缺少/)).toBeVisible();
  expect(screen.getByText(/Remediation 权限不可替代知识库写权限/)).toBeVisible();
  expect(submitButton).toBeDisabled();
  expect(fetchMock.mock.calls.filter(([, opts]) => opts?.method === 'POST')).toHaveLength(0);

  // Switch to operator with knowledge_write permission
  fireEvent.click(screen.getByRole('button', { name: '断开操作身份' }));
  operatorPayload = { actor: 'kb-editor', permissions: ['knowledge_write'] };
  await connectOperator('knowledge-writer-token-0123456789abcdef01');

  expect(submitButton).toBeEnabled();
  fireEvent.click(submitButton);

  expect(await screen.findByText('知识库版本写入成功')).toBeVisible();
  const postCall = fetchMock.mock.calls.find(([, opts]) => opts?.method === 'POST');
  expect(postCall?.[0]).toBe('http://127.0.0.1:8010/api/knowledge/documents');
  expect(postCall?.[1]?.headers).toEqual(
    expect.objectContaining({ Authorization: 'Bearer knowledge-writer-token-0123456789abcdef01' }),
  );
  expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({
    service: 'order-service',
    title: '订单超时排障指南',
    source: 'order_timeout.md',
    raw_markdown: mdText,
    publish: true,
  });
});

it('supports switching an existing published version and displaying idempotent responses', async () => {
  const fetchMock = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/api/operator')) {
      return json({ actor: 'kb-editor', permissions: ['knowledge_write'] });
    }
    if (url.includes('/api/knowledge/documents/doc-order-5xx-001/versions') && options?.method === 'POST') {
      return json({
        ...knowledgeImportResponse,
        version: knowledgeDocument.versions[0],
        idempotent: true,
      });
    }
    if (url.includes('/api/knowledge/documents')) return json(knowledgeDocumentPage);
    return json({ detail: 'not_found' }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);

  open('/knowledge');
  await screen.findByText('订单服务 HTTP 5xx 排障手册');
  await connectOperator();

  fireEvent.click(screen.getByRole('tab', { name: '切换发布版本' }));
  const versionSelect = screen.getByRole('combobox', { name: '选择发布版本' });
  expect(versionSelect).toHaveValue('ver-order-5xx-v1');

  fireEvent.click(screen.getByRole('button', { name: '切换为当前发布版本' }));
  expect(await screen.findByText('幂等命中已有版本（内容未变化）')).toBeVisible();

  const postCall = fetchMock.mock.calls.find(([, opts]) => opts?.method === 'POST');
  expect(postCall?.[0]).toBe('http://127.0.0.1:8010/api/knowledge/documents/doc-order-5xx-001/versions');
  expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({ publish_version_id: 'ver-order-5xx-v1' });
});

it('uploads a new markdown version for an existing document from the document list action', async () => {
  const fetchMock = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/api/operator')) {
      return json({ actor: 'kb-editor', permissions: ['knowledge_write'] });
    }
    if (url.includes('/api/knowledge/documents/doc-order-5xx-001/versions') && options?.method === 'POST') {
      return json({
        ...knowledgeImportResponse,
        version: { ...knowledgeImportResponse.version, version_id: 'ver-order-5xx-v3' },
        chunk_count: 4,
      });
    }
    if (url.includes('/api/knowledge/documents')) return json(knowledgeDocumentPage);
    return json({ detail: 'not_found' }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);

  open('/knowledge');
  await screen.findByText('订单服务 HTTP 5xx 排障手册');
  await connectOperator();

  fireEvent.click(screen.getByRole('button', { name: '更新版本' }));
  expect(screen.getByRole('tab', { name: '上传新版本' })).toHaveAttribute('aria-selected', 'true');

  const newMd = '# 订单服务 HTTP 5xx 排障手册 v3\n\n新增数据库连接池排查项。';
  const file = new File([newMd], 'order_5xx_v3.md', { type: 'text/markdown' });
  fireEvent.change(screen.getByLabelText('选择 Markdown 文件'), { target: { files: [file] } });
  expect(await screen.findByText(/新增数据库连接池排查项/)).toBeVisible();

  fireEvent.click(screen.getByRole('button', { name: '上传并发布新版本' }));
  expect(await screen.findByText('知识库版本写入成功')).toBeVisible();
  expect(screen.getByText('ver-order-5xx-v3')).toBeVisible();

  const postCall = fetchMock.mock.calls.find(([, opts]) => opts?.method === 'POST');
  expect(postCall?.[0]).toBe('http://127.0.0.1:8010/api/knowledge/documents/doc-order-5xx-001/versions');
  expect(JSON.parse(String(postCall?.[1]?.body))).toMatchObject({
    raw_markdown: newMd,
    publish: true,
  });
});

it('shows historical version notice with link to published version and keeps raw markdown HTML inert', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      json({
        ...knowledgeVersionDetail,
        version_id: 'ver-order-5xx-v1',
        status: 'archived',
        published_version_id: 'ver-order-5xx-v2',
        raw_markdown: '# 旧版手册\n<img src=x onerror=alert(1)>',
      }),
    ),
  );

  const { container } = open('/knowledge/documents/doc-order-5xx-001/versions/ver-order-5xx-v1');
  expect(await screen.findByText(/当前查看的是历史或草稿版本/)).toBeVisible();
  expect(screen.getByRole('link', { name: 'ver-order-5xx-v2' })).toHaveAttribute(
    'href',
    '/knowledge/documents/doc-order-5xx-001/versions/ver-order-5xx-v2',
  );
  expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeVisible();
  expect(container.querySelector('img, script')).toBeNull();
});

it('renders GET /api/knowledge/search ok, no_match, and unavailable states distinctly in read-only mode', async () => {
  let mode: 'ok' | 'no_match' | 'unavailable' = 'ok';
  const fetchMock = vi.fn(async (url: string, _options?: RequestInit) => {
    if (url.includes('/api/knowledge/documents')) return json(knowledgeDocumentPage);
    if (url.includes('/api/knowledge/search')) {
      if (mode === 'ok') return json(knowledgeSearchOk);
      if (mode === 'no_match') {
        return json({
          status: 'no_match',
          service: 'order-service',
          query: 'UNKNOWN_XYZ_999',
          method: 'hybrid_rrf',
          hits: [],
          candidate_count: 0,
          reason: null,
        });
      }
      return json({
        status: 'unavailable',
        service: 'order-service',
        query: 'HighHttp5xxRate',
        method: 'hybrid_rrf',
        hits: [],
        candidate_count: 0,
        reason: 'embedding_unavailable',
      });
    }
    return json({ detail: 'not_found' }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);

  open('/knowledge');
  await screen.findByText('订单服务 HTTP 5xx 排障手册');

  // 1) ok state (uses GET in read-only mode without auth header)
  fireEvent.change(screen.getByRole('searchbox', { name: '查询内容' }), {
    target: { value: 'HighHttp5xxRate' },
  });
  fireEvent.change(screen.getByRole('spinbutton', { name: '返回条数' }), {
    target: { value: '5' },
  });
  fireEvent.click(screen.getByRole('button', { name: '检索手册' }));

  const jumpLink = await screen.findByRole('link', { name: '查看原文定位' });
  expect(jumpLink).toHaveAttribute(
    'href',
    '/knowledge/documents/doc-order-5xx-001/versions/ver-order-5xx-v2?start=4&end=7&chunk=chk-order-5xx-02',
  );
  expect(screen.getByText('精确码命中')).toBeVisible();
  expect(screen.getByText('L4-L7')).toBeVisible();
  expect(screen.getByText('0.0528', { exact: false })).toBeVisible();
  const searchCall = fetchMock.mock.calls.find(([url]) => String(url).includes('/api/knowledge/search'));
  expect(searchCall?.[1]?.method).toBe('GET');
  expect(searchCall?.[1]?.credentials).toBe('omit');

  // 2) no_match state
  mode = 'no_match';
  fireEvent.change(screen.getByRole('searchbox', { name: '查询内容' }), {
    target: { value: 'UNKNOWN_XYZ_999' },
  });
  fireEvent.click(screen.getByRole('button', { name: '检索手册' }));
  expect(await screen.findByText('未命中匹配手册')).toBeVisible();
  expect(screen.queryByText('知识库检索暂不可用')).not.toBeInTheDocument();

  // 3) unavailable state
  mode = 'unavailable';
  fireEvent.change(screen.getByRole('searchbox', { name: '查询内容' }), {
    target: { value: 'HighHttp5xxRate' },
  });
  fireEvent.click(screen.getByRole('button', { name: '检索手册' }));
  const alert = await screen.findByRole('alert');
  expect(alert).toHaveTextContent('知识库检索暂不可用');
  expect(alert).toHaveTextContent('向量化服务暂不可用');
  expect(screen.queryByText('未命中匹配手册')).not.toBeInTheDocument();
});

it('highlights requested line range from query params on KnowledgeVersionPage and updates on chunk click', async () => {
  const scrollIntoView = vi.fn();
  Element.prototype.scrollIntoView = scrollIntoView;
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => json(knowledgeVersionDetail)),
  );

  const { container } = open(
    '/knowledge/documents/doc-order-5xx-001/versions/ver-order-5xx-v2?start=4&end=7&chunk=chk-order-5xx-02',
  );

  expect(await screen.findByText('2'.repeat(64))).toBeVisible();
  expect(screen.getByText('3 个')).toBeVisible();
  expect(screen.getByText('当前发布版本', { selector: '.badge' })).toBeVisible();

  const highlighted = Array.from(container.querySelectorAll('.kb-line.is-highlighted')).map(el =>
    Number(el.getAttribute('data-line')),
  );
  expect(highlighted).toEqual([4, 5, 6, 7]);
  expect(container.querySelector('#kb-line-3')).not.toHaveClass('is-highlighted');
  expect(container.querySelector('#kb-line-8')).not.toHaveClass('is-highlighted');
  await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());

  // Click another chunk in the outline to jump highlight to L9-L10
  fireEvent.click(screen.getByRole('button', { name: /恢复核对/ }));
  const nextHighlighted = Array.from(container.querySelectorAll('.kb-line.is-highlighted')).map(el =>
    Number(el.getAttribute('data-line')),
  );
  expect(nextHighlighted).toEqual([9, 10]);
});

it('displays structured Runbook citation links on SessionDetailPage and navigates to exact highlighted version lines', async () => {
  Element.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.includes('/api/sessions/ses-fixture')) {
        return json({ ...detail, runbook_citations: [runbookCitation] });
      }
      if (url.includes('/api/knowledge/documents/doc-order-5xx-001/versions/ver-order-5xx-v2')) {
        return json(knowledgeVersionDetail);
      }
      return json({ detail: 'not_found' }, 404);
    }),
  );

  const { container } = open('/sessions/ses-fixture');

  expect(await screen.findByText('手册引用（辅助线索，非实时状态）')).toBeVisible();
  const citationLink = screen.getByRole('link', { name: '查看手册原文（L4-L7）' });
  expect(citationLink).toHaveAttribute(
    'href',
    '/knowledge/documents/doc-order-5xx-001/versions/ver-order-5xx-v2?start=4&end=7&chunk=chk-order-5xx-02',
  );
  expect(screen.getByText('doc-order-5xx-001@ver-order-5xx-v2#L4-L7')).toBeVisible();

  // Clicking the citation link opens KnowledgeVersionPage with lines 4..7 highlighted
  fireEvent.click(citationLink);
  expect(await screen.findByText('Markdown 原文')).toBeVisible();
  const highlighted = Array.from(container.querySelectorAll('.kb-line.is-highlighted')).map(el =>
    Number(el.getAttribute('data-line')),
  );
  expect(highlighted).toEqual([4, 5, 6, 7]);
});

it('validates knowledge pagination responses and maps knowledge error codes safely in client.ts', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(json({ items: [], limit: 20, offset: 0, has_more: false })),
  );
  await expect(getJson('/api/knowledge/documents', new AbortController().signal)).rejects.toThrow(
    'API 分页响应格式不正确',
  );

  const cases: [number, string, string][] = [
    [503, 'knowledge_unavailable', '运维知识库暂不可用'],
    [503, 'knowledge_schema_not_migrated', '数据表尚未完成迁移'],
    [503, 'embedding_unavailable', '向量化服务暂不可用'],
    [404, 'knowledge_not_found', '没有找到指定的知识库文档或版本'],
    [422, 'unknown_service', '未在 ServiceRegistry 中注册'],
    [422, 'knowledge_validation_failed', '校验未通过'],
  ];
  for (const [status, code, expectedMessage] of cases) {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: code }, status)));
    await expect(
      postJson('/api/knowledge/documents', 'token-test', {}, new AbortController().signal),
    ).rejects.toThrow(expectedMessage);
  }
});

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { detail, incident, json, page, session } from './fixtures';
import type { HumanRequestStatus } from '../src/api/types';

function open(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
}

it('shows bounded overview counts and derives activity only from API records', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url.includes('/incidents')
    ? json(page([incident], true, 0, 100)) : json(page([session], false, 0, 100))));
  open('/');
  expect(screen.getAllByText('正在读取数据').length).toBeGreaterThan(0);
  expect(await screen.findByText('1+')).toBeInTheDocument();
  expect(screen.getByText(/“\+”表示还有未加载记录/)).toBeInTheDocument();
  const activity = screen.getByRole('list');
  expect(within(activity).getByText('已完成')).toBeInTheDocument();
  expect(within(activity).getByText('模型 2 / 工具 4')).toBeInTheDocument();
  expect(screen.getByText(/完整状态变更历史暂未提供/)).toBeInTheDocument();
});

it('shows empty data and API failure as distinct states, and retries on request', async () => {
  const fetch = vi.fn().mockRejectedValue(new Error('private connection text'));
  vi.stubGlobal('fetch', fetch);
  open('/incidents');
  expect(await screen.findByRole('alert')).toHaveTextContent('无法连接本地 API');
  expect(screen.queryByText('暂无 Incident')).not.toBeInTheDocument();
  expect(screen.queryByText('private connection text')).not.toBeInTheDocument();
  fetch.mockImplementation(async () => json(page([])));
  fireEvent.click(screen.getByRole('button', { name: '重新加载' }));
  expect(await screen.findByText('暂无 Incident')).toBeInTheDocument();
});

it('paginates server results, searches only the current page, and resets offset on filter change', async () => {
  const fetch = vi.fn(async (url: string) => {
    const query = new URL(url).searchParams;
    return json(page([{ ...incident, service: query.get('offset') === '20' ? 'next-page-service' : incident.service }], true, Number(query.get('offset')), 20));
  });
  vi.stubGlobal('fetch', fetch);
  open('/incidents');
  await screen.findByText('fixture-service');
  fireEvent.change(screen.getByRole('textbox', { name: '搜索当前页' }), { target: { value: 'no-match' } });
  expect(screen.getByText('当前页没有匹配记录')).toBeInTheDocument();
  expect(fetch).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  await screen.findByText('next-page-service');
  expect(fetch.mock.calls.at(-1)![0]).toContain('offset=20');
  fireEvent.change(screen.getByRole('combobox', { name: '状态' }), { target: { value: 'resolved' } });
  await screen.findByText('fixture-service');
  expect(fetch.mock.calls.at(-1)![0]).toContain('offset=0&status=resolved');
});

it('lists real session counters and retains every distinct lifecycle filter', async () => {
  const fetch = vi.fn().mockResolvedValue(json(page([session])));
  vi.stubGlobal('fetch', fetch);
  open('/sessions?status=WAITING_HUMAN');
  const table = await screen.findByRole('table');
  expect(within(table).getByText('2')).toBeInTheDocument();
  expect(within(table).getByText('4')).toBeInTheDocument();
  expect(screen.getByRole('option', { name: '已暂停' })).toBeInTheDocument();
  expect(screen.getByRole('option', { name: '预算耗尽' })).toBeInTheDocument();
  expect(screen.getByRole('option', { name: '已取消' })).toBeInTheDocument();
  expect(fetch.mock.calls[0][0]).toContain('status=WAITING_HUMAN');
});

it('keeps missing observations distinct from zero and links retained investigations', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(incident)));
  open('/incidents/inc-fixture');
  expect(await screen.findByText('FixtureAlert')).toBeInTheDocument();
  const row = screen.getByText('FixtureAlert').closest('tr')!;
  expect(within(row).getByText('无数据')).toBeInTheDocument();
  expect(within(row).getByText('0')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: '查看 Session' })).toHaveAttribute('href', '/sessions/ses-fixture');
  expect(screen.getByText(/回答已被 API 截断/)).toBeInTheDocument();
});

it('renders answers as text, shares Incident budget, and does not infer proposal approval', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(detail)));
  const { container } = open('/sessions/ses-fixture');
  expect(await screen.findByText('<img src=x onerror=alert(1)> 纯文本调查结论')).toBeInTheDocument();
  expect(container.querySelector('img')).toBeNull();
  expect(screen.getByText('投影已阻塞')).toBeInTheDocument();
  expect(screen.getByText('fixture_evidence_gap')).toBeInTheDocument();
  expect(screen.getByText('无待处理任务')).toBeInTheDocument();
  expect(screen.getByText(/不代表已生成或获批修复提案/)).toBeInTheDocument();
  const budget = screen.getByText('模型请求').closest('.budget-item')!;
  expect(budget.textContent).toContain('7 / 11');
  expect(screen.getByText('此额度未开放')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: '关联 Incident' })).toHaveAttribute('href', '/incidents/inc-fixture');
  expect(screen.queryByRole('button', { name: /执行|批准|恢复|取消/ })).toBeNull();
});

it('displays absent final answers without substituting a conclusion', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ ...detail, investigation_result: null, session: { ...session, status: 'RUNNING' } })));
  open('/sessions/ses-fixture');
  expect(await screen.findByText('尚无最终回答')).toBeInTheDocument();
});

it('handles unknown records and direct links with safe errors', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: 'session_not_found' }, 404)));
  open('/sessions/ses-missing');
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('没有找到这条记录'));
  expect(screen.getByRole('link', { name: '返回 Sessions' })).toBeInTheDocument();
});

it('shows loading counts until successful empty responses establish real zero counts', async () => {
  const resolveRequests: ((response: Response) => void)[] = [];
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => resolveRequests.push(resolve))));
  open('/');
  const cards = ['Incident 总数', '运行中的调查', '等待人类回复', '已完成的调查']
    .map(label => screen.getByRole('region', { name: label }));
  for (const card of cards) {
    expect(card).toHaveAttribute('aria-busy', 'true');
    expect(within(card).getByText('正在加载')).toBeVisible();
    expect(within(card).queryByText('0')).not.toBeInTheDocument();
  }
  await act(async () => { resolveRequests.forEach(resolve => resolve(json(page([])))); });
  for (const card of cards) {
    expect(card).toHaveAttribute('aria-busy', 'false');
    expect(within(card).getByText('0')).toBeVisible();
    expect(within(card).queryByText('读取失败')).not.toBeInTheDocument();
  }
  expect(screen.getByText('尚无 Incident 记录')).toBeVisible();
  expect(screen.getAllByText('尚无 Session 记录')).toHaveLength(3);
});

it.each([500, 503])('marks failed overview counts explicitly on HTTP %s and reloads through the existing refresh', async status => {
  const fetch = vi.fn().mockResolvedValue(json({ detail: 'private-store-error' }, status));
  vi.stubGlobal('fetch', fetch);
  open('/');
  await waitFor(() => expect(screen.getAllByText('读取失败')).toHaveLength(4));
  expect(screen.queryByText('数据尚未读取')).not.toBeInTheDocument();
  expect(screen.queryByText('private-store-error')).not.toBeInTheDocument();
  expect(screen.queryByText('0')).not.toBeInTheDocument();
  expect(screen.getAllByRole('alert').length).toBeGreaterThan(0);
  fetch.mockImplementation(async () => json(page([])));
  fireEvent.click(screen.getByRole('button', { name: '刷新数据' }));
  expect(screen.getAllByText('正在加载')).toHaveLength(4);
  await waitFor(() => expect(screen.getAllByText('0')).toHaveLength(4));
  expect(screen.queryByText('读取失败')).not.toBeInTheDocument();
  expect(fetch).toHaveBeenCalledTimes(4);
});

it.each(['incidents', 'sessions'])('keeps healthy overview counts when only %s fail', async failedStore => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url.includes('/' + failedStore)
    ? json({ detail: 'store_unavailable' }, 503)
    : json(page(url.includes('/incidents') ? [incident] : [session]))));
  open('/');
  await waitFor(() => expect(screen.getAllByText('读取失败')).toHaveLength(failedStore === 'incidents' ? 1 : 3));
  const card = screen.getByRole('region', { name: failedStore === 'incidents' ? '已完成的调查' : 'Incident 总数' });
  expect(within(card).getByText('1')).toBeVisible();
  expect(within(card).queryByText('读取失败')).not.toBeInTheDocument();
});

it.each(['/incidents', '/sessions'])('labels current-page search and counts visible versus loaded rows accurately on %s', async path => {
  const records = ['alpha', 'beta', 'gamma'].map(service => path === '/incidents'
    ? { ...incident, service, incident_id: 'inc-' + service }
    : { ...session, service, session_id: 'ses-' + service });
  const fetch = vi.fn().mockResolvedValue(json(page(records)));
  vi.stubGlobal('fetch', fetch);
  open(path);
  await screen.findByText('alpha');
  expect(screen.getByText('搜索当前页')).toBeVisible();
  expect(screen.getByText('仅筛选本页已加载记录')).toBeVisible();
  const search = screen.getByRole('textbox', { name: '搜索当前页' });
  expect(search).toHaveAccessibleDescription('仅筛选本页已加载记录');
  expect(screen.getByRole('status')).toHaveTextContent('显示 3 条 / 本页已加载 3 条');
  fireEvent.change(search, { target: { value: 'alpha' } });
  expect(screen.getByRole('status')).toHaveTextContent('显示 1 条 / 本页已加载 3 条');
  expect(screen.queryByText('beta')).not.toBeInTheDocument();
  fireEvent.change(search, { target: { value: 'missing' } });
  expect(screen.getByRole('status')).toHaveTextContent('显示 0 条 / 本页已加载 3 条');
  expect(screen.getByText('当前页没有匹配记录')).toBeVisible();
  fireEvent.change(search, { target: { value: '' } });
  expect(screen.getByRole('status')).toHaveTextContent('显示 3 条 / 本页已加载 3 条');
  expect(fetch).toHaveBeenCalledTimes(1);
});

it('displays every human request state as read-only text without accepting HTML or implying approval', async () => {
  const statuses: HumanRequestStatus[] = ['pending', 'answered', 'consumed', 'expired', 'cancelled'];
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ ...detail, human_requests: statuses.map(status => ({
    request_id: 'req-' + status, status, question: '<script>alert(1)</script> ' + status,
    created_at: session.created_at, expires_at: session.updated_at,
  })) })));
  const { container } = open('/sessions/ses-fixture');
  expect(await screen.findByText('等待回复')).toBeVisible();
  for (const label of ['已回复', '已消费', '已过期', '已取消']) expect(screen.getByText(label)).toBeVisible();
  for (const status of statuses) expect(screen.getByText('<script>alert(1)</script> ' + status)).toBeVisible();
  expect(container.querySelector('script')).toBeNull();
  expect(screen.getByText(/回复与正式修复审批通过各自的原有流程处理/)).toBeVisible();
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /批准|执行|提交回复/ })).not.toBeInTheDocument();
  expect(screen.getByRole('progressbar', { name: '模型请求' })).toHaveAttribute('aria-valuenow', '7');
  expect(screen.getByRole('progressbar', { name: '模型请求' })).toHaveAttribute('aria-valuemax', '11');
});

it('formats real Markdown conclusions while keeping HTML inert and excluding images and navigable model links', async () => {
  const answer = '# 调查结论\n\n确认 **请求失败**。\n\n- 第一项证据\n- 第二项证据\n\n```json\n{"status":"observed"}\n```\n\n[外部内容](https://external.example)\n\n[危险链接](javascript:alert(1))\n\n![远程图片](https://external.example/track.png)\n\n<iframe src="https://external.example"></iframe>\n\n<script>alert(1)</script>';
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ ...detail,
    investigation_result: { ...detail.investigation_result, answer },
  })));
  const { container } = open('/sessions/ses-fixture');
  expect(await screen.findByRole('heading', { level: 3, name: '调查结论' })).toBeVisible();
  expect(screen.getByText('请求失败').tagName).toBe('STRONG');
  expect(screen.getByText('第一项证据').closest('li')).not.toBeNull();
  expect(screen.getByText('{"status":"observed"}').closest('pre')).not.toBeNull();
  expect(screen.getByText('外部内容')).toBeVisible();
  expect(screen.getByText('危险链接')).toBeVisible();
  expect(container.querySelector('.markdown-text a, img, iframe, script')).toBeNull();
  expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
});

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { detail, incident, json, page, session } from './fixtures';

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
  expect(screen.getByText('已完成 · 模型 2 / 工具 4')).toBeInTheDocument();
  expect(screen.getByText(/完整状态变更历史暂未提供/)).toBeInTheDocument();
});

it('shows empty data and API failure as distinct states, and retries on request', async () => {
  const fetch = vi.fn().mockRejectedValue(new Error('private connection text'));
  vi.stubGlobal('fetch', fetch);
  open('/incidents');
  expect(await screen.findByRole('alert')).toHaveTextContent('无法连接本地 API');
  expect(screen.queryByText('暂无 Incident')).not.toBeInTheDocument();
  expect(screen.queryByText('private connection text')).not.toBeInTheDocument();
  fetch.mockResolvedValue(json(page([])));
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

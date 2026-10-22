import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import { App } from '../src/App';
import type { RemediationDetail } from '../src/api/types';
import { json } from './fixtures';

const fixture: RemediationDetail = {
  proposal_id: 'rem-fixture', incident_id: 'inc-fixture', service: 'fixture-service',
  summary: 'Fixture repair', action_type: 'fixture_action', risk: 'low', status: 'verification_failed',
  created_at: '2026-10-22T01:00:00Z', execution_outcome: 'executed', original_proposal_id: null,
  reason: 'Fixture evidence', parameters: { service: 'fixture-service' }, hidden_parameter_count: 0,
  proposal_digest: 'a'.repeat(64), expected_commit_sha: 'b'.repeat(40),
  approved_by: 'original-approver', approved_at: '2026-10-22T01:01:00Z', approval_valid: true,
  rejected_by: null, rejected_at: null, rejection_reason: null, closure_reason: null,
  compensation: { status: 'blocked', reason: 'safe_compensation_target_not_registered', target: null, compensation_proposal_id: null },
  target: null, related_proposals: [], actions: [], verifications: [], requests: [], audit: [],
};

function open(record = fixture, identity = { actor: 'fixture-operator', permissions: ['request', 'approve', 'execute'] }) {
  const fetch = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/api/operator')) return json(identity);
    return json(record);
  });
  vi.stubGlobal('fetch', fetch);
  render(<MemoryRouter initialEntries={['/remediations/rem-fixture']}><App /></MemoryRouter>);
  return fetch;
}

async function connect() {
  fireEvent.change(await screen.findByLabelText('本地操作凭据'), { target: { value: 'fixture-token'.repeat(4) } });
  fireEvent.click(screen.getByRole('button', { name: '连接操作身份' }));
  await screen.findByRole('button', { name: '断开操作身份' });
}

it('shows safe target blocking, original links and independent verification without fabricating recovery', async () => {
  open({ ...fixture, related_proposals: [{ ...fixture, proposal_id: 'rem-compensation', original_proposal_id: fixture.proposal_id, status: 'pending' }],
    verifications: [{ verification_id: 'verify-1', verified_at: fixture.created_at, purpose: 'post_action', outcome: 'no_data', evidence_status: 'mismatch', historical_success: false, action_id: null, checks: [] }] });
  expect(await screen.findByText('没有注册经过验证的安全补偿目标。不会重开故障开关或使用监控配置快照回滚。')).toBeVisible();
  expect(screen.getByRole('link', { name: 'rem-compensation' })).toHaveAttribute('href', '/remediations/rem-compensation');
  expect(screen.getByText(/验证无数据/)).toBeVisible();
  expect(screen.queryByRole('button', { name: '执行已批准动作' })).not.toBeInTheDocument();
  expect(screen.getByText('请连接操作身份后申请、审批或执行。')).toBeVisible();
  if (process.env.COMPENSATION_PREVIEW_FILE) {
    const { readFileSync, writeFileSync } = await import('node:fs');
    const { resolve } = await import('node:path');
    const css = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8');
    writeFileSync(process.env.COMPENSATION_PREVIEW_FILE,
      `<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>离线测试快照 · Remediation</title><style>${css}</style><body>${document.body.innerHTML}</body></html>`);
  }
});

it.each([
  ['pending', 'executed', '待审批'], ['rejected', 'executed', '已拒绝'], ['executing', 'executed', '执行中 · 等待结果'],
  ['executing', 'unknown', '结果未知 · 需要核查'], ['failed', 'executed', '补偿动作失败'],
  ['verification_failed', 'executed', '补偿后独立验证未通过'], ['succeeded', 'executed', '流程已完成'],
] as const)('renders remediation state %s / %s accurately', async (status, execution_outcome, label) => {
  open({ ...fixture, status, execution_outcome, original_proposal_id: 'rem-original' });
  expect(await screen.findByText(label, { selector: '.badge' })).toBeVisible();
  expect(screen.getByRole('link', { name: 'rem-original' })).toHaveAttribute('href', '/remediations/rem-original');
});

it('authenticates using a header, displays insufficient permissions, and never sends a trusted body role', async () => {
  const fetch = open({ ...fixture, status: 'pending', approval_valid: false }, { actor: 'request-only', permissions: ['request'] });
  await connect();
  expect(screen.getByText('当前身份无审批权限。')).toBeVisible();
  expect(screen.getByRole('button', { name: '批准此提案' })).toBeDisabled();
  const options = fetch.mock.calls.find(([url]) => url.endsWith('/api/operator'))![1]!;
  expect(options.headers).toEqual(expect.objectContaining({ Authorization: expect.stringContaining('Bearer ') }));
  expect(options.credentials).toBe('omit');
  expect(localStorage.length).toBe(0);
});

it.each([401, 403, 503])('keeps record context and explains rejected operator authentication HTTP %s', async status => {
  const fetch = open();
  fetch.mockImplementation(async url => url.endsWith('/api/operator') ? json({ detail: 'private credential' }, status) : json(fixture));
  fireEvent.change(await screen.findByLabelText('本地操作凭据'), { target: { value: 'fixture-token'.repeat(4) } });
  fireEvent.click(screen.getByRole('button', { name: '连接操作身份' }));
  expect(await screen.findByRole('alert')).not.toHaveTextContent('private credential');
  expect(screen.getByText('Fixture repair')).toBeVisible();
  expect(screen.queryByRole('button', { name: '申请回滚' })).not.toBeInTheDocument();
});

it('records a blocked application once during rapid clicks and retains context on failure', async () => {
  const fetch = open();
  await connect();
  fireEvent.change(screen.getByLabelText('申请 / 拒绝 / 收口理由'), { target: { value: 'please assess' } });
  let fail!: (value: Response) => void;
  fetch.mockImplementation(async (_url, options) => options?.method === 'POST'
    ? new Promise<Response>(resolve => { fail = resolve; }) : json(fixture));
  const button = screen.getByRole('button', { name: '记录被阻断的回滚申请' });
  fireEvent.click(button); fireEvent.click(button);
  expect(fetch.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
  fail(json({ detail: 'private error' }, 409));
  expect(await screen.findByRole('alert')).toHaveTextContent('请刷新核查');
  expect(screen.getByText('Fixture repair')).toBeVisible();
  expect(screen.getByLabelText('申请 / 拒绝 / 收口理由')).toHaveValue('please assess');
  expect(button).toBeDisabled();
  expect(screen.queryByText('申请已记录，请查看下方资格评估与关联提案。')).not.toBeInTheDocument();
});

it('requires explicit execution confirmation and renders the returned independent failure', async () => {
  const record = { ...fixture, status: 'approved' as const, original_proposal_id: 'rem-original' };
  const fetch = open(record);
  await connect();
  expect(screen.getByRole('button', { name: '执行已批准动作' })).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox', { name: '确认执行已批准的精确目标和参数' }));
  fetch.mockImplementation(async () => json({ ...record, status: 'verification_failed', verifications: [{
    verification_id: 'verify-comp', verified_at: fixture.created_at, purpose: 'post_action', outcome: 'failed',
    evidence_status: 'bound',
    historical_success: false, action_id: 'act-comp', checks: [],
  }] }));
  fireEvent.click(screen.getByRole('button', { name: '执行已批准动作' }));
  expect(await screen.findByText('补偿后独立验证未通过')).toBeVisible();
  expect(screen.getByText(/业务验证明确失败/)).toBeVisible();
  const request = fetch.mock.calls.find(([, options]) => options?.method === 'POST')![1]!;
  expect(JSON.parse(request.body as string)).toEqual({ proposal_digest: fixture.proposal_digest, command_id: expect.any(String) });
});

it('handles remediation list empty, loading, error and pagination via the real API contract', async () => {
  const fetch = vi.fn().mockResolvedValue(json({ items: [], limit: 20, offset: 0, has_more: false }));
  vi.stubGlobal('fetch', fetch);
  render(<MemoryRouter initialEntries={['/remediations']}><App /></MemoryRouter>);
  expect(screen.getByText('正在读取数据')).toBeVisible();
  expect(await screen.findByText('暂无 Remediation')).toBeVisible();
  fetch.mockResolvedValue(json({ detail: 'store_unavailable' }, 503));
  fireEvent.click(screen.getByRole('button', { name: '刷新数据' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('存储暂不可用');
  expect(screen.queryByText('暂无 Remediation')).not.toBeInTheDocument();
});

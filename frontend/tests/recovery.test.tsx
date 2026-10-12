import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import { App } from '../src/App';
import type { IncidentDetail, RecoveryAttemptView, RecoveryStatus, RecoveryVerificationView } from '../src/api/types';
import { incident, json } from './fixtures';

function recovery(status: RecoveryStatus): RecoveryVerificationView {
  const recorded = !['alert_firing', 'not_recorded', 'legacy_only', 'pending'].includes(status);
  const outcome: RecoveryAttemptView['outcome'] = status === 'verified' ? 'success'
    : status === 'running' ? 'running' : status === 'failed' ? 'failed' : 'insufficient';
  const reason = status === 'verified' ? 'conditions_met'
    : status === 'failed' ? 'business_condition_mismatch'
    : status === 'superseded' ? 'alert_changed'
    : status === 'insufficient' ? 'invalid_response' : null;
  return {
    status, target_id: 'fixture_query', attempt_count: recorded ? 1 : 0, max_attempts: 3,
    last_attempt_at: recorded ? '2026-10-12T06:36:39Z' : null,
    next_retry_at: ['failed', 'insufficient'].includes(status) ? '2026-10-12T06:37:10Z' : null,
    reason, last_http_status: recorded && status !== 'running' ? 200 : null,
    verified_at: status === 'verified' ? '2026-10-12T06:36:40Z' : null,
    attempts: recorded ? [{
      number: 1, started_at: '2026-10-12T06:36:39Z',
      completed_at: status === 'running' ? null : '2026-10-12T06:36:40Z', outcome, reason,
      http_status: status === 'running' ? null : 200,
      checks: status === 'running' ? [] : [
        { kind: 'http_status', field: '', matched: true },
        { kind: 'json_equals', field: 'service', matched: true },
        { kind: 'nonempty_object', field: 'order', matched: status === 'verified' },
      ],
    }] : [],
  };
}

function open(record: IncidentDetail) {
  const fetch = vi.fn().mockImplementation(async () => json(record));
  vi.stubGlobal('fetch', fetch);
  const rendered = render(<MemoryRouter initialEntries={['/incidents/inc-fixture']}><App /></MemoryRouter>);
  return { ...rendered, fetch };
}

it.each([
  ['alert_firing', '告警仍在触发', 'red'],
  ['pending', '告警已解除，等待验证', 'amber'],
  ['running', '正在验证', 'blue'],
  ['verified', '验证成功', 'green'],
  ['failed', '验证失败，等待重试', 'red'],
  ['insufficient', '证据不足', 'amber'],
  ['superseded', '验证任务已失效', 'gray'],
  ['not_recorded', '未记录独立业务恢复验证', 'gray'],
  ['legacy_only', '仅有历史恢复验证时间', 'gray'],
] as const)('distinguishes %s with an explicit label and color, independently of resolved', async (status, label, tone) => {
  open({ ...incident, status: 'resolved', alert_state: status === 'alert_firing' ? 'firing' : 'cleared',
    recovery_verified_at: status === 'legacy_only' ? '2026-10-11T01:01:00Z' : null,
    recovery_verification: recovery(status),
  });
  const region = await screen.findByRole('region', { name: '业务恢复验证' });
  expect(within(region).getAllByText(label)[0]).toHaveClass('badge', tone);
  expect(screen.getByText('已解决')).toBeVisible();
  if (status === 'verified') {
    expect(within(region).getByRole('heading', { name: /业务已于 2026\/10\/12 14:36:40 通过恢复验证/ })).toBeVisible();
  } else {
    expect(within(region).queryByText('验证成功')).not.toBeInTheDocument();
    expect(within(region).queryByRole('heading', { name: /业务已于/ })).not.toBeInTheDocument();
  }
  expect(within(region).getByText(/不代表服务现在仍然健康/)).toBeVisible();
});

it('does not turn an old timestamp or an alert clear into verified evidence or failure', async () => {
  open({ ...incident, status: 'resolved', alert_state: 'cleared',
    recovery_verified_at: '2026-10-11T01:01:00Z', recovery_verification: recovery('legacy_only'),
  });
  const region = await screen.findByRole('region', { name: '业务恢复验证' });
  expect(within(region).getByText('历史恢复验证时间')).toBeVisible();
  expect(within(region).getByText('2026/10/11 09:01:00')).toBeVisible();
  expect(within(region).getByText(/旧版记录缺少结构化业务检查收据/)).toBeVisible();
  expect(within(region).queryByText('验证失败')).not.toBeInTheDocument();
  expect(within(region).queryByText('验证成功')).not.toBeInTheDocument();
});

it('shows exhausted attempts without promising another retry', async () => {
  const view = recovery('failed');
  view.attempt_count = 3;
  view.next_retry_at = null;
  view.attempts = [1, 2, 3].map(number => ({ ...view.attempts[0], number }));
  open({ ...incident, alert_state: 'cleared', recovery_verification: view });
  const region = await screen.findByRole('region', { name: '业务恢复验证' });
  expect(within(region).getByText('3 / 3 次')).toBeVisible();
  expect(within(region).getByText(/验证尝试次数已用尽/)).toBeVisible();
  expect(within(region).queryByText('验证失败，等待重试')).not.toBeInTheDocument();
  fireEvent.click(within(region).getByText('验证尝试记录（3）'));
  expect(within(region).getByText('第 3 次')).toBeVisible();
  expect(within(region).getByText('第 1 次')).toBeVisible();
});

it('renders HTTP and matched/unmatched business conditions as bounded read-only observations', async () => {
  const { fetch } = open({ ...incident, status: 'resolved', alert_state: 'cleared', recovery_verification: recovery('failed') });
  const region = await screen.findByRole('region', { name: '业务恢复验证' });
  const httpField = within(region).getByText('最近 HTTP 状态').closest('div')!;
  expect(within(httpField).getByText('HTTP 200')).toBeVisible();
  expect(within(region).getByRole('heading', { name: '已通过 2' })).toBeVisible();
  expect(within(region).getByRole('heading', { name: '未通过 1' })).toBeVisible();
  expect(within(region).getByText('order 业务对象未满足非空条件')).toBeVisible();
  expect(within(region).getByText('2026/10/12 14:37:10')).toBeVisible();
  fireEvent.click(within(region).getByText('验证尝试记录（1）'));
  expect(within(region).getByText('第 1 次')).toBeVisible();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole('button', { name: /执行|开始验证|立即重试|修复|批准/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '刷新数据' }));
  await screen.findByRole('region', { name: '业务恢复验证' });
  expect(fetch).toHaveBeenCalledTimes(2);
  for (const [, options] of fetch.mock.calls) expect(options).toMatchObject({ method: 'GET', credentials: 'omit' });
});

it('keeps absent HTTP and missing checks distinct from healthy business evidence', async () => {
  const view = recovery('insufficient');
  view.last_http_status = null;
  view.attempts[0].http_status = null;
  view.attempts[0].checks = [];
  view.attempts[0].outcome = 'abandoned';
  view.attempts[0].reason = 'lease_expired';
  open({ ...incident, alert_state: 'cleared', recovery_verification: view });
  const region = await screen.findByRole('region', { name: '业务恢复验证' });
  expect(within(region).getAllByText('未记录 HTTP 响应')[0]).toBeVisible();
  expect(within(region).getByText(/未记录业务检查结果，不能据此判定业务正常/)).toBeVisible();
  expect(within(region).queryByText('HTTP 0')).not.toBeInTheDocument();
  expect(within(region).queryByText('全部通过')).not.toBeInTheDocument();
  fireEvent.click(within(region).getByText('验证尝试记录（1）'));
  expect(within(region).getByText('尝试中断')).toBeVisible();
});

it('keeps long identifiers and condition fields as inert wrapping text', async () => {
  const view = recovery('verified');
  view.target_id = 'fixture_' + 'x'.repeat(112);
  view.attempts[0].checks[1].field = '<img src=x onerror=alert(1)>';
  const { container } = open({ ...incident, alert_state: 'cleared', recovery_verification: view });
  const region = await screen.findByRole('region', { name: '业务恢复验证' });
  expect(within(region).getByText(view.target_id)).toHaveClass('technical-value');
  expect(within(region).getByText('<img src=x onerror=alert(1)> 字段符合注册条件')).toBeVisible();
  expect(container.querySelector('img, script')).toBeNull();
});

it('uses loading and safe API errors without displaying cached or fabricated verification success', async () => {
  let fail: (reason: Error) => void = () => {};
  vi.stubGlobal('fetch', vi.fn(() => new Promise((_resolve, reject) => { fail = reject; })));
  render(<MemoryRouter initialEntries={['/incidents/inc-fixture']}><App /></MemoryRouter>);
  expect(screen.getByText('正在读取数据')).toBeVisible();
  expect(screen.queryByText('验证成功')).not.toBeInTheDocument();
  fail(new Error('private failure text'));
  expect(await screen.findByRole('alert')).toHaveTextContent('无法连接本地 API');
  expect(screen.queryByRole('region', { name: '业务恢复验证' })).not.toBeInTheDocument();
  expect(screen.queryByText('private failure text')).not.toBeInTheDocument();
});

it('reports an API version without the new view as unavailable instead of guessing from resolved', async () => {
  const old = { ...incident, status: 'resolved', alert_state: 'cleared' };
  Reflect.deleteProperty(old, 'recovery_verification');
  open(old);
  expect(await screen.findByText('恢复验证数据暂不可用')).toBeVisible();
  expect(screen.queryByText('验证成功')).not.toBeInTheDocument();
  expect(screen.queryByText('验证失败')).not.toBeInTheDocument();
});

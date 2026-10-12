import { Activity, Ban, Check, ChevronDown, CircleHelp, Clock3, History, ShieldCheck, TriangleAlert, X } from 'lucide-react';
import type { IncidentDetail, RecoveryAttemptView, RecoveryCheckView, RecoveryReason, RecoveryStatus } from '../api/types';
import { formatTime } from '../lib/format';
import { EmptyState, Fields, Panel, StatusBadge, TechnicalValue } from './ui';

const states = {
  alert_firing: { label: '告警仍在触发', tone: 'red', icon: TriangleAlert, description: '当前告警记录尚未解除，不能确认本次业务恢复。' },
  pending: { label: '告警已解除，等待验证', tone: 'amber', icon: Clock3, description: '已记录恢复验证任务，尚无完成的业务探测结果。' },
  running: { label: '正在验证', tone: 'blue', icon: Activity, description: '验证任务已领取，结果尚未记录。' },
  verified: { label: '验证成功', tone: 'green', icon: ShieldCheck, description: '注册业务探测的 HTTP 状态与全部业务条件均已通过。' },
  failed: { label: '验证失败', tone: 'red', icon: TriangleAlert, description: '最近一次业务探测未满足注册条件，尚未确认业务恢复。' },
  insufficient: { label: '证据不足', tone: 'amber', icon: CircleHelp, description: '现有记录无法确认业务恢复，未记录的检查不视为通过。' },
  superseded: { label: '验证任务已失效', tone: 'gray', icon: Ban, description: '告警或解除批次已变化，此任务不能确认本次业务恢复。' },
  not_recorded: { label: '未记录独立业务恢复验证', tone: 'gray', icon: History, description: '该历史事件没有结构化恢复任务；告警解除或 Incident 已解决均不能证明业务验证成功。' },
  legacy_only: { label: '仅有历史恢复验证时间', tone: 'gray', icon: History, description: '旧版记录缺少结构化业务检查收据，无法确认独立业务验证结果。' },
} satisfies Record<RecoveryStatus, {
  label: string; tone: 'red' | 'amber' | 'blue' | 'green' | 'gray';
  icon: typeof Activity; description: string;
}>;

const reasons: Record<RecoveryReason, string> = {
  conditions_met: '注册业务条件全部满足',
  recovery_capability_not_configured: '尚未配置业务恢复验证能力',
  clear_time_invalid: '告警解除时间无效',
  http_status_mismatch: 'HTTP 状态不符合注册条件',
  business_condition_mismatch: '业务条件未满足',
  redirect_refused: '探测收到重定向，未接受该证据',
  timeout: '业务探测超时',
  connection_failed: '业务探测连接失败',
  invalid_response: '响应无法作为有效业务证据',
  response_too_large: '响应超过允许大小',
  content_encoding_refused: '响应编码不符合探测要求',
  invalid_evidence: '恢复验证证据缺失或无效',
  lease_expired: '验证尝试超时中断',
  alert_changed: '告警或解除批次已变化',
  configuration_changed: '注册验证配置已变化',
  internal_probe_error: '业务探测未能完成',
};

const outcomes = {
  running: { label: '结果待记录', tone: 'blue' },
  success: { label: '尝试成功', tone: 'green' },
  failed: { label: '尝试失败', tone: 'red' },
  insufficient: { label: '证据不足', tone: 'amber' },
  abandoned: { label: '尝试中断', tone: 'gray' },
} as const;

function checkLabel(check: RecoveryCheckView) {
  if (check.kind === 'http_status') return `HTTP 状态${check.matched ? '符合' : '不符合'}注册条件`;
  if (check.kind === 'nonempty_object') return `${check.field} 业务对象${check.matched ? '非空' : '未满足非空条件'}`;
  return `${check.field} 字段${check.matched ? '符合' : '不符合'}注册条件`;
}

function CheckGroup({ checks, passed }: { checks: RecoveryCheckView[]; passed: boolean }) {
  const Icon = passed ? Check : X;
  return <div className={`recovery-check-group ${passed ? 'passed' : 'unmatched'}`}>
    <h4>{passed ? '已通过' : '未通过'} <span>{checks.length}</span></h4>
    {checks.length ? <ul>{checks.map((check, index) => <li key={`${check.kind}-${check.field}-${index}`}>
      <Icon size={15} aria-hidden="true" /><span>{checkLabel(check)}</span>
    </li>)}</ul> : <p>未记录{passed ? '通过' : '未通过'}条件</p>}
  </div>;
}

export function RecoveryVerificationCard({ incident }: { incident: IncidentDetail }) {
  const recovery = incident.recovery_verification;
  // An older running API cannot establish the absence of a recovery task.
  if (!recovery) return <Panel title="业务恢复验证">
    <EmptyState title="恢复验证数据暂不可用" description="详情接口尚未提供恢复验证视图，请确认 API 已更新后刷新。" />
  </Panel>;
  const state = states[recovery.status];
  const Icon = state.icon;
  const label = recovery.status === 'failed' && recovery.next_retry_at ? '验证失败，等待重试' : state.label;
  const latest = recovery.attempts.at(-1);
  const passed = latest?.checks.filter(check => check.matched) ?? [];
  const unmatched = latest?.checks.filter(check => !check.matched) ?? [];
  const exhausted = recovery.max_attempts !== null && recovery.attempt_count >= recovery.max_attempts;
  return <div role="region" aria-label="业务恢复验证">
    <Panel title="业务恢复验证" subtitle="告警解除与业务验证分别记录 · 时间均为北京时间" className="recovery-panel"
      action={<StatusBadge value={recovery.status} label={label} tone={state.tone} />}>
      <div className={`recovery-state ${state.tone}`}>
        <div className="recovery-state-icon"><Icon size={21} aria-hidden="true" /></div>
        <div><h3>{recovery.status === 'verified' && recovery.verified_at
          ? `业务已于 ${formatTime(recovery.verified_at)} 通过恢复验证` : label}</h3>
          <p>{state.description}</p>
          {recovery.status === 'failed' && !recovery.next_retry_at && <p>{exhausted ? '验证尝试次数已用尽，未安排自动重试。' : '当前记录未安排重试。'}</p>}
        </div>
      </div>
      {recovery.status === 'legacy_only' ? <Fields items={[
        ['历史恢复验证时间', formatTime(incident.recovery_verified_at)],
        ['证据限制', '缺少结构化业务检查收据'],
      ]} /> : recovery.status !== 'not_recorded' && <Fields items={[
        ['目标标识', <TechnicalValue value={recovery.target_id} />],
        ['验证尝试', recovery.max_attempts === null ? `${recovery.attempt_count} 次 · 上限未记录` : `${recovery.attempt_count} / ${recovery.max_attempts} 次`],
        ['最近尝试', formatTime(recovery.last_attempt_at)],
        ['下次重试', recovery.next_retry_at ? formatTime(recovery.next_retry_at) : '未安排重试'],
        ['状态原因', recovery.reason ? reasons[recovery.reason] : '暂无记录'],
        ['最近 HTTP 状态', recovery.last_http_status === null ? '未记录 HTTP 响应' : `HTTP ${recovery.last_http_status}`],
      ]} />}
      {latest && <div className="recovery-checks">
        <h3>最近一次业务检查</h3>
        <p className="recovery-hint">展示最近尝试的最后一条采样；未记录的条件不视为通过。</p>
        {latest.checks.length ? <div className="recovery-check-grid">
          <CheckGroup checks={passed} passed /><CheckGroup checks={unmatched} passed={false} />
        </div> : <p className="recovery-hint">未记录业务检查结果，不能据此判定业务正常。</p>}
      </div>}
      {recovery.attempts.length ? <details className="recovery-attempts">
        <summary>验证尝试记录（{recovery.attempt_count}）<ChevronDown size={16} aria-hidden="true" /></summary>
        <ol>{[...recovery.attempts].reverse().map((attempt: RecoveryAttemptView) => <li key={attempt.number}>
          <div className="record-heading"><strong>第 {attempt.number} 次</strong>
            <StatusBadge value={attempt.outcome} label={outcomes[attempt.outcome].label} tone={outcomes[attempt.outcome].tone} />
            <span className="recovery-http">{attempt.http_status === null ? '未记录 HTTP 响应' : `HTTP ${attempt.http_status}`}</span>
          </div>
          <div className="recovery-attempt-times"><span>开始 {formatTime(attempt.started_at)}</span>
            <span>完成 {attempt.completed_at ? formatTime(attempt.completed_at) : '尚未记录'}</span></div>
          <p>{attempt.reason ? reasons[attempt.reason] : '结果尚未记录'} · 已通过 {attempt.checks.filter(check => check.matched).length} / 未通过 {attempt.checks.filter(check => !check.matched).length}</p>
        </li>)}</ol>
      </details> : !['not_recorded', 'legacy_only'].includes(recovery.status) && <p className="panel-footnote">暂无验证尝试记录。</p>}
      <p className="panel-footnote">验证结果仅证明记录时间的业务检查情况，不代表服务现在仍然健康。刷新页面只读取记录，不会触发探测或重试。</p>
    </Panel>
  </div>;
}

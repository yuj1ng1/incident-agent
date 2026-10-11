import { ArrowRight } from 'lucide-react';
import { Link, useParams } from 'react-router-dom';
import type { SessionDetail } from '../api/types';
import { InvestigationText } from '../components/InvestigationText';
import { DataState, EmptyState, Fields, PageHeader, Panel, StatusBadge, TechnicalValue } from '../components/ui';
import { useApi } from '../hooks/useApi';
import { formatTime, recordPath } from '../lib/format';

function BudgetBar({ label, used, limit }: { label: string; used: number; limit: number }) {
  const percent = limit > 0 ? Math.min(100, used / limit * 100) : 0;
  return <div className="budget-item">
    <div><span>{label}</span><strong>{used}<span> / {limit}</span></strong></div>
    <div className="budget-track" role={limit > 0 ? 'progressbar' : undefined}
      aria-label={label} aria-valuemin={limit > 0 ? 0 : undefined}
      aria-valuemax={limit > 0 ? limit : undefined} aria-valuenow={limit > 0 ? Math.min(used, limit) : undefined}
      aria-valuetext={used + ' / ' + limit}>
      <span className={used >= limit && limit > 0 ? 'at-limit' : ''} style={{ width: percent + '%' }} />
    </div>
    {limit === 0 && <small>此额度未开放</small>}
  </div>;
}

export function SessionDetailPage() {
  const { sessionId = '' } = useParams();
  const result = useApi<SessionDetail>('/api/sessions/' + encodeURIComponent(sessionId));
  const detail = result.data;
  return <>
    <PageHeader title="Session 详情" description="调查结论、资源用量与后续处理进度。"
      back={{ to: '/sessions', label: '返回 Sessions' }} loading={result.loading}
      reload={result.reload} fetchedAt={result.fetchedAt} />
    <DataState loading={result.loading} error={result.error} retry={result.reload}>
      {detail && <>
        <div className="detail-banner">
          <div><span className="eyebrow">INVESTIGATION SESSION</span><h2>{detail.session.service}</h2><span className="full-id">{detail.session.session_id}</span></div>
          <div className="session-summary"><StatusBadge value={detail.session.status} />
            <div className="session-metrics"><span><strong>{detail.session.total_model_requests}</strong> 模型调用</span><span><strong>{detail.session.total_tool_executions}</strong> 工具执行</span></div>
          </div>
        </div>
        <div className="detail-layout">
          <div className="detail-main">
            <Panel title="Incident 累计预算" subtitle="同一授权组共享额度，包含相关 Session 的已用次数">
              <div className="budget-grid">
                <BudgetBar label="尝试次数" used={detail.incident_budget_usage.attempts} limit={detail.limits.max_attempts} />
                <BudgetBar label="模型请求" used={detail.incident_budget_usage.model_requests} limit={detail.limits.max_model_requests} />
                <BudgetBar label="工具执行" used={detail.incident_budget_usage.tool_executions} limit={detail.limits.max_tool_executions} />
                <BudgetBar label="恢复次数" used={detail.incident_budget_usage.resumes} limit={detail.limits.max_resumes} />
                <BudgetBar label="人类提问" used={detail.incident_budget_usage.human_requests} limit={detail.limits.max_human_requests} />
              </div>
              <p className="panel-footnote">授权开始：{formatTime(detail.incident_budget_usage.started_at)} · 时长上限：{detail.limits.max_duration_seconds} 秒</p>
            </Panel>
            <Panel title="Agent 最终回答" subtitle="调查结论与服务恢复验证分别记录"
              action={detail.investigation_result && <StatusBadge value={detail.investigation_result.status} />}>
              {detail.investigation_result ? <div className="answer-container">
                <InvestigationText text={detail.investigation_result.answer || '没有保存回答正文。'} />
                <div className="answer-meta">模型轮次 {detail.investigation_result.model_rounds} · 工具调用 {detail.investigation_result.tool_calls}
                  {detail.investigation_result.answer_truncated && <span className="truncated"> · 回答已被 API 截断为 8000 字符</span>}
                </div>
              </div> : <EmptyState title="尚无最终回答" description="当前 Session 没有保存最终调查结果；请结合运行状态查看。" />}
            </Panel>
            <Panel title="人工请求" subtitle="调查中提出的问题及其回复状态">
              {detail.human_requests.length ? <div className="human-request-list">{detail.human_requests.map(request =>
                <article className="human-request" key={request.request_id}>
                  <div className="record-heading"><StatusBadge value={request.status} /><code className="full-id">{request.request_id}</code></div>
                  <p className="answer-text">{request.question}</p>
                  <div className="human-request-times"><span>创建：{formatTime(request.created_at)}</span><span>到期：{formatTime(request.expires_at)}</span></div>
                </article>,
              )}</div> : <EmptyState title="暂无人工请求" description="此 Session 没有记录向人类提出的问题。" />}
              <p className="panel-footnote">工作台仅查看请求；回复与正式修复审批通过各自的原有流程处理。</p>
            </Panel>
          </div>
          <aside className="detail-aside" aria-label="Session 调查信息和后续处理">
            <Panel title="调查信息" action={<Link className="text-link" to={recordPath('incidents', detail.session.incident_id)}>关联 Incident<ArrowRight size={14} /></Link>}>
              <Fields items={[
                ['创建时间', formatTime(detail.session.created_at)], ['更新时间', formatTime(detail.session.updated_at)],
                ['结束时间', formatTime(detail.session.completed_at)], ['下次重试', formatTime(detail.session.next_retry_at)],
                ['当前尝试模型轮次', detail.session.current_attempt_rounds], ['当前尝试工具调用', detail.session.current_attempt_tool_calls],
                ['尝试次数', detail.session.attempt_count], ['最近错误类别', <TechnicalValue value={detail.session.last_error_category} />],
              ]} />
            </Panel>
            <Panel title="结果投影" subtitle="调查结果写入 Incident 的进度">
              <div className={'state-callout ' + (detail.projection.blocked ? 'state-danger' : detail.projection.projected ? 'state-success' : 'state-neutral')}>
                <span className="state-dot" aria-hidden="true" /><strong>{detail.projection.blocked ? '投影已阻塞' : detail.projection.projected ? '已写入 Incident' : '尚未写入 Incident'}</strong>
              </div>
              <Fields items={[
                ['错误类别', <TechnicalValue value={detail.projection.error_category} />], ['重试时间', formatTime(detail.projection.retry_at)],
              ]} />
            </Panel>
            <Panel title="提案生成" subtitle="Remediation 生成任务状态">
              <div className="proposal-status"><StatusBadge value={detail.proposal_generation.status} /></div>
              <Fields items={[
                ['生成尝试', detail.proposal_generation.attempts], ['阻塞原因', <TechnicalValue value={detail.proposal_generation.blocked_reason} />],
                ['错误类别', <TechnicalValue value={detail.proposal_generation.error_category} />], ['证据缺口', <TechnicalValue value={detail.proposal_generation.evidence_gap} />],
                ['重试时间', formatTime(detail.proposal_generation.retry_at)],
              ]} />
              <p className="panel-footnote">此状态仅表示生成任务进度，不代表已生成或获批修复提案。</p>
            </Panel>
          </aside>
        </div>
      </>}
    </DataState>
  </>;
}

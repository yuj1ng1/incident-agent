import { ArrowRight } from 'lucide-react';
import { Link, useParams } from 'react-router-dom';
import type { IncidentDetail } from '../api/types';
import { InvestigationText } from '../components/InvestigationText';
import { RecoveryVerificationCard } from '../components/RecoveryVerificationCard';
import { DataState, EmptyState, Fields, PageHeader, Panel, StatusBadge, TechnicalValue, TimeValue } from '../components/ui';
import { useApi } from '../hooks/useApi';
import { formatTime, recordPath } from '../lib/format';

export function IncidentDetailPage() {
  const { incidentId = '' } = useParams();
  const result = useApi<IncidentDetail>(`/api/incidents/${encodeURIComponent(incidentId)}`);
  const incident = result.data;
  return <><PageHeader title="Incident 详情" description="告警观测、独立业务恢复验证与最近调查记录。" back={{ to: '/incidents', label: '返回 Incidents' }} loading={result.loading} reload={result.reload} fetchedAt={result.fetchedAt} />
    <DataState loading={result.loading} error={result.error} retry={result.reload}>
      {incident && <>
        <div className="detail-banner"><div><span className="eyebrow">INCIDENT</span><h2>{incident.service}</h2><span className="full-id">{incident.incident_id}</span></div><div className="badge-group"><StatusBadge value={incident.severity} /><StatusBadge value={incident.status} /></div></div>
        <div className="detail-layout"><div className="detail-main">
        <RecoveryVerificationCard incident={incident} />
        <Panel title="最近告警" subtitle="API 最多返回最近 3 条告警观测">
          {incident.recent_alerts.length ? <div className="table-scroll" tabIndex={0} role="region" aria-label="告警观测，可横向滚动"><table><caption className="sr-only">最近告警观测</caption><thead><tr><th scope="col">告警 / 指标</th><th scope="col">状态</th><th scope="col">观测值</th><th scope="col">阈值</th><th scope="col">观测时间 · 北京时间</th></tr></thead><tbody>{[...incident.recent_alerts].reverse().map((alert, index) => <tr key={index}>
            <td><strong className="alert-name">{alert.alertname}</strong><span className="record-id">{alert.metric ?? '未记录指标名'}</span></td>
            <td><StatusBadge value={alert.state} /></td><td className="numeric">{alert.value ?? '无数据'}</td><td className="numeric">{alert.threshold ?? '无数据'}</td><td className="time-cell"><TimeValue value={alert.observed_at} /></td>
          </tr>)}</tbody></table></div> : <EmptyState title="暂无告警观测" />}
        </Panel>
        <Panel title="最近调查" subtitle="最近 3 条保留记录；完整归档历史可通过 Runtime CLI 查询">
          {incident.recent_investigations.length ? <div className="investigation-list">{[...incident.recent_investigations].reverse().map((record, index) => <article key={record.session_id || index} className="investigation-record">
            <div className="record-heading"><StatusBadge value={record.status} /><time>{formatTime(record.investigated_at)}</time>{record.session_id && <Link className="text-link" to={recordPath('sessions', record.session_id)}>查看 Session<ArrowRight size={14} /></Link>}</div>
            <InvestigationText text={record.answer || '此记录没有保存回答正文。'} />
            <div className="answer-meta">模型轮次 {record.model_rounds} · 工具调用 {record.tool_calls}{record.answer_truncated && <span className="truncated"> · 回答已被 API 截断为 8000 字符</span>}</div>
          </article>)}</div> : <EmptyState title="暂无调查记录" description="调查结果写入事件后，可在这里查看。" />}
        </Panel>
        </div><aside className="detail-aside" aria-label="Incident 事件摘要">
          <Panel title="事件信息"><Fields items={[
            ['告警状态', <StatusBadge value={incident.alert_state} />], ['最近调查', formatTime(incident.last_investigated_at)],
            ['告警解除时间', formatTime(incident.cleared_at)],
            ['连续调查失败', incident.consecutive_investigation_failures], ['暂停调查原因', <TechnicalValue value={incident.investigation_suspended_reason} />],
            ['当前保留证据', incident.evidence_count], ['归档 Session 数', incident.archived_session_count],
          ]} /></Panel>
        </aside></div>
      </>}
    </DataState>
  </>;
}

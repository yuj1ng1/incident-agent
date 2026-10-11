import { Activity, ArrowRight, CheckCheck, Files, MessageCircle, ScanLine } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { IncidentSummary, Page, SessionSummary } from '../api/types';
import { SESSION_STATUSES } from '../api/types';
import { IncidentTable } from '../components/RecordTables';
import { DataState, EmptyState, PageHeader, Panel, StatusBadge, statusLabels } from '../components/ui';
import { useApi } from '../hooks/useApi';
import { formatTime, recordPath } from '../lib/format';

function StatCard({ label, value, note, icon: Icon }: { label: string; value: string; note: string; icon: LucideIcon }) {
  return <div className="stat-card"><div className="stat-top"><span>{label}</span><Icon size={19} /></div><div className="stat-value">{value}</div><p>{note}</p></div>;
}

export function OverviewPage() {
  // The existing API has no aggregate endpoint. Read one bounded page per store.
  const incidents = useApi<Page<IncidentSummary>>('/api/incidents?limit=100&offset=0');
  const sessions = useApi<Page<SessionSummary>>('/api/sessions?limit=100&offset=0');
  const count = (status: string) => sessions.data
    ? `${sessions.data.items.filter(item => item.status === status).length}${sessions.data.has_more ? '+' : ''}` : '—';
  const countNote = !sessions.data ? '数据尚未读取'
    : sessions.data.has_more ? '最近 100 条中的数量；全库可能更多' : '当前已读取的全部 Session';
  const activities = [...(sessions.data?.items || [])].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)).slice(0, 6);
  const reload = () => { incidents.reload(); sessions.reload(); };
  return <>
    <PageHeader title="调查总览" description="集中查看告警事件、调查状态与近期进展。" loading={incidents.loading || sessions.loading} reload={reload} />
    <div className="overview-note"><Activity size={15} /><span>按需读取真实数据，点击刷新获取最新状态。{(incidents.data?.has_more || sessions.data?.has_more) && ' 总览最多读取每类最近 100 条，“+”表示还有未加载记录。'}</span></div>
    <div className="stats-grid">
      <StatCard label="Incidents" icon={Files} value={incidents.data ? `${incidents.data.items.length}${incidents.data.has_more ? '+' : ''}` : '—'} note={!incidents.data ? '数据尚未读取' : incidents.data.has_more ? '已读取 100 条，更多记录见列表' : '当前已读取的全部 Incident'} />
      <StatCard label="运行中的调查" icon={ScanLine} value={count('RUNNING')} note={countNote} />
      <StatCard label="等待人类回复" icon={MessageCircle} value={count('WAITING_HUMAN')} note={countNote} />
      <StatCard label="已完成的调查" icon={CheckCheck} value={count('COMPLETED')} note={countNote} />
    </div>
    <div className="overview-grid">
      <Panel title="调查状态" subtitle="保留运行、等待、暂停与终止状态的区别" action={<span className="small-label">{sessions.data ? `${sessions.data.items.length} 条已加载` : 'Session'}</span>}>
        <DataState loading={sessions.loading} error={sessions.error} retry={sessions.reload}>
          {sessions.data?.items.length ? <div className="status-chart">{SESSION_STATUSES.filter(status => sessions.data!.items.some(item => item.status === status)).map(status => {
            const number = sessions.data!.items.filter(item => item.status === status).length;
            return <Link key={status} to={`/sessions?status=${status}`} className="status-chart-row"><StatusBadge value={status} /><span className="chart-track"><span style={{ width: `${number / sessions.data!.items.length * 100}%` }} /></span><strong>{number}</strong></Link>;
          })}</div> : <EmptyState description="Runtime 暂未保存调查 Session。" />}
        </DataState>
      </Panel>
      <Panel title="近期事件" subtitle="所加载 Session 的最新状态，按更新时间排列">
        <DataState loading={sessions.loading} error={sessions.error} retry={sessions.reload}>
          {activities.length ? <ul className="activity-list">{activities.map(item => <li key={item.session_id}>
            <span className={`activity-dot ${item.status === 'FAILED' || item.status === 'EXHAUSTED' ? 'danger' : ''}`} />
            <div><Link to={recordPath('sessions', item.session_id)}>{item.service}<ArrowRight size={13} /></Link><p>{statusLabels[item.status] || item.status} · 模型 {item.total_model_requests} / 工具 {item.total_tool_executions}</p><time>{formatTime(item.updated_at)}</time></div>
          </li>)}</ul> : <EmptyState title="暂无近期事件" description="调查记录保存后会出现在这里。" />}
        </DataState>
        {sessions.data?.items.length ? <p className="panel-footnote">每个 Session 展示一条最新记录，完整状态变更历史暂未提供。</p> : null}
      </Panel>
    </div>
    <Panel title="近期 Incidents" subtitle="按现有 API 的事件创建顺序显示" action={<Link className="text-link" to="/incidents">查看全部<ArrowRight size={15} /></Link>}>
      <DataState loading={incidents.loading} error={incidents.error} retry={incidents.reload}>
        {incidents.data?.items.length ? <IncidentTable items={incidents.data.items.slice(0, 6)} /> : <EmptyState title="暂无 Incident" description="监控发现告警并保存事件后，可在这里查看。" />}
      </DataState>
    </Panel>
  </>;
}

import { ArrowRight, CheckCheck, Files, MessageCircle, ScanLine } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { IncidentPage, SessionPage } from '../api/types';
import { SESSION_STATUSES } from '../api/types';
import { IncidentTable } from '../components/RecordTables';
import { DataState, EmptyState, PageHeader, Panel, StatusBadge, statusTone } from '../components/ui';
import { useApi } from '../hooks/useApi';
import { formatTime, recordPath } from '../lib/format';

function StatCard({ label, value, note, loading, error, tone, icon: Icon }: {
  label: string; value: string | null; note: string; loading: boolean;
  error: string | null; tone: string; icon: LucideIcon;
}) {
  const unavailable = loading || error || value === null;
  return <section className={`stat-card ${error ? 'stat-error' : ''}`} aria-label={label} aria-busy={loading}>
    <div className="stat-top"><span>{label}</span><Icon size={16} aria-hidden="true" className={`stat-icon ${tone}`} /></div>
    <div className={unavailable ? 'stat-state' : 'stat-value'}>{loading ? '正在加载' : error ? '读取失败' : value ?? '—'}</div>
    <p>{loading ? '正在获取最新记录…' : error ? '请点击刷新数据重试' : value === null ? '尚无数据' : note}</p>
  </section>;
}

export function OverviewPage() {
  // The existing API has no aggregate endpoint. Read one bounded page per store.
  const incidents = useApi<IncidentPage>('/api/incidents?limit=100&offset=0');
  const sessions = useApi<SessionPage>('/api/sessions?limit=100&offset=0');
  const count = (status: string) => sessions.data
    ? `${sessions.data.items.filter(item => item.status === status).length}${sessions.data.has_more ? '+' : ''}` : null;
  const countNote = (emptyNote: string) => !sessions.data?.items.length ? '尚无 Session 记录'
    : sessions.data.has_more ? '最近 100 条内的数量下界' : emptyNote;
  const distribution = SESSION_STATUSES.map(status => ({
    status, count: sessions.data?.items.filter(item => item.status === status).length || 0,
  })).filter(item => item.count > 0);
  const activities = [...(sessions.data?.items || [])].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)).slice(0, 6);
  const reload = () => { incidents.reload(); sessions.reload(); };
  return <>
    <PageHeader title="调查总览" description="服务事件与调查进展，一处掌握。" loading={incidents.loading || sessions.loading} reload={reload} fetchedAt={incidents.fetchedAt && sessions.fetchedAt ? incidents.fetchedAt : null} />
    {(incidents.data?.has_more || sessions.data?.has_more) && <p className="overview-note">总览最多读取每类最近 100 条，“+”表示还有未加载记录；各状态数量为已加载记录的下界。</p>}
    <div className="stats-grid">
      <StatCard label="Incident 总数" icon={Files} tone="gray" loading={incidents.loading} error={incidents.error}
        value={incidents.data ? `${incidents.data.items.length}${incidents.data.has_more ? '+' : ''}` : null}
        note={!incidents.data?.items.length ? '尚无 Incident 记录' : incidents.data.has_more ? '已读取 100 条，更多记录见列表' : '已保存的服务事件'} />
      <StatCard label="运行中的调查" icon={ScanLine} tone="blue" loading={sessions.loading} error={sessions.error} value={count('RUNNING')} note={countNote('当前正在执行的 Session')} />
      <StatCard label="等待人类回复" icon={MessageCircle} tone="amber" loading={sessions.loading} error={sessions.error} value={count('WAITING_HUMAN')} note={countNote('等待补充信息的 Session')} />
      <StatCard label="已完成的调查" icon={CheckCheck} tone="green" loading={sessions.loading} error={sessions.error} value={count('COMPLETED')} note={countNote('已产生调查结论的 Session')} />
    </div>
    <Panel title="调查状态分布" subtitle={sessions.data ? `基于 ${sessions.data.items.length} 条已加载 Session${sessions.data.has_more ? '，非全库分布' : ''}` : '当前已加载的 Session'} action={<Link className="text-link" to="/sessions">查看 Sessions<ArrowRight size={14} /></Link>} className={`distribution-panel ${distribution.length === 1 ? 'distribution-single' : ''}`}>
        <DataState loading={sessions.loading} error={sessions.error} retry={sessions.reload}>
          {sessions.data?.items.length ? <div className="status-chart">{distribution.map(({ status, count: number }) => {
            const percent = number / sessions.data!.items.length * 100;
            return <Link key={status} to={`/sessions?status=${status}`} className="status-chart-row">
              <div><StatusBadge value={status} /><strong>{number}<span> 条</span></strong></div>
              <div><span className={`chart-track ${statusTone(status)}`} aria-hidden="true"><span style={{ width: `${percent}%` }} /></span><span className="chart-percent">{new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 }).format(percent)}%</span></div>
            </Link>;
          })}</div> : <EmptyState description="Runtime 暂未保存调查 Session。" />}
        </DataState>
    </Panel>
    <div className="overview-grid">
      <Panel title="近期 Incidents" subtitle="按事件创建顺序显示" action={<Link className="text-link" to="/incidents">查看全部<ArrowRight size={14} /></Link>}>
        <DataState loading={incidents.loading} error={incidents.error} retry={incidents.reload}>
          {incidents.data?.items.length ? <IncidentTable items={incidents.data.items.slice(0, 6)} /> : <EmptyState title="暂无 Incident" description="监控发现告警并保存事件后，可在这里查看。" />}
        </DataState>
      </Panel>
      <Panel title="近期事件" subtitle="按 Session 最新更新时间排列">
        <DataState loading={sessions.loading} error={sessions.error} retry={sessions.reload}>
          {activities.length ? <ul className="activity-list">{activities.map(item => <li key={item.session_id}>
            <div className="activity-heading"><Link to={recordPath('sessions', item.session_id)}>{item.service}<ArrowRight size={13} aria-hidden="true" /></Link><StatusBadge value={item.status} /></div>
            <div className="activity-meta"><p>模型 {item.total_model_requests} / 工具 {item.total_tool_executions}</p><time>{formatTime(item.updated_at)}</time></div>
          </li>)}</ul> : <EmptyState title="暂无近期事件" description="调查记录保存后会出现在这里。" />}
        </DataState>
        {sessions.data?.items.length ? <p className="panel-footnote">每个 Session 展示一条最新记录，完整状态变更历史暂未提供。</p> : null}
      </Panel>
    </div>
  </>;
}

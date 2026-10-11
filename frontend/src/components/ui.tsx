import type { ReactNode } from 'react';
import { AlertCircle, ArrowLeft, ChevronLeft, ChevronRight, Inbox, LoaderCircle, RefreshCw } from 'lucide-react';
import { Link } from 'react-router-dom';
import { formatTime } from '../lib/format';

export const statusLabels: Record<string, string> = {
  investigating: '调查中', resolved: '已解决', firing: '告警触发', cleared: '告警解除',
  CREATED: '已创建', QUEUED: '排队中', RUNNING: '运行中', WAITING_RETRY: '等待重试',
  WAITING_HUMAN: '等待人类', PAUSED: '已暂停', COMPLETED: '已完成', EXHAUSTED: '预算耗尽',
  FAILED: '失败', CANCELLED: '已取消', critical: '严重', warning: '警告', info: '提示',
  completed: '回答完成', max_round_reached: '达到轮次上限', model_incomplete: '模型响应不完整',
  not_pending: '无待处理任务', waiting_retry: '等待重试', blocked: '已阻塞',
  pending: '等待回复', answered: '已回复', consumed: '已消费', expired: '已过期', cancelled: '已取消',
};

export function statusTone(value: string) {
  return ['COMPLETED', 'resolved', 'completed', 'cleared', 'answered', 'consumed'].includes(value) ? 'green'
    : ['FAILED', 'EXHAUSTED', 'critical', 'blocked', 'model_incomplete', 'firing'].includes(value) ? 'red'
    : ['WAITING_HUMAN', 'WAITING_RETRY', 'PAUSED', 'warning', 'waiting_retry', 'pending', 'max_round_reached'].includes(value) ? 'amber'
    : ['RUNNING', 'investigating', 'QUEUED'].includes(value) ? 'blue' : 'gray';
}

export function StatusBadge({ value }: { value: string }) {
  const tone = statusTone(value);
  return <span className={`badge ${tone}`} title={value}><span className="badge-dot" />{statusLabels[value] || value}</span>;
}

export function PageHeader({ title, description, back, loading, reload, fetchedAt }: {
  title: string; description: string; back?: { to: string; label: string };
  loading?: boolean; reload?: () => void; fetchedAt?: string | null;
}) {
  return <header className="page-header">
    <div>{back && <Link className="back-link" to={back.to}><ArrowLeft size={15} />{back.label}</Link>}
      <h1>{title}</h1><p>{description}</p></div>
    {reload && <div className="refresh-group">
      {fetchedAt && <span className="updated">读取于 {formatTime(fetchedAt)}</span>}
      <button className="button" onClick={reload} disabled={loading}><RefreshCw size={15} aria-hidden="true" className={loading ? 'spin' : ''} />刷新数据</button>
    </div>}
  </header>;
}

export function Panel({ title, subtitle, action, children, className = '' }: {
  title?: string; subtitle?: string; action?: ReactNode; children: ReactNode; className?: string;
}) {
  return <section className={`panel ${className}`}>
    {title && <div className="panel-heading"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>{action}</div>}
    {children}
  </section>;
}

export function LoadingState() {
  return <div className="empty-state" role="status"><LoaderCircle size={25} className="spin" /><h3>正在读取数据</h3><p>从本地 API 获取最新记录…</p></div>;
}
export function EmptyState({ title = '暂无记录', description = '当前查询没有返回数据。' }: { title?: string; description?: string }) {
  return <div className="empty-state"><Inbox size={28} /><h3>{title}</h3><p>{description}</p></div>;
}
export function ErrorState({ message, retry }: { message: string; retry: () => void }) {
  return <div className="empty-state error-state" role="alert"><AlertCircle size={27} /><h3>数据暂时无法读取</h3><p>{message}</p><button className="button" onClick={retry}><RefreshCw size={14} />重新加载</button></div>;
}
export function DataState({ loading, error, retry, children }: {
  loading: boolean; error: string | null; retry: () => void; children: ReactNode;
}) {
  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} retry={retry} />;
  return children;
}
export function Fields({ items }: { items: [string, ReactNode][] }) {
  return <dl className="fields">{items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value ?? '暂无记录'}</dd></div>)}</dl>;
}
export function TimeValue({ value }: { value: string | null }) {
  const formatted = formatTime(value);
  if (!value || formatted === '时间无效') return <span>{formatted}</span>;
  const [date, time] = formatted.split(' ');
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}+08:00`;
  return <time className="time-value" dateTime={normalized} title={`北京时间 ${formatted}`}><span>{date}</span><span>{time}</span></time>;
}
export function TechnicalValue({ value }: { value: string | null }) {
  return value ? <code className="technical-value">{value}</code> : <span>暂无记录</span>;
}
export function Pagination({ offset, visibleCount, loadedCount, hasMore, loading, onPage }: {
  offset: number; visibleCount: number; loadedCount: number; hasMore: boolean; loading: boolean; onPage: (offset: number) => void;
}) {
  return <div className="pagination"><span role="status">第 {offset / 20 + 1} 页 · {loading ? '正在加载当前页' : `显示 ${visibleCount} 条 / 本页已加载 ${loadedCount} 条`}{hasMore && offset >= 10000 ? ' · 已到 API 分页上限' : ''}</span>
    <div><button className="button" disabled={loading || offset === 0} onClick={() => onPage(offset - 20)}><ChevronLeft size={15} />上一页</button>
      <button className="button" disabled={loading || !hasMore || offset >= 10000} onClick={() => onPage(offset + 20)}>下一页<ChevronRight size={15} /></button></div>
  </div>;
}

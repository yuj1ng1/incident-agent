import { Search } from 'lucide-react';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { IncidentPage, SessionPage } from '../api/types';
import { SESSION_STATUSES } from '../api/types';
import { IncidentTable, SessionTable } from '../components/RecordTables';
import { DataState, EmptyState, PageHeader, Pagination, Panel, statusLabels } from '../components/ui';
import { useApi } from '../hooks/useApi';
import { pageQuery } from '../lib/format';

function useListQuery(allowed: readonly string[]) {
  const [search, setSearch] = useSearchParams();
  const { offset, status } = pageQuery(search, allowed);
  const update = (nextOffset: number, nextStatus = status) => {
    const params = new URLSearchParams();
    if (nextStatus) params.set('status', nextStatus);
    if (nextOffset) params.set('offset', String(nextOffset));
    setSearch(params);
  };
  const params = new URLSearchParams({ limit: '20', offset: String(offset) });
  if (status) params.set('status', status);
  return { offset, status, update, params };
}

function ListToolbar({ value, search, onSearch, statuses, onStatus }: {
  value: string; search: string; onSearch: (value: string) => void;
  statuses: readonly string[]; onStatus: (status: string) => void;
}) {
  return <div className="list-toolbar"><div className="search-control"><label className="search-label">搜索当前页
    <span className="search-box"><Search size={16} aria-hidden="true" /><input value={search} onChange={e => onSearch(e.target.value)} placeholder="服务名称或记录 ID" aria-describedby="search-scope" /></span></label>
    <span className="search-hint" id="search-scope">仅筛选本页已加载记录</span></div>
    <label className="status-filter">状态<select value={value} onChange={e => onStatus(e.target.value)}><option value="">全部状态</option>{statuses.map(status => <option key={status} value={status}>{statusLabels[status] || status}</option>)}</select></label>
  </div>;
}

const INCIDENT_STATUSES = ['investigating', 'resolved'];
export function IncidentListPage() {
  const { offset, status, update, params } = useListQuery(INCIDENT_STATUSES);
  const result = useApi<IncidentPage>(`/api/incidents?${params}`);
  const [search, setSearch] = useState('');
  const needle = search.trim().toLowerCase();
  const items = (result.data?.items || []).filter(item => `${item.service} ${item.incident_id}`.toLowerCase().includes(needle));
  return <><PageHeader title="Incidents" description="查看服务告警事件、严重程度和最近调查时间。" loading={result.loading} reload={result.reload} fetchedAt={result.fetchedAt} />
    <Panel><ListToolbar value={status} search={search} onSearch={setSearch} statuses={INCIDENT_STATUSES} onStatus={value => { setSearch(''); update(0, value); }} />
      <DataState loading={result.loading} error={result.error} retry={result.reload}>
        {items.length ? <IncidentTable items={items} /> : <EmptyState title={search ? '当前页没有匹配记录' : '暂无 Incident'} description={search ? '尝试其他关键词，或清空搜索后翻页。' : '当前筛选条件没有事件记录。'} />}
      </DataState>
      {!result.error && <Pagination offset={offset} visibleCount={items.length} loadedCount={result.data?.items.length || 0} hasMore={result.data?.has_more || false} loading={result.loading} onPage={value => { setSearch(''); update(value); }} />}
    </Panel>
  </>;
}

export function SessionListPage() {
  const { offset, status, update, params } = useListQuery(SESSION_STATUSES);
  const result = useApi<SessionPage>(`/api/sessions?${params}`);
  const [search, setSearch] = useState('');
  const needle = search.trim().toLowerCase();
  const items = (result.data?.items || []).filter(item => `${item.service} ${item.session_id} ${item.incident_id}`.toLowerCase().includes(needle));
  return <><PageHeader title="Sessions" description="查看每次调查的运行状态与累计模型、工具使用次数。" loading={result.loading} reload={result.reload} fetchedAt={result.fetchedAt} />
    <Panel><ListToolbar value={status} search={search} onSearch={setSearch} statuses={SESSION_STATUSES} onStatus={value => { setSearch(''); update(0, value); }} />
      <DataState loading={result.loading} error={result.error} retry={result.reload}>
        {items.length ? <SessionTable items={items} /> : <EmptyState title={search ? '当前页没有匹配记录' : '暂无 Session'} description={search ? '尝试其他关键词，或清空搜索后翻页。' : '当前筛选条件没有调查记录。'} />}
      </DataState>
      {!result.error && <Pagination offset={offset} visibleCount={items.length} loadedCount={result.data?.items.length || 0} hasMore={result.data?.has_more || false} loading={result.loading} onPage={value => { setSearch(''); update(value); }} />}
    </Panel>
  </>;
}

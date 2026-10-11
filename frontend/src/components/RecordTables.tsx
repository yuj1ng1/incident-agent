import { ArrowUpRight } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { IncidentSummary, SessionSummary } from '../api/types';
import { formatTime, recordPath } from '../lib/format';
import { StatusBadge } from './ui';

export function IncidentTable({ items }: { items: IncidentSummary[] }) {
  return <div className="table-scroll"><table><caption className="sr-only">Incident 列表</caption><thead><tr>
    <th>服务 / Incident</th><th>严重程度</th><th>状态</th><th>最近调查时间（北京）</th><th><span className="sr-only">详情</span></th>
  </tr></thead><tbody>{items.map(item => <tr key={item.incident_id}>
    <td><Link className="record-link" to={recordPath('incidents', item.incident_id)}>{item.service}</Link><span className="record-id" title={item.incident_id}>{item.incident_id}</span></td>
    <td><StatusBadge value={item.severity} /></td><td><StatusBadge value={item.status} /></td>
    <td className="time-cell">{formatTime(item.last_investigated_at)}</td>
    <td><Link className="icon-link" aria-label={`查看 Incident ${item.incident_id}`} to={recordPath('incidents', item.incident_id)}><ArrowUpRight size={17} /></Link></td>
  </tr>)}</tbody></table></div>;
}

export function SessionTable({ items }: { items: SessionSummary[] }) {
  return <div className="table-scroll"><table><caption className="sr-only">Session 列表</caption><thead><tr>
    <th>服务 / Session</th><th>运行状态</th><th>模型调用</th><th>工具执行</th><th>最近更新（北京）</th><th><span className="sr-only">详情</span></th>
  </tr></thead><tbody>{items.map(item => <tr key={item.session_id}>
    <td><Link className="record-link" to={recordPath('sessions', item.session_id)}>{item.service}</Link><span className="record-id" title={item.session_id}>{item.session_id}</span></td>
    <td><StatusBadge value={item.status} /></td><td className="numeric">{item.total_model_requests}</td><td className="numeric">{item.total_tool_executions}</td>
    <td className="time-cell">{formatTime(item.updated_at)}</td>
    <td><Link className="icon-link" aria-label={`查看 Session ${item.session_id}`} to={recordPath('sessions', item.session_id)}><ArrowUpRight size={17} /></Link></td>
  </tr>)}</tbody></table></div>;
}

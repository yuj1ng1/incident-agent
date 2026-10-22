import { useEffect, useRef, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { postJson } from '../api/client';
import type { RemediationDetail, RemediationPage, RemediationSummary } from '../api/types';
import { OperatorAccess, useOperator } from '../components/OperatorAccess';
import { DataState, EmptyState, Fields, PageHeader, Pagination, Panel, StatusBadge } from '../components/ui';
import { useApi } from '../hooks/useApi';
import { formatTime, pageQuery } from '../lib/format';
import { reasonLabel, remediationLabels, remediationState } from '../lib/remediation';

const pathFor = (id: string) => `/remediations/${encodeURIComponent(id)}`;
function State({ item }: { item: RemediationSummary }) {
  const tone = ['failed', 'verification_failed'].includes(item.status) || item.execution_outcome === 'unknown' ? 'red'
    : item.status === 'succeeded' ? 'green' : item.status === 'pending' ? 'amber' : 'gray';
  return <StatusBadge value={item.status} label={remediationState(item)} tone={tone} />;
}

export function RemediationListPage() {
  const [params, setParams] = useSearchParams();
  const { offset } = pageQuery(params, []);
  const incidentId = params.get('incident_id');
  const result = useApi<RemediationPage>(`/api/remediations?limit=20&offset=${offset}${incidentId ? `&incident_id=${encodeURIComponent(incidentId)}` : ''}`);
  return <><PageHeader title="Remediations" description="人工审批、动作执行、独立业务验证与关联补偿。" reload={result.reload} loading={result.loading} fetchedAt={result.fetchedAt} />
    <Panel><DataState loading={result.loading} error={result.error} retry={result.reload}>
      {result.data?.items.length ? <div className="table-wrap"><table><thead><tr><th>提案 / 服务</th><th>动作</th><th>状态</th><th>创建时间</th></tr></thead>
        <tbody>{result.data.items.map(item => <tr key={item.proposal_id}>
          <td><Link className="record-link" to={pathFor(item.proposal_id)}>{item.proposal_id}</Link><div>{item.service}{item.original_proposal_id && ' · 补偿提案'}</div></td>
          <td>{item.action_type}</td><td><State item={item} /></td><td>{formatTime(item.created_at)}</td>
        </tr>)}</tbody></table></div> : <EmptyState title="暂无 Remediation" description="尚未生成修复或补偿提案。" />}
    </DataState>{!result.error && <Pagination offset={offset} visibleCount={result.data?.items.length || 0} loadedCount={result.data?.items.length || 0}
      hasMore={result.data?.has_more || false} loading={result.loading} onPage={value => setParams({ ...(incidentId ? { incident_id: incidentId } : {}), ...(value ? { offset: String(value) } : {}) })} />}</Panel>
  </>;
}

export function RemediationDetailPage() {
  const { proposalId = '' } = useParams();
  return <RemediationRecord key={proposalId} proposalId={proposalId} />;
}

function RemediationRecord({ proposalId }: { proposalId: string }) {
  const apiPath = `/api/remediations/${encodeURIComponent(proposalId)}`;
  const result = useApi<RemediationDetail>(apiPath);
  const [record, setRecord] = useState<RemediationDetail | null>(null);
  const { token, operator } = useOperator();
  const [reason, setReason] = useState('');
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const inFlight = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const commands = useRef<Record<string, { payload: string; id: string }>>({});
  useEffect(() => { if (result.data) { setRecord(result.data); setConfirm(false); setError(''); } }, [result.data]);
  useEffect(() => () => controller.current?.abort(), []);

  async function submit(operation: string) {
    if (!record || !operator || inFlight.current) return;
    inFlight.current = true; setBusy(operation); setError(''); setNotice('');
    const needsReason = ['compensation-requests', 'reject', 'close-execution'].includes(operation);
    const body = { proposal_digest: record.proposal_digest, ...(needsReason ? { reason: reason.trim() } : {}),
      ...(operation === 'compensation-requests' ? { target_id: record.compensation.target?.target_id || null } : {}) };
    const payload = JSON.stringify(body);
    const old = commands.current[operation];
    const commandId = old?.payload === payload ? old.id : crypto.randomUUID();
    commands.current[operation] = { payload, id: commandId };
    controller.current = new AbortController();
    try {
      const current = await postJson<RemediationDetail>(`${apiPath}/${operation}`, token, { ...body, command_id: commandId }, controller.current.signal);
      if (current.proposal_id !== proposalId) throw new Error('返回记录身份不匹配，请刷新核查。');
      setRecord(current); setConfirm(false);
      setNotice(operation === 'compensation-requests' ? '申请已记录，请查看下方资格评估与关联提案。' : '操作响应已收到，请核对当前状态和独立验证结果。');
    } catch (failure) {
      setError(`${(failure as Error).message} 当前页面保留操作前的记录，请刷新核查结果。`);
    } finally { inFlight.current = false; setBusy(''); }
  }
  const can = (permission: 'request' | 'approve' | 'execute') => Boolean(operator?.permissions.includes(permission));
  const disabled = Boolean(busy || result.loading || result.error || error);
  const target = record?.target || record?.compensation.target;
  return <><PageHeader title="Remediation 详情" description="动作结果与业务恢复分别记录；所有执行均经过后端审批和状态校验。"
    back={{ to: '/remediations', label: '返回 Remediations' }} reload={result.reload} loading={result.loading || Boolean(busy)} fetchedAt={result.fetchedAt} />
    {!record ? <DataState loading={result.loading} error={result.error} retry={result.reload}><EmptyState /></DataState> : <>
      {result.error && <p role="alert" className="operation-error">{result.error} · 保留上次读取内容，写操作暂不可用。</p>}
      {result.loading && <p role="status">正在刷新记录…</p>}
      <div className="detail-heading"><State item={record} /><span>{record.original_proposal_id ? '补偿提案' : '原修复提案'} · {record.service}</span></div>
      <div className="detail-grid"><div className="detail-main">
        <Panel title={record.summary}><Fields items={[
          ['提案', <code>{record.proposal_id}</code>], ['Incident', <Link className="record-link" to={`/incidents/${encodeURIComponent(record.incident_id)}`}>{record.incident_id}</Link>],
          ['动作', record.action_type], ['参数摘要', JSON.stringify(record.parameters)],
          ['其他参数', `${record.hidden_parameter_count} 项未公开；审批绑定全部原始参数`],
          ['申请理由', record.reason], ['风险', record.risk], ['预期版本', <code>{record.expected_commit_sha}</code>],
          ['审批摘要', <code>{record.proposal_digest}</code>], ['动作结果', remediationLabels[record.execution_outcome]],
        ]} /></Panel>
        <Panel title="独立业务验证" subtitle="这是本提案动作后的验证记录。人工核查仅描述核查时的业务状态，不能证明历史动作已执行。">
          {record.verifications.length ? record.verifications.map(v => <article className="remediation-entry" key={v.verification_id}>
            <h3>{v.purpose === 'manual_check' ? '人工核查' : '动作后验证'} · {v.outcome === 'failed' ? '业务验证明确失败' : remediationLabels[v.outcome]}</h3>
            <p>{formatTime(v.verified_at)} · {v.verification_id}</p>
            <p>证据状态：{remediationLabels[v.evidence_status]}。验证只代表记录时的观测。</p>
            {v.outcome === 'unknown' && <p>历史 success 字段不构成当前业务恢复证明。</p>}
            <ul>{v.checks.map(check => <li key={check.name}>{check.name}：{check.success === null ? '未知' : check.success ? '条件符合' : '条件未通过'}{check.http_status !== null && ` · HTTP ${check.http_status}`}</li>)}</ul>
          </article>) : <EmptyState title="尚无独立业务验证" />}
        </Panel>
        <Panel title="动作执行记录" subtitle="执行成功不等于业务恢复成功。结果未知时禁止盲目重试。">
          {record.actions.length ? record.actions.map(action => <article className="remediation-entry" key={action.action_id}>
            <h3>{remediationLabels[action.execution_outcome]} · {action.status}</h3><p>{formatTime(action.started_at)} → {formatTime(action.finished_at)}</p>
          </article>) : <EmptyState title={record.status === 'executing' ? '执行结果尚未持久化 · 需要等待或核查' : '暂无动作执行记录'} />}
        </Panel>
        <Panel title="补偿资格与关联" subtitle="缺少安全目标时只记录阻断申请，不创建可执行的虚假动作。">
          <div className="remediation-entry"><StatusBadge value={record.compensation.status} label={remediationLabels[record.compensation.status]} tone={record.compensation.status === 'blocked' ? 'amber' : 'blue'} />
            <p>{reasonLabel(record.compensation.reason)}</p>
            {target && <Fields items={[
              ['补偿目标', target.target_id], ['目标版本', <code>{target.commit_sha}</code>], ['制品 SHA-256', <code>{target.artifact_sha256}</code>],
              ['配置 SHA-256', <code>{target.configuration_digest}</code>], ['前置条件', target.preconditions],
              ['预期副作用', target.side_effects], ['业务验证标准', target.verification_policy], ['数据兼容条件', target.data_compatibility],
            ]} />}
            {record.original_proposal_id && <p>原提案：<Link className="record-link" to={pathFor(record.original_proposal_id)}>{record.original_proposal_id}</Link></p>}
            {record.related_proposals.map(p => <p key={p.proposal_id}><Link className="record-link" to={pathFor(p.proposal_id)}>{p.proposal_id}</Link> · {remediationState(p)}</p>)}
          </div>
          {record.requests.map(request => <article className="remediation-entry" key={request.assessment_id}>
            <h3>{remediationLabels[request.status]} · {request.trigger === 'operator_request' ? '人工申请' : '验证失败自动评估'}</h3>
            <p>{request.request_reason}</p><p>{reasonLabel(request.reason)}</p><p>{request.requested_by} · {formatTime(request.evaluated_at)}</p>
          </article>)}
        </Panel>
        <Panel title="审计时间线" subtitle="最多展示最近 100 条事件，原始记录保存在后端 Store。">
          {record.audit.length ? <ol className="remediation-audit">{record.audit.map(event => <li key={event.event_id}>
            <time>{formatTime(event.occurred_at)}</time><strong>{event.event}</strong><span>{event.actor}</span>{event.reason && <p>{event.reason}</p>}
          </li>)}</ol> : <EmptyState title="暂无审计事件" />}
        </Panel>
      </div><aside className="detail-aside">
        <Panel title="审批记录"><Fields items={[
          ['批准人', record.approved_by], ['批准时间', formatTime(record.approved_at)], ['审批内容一致', record.approval_valid ? '是' : '未批准或摘要不匹配'],
          ['拒绝人', record.rejected_by], ['拒绝时间', formatTime(record.rejected_at)], ['拒绝理由', record.rejection_reason], ['收口理由', record.closure_reason],
        ]} /></Panel>
        <Panel title="受控操作"><OperatorAccess />
          <div className="remediation-controls">
            {!operator && <p>请连接操作身份后申请、审批或执行。</p>}
            {operator && <>
              <label>申请 / 拒绝 / 收口理由<textarea value={reason} onChange={e => setReason(e.target.value)} rows={4} maxLength={1000} disabled={Boolean(busy)} /></label>
              {!record.original_proposal_id && ['succeeded', 'verification_failed'].includes(record.status) && record.compensation.status !== 'already_requested' && <>
                {!can('request') && <p>当前身份无申请权限。</p>}
                <button className="button" disabled={disabled || !can('request') || !reason.trim()} onClick={() => submit('compensation-requests')}>
                  {record.compensation.status === 'blocked' ? '记录被阻断的回滚申请' : '申请回滚'}</button>
              </>}
              {record.status === 'pending' && <>
                <p>批准仅授权当前提案摘要；原修复审批不会批准补偿。</p>
                {!can('approve') && <p>当前身份无审批权限。</p>}
                <button className="button" disabled={disabled || !can('approve')} onClick={() => submit('approve')}>批准此提案</button>
                <button className="button" disabled={disabled || !can('approve') || !reason.trim()} onClick={() => submit('reject')}>拒绝此提案</button>
              </>}
              {record.status === 'approved' && <>
                {!can('execute') && <p>当前身份无执行权限。</p>}
                <label className="confirm-operation"><input type="checkbox" checked={confirm} onChange={e => setConfirm(e.target.checked)} disabled={disabled} />确认执行已批准的精确目标和参数</label>
                <button className="button" disabled={disabled || !can('execute') || !record.approval_valid || !confirm} onClick={() => submit('execute')}>执行已批准动作</button>
              </>}
              {(record.status === 'executing' || (record.status === 'failed' && record.execution_outcome === 'unknown')) && <>
                <p>执行可能仍在进行或已经中断。核查不会重放动作，人工收口也不会将未知结果改写为成功。</p>
                <button className="button" disabled={disabled || !can('execute')} onClick={() => submit('inspect-execution')}>核查当前业务</button>
                {record.status === 'executing' && <button className="button" disabled={disabled || !can('execute') || !reason.trim()} onClick={() => submit('close-execution')}>人工收口</button>}
              </>}
            </>}
            {busy && <p role="status">请求处理中，请勿重复操作…</p>}
            {error && <p role="alert" className="operation-error">{error}</p>}
            {notice && <p role="status">{notice}</p>}
          </div>
        </Panel>
      </aside></div>
    </>}
  </>;
}

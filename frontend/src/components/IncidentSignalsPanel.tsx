import type { IncidentSignals, MetricSignal, SignalMetric, SignalStatus, SignalWindow } from '../api/types';
import { useApi } from '../hooks/useApi';
import { formatTime } from '../lib/format';
import { DataState, EmptyState, Panel } from './ui';

const metrics: Record<SignalMetric, { title: string; unit: string; scale: number; decimals: number }> = {
  http_5xx_rate: { title: 'HTTP 5xx 比例', unit: '%', scale: 100, decimals: 1 },
  http_latency_p95_seconds: { title: 'P95 响应延迟', unit: 'ms', scale: 1000, decimals: 1 },
  process_cpu_cores: { title: '进程 CPU', unit: '核', scale: 1, decimals: 3 },
  process_resident_memory_bytes: { title: '进程常驻内存', unit: 'MiB', scale: 1 / (1024 ** 2), decimals: 1 },
};

const timeOfDay = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
});

function SignalState({ status, reason, label }: { status: SignalStatus; reason: string | null; label: string }) {
  if (status === 'error') return <p className="signal-message signal-error" role="status">{label}查询失败</p>;
  const unconfigured = reason?.endsWith('_not_configured') || reason === 'service_not_registered'
    || reason === 'metric_not_supported_by_profile';
  return <p className="signal-message">{unconfigured ? `此服务暂无该项${label}` : `这个时间窗没有${label}样本`}</p>;
}

function MetricChart({ signal, window }: { signal: MetricSignal; window: SignalWindow }) {
  const { title, unit, scale, decimals } = metrics[signal.metric];
  const last = signal.samples.at(-1);
  const hints = signal.infos?.length ? <aside className="signal-message signal-infos" aria-label="Prometheus 查询提示">
    <p>Prometheus 查询提示</p>
    <ul>{signal.infos.map((info, index) => <li key={index}>{info}</li>)}</ul>
  </aside> : null;
  if (signal.status !== 'ok' || !last) {
    return <article className="signal-metric"><h3>{title}</h3>
      <SignalState status={signal.status} reason={signal.reason} label="指标" />
      {hints}
    </article>;
  }

  const from = Date.parse(window.start);
  const to = Date.parse(window.end);
  const measuredMax = Math.max(...signal.samples.map(sample => sample.value * scale), 10 ** -decimals);
  const magnitude = 10 ** Math.floor(Math.log10(measuredMax));
  const top = signal.metric === 'http_5xx_rate' ? 100 : Math.ceil(measuredMax / magnitude) * magnitude;
  const display = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: decimals });
  const width = 320;
  const height = 96;
  const points = signal.samples.map(sample => {
    const time = Date.parse(sample.evaluated_at);
    return {
      time,
      x: 2 + (width - 4) * (time - from) / (to - from),
      y: 2 + (height - 4) * (1 - sample.value * scale / top),
    };
  });
  const path = points.map((point, index) => {
    const joinsPrevious = index > 0 && point.time - points[index - 1].time === window.step_seconds * 1000;
    return `${joinsPrevious ? 'L' : 'M'} ${point.x} ${point.y}`;
  }).join(' ');

  return <article className="signal-metric">
    <div className="signal-metric-heading">
      <h3>{title}</h3>
      <span title={`末次采样于 ${formatTime(last.evaluated_at)}`}><strong>{display(last.value * scale)}</strong> {unit}</span>
    </div>
    <div className="signal-plot">
      <div className="signal-y-axis" aria-hidden="true"><span>{display(top)}</span><span>{display(top / 2)}</span><span>0</span></div>
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img"
        aria-label={`${title}曲线，${signal.samples.length} 个样本，末次采样 ${display(last.value * scale)} ${unit}`}>
        {[2, height / 2, height - 2].map(y =>
          <line className="signal-grid-line" key={y} x1="0" y1={y} x2={width} y2={y} vectorEffect="non-scaling-stroke" />)}
        <path className="signal-line" d={path} vectorEffect="non-scaling-stroke" />
        {points.map(point => <circle className="signal-point" key={point.time} cx={point.x} cy={point.y} r="1.8" />)}
      </svg>
    </div>
    <div className="signal-x-axis" aria-hidden="true"><span>{timeOfDay.format(from)}</span><span>{timeOfDay.format(to)}</span></div>
    {hints}
  </article>;
}

export function IncidentSignalsPanel({ incidentId }: { incidentId: string }) {
  const result = useApi<IncidentSignals>(`/api/incidents/${encodeURIComponent(incidentId)}/signals`);
  const signals = result.data;
  const window = signals?.window;
  return <Panel title="告警时间窗观测" className="signals-panel"
    subtitle={window ? `${formatTime(window.start)} 至 ${formatTime(window.end)} · 北京时间` : undefined}
    action={<button className="button" onClick={result.reload} disabled={result.loading}>刷新观测</button>}>
    <DataState loading={result.loading} error={result.error} retry={result.reload}>
      {signals && window ? <>
        <div className="signal-charts">{signals.metrics.map(signal => <MetricChart key={signal.metric} signal={signal} window={window} />)}</div>
        <section className="signal-logs" aria-label="服务日志">
          <h3>服务日志</h3>
          {signals.logs.status === 'ok' ? <>
            {signals.logs.truncated && <p className="signal-message">部分日志已截断。</p>}
            <ol tabIndex={0} aria-label="日志记录">
              {signals.logs.entries.map((log, index) => <li key={`${log.timestamp}-${index}`}>
                <time dateTime={log.timestamp}>{formatTime(log.timestamp)}</time>
                <code>{log.line}</code>
              </li>)}
            </ol>
          </> : <SignalState status={signals.logs.status} reason={signals.logs.reason} label="日志" />}
        </section>
      </> : <EmptyState title="暂无告警时间" description="保存告警时间后即可查询相关指标和日志。" />}
    </DataState>
  </Panel>;
}

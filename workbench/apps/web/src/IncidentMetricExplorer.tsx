import { useEffect, useId, useMemo, useState, type PointerEvent } from 'react';
import { api, id } from './api';
import type { Language } from './i18n';
import './incidentMetricExplorer.css';

type MetricPoint = { at: string; value: number };
type MetricSeries = {
  id: string;
  title: string;
  title_zh?: string;
  group: 'requests' | 'resources';
  unit: string;
  source: string;
  scope?: string;
  threshold?: number;
  points: MetricPoint[];
};
type MetricResponse = {
  incident_id: string;
  start: string;
  end: string;
  alert_at?: string;
  status: 'available' | 'partial' | 'unavailable';
  series: MetricSeries[];
};
type Sample = { at: number; value: number };
type ChartSeries = Omit<MetricSeries, 'points'> & { points: Sample[] };
type RequestState = { caseId: string; data?: MetricResponse; error?: true };

const copy = {
  en: {
    explore: 'Explore metrics', intro: 'Inspect measurements in the incident window',
    requests: 'Service signals', resources: 'Resource usage', host: 'Host', process: 'Process', service: 'Service',
    loading: 'Loading incident metrics…', refresh: 'Reload metrics',
    failure: 'The metric history could not be loaded.',
    unavailable: 'No time-series samples are available for this incident window.',
    partial: 'Some measurements are missing from this window. Charts show the samples returned by the source.',
    window: 'Window (UTC)', cursor: 'Inspect time', nearest: 'Nearest recorded sample',
    alert: 'Alert', threshold: 'Threshold', samples: 'samples', source: 'Source',
    archiveUnits: 'Archive units',
  },
  zh: {
    explore: '查看详细指标', intro: '查看事故时间窗内的观测数据',
    requests: '服务指标', resources: '资源使用', host: '主机', process: '进程', service: '服务',
    loading: '正在读取事故指标…', refresh: '重新加载指标',
    failure: '暂时无法读取指标历史。',
    unavailable: '这条事故的时间窗内没有可用的时序采样。',
    partial: '该时间窗有部分指标缺失。图表展示数据源返回的采样点。',
    window: '时间窗（UTC）', cursor: '查看时间点', nearest: '最接近的已记录样本',
    alert: '报警', threshold: '阈值', samples: '个样本', source: '数据来源',
    archiveUnits: '原始归档单位',
  },
} as const;
type Copy = typeof copy[Language];

const metricNames: Record<string, string> = {
  checkout_latency: '结账请求延迟',
  probe_throughput: '探测请求吞吐量',
  probe_error_rate: '探测请求错误率',
  payment_latency: '支付调用延迟',
  journal_throughput: '已记录请求吞吐量',
  journal_error_rate: '已记录 HTTP 5xx 错误率',
  lab_process_cpu: '测试进程 CPU 使用率',
  lab_process_memory: '测试进程常驻内存',
  lab_host_cpu: '测试主机 CPU 使用率',
  lab_host_memory: '测试主机内存使用率',
};

const archiveNames: Record<string, string> = {
  cpu_usage_total: 'CPU 用量', memory_usage: '内存用量', rx_bytes: '网络接收', tx_bytes: '网络发送',
};

const scopeNames: Record<string, string> = {
  'Checkout probe · latest recorded request': '结账探测 · 最近实测请求',
  'Checkout probe · 30 s rate': '结账探测 · 30 秒速率',
  'Checkout probe · error share over 30 s': '结账探测 · 30 秒错误占比',
  'Local checkout and payment lab process · 30 s CPU rate': '本地结账和支付测试进程 · 30 秒 CPU 速率',
  'Local checkout and payment lab process': '本地结账和支付测试进程',
  'Local test host · all CPU cores': '本地测试主机 · 所有 CPU 核心',
  'Local test host · physical memory': '本地测试主机 · 物理内存',
  'Observed requests from the same checkout route around the recorded alert': '报警前后同一结账路由的实测请求',
};

function metricTitle(series: ChartSeries, locale: string) {
  if (locale !== 'zh-CN') return series.title;
  if (series.title_zh || metricNames[series.id]) return series.title_zh ?? metricNames[series.id];
  const suffix = Object.keys(archiveNames).find(key => series.id.endsWith(`-${key}`));
  return suffix ? `${series.id.slice(0, -suffix.length - 1)} · ${archiveNames[suffix]}` : series.title;
}

const width = 416;
const height = 130;
const plot = { left: 46, right: 408, top: 10, bottom: 116 };

function nearestIndex(times: number[], target: number) {
  let low = 0;
  let high = times.length - 1;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (times[middle] < target) low = middle + 1;
    else high = middle;
  }
  return low > 0 && target - times[low - 1] <= times[low] - target ? low - 1 : low;
}

function measuredUnit(unit: string, points: Sample[]) {
  if (!['byte', 'bytes', 'B'].includes(unit)) return { factor: 1, unit };
  const magnitude = Math.max(...points.map(point => Math.abs(point.value)));
  const units = [[2 ** 30, 'GiB'], [2 ** 20, 'MiB'], [2 ** 10, 'KiB']] as const;
  const match = units.find(([factor]) => magnitude >= factor);
  return match ? { factor: match[0], unit: match[1] } : { factor: 1, unit: 'B' };
}

function chartSegments(points: Sample[], x: (at: number) => number, y: (value: number) => number) {
  const steps = points.slice(1).map((point, index) => point.at - points[index].at).sort((a, b) => a - b);
  const maximumGap = steps.length > 1 ? steps[Math.floor((steps.length - 1) / 2)] * 3 : Infinity;
  return points.map((point, index) => `${index && point.at - points[index - 1].at <= maximumGap ? 'L' : 'M'}${x(point.at)} ${y(point.value)}`).join(' ');
}

function MetricChart({ series, start, end, cursor, alertAt, locale, t, onInspect }: {
  series: ChartSeries; start: number; end: number; cursor: number; alertAt?: number; locale: string; t: Copy; onInspect: (time: number) => void;
}) {
  const title = metricTitle(series, locale);
  const { factor, unit: measured } = measuredUnit(series.unit, series.points);
  const unit = measured.toLowerCase() === 'archive units' ? t.archiveUnits
    : measured === '% of one core' && locale === 'zh-CN' ? '单核 %' : measured;
  const scope = series.scope === 'host' || series.scope === 'process' || series.scope === 'service' ? t[series.scope]
    : locale === 'zh-CN' && series.scope ? scopeNames[series.scope] ?? series.scope : series.scope;
  const threshold = series.threshold !== undefined && Number.isFinite(series.threshold) ? series.threshold : undefined;
  const values = series.points.map(point => point.value / factor);
  const minimum = Math.min(0, ...values, (threshold ?? 0) / factor);
  const maximum = Math.max(...values, measured === '%' ? 100 : 0, (threshold ?? 0) / factor) || 1;
  const x = (at: number) => plot.left + (at - start) / Math.max(1, end - start) * (plot.right - plot.left);
  const y = (raw: number) => plot.bottom - (raw / factor - minimum) / (maximum - minimum) * (plot.bottom - plot.top);
  const number = new Intl.NumberFormat(locale, { maximumFractionDigits: 3 });
  const time = new Intl.DateTimeFormat(locale, { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', timeZone: 'UTC' });
  const format = (value: number) => value !== 0 && Math.abs(value) < .001 ? value.toExponential(1) : number.format(value);
  const nearest = series.points[nearestIndex(series.points.map(point => point.at), cursor)];
  const ticks = [maximum, (maximum + minimum) / 2, minimum];
  const hasAlert = alertAt !== undefined && alertAt >= start && alertAt <= end;

  function inspect(event: PointerEvent<SVGSVGElement>) {
    const bounds = event.currentTarget.getBoundingClientRect();
    const offset = ((event.clientX - bounds.left) / bounds.width * width - plot.left) / (plot.right - plot.left);
    onInspect(start + Math.max(0, Math.min(1, offset)) * (end - start));
  }

  return <article className="metric-explorer-chart">
    <header>
      <div><h5>{title}</h5>{scope && <span className="metric-explorer-scope">{scope}</span>}</div>
      <p className="metric-explorer-reading">{format(nearest.value / factor)} <span>{unit}</span></p>
    </header>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${title} · ${series.points.length} ${t.samples}`} onPointerMove={inspect} onPointerDown={inspect}>
      {ticks.map(value => <g key={value}>
        <line className="metric-explorer-gridline" x1={plot.left} x2={plot.right} y1={y(value * factor)} y2={y(value * factor)} />
        <text x={plot.left - 5} y={y(value * factor) + 3} textAnchor="end">{format(value)}</text>
      </g>)}
      {hasAlert && <line className="metric-explorer-alert" x1={x(alertAt)} x2={x(alertAt)} y1={plot.top} y2={plot.bottom} />}
      {threshold !== undefined && <line className="metric-explorer-threshold" x1={plot.left} x2={plot.right} y1={y(threshold)} y2={y(threshold)} />}
      <line className="metric-explorer-crosshair" x1={x(cursor)} x2={x(cursor)} y1={plot.top} y2={plot.bottom} />
      <path className="metric-explorer-trace" d={chartSegments(series.points, x, y)} />
      <circle className="metric-explorer-sample" cx={x(nearest.at)} cy={y(nearest.value)} r="3.4" />
    </svg>
    <div className="metric-explorer-axis"><span>{time.format(start)}</span><span>{time.format(end)}</span></div>
    {threshold !== undefined && <p className="metric-explorer-provenance">{t.threshold} · {format(threshold / factor)} {unit}</p>}
    <p className="metric-explorer-provenance">{t.nearest} <time dateTime={new Date(nearest.at).toISOString()}>{time.format(nearest.at)} UTC</time> · {series.points.length} {t.samples}</p>
    <p className="metric-explorer-provenance">{t.source} · {series.source}</p>
  </article>;
}

function MetricPlots({ response, language }: { response: MetricResponse; language: Language }) {
  const t = copy[language];
  const locale = language === 'zh' ? 'zh-CN' : 'en-US';
  const cursorId = useId();
  const [inspectAt, setInspectAt] = useState<number>();
  const start = Date.parse(response.start);
  const end = Date.parse(response.end);
  const alertAt = response.alert_at ? Date.parse(response.alert_at) : NaN;
  const series = useMemo(() => response.series.flatMap<ChartSeries>(item => {
    const byTime = new Map<number, number>();
    item.points.forEach(point => {
      const at = Date.parse(point.at);
      if (at >= start && at <= end && Number.isFinite(point.value)) byTime.set(at, point.value);
    });
    const points = [...byTime].sort(([a], [b]) => a - b).map(([at, value]) => ({ at, value }));
    return points.length ? [{ ...item, points }] : [];
  }), [response.series, start, end]);
  const times = useMemo(() => [...new Set(series.flatMap(item => item.points.map(point => point.at)))].sort((a, b) => a - b), [series]);
  if (!series.length || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    return <p className="metric-explorer-state" role="status">{t.unavailable}</p>;
  }
  const index = nearestIndex(times, inspectAt ?? (Number.isFinite(alertAt) ? alertAt : times[times.length - 1]));
  const cursor = times[index];
  const time = new Intl.DateTimeFormat(locale, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', timeZone: 'UTC' });
  return <>
    <div className="metric-explorer-window">
      <span>{t.window}</span><strong>{time.format(start)} → {time.format(end)}</strong>
      {Number.isFinite(alertAt) && alertAt >= start && alertAt <= end && <span className="metric-explorer-alert-key">{t.alert} · {time.format(alertAt)}</span>}
    </div>
    {response.status === 'partial' && <p className="metric-explorer-state" role="status">{t.partial}</p>}
    <div className="metric-explorer-cursor">
      <label htmlFor={cursorId}>{t.cursor}</label>
      <input id={cursorId} type="range" min={0} max={times.length - 1} value={index} disabled={times.length === 1}
        aria-valuetext={`${time.format(cursor)} UTC`} onChange={event => setInspectAt(times[Number(event.currentTarget.value)])} />
      <time dateTime={new Date(cursor).toISOString()}>{time.format(cursor)} UTC</time>
    </div>
    {(['requests', 'resources'] as const).map(group => {
      const matching = series.filter(item => item.group === group);
      if (!matching.length) return null;
      return <section className="metric-explorer-group" key={group} aria-label={t[group]}>
        <h4>{t[group]}</h4>
        <div className="metric-explorer-grid">{matching.map(item => <MetricChart key={item.id} series={item} start={start} end={end}
          cursor={cursor} alertAt={alertAt} locale={locale} t={t} onInspect={at => setInspectAt(times[nearestIndex(times, at)])} />)}</div>
      </section>;
    })}
  </>;
}

export default function IncidentMetricExplorer({ incidentId, language }: { incidentId: string; language: Language }) {
  const t = copy[language];
  const [expanded, setExpanded] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [response, setResponse] = useState<RequestState>();
  useEffect(() => {
    if (!expanded) return;
    const controller = new AbortController();
    setResponse(undefined);
    void api<MetricResponse>(`/incident-metrics/${id(incidentId)}`, { signal: controller.signal }).then(data => {
      if (controller.signal.aborted) return;
      if (data.incident_id !== incidentId || !Array.isArray(data.series)) throw new Error('Unexpected metric record');
      setResponse({ caseId: incidentId, data });
    }).catch(() => {
      if (!controller.signal.aborted) setResponse({ caseId: incidentId, error: true });
    });
    return () => controller.abort();
  }, [expanded, incidentId, attempt]);

  const current = response?.caseId === incidentId ? response : undefined;
  return <details className="metric-explorer" onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary><span><strong>{t.explore}</strong><small>{t.intro}</small></span><span className="metric-explorer-chevron" aria-hidden="true">›</span></summary>
    {expanded && <div className="metric-explorer-body" aria-busy={!current}>
      {!current ? <p className="metric-explorer-state" role="status">{t.loading}</p>
        : current.error ? <div className="metric-explorer-state" role="status">{t.failure}<button type="button" onClick={() => setAttempt(value => value + 1)}>{t.refresh}</button></div>
          : current.data && <MetricPlots response={current.data} language={language} />}
    </div>}
  </details>;
}

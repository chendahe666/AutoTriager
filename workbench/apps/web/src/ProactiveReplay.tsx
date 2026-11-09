import { useEffect, useRef, useState, type ReactNode } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import Spinner from '@cloudscape-design/components/spinner';
import Tabs from '@cloudscape-design/components/tabs';
import { api, errorText } from './api';
import type { Language } from './i18n';
import './proactiveReplay.css';

const datasets = ['petshop', 'eadro'] as const;
type Dataset = typeof datasets[number];
type Chart = {
  label: string;
  unit: string;
  threshold?: number;
  points: { time: string; observed?: number; forecast?: number }[];
};
type Experiment = {
  id: Dataset;
  source: string;
  title: string;
  scope: string;
  summary: string;
  metrics: { label: string; value: string; note: string }[];
  timeline: { time: string; title: string; state: string; evidence_ids: string[] }[];
  evidence: { id: string; title: string; detail: string; source_url?: string }[];
  actions: { label: string; detail: string; status: string }[];
  chart?: Chart;
};
type Receipt = {
  replay_schema_version: string;
  experiments: Experiment[];
  pending: Dataset[];
  errors?: Dataset[];
};

const translations = {
  en: {
    title: 'Proactive replay',
    subtitle: 'Inspect an early warning and a root-cause investigation using separate public benchmark recordings.',
    scope: 'Offline pilot',
    scopeText: 'Each tab replays its own controlled experiment. Forecasts and root-cause rankings are evaluated independently.',
    refresh: 'Refresh records',
    loading: 'Loading experiment records',
    readError: 'Could not load experiment records',
    invalid: 'This experiment record could not be read.',
    pending: 'Experiment record pending',
    unavailable: 'No record is available for this dataset.',
    petshop: 'PetShop · Early warning',
    eadro: 'TrainTicket · Root-cause investigation',
    results: 'Recorded results',
    timeline: 'Replay timeline',
    evidence: 'Evidence records',
    inspect: 'Inspect a timeline reference or select a record below.',
    emptyEvidence: 'No evidence records are available.',
    source: 'Open public source',
    action: 'Suggested checks',
    actionText: 'Read-only preview. No actions were executed from this page.',
    forecast: 'Forecast',
    observed: 'Observed',
    threshold: 'Alert threshold',
    chartNote: 'Forecasts use prior bins. Later observations are shown for evaluation.',
    chartData: 'View recorded values',
    time: 'Time',
  },
  zh: {
    title: '预警与根因回放',
    subtitle: '使用两组独立的公开基准记录，分别查看提前预警和根因调查。',
    scope: '离线初步实验',
    scopeText: '每个标签页回放一个独立的受控实验。预测和根因排名分别评价。',
    refresh: '刷新实验记录',
    loading: '正在加载实验记录',
    readError: '无法加载实验记录',
    invalid: '此实验记录无法读取。',
    pending: '实验记录待生成',
    unavailable: '该数据集暂无实验记录。',
    petshop: 'PetShop · 提前预警',
    eadro: 'TrainTicket · 根因调查',
    results: '实验结果',
    timeline: '回放时间线',
    evidence: '证据记录',
    inspect: '点击时间线中的引用，或在下方选择一条记录。',
    emptyEvidence: '暂无可查看的证据。',
    source: '打开公开来源',
    action: '建议检查',
    actionText: '只读预览。此页面未执行任何操作。',
    forecast: '预测值',
    observed: '观测值',
    threshold: '告警阈值',
    chartNote: '预测使用此前的时间桶。后续观测仅用于评价。',
    chartData: '查看记录数值',
    time: '时间',
  },
} as const;
type Copy = typeof translations[Language];

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return <section className="panel replay-panel">
    <div className="panel-heading"><h2>{title}</h2></div>
    <div className="panel-content">{children}</div>
  </section>;
}

function ForecastChart({ chart, copy }: { chart: Chart; copy: Copy }) {
  const isNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
  const values = [...chart.points.flatMap(point => [point.observed, point.forecast]), chart.threshold].filter(isNumber);
  if (!chart.points.length || !values.length) return null;
  const lower = Math.min(...values);
  const upper = Math.max(...values);
  const padding = (upper - lower || Math.abs(upper) || 1) * 0.1;
  const y = (value: number) => 150 - 126 * (value - lower + padding) / (upper - lower + padding * 2);
  const times = chart.points.map(point => Date.parse(point.time));
  const timed = times.every(Number.isFinite) && times[times.length - 1] > times[0] && times.every((time, index) => index === 0 || time >= times[index - 1]);
  const x = (index: number) => 66 + 618 * (timed
    ? (times[index] - times[0]) / (times[times.length - 1] - times[0])
    : index / Math.max(1, chart.points.length - 1));
  const format = (value: number | undefined) => isNumber(value) ? String(Number(value.toPrecision(5))) : '—';
  const series = ['observed', 'forecast'] as const;
  const ticks = [...new Set([lower, (lower + upper) / 2, upper])];
  const timeLabel = (value: string) => /^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(value) ? value.slice(11, 16) : value;
  return <Panel title={chart.unit ? `${chart.label} (${chart.unit})` : chart.label}>
    <p className="replay-caption">{copy.chartNote}</p>
    <div className="replay-chart-legend">
      {series.map(key => <span key={key}><i className={`replay-series-${key}`} />{copy[key]}</span>)}
      {isNumber(chart.threshold) && <span><i className="replay-series-threshold" />{copy.threshold} · {format(chart.threshold)}</span>}
    </div>
    <div className="replay-chart-scroll">
      <svg viewBox="0 0 720 180" role="img" aria-label={`${chart.label}. ${copy.chartNote}`}>
        {ticks.map(value => <g key={value}>
          <line className="replay-chart-grid" x1="66" x2="684" y1={y(value)} y2={y(value)} />
          <text x="58" y={y(value) + 4} textAnchor="end">{format(value)}</text>
        </g>)}
        {isNumber(chart.threshold) && <line className="replay-series-threshold" x1="66" x2="684" y1={y(chart.threshold)} y2={y(chart.threshold)} />}
        {series.map(key => <g className={`replay-series-${key}`} key={key}>
          {chart.points.map((point, index) => {
            const value = point[key];
            const previous = chart.points[index - 1]?.[key];
            if (!isNumber(value)) return null;
            return <g key={index}>
              {isNumber(previous) && <line x1={x(index - 1)} y1={y(previous)} x2={x(index)} y2={y(value)} />}
              <circle cx={x(index)} cy={y(value)} r="3.5" />
            </g>;
          })}
        </g>)}
        <text x="66" y="173" textAnchor="start">{timeLabel(chart.points[0].time)}</text>
        {chart.points.length > 1 && <text x="684" y="173" textAnchor="end">{timeLabel(chart.points[chart.points.length - 1].time)}</text>}
      </svg>
    </div>
    <details className="replay-chart-values"><summary>{copy.chartData}</summary>
      <div className="table-scroll"><table><thead><tr><th scope="col">{copy.time}</th><th scope="col">{copy.observed}</th><th scope="col">{copy.forecast}</th></tr></thead>
        <tbody>{chart.points.map((point, index) => <tr key={index}><td><code>{point.time}</code></td><td>{format(point.observed)}</td><td>{format(point.forecast)}</td></tr>)}</tbody>
      </table></div>
    </details>
  </Panel>;
}

function publicLink(value?: string) {
  if (!value) return undefined;
  try { const url = new URL(value); return url.protocol === 'https:' ? url.href : undefined; }
  catch { return undefined; }
}

function ReplayResult({ experiment, copy }: { experiment: Experiment; copy: Copy }) {
  const [selected, setSelected] = useState<string>();
  const evidencePane = useRef<HTMLElement>(null);
  const evidenceById = new Map(experiment.evidence.map(item => [item.id, item]));
  const evidence = (selected ? evidenceById.get(selected) : undefined) ?? experiment.evidence[0];
  const link = publicLink(evidence?.source_url);
  const inspect = (evidenceId: string) => {
    setSelected(evidenceId);
    evidencePane.current?.focus();
  };

  return <div className="replay-result">
    <section className="replay-summary" aria-label={copy.results}>
      <div className="replay-source">{experiment.source}</div>
      <h2>{experiment.title}</h2>
      <p>{experiment.summary}</p>
      <p className="replay-scope">{experiment.scope}</p>
      {experiment.metrics.length > 0 && <dl className="replay-metrics">
        {experiment.metrics.map((metric, index) => <div key={index}>
          <dt>{metric.label}</dt><dd><strong>{metric.value}</strong><span>{metric.note}</span></dd>
        </div>)}
      </dl>}
    </section>
    {experiment.chart && <ForecastChart chart={experiment.chart} copy={copy} />}
    <div className="replay-investigation">
      <Panel title={copy.timeline}>
        <ol className="replay-timeline">
          {experiment.timeline.map((event, index) => <li key={index}>
            <div className="replay-event-meta"><code>{event.time}</code>{event.state && <span className="tag">{event.state}</span>}</div>
            <p>{event.title}</p>
            <div className="replay-references">
              {event.evidence_ids.filter(evidenceId => evidenceById.has(evidenceId)).map(evidenceId =>
                <button key={evidenceId} type="button" onClick={() => inspect(evidenceId)} aria-label={`${copy.evidence} ${evidenceId}`}>{evidenceId}</button>)}
            </div>
          </li>)}
        </ol>
      </Panel>
      <aside ref={evidencePane} tabIndex={-1} aria-label={copy.evidence}>
        <Panel title={copy.evidence}>
          {evidence ? <>
            <p className="replay-caption">{copy.inspect}</p>
            <div className="replay-references">
              {experiment.evidence.map(item => <button key={item.id} type="button" onClick={() => setSelected(item.id)}
                aria-pressed={item.id === evidence.id} title={item.title}>{item.id}</button>)}
            </div>
            <div className="replay-evidence" aria-live="polite">
              <h3>{evidence.title}</h3><p>{evidence.detail}</p>
              {link && <a href={link} target="_blank" rel="noreferrer">{copy.source} ↗</a>}
            </div>
          </> : <p className="replay-caption">{copy.emptyEvidence}</p>}
        </Panel>
      </aside>
    </div>
    {experiment.actions.length > 0 && <Panel title={copy.action}>
      <p className="replay-caption">{copy.actionText}</p>
      <ul className="replay-actions">
        {experiment.actions.map((action, index) => <li key={index}>
          <div><strong>{action.label}</strong><span className="tag">{action.status}</span></div>
          <p>{action.detail}</p>
        </li>)}
      </ul>
    </Panel>}
  </div>;
}

export default function ProactiveReplay({ language }: { language: Language }) {
  const copy = translations[language];
  const [receipt, setReceipt] = useState<Receipt>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [active, setActive] = useState<Dataset>('petshop');
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    api<Receipt>('/proactive-replay', { signal: controller.signal })
      .then(result => {
        if (controller.signal.aborted) return;
        if (result.replay_schema_version !== 'autotriager.proactive_replay.v1') throw new Error('Unsupported replay record version.');
        setReceipt(result);
      })
      .catch(cause => { if (!controller.signal.aborted) setError(errorText(cause)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision]);

  return <div className="proactive-replay">
    <div className="page-title">
      <div><div className="eyebrow">{copy.scope}</div><h1>{copy.title}</h1><p>{copy.subtitle}</p></div>
      <Button iconName="refresh" loading={loading} onClick={() => setRevision(value => value + 1)}>{copy.refresh}</Button>
    </div>
    <Alert type="info" header={copy.scope}>{copy.scopeText}</Alert>
    {error && <Alert type="error" header={copy.readError}>{error}</Alert>}
    {loading && !receipt && <div className="loading" role="status"><Spinner /> {copy.loading}</div>}
    {receipt && <Tabs activeTabId={active} onChange={({ detail }) => setActive(detail.activeTabId as Dataset)} tabs={datasets.map(dataset => {
      const experiment = receipt.experiments.find(item => item.id === dataset);
      return {
        id: dataset,
        label: copy[dataset],
        content: experiment ? <ReplayResult key={dataset} experiment={experiment} copy={copy} />
          : receipt.errors?.includes(dataset) ? <Alert type="error">{copy.invalid}</Alert>
            : <Alert type="info">{receipt.pending.includes(dataset) ? copy.pending : copy.unavailable}</Alert>,
      };
    })} />}
  </div>;
}

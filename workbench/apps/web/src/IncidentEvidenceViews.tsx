import { useId, type CSSProperties, type ReactNode } from 'react';
import { lifecycleEvidenceAnchor } from './IncidentLifecycle';
import IncidentTopology, { type TopologyEdge } from './IncidentTopology';
import IncidentMetricExplorer from './IncidentMetricExplorer';
import type { Language } from './i18n';
import './incidentEvidenceViews.css';

type Evidence = { id: string; title: string; detail: string };
type Measurement = { service: string; metric: string; unit: string; value: number; threshold: number; start: string; end: string; observed_at?: string; source: string };
type Request = { service: string; dependency: string; dependency_latency_ms: number; observed_at_unix: number; route?: string; dependency_route?: string };
type Incident = { id: string; evidence: Evidence[]; metric_evidence?: Measurement; matched_request?: Request | null; topology_edges?: TopologyEdge[] };
type Median = { id: string; service: string; metric: string; reference: number; investigation: number };

const copy = {
  en: {
    metrics: 'Metrics', alert: 'Alert observation',
    threshold: 'Alert threshold', observed: 'Measured', comparison: 'Window medians',
    reference: 'Reference', investigation: 'Investigation', archiveUnits: 'Archive units',
    metricGap: 'No metric measurements are available for this incident.',
    evidence: 'View evidence', utc: 'UTC',
  },
  zh: {
    metrics: '指标', alert: '报警观测值',
    threshold: '报警阈值', observed: '实测值', comparison: '时间窗中位数',
    reference: '参考时段', investigation: '调查时段', archiveUnits: '原始数据单位',
    metricGap: '本次事故没有可展示的指标测量记录。',
    evidence: '查看证据', utc: 'UTC',
  },
} as const;

type Copy = typeof copy[Language];
const numeric = '([+-]?(?:\\d+(?:\\.\\d+)?|\\.\\d+)(?:[eE][+-]?\\d+)?)';
const medianText = new RegExp(`^([\\w.-]+) median changed from ${numeric} to ${numeric}\\. `);
const callText = /(?:^|\. )Observed call ([a-zA-Z0-9_.-]+) to ([a-zA-Z0-9_.-]+) at (\d{4}-\d\d-\d\dT[\d:.]+Z)\. Trace [a-f0-9]+, parent [a-f0-9]+, child [a-f0-9]+\./i;

// Existing archived TrainTicket records expose a fixed, source-linked summary format.
function archivedMedians(evidence: Evidence[]): Median[] {
  return evidence.flatMap(record => {
    if (!/^tt-\d+-\d+-metric$/.test(record.id)) return [];
    const service = record.title.match(/^([\w.-]+) metric change$/)?.[1];
    const match = record.detail.match(medianText);
    if (!service || !match) return [];
    const [, metric, before, after] = match;
    const reference = Number(before);
    const investigation = Number(after);
    return Number.isFinite(reference) && Number.isFinite(investigation)
      ? [{ id: record.id, service, metric, reference, investigation }]
      : [];
  });
}

function observedEdges(incident: Incident): TopologyEdge[] {
  const edges = incident.evidence.flatMap<TopologyEdge>(record => {
    if (!/^tt-\d+-\d+-trace$/.test(record.id)) return [];
    const match = record.detail.match(callText);
    if (!match) return [];
    const [, from, to, at] = match;
    return [{ id: record.id, from, to, at }];
  });
  const request = incident.matched_request;
  if (request?.service && request.dependency && Number.isFinite(request.observed_at_unix)
    && Number.isFinite(request.dependency_latency_ms) && request.dependency_latency_ms >= 0
    && incident.evidence.some(record => record.id === 'lab-request')) {
    const observedAt = new Date(request.observed_at_unix * 1000);
    if (!Number.isNaN(observedAt.getTime())) {
      edges.push({ id: 'lab-request', from: request.service, to: request.dependency, at: observedAt.toISOString(),
        latency: request.dependency_latency_ms, fromRoute: request.route, toRoute: request.dependency_route });
    }
  }
  return edges;
}

function DisplayTime({ value, language }: { value: string; language: Language }) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return <time dateTime={value}>{new Intl.DateTimeFormat(language === 'zh' ? 'zh-CN' : 'en', {
    dateStyle: 'short', timeStyle: 'medium', timeZone: 'UTC', hourCycle: 'h23',
  }).format(date)} {copy[language].utc}</time>;
}

function EvidenceButton({ evidenceId, title, open }: { evidenceId: string; title: string; open: (id: string) => void }) {
  return <button type="button" className="incident-viz-evidence" onClick={() => open(evidenceId)}>
    {title}<span aria-hidden="true"> ↗</span>
  </button>;
}

function AlertPlot({ metric, language, format }: { metric: Measurement; language: Language; format: Intl.NumberFormat }) {
  const t = copy[language];
  const min = Math.min(0, metric.value, metric.threshold);
  const span = Math.max(1, Math.max(metric.value, metric.threshold) - min) * 1.12;
  const percent = (value: number) => 100 * (value - min) / span;
  const exceeded = metric.value > metric.threshold;
  return <figure className="incident-viz-alert">
    <div className="incident-viz-metric-header">
      <div><figcaption>{metric.metric}</figcaption><span>{metric.service}</span></div>
      <strong>{format.format(metric.value)} <small>{metric.unit}</small></strong>
    </div>
    <div className="incident-viz-alert-track" aria-hidden="true" style={{
      '--reading': `${percent(metric.value)}%`, '--threshold': `${percent(metric.threshold)}%`,
    } as CSSProperties}>
      <span className={exceeded ? 'incident-viz-alert-reading is-high' : 'incident-viz-alert-reading'} />
      <span className="incident-viz-threshold" />
    </div>
    <div className="incident-viz-legend">
      <span><i className={exceeded ? 'is-high' : undefined} />{t.observed}</span>
      <span><i className="incident-viz-threshold-key" />{t.threshold} {format.format(metric.threshold)} {metric.unit}</span>
    </div>
    <div className="incident-viz-meta"><DisplayTime value={metric.observed_at || metric.end} language={language} /> · {metric.source}</div>
  </figure>;
}

function MedianPlot({ point, format, t, open }: { point: Median; format: Intl.NumberFormat; t: Copy; open: (id: string) => void }) {
  const min = Math.min(0, point.reference, point.investigation);
  const span = Math.max(0, point.reference, point.investigation) - min;
  const percent = (value: number) => span ? 100 * (value - min) / span : 0;
  const position = (value: number) => ({ left: `${Math.min(percent(0), percent(value))}%`, width: `${Math.abs(percent(value) - percent(0))}%` });
  return <figure className="incident-viz-median">
    <figcaption><strong>{point.service}</strong><code>{point.metric}</code></figcaption>
    <dl>
      {([[t.reference, point.reference], [t.investigation, point.investigation]] as const).map(([label, value]) => <div key={label}>
        <dt>{label}</dt><dd><span className="incident-viz-median-track" aria-hidden="true"><i style={position(value)} /></span>
          <span>{format.format(value)}</span></dd>
      </div>)}
    </dl>
    <EvidenceButton evidenceId={point.id} title={t.evidence} open={open} />
  </figure>;
}

export default function IncidentEvidenceViews({ incident, language, onEvidence, children, showMetricSummaryGap = true }: {
  incident: Incident; language: Language; onEvidence?: (evidenceId: string) => void; children?: ReactNode; showMetricSummaryGap?: boolean;
}) {
  const t = copy[language];
  const headings = useId();
  const format = new Intl.NumberFormat(language === 'zh' ? 'zh-CN' : 'en', { maximumSignificantDigits: 6 });
  const metric = incident.metric_evidence;
  const validMetric = metric && Number.isFinite(metric.value) && Number.isFinite(metric.threshold);
  const medians = archivedMedians(incident.evidence);
  const edges = incident.topology_edges ?? observedEdges(incident);

  function open(evidenceId: string) {
    if (onEvidence) {
      onEvidence(evidenceId);
      return;
    }
    const target = document.getElementById(lifecycleEvidenceAnchor(incident.id, evidenceId));
    if (!target) return;
    const disclosure = target.closest('details');
    if (disclosure) disclosure.open = true;
    target.scrollIntoView({ block: 'nearest' });
    target.focus({ preventScroll: true });
  }

  return <div className="incident-viz">
    <IncidentTopology edges={edges} language={language} onEvidence={open} />
    {children}
    <section className="incident-viz-panel" aria-labelledby={`${headings}-metrics`}>
      <header><h3 id={`${headings}-metrics`}>{t.metrics}</h3>
        {validMetric && <span>{t.alert}</span>}
        {medians.length > 0 && <span>{t.comparison} · {t.archiveUnits}</span>}
      </header>
      {validMetric && <AlertPlot metric={metric} language={language} format={format} />}
      {medians.length > 0 && <div className="incident-viz-medians">
        {medians.map(point => <MedianPlot key={point.id} point={point} format={format} t={t} open={open} />)}
      </div>}
      {showMetricSummaryGap && !validMetric && medians.length === 0 && <p className="incident-viz-empty">{t.metricGap}</p>}
      <IncidentMetricExplorer key={incident.id} incidentId={incident.id} language={language} />
    </section>
  </div>;
}

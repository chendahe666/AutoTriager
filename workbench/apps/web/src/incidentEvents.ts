import type { IncidentEvent } from './IncidentEventTimeline';
import type { Language } from './i18n';

type RecordedEvidence = { id: string; title: string; detail: string };

export type IncidentTimelineSource = {
  source?: 'live';
  start: string;
  end: string;
  triggered_at?: string;
  evidence: RecordedEvidence[];
  metric_evidence?: { metric: string; observed_at?: string; value: number; threshold: number; unit: string };
  matched_request?: {
    service: string; dependency: string; observed_at_unix: number; latency_ms: number; dependency_latency_ms: number;
    route?: string; dependency_route?: string;
  } | null;
};

type RecoverySample = { request_id: string; observed_at_unix: number; latency_ms: number; http_status: number };
type TimelineLifecycle = {
  routing: { decision: string; event_id: string; triggered_at: string; rule: string }[];
  action: {
    status: string;
    approved_at_unix?: number;
    plan: { action: { expected_delay_ms: number; delay_ms: number } };
    action_receipt?: { status: string; applied_at_unix: number };
    recovery?: { samples: RecoverySample[] };
  } | null;
};

const copy = {
  en: { opened: 'Recorded window opened', ended: 'Recorded window ended', call: 'Dependency call observed',
    log: 'First log entry observed', alert: 'Prometheus rule fired', received: 'Workbench received the alert',
    routed: 'Workbench opened the investigation', linked: 'Related alert fired', approved: 'Local recovery approved',
    applied: 'Payment delay setting restored', sample: 'Recovery request measured', verified: 'Recovery criteria met',
    threshold: 'threshold', requests: 'consecutive passing requests' },
  zh: { opened: '记录窗口开始', ended: '记录窗口结束', call: '观测到服务调用',
    log: '观测到首条日志', alert: 'Prometheus 规则触发', received: '工作台收到报警',
    routed: '工作台接到报警并发起调查', linked: '关联报警触发', approved: '已批准本地恢复',
    applied: '支付延迟设置已恢复', sample: '已测量恢复请求', verified: '达到恢复验证条件',
    threshold: '阈值', requests: '个连续请求通过' },
} as const;

function asIso(unixSeconds: number | undefined) {
  if (unixSeconds === undefined || !Number.isFinite(unixSeconds)) return undefined;
  const date = new Date(unixSeconds * 1000);
  return Number.isFinite(date.getTime()) ? date.toISOString() : undefined;
}

export function buildIncidentEvents(incident: IncidentTimelineSource, language: Language, lifecycle?: TimelineLifecycle) {
  const t = copy[language];
  const events: IncidentEvent[] = [];
  const has = (key: string) => incident.evidence.some(item => item.id === key);
  const ms = (value: number) => `${value.toFixed(2)} ms`;
  function add(id: string, at: string | undefined, kind: IncidentEvent['kind'], title: string, detail?: string, evidenceIds?: string[]) {
    if (at && Number.isFinite(Date.parse(at))) events.push({ id, at, kind, title, detail, evidenceIds });
  }

  if (incident.source !== 'live') {
    add('window-start', incident.start, 'window', t.opened);
    add('window-end', incident.end, 'window', t.ended);
    for (const evidence of incident.evidence) {
      const isTrace = /^tt-\d+-\d+-trace$/.test(evidence.id);
      const isLog = /^tt-\d+-\d+-log$/.test(evidence.id);
      const match = isTrace ? evidence.detail.match(/Observed call ([\w.-]+) to ([\w.-]+) at (\d{4}-\d\d-\d\dT[\d:.]+Z)\. Trace /) : null;
      const logTime = isLog ? evidence.detail.match(/First observed entry at (\d{4}-\d\d-\d\dT[\d:.]+Z), row \d+/)?.[1] : undefined;
      if (match) add(evidence.id, match[3], 'evidence', t.call, `${match[1]} → ${match[2]}`, [evidence.id]);
      else if (logTime) add(evidence.id, logTime, 'evidence', t.log, evidence.title, [evidence.id]);
    }
    return events;
  }

  const metric = incident.metric_evidence;
  const request = incident.matched_request;
  if (request && has('lab-request')) {
    add('lab-request', asIso(request.observed_at_unix), 'evidence', t.call,
      `${request.service} → ${request.dependency} · ${ms(request.dependency_latency_ms)}`, ['lab-request']);
  }
  add('prometheus-fired', incident.triggered_at, 'alert', t.alert, metric &&
    `${metric.metric} · ${metric.value.toFixed(2)} ${metric.unit} · ${t.threshold} ${metric.threshold} ${metric.unit}`,
  ['prom-rule', 'prom-metric'].filter(has));
  const received = incident.evidence.find(item => item.id === 'prom-rule')?.detail.match(/Received at (\S+)\.$/)?.[1];
  const opened = lifecycle?.routing.find(route => route.decision === 'new');
  add('workbench-received', received, 'route', opened ? t.routed : t.received, opened?.rule, ['prom-rule'].filter(has));
  for (const route of lifecycle?.routing ?? []) {
    if (route.decision === 'link') add(`linked-${route.event_id}`, route.triggered_at, 'route', t.linked, route.rule);
  }

  const action = lifecycle?.action;
  if (!action) return events;
  const { expected_delay_ms, delay_ms } = action.plan.action;
  const change = `${expected_delay_ms} ms → ${delay_ms} ms`;
  add('recovery-approved', asIso(action.approved_at_unix), 'approval', t.approved, change);
  if (action.action_receipt?.status === 'applied') {
    add('recovery-applied', asIso(action.action_receipt.applied_at_unix), 'action', t.applied, change);
  }
  const samples = action.recovery?.samples ?? [];
  for (const sample of samples) {
    add(`sample-${sample.request_id}`, asIso(sample.observed_at_unix), 'evidence', t.sample,
      `${ms(sample.latency_ms)} · HTTP ${sample.http_status}`, [sample.request_id]);
  }
  if (action.status === 'verified' && samples.length > 0) {
    add('recovery-verified', asIso(Math.max(...samples.map(sample => sample.observed_at_unix))),
      'verification', t.verified, `${samples.length} ${t.requests}`, samples.map(sample => sample.request_id));
  }
  return events;
}

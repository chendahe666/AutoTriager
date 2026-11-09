import { useEffect, useRef, useState, type ReactNode } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import Checkbox from '@cloudscape-design/components/checkbox';
import Spinner from '@cloudscape-design/components/spinner';
import { api, ApiError, errorText, id } from './api';
import IncidentEventTimeline from './IncidentEventTimeline';
import { buildIncidentEvents, type IncidentTimelineSource } from './incidentEvents';
import type { Language } from './i18n';
import './incidentLifecycle.css';

type RouteDecision = 'new' | 'link' | 'skip';
type RouteReceipt = { decision: RouteDecision; event_id: string; triggered_at: string; rule: string };
type FindingStatus = 'insufficient' | 'hypothesis' | 'confirmed_in_lab';
type Finding = { status: FindingStatus; summary: string; evidence_ids: string[] };
type Operation = { expected_delay_ms: number; delay_ms: number };
type Verification = { min_samples: number; max_seconds: number; checkout_threshold_ms: number; expected_http_status: number };
type Plan = { plan_id: string; action: Operation; rollback: Operation; verification: Verification };
type Sample = { request_id: string; observed_at_unix: number; latency_ms: number; http_status: number };
type ActionStatus = 'applying' | 'verifying' | 'verified' | 'verification_failed' | 'rejected' | 'unknown';
type Action = { status: ActionStatus; plan: Plan; approved_at_unix?: number;
  action_receipt?: { status: string; applied_at_unix: number }; recovery?: { samples: Sample[] } };
type Recommendation = { recommendation_id: string; title: string; summary: string; steps: string[]; verification: string; limitation: string; incident_ids: string[] };
type Lifecycle = {
  incident_id: string;
  routing: RouteReceipt[];
  diagnosis: { finding: Finding; limitations: string[]; next_checks: string[] };
  mitigation: Plan | null;
  action: Action | null;
  prevention: Recommendation[];
};

const copy = {
  en: {
    title: 'Incident lifecycle', loading: 'Loading the incident lifecycle', retry: 'Refresh lifecycle',
    intake: 'Alert routing', diagnosis: 'Diagnosis', recovery: 'Mitigation and recovery', prevention: 'Prevent recurrence',
    routes: { new: 'Opened an investigation', link: 'Linked to this investigation', skip: 'Skipped delivery' },
    unrecordedRoute: 'This alert was recorded before routing history was available.',
    findings: { insufficient: 'Insufficient evidence', hypothesis: 'Hypothesis', confirmed_in_lab: 'Confirmed in the lab' },
    evidence: 'Evidence', next: 'Next checks', evidenceLanguage: '',
    change: 'Injected payment delay', rollback: 'Rollback plan',
    rollbackNote: 'In the lab, reverting the change would restore the injected delay.',
    check: 'Success requires {samples} consecutive checkout requests at or below {threshold} ms with HTTP {status}, within {seconds} seconds.',
    confirm: 'I approve this specific change in the local checkout lab.', approve: 'Approve lab recovery',
    noPlan: 'A recovery action is available when the investigation is bound to the current, controlled lab fault.',
    statuses: {
      applying: 'Applying the approved change', verifying: 'Measuring recovery', verified: 'Recovery verified',
      verification_failed: 'Recovery did not pass verification', rejected: 'The lab state changed before application', unknown: 'Action outcome is unknown',
    },
    samples: 'Post-change checkout requests', request: 'Request', time: 'Time (UTC)', latency: 'Latency', http: 'HTTP',
    waiting: 'Prevention recommendations appear after two independently verified incidents with the same mechanism.',
    proposed: 'Proposed improvement', supporting: 'Supporting investigations',
  },
  zh: {
    title: '事故处理流程', loading: '正在加载事故处理流程', retry: '刷新流程',
    intake: '报警分流', diagnosis: '诊断', recovery: '缓解与恢复', prevention: '预防复发',
    routes: { new: '已新开调查', link: '已并入本调查', skip: '已跳过投递' },
    unrecordedRoute: '记录本条报警时，尚无分流历史。',
    findings: { insufficient: '证据不足', hypothesis: '待验证假设', confirmed_in_lab: '已在实验中验证' },
    evidence: '证据', next: '下一步检查', evidenceLanguage: '调查结论和建议保留原始英文。',
    change: '注入的支付延迟', rollback: '回退方案', rollbackNote: '在实验中，回退会恢复注入的延迟。',
    check: '在 {seconds} 秒内，连续 {samples} 次结账请求必须返回 HTTP {status}，延迟均不超过 {threshold} ms。',
    confirm: '我批准在本地结账实验中执行这项具体操作。', approve: '批准实验恢复操作',
    noPlan: '当调查证据对应当前受控实验故障时，才会提供恢复操作。',
    statuses: {
      applying: '正在执行已批准的操作', verifying: '正在测量恢复情况', verified: '已验证恢复',
      verification_failed: '未通过恢复验证', rejected: '执行前实验状态已变化', unknown: '操作结果未知',
    },
    samples: '变更后的结账请求', request: '请求', time: '时间（UTC）', latency: '延迟', http: 'HTTP',
    waiting: '同一机制的两次独立事故均验证恢复后，将生成预防建议。',
    proposed: '改进建议', supporting: '支持该建议的调查',
  },
} as const;

type Copy = typeof copy[Language];

export function lifecycleEvidenceAnchor(caseId: string, evidenceId: string) {
  return `evidence-${caseId}-${evidenceId}`;
}

export function showEvidence(caseId: string, evidenceId: string) {
  const target = document.getElementById(lifecycleEvidenceAnchor(caseId, evidenceId));
  const parent = target?.closest('details');
  if (parent) parent.open = true;
  target?.focus();
  target?.scrollIntoView({ block: 'nearest' });
}

function Step({ title, children, sectionId }: { title: string; children: ReactNode; sectionId?: string }) {
  return <li id={sectionId} tabIndex={sectionId ? -1 : undefined}><h4>{title}</h4>{children}</li>;
}

function EvidenceLinks({ caseId, ids, title }: { caseId: string; ids: string[]; title: string }) {
  if (ids.length === 0) return null;
  return <div className="lifecycle-evidence-links" aria-label={title}>
    {ids.map(evidenceId => <Button key={evidenceId} variant="inline-link" onClick={() => showEvidence(caseId, evidenceId)}>{evidenceId}</Button>)}
  </div>;
}

function RecoveryTable({ caseId, samples, t, format }: { caseId: string; samples: Sample[]; t: Copy; format: Intl.NumberFormat }) {
  return <table className="lifecycle-samples">
    <caption>{t.samples}</caption>
    <thead><tr><th scope="col">{t.request}</th><th scope="col">{t.time}</th><th scope="col">{t.latency}</th><th scope="col">{t.http}</th></tr></thead>
    <tbody>{samples.map(sample => <tr key={sample.request_id} id={lifecycleEvidenceAnchor(caseId, sample.request_id)} tabIndex={-1}>
      <td><code>{sample.request_id.slice(0, 12)}</code></td>
      <td>{new Date(sample.observed_at_unix * 1000).toISOString().slice(11, 23)}</td>
      <td>{format.format(sample.latency_ms)} ms</td><td>{sample.http_status}</td>
    </tr>)}</tbody>
  </table>;
}

function Approval({ plan, t, busy, onApprove }: { plan: Plan; t: Copy; busy: boolean; onApprove: () => void }) {
  const [checked, setChecked] = useState(false);
  return <div className="lifecycle-approval">
    <Checkbox checked={checked} disabled={busy} onChange={event => setChecked(event.detail.checked)}>{t.confirm}</Checkbox>
    <Button variant="primary" disabled={!checked || busy} loading={busy} onClick={() => {
      setChecked(false);
      onApprove();
    }}>{t.approve}</Button>
    <code>{plan.plan_id}</code>
  </div>;
}

export default function IncidentLifecycle({ caseId, incident, language, fallback, onVerified }: {
  caseId: string; incident: IncidentTimelineSource; language: Language; fallback: ReactNode; onVerified?: () => void;
}) {
  const t = copy[language];
  const path = `/incident-lifecycle/${id(caseId)}`;
  const [data, setData] = useState<Lifecycle>();
  const [available, setAvailable] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [approvalError, setApprovalError] = useState('');
  const reportedVerification = useRef(false);

  useEffect(() => {
    const request = new AbortController();
    void api<Lifecycle>(path, { signal: request.signal }).then(result => {
      if (request.signal.aborted) return;
      if (result.incident_id !== caseId) throw new Error('Unexpected lifecycle record');
      setData(result);
      setAvailable(true);
      setLoadError('');
    }).catch(error => {
      if (request.signal.aborted) return;
      if (error instanceof ApiError && error.status === 404) setAvailable(false);
      else setLoadError(errorText(error));
    }).finally(() => {
      if (!request.signal.aborted) setLoading(false);
    });
    return () => request.abort();
  }, [caseId, path, refresh]);

  const status = data?.action?.status;
  useEffect(() => {
    if (status !== 'verified' || reportedVerification.current || !onVerified) return;
    reportedVerification.current = true;
    onVerified();
  }, [status, onVerified]);

  useEffect(() => {
    if (status !== 'applying' && status !== 'verifying') return;
    const timer = setInterval(() => setRefresh(value => value + 1), 1800);
    return () => clearInterval(timer);
  }, [status]);

  async function approve(plan: Plan) {
    if (sending || loading || loadError || data?.action) return;
    setSending(true);
    setApprovalError('');
    try {
      const action = await api<Action>(`${path}/approve`, { method: 'POST', body: { plan_id: plan.plan_id, approve: true } });
      if (action.plan.plan_id !== plan.plan_id) throw new Error('Unexpected approval receipt');
      setData(current => current && { ...current, action });
    } catch (error) {
      setApprovalError(errorText(error));
    } finally {
      setLoading(true);
      setSending(false);
      setRefresh(value => value + 1);
    }
  }

  const timeline = <IncidentEventTimeline events={buildIncidentEvents(incident, language, data)} language={language}
    onEvidence={evidenceId => showEvidence(caseId, evidenceId)} />;
  if (!available) return <>{timeline}{fallback}</>;
  if (!data) return <section className="incident-desk-context incident-lifecycle">
    <h3>{t.title}</h3>
    {loadError ? <Alert type="error" action={<Button onClick={() => setRefresh(value => value + 1)}>{t.retry}</Button>}>{loadError}</Alert>
      : <p role="status"><Spinner /> {t.loading}</p>}
  </section>;

  const { diagnosis, action, routing } = data;
  const prevention = data.prevention.filter(item => item.incident_ids.includes(caseId));
  const plan = data.mitigation;
  const format = new Intl.NumberFormat(language === 'zh' ? 'zh-CN' : 'en', { maximumFractionDigits: 2 });
  return <>{timeline}<section className="incident-desk-context incident-lifecycle" aria-label={t.title}>
    <h3>{t.title}</h3>
    {loadError && <Alert type="error" action={<Button onClick={() => setRefresh(value => value + 1)}>{t.retry}</Button>}>{loadError}</Alert>}
    <ol className="lifecycle-steps">
      <Step title={t.intake}>
        {routing.length ? <ul className="lifecycle-lines">{routing.map(route => <li key={route.event_id}>
          <strong>{t.routes[route.decision]}</strong> · {route.rule} · <time dateTime={route.triggered_at}>{route.triggered_at}</time>
        </li>)}</ul> : <p>{t.unrecordedRoute}</p>}
      </Step>
      <Step title={t.diagnosis}>
        <p><strong>{t.findings[diagnosis.finding.status]}</strong> · {diagnosis.finding.summary}</p>
        {t.evidenceLanguage && <p>{t.evidenceLanguage}</p>}
        <EvidenceLinks caseId={caseId} ids={diagnosis.finding.evidence_ids} title={t.evidence} />
        {diagnosis.limitations.map(line => <p key={line}>{line}</p>)}
        {diagnosis.next_checks.length > 0 && <p id={`incident-next-${caseId}`} tabIndex={-1}><strong>{t.next}</strong> · {diagnosis.next_checks.join(' ')}</p>}
      </Step>
      <Step title={t.recovery} sectionId={diagnosis.next_checks.length === 0 ? `incident-next-${caseId}` : undefined}>
        {plan ? <>
          <p><strong>{t.change}</strong> · {plan.action.expected_delay_ms} ms → {plan.action.delay_ms} ms</p>
          <p>{t.check.replace('{samples}', String(plan.verification.min_samples)).replace('{threshold}', String(plan.verification.checkout_threshold_ms))
            .replace('{status}', String(plan.verification.expected_http_status)).replace('{seconds}', String(plan.verification.max_seconds))}</p>
          <p><strong>{t.rollback}</strong> · {plan.rollback.expected_delay_ms} ms → {plan.rollback.delay_ms} ms. {t.rollbackNote}</p>
          {!action && <Approval key={plan.plan_id} plan={plan} t={t} busy={sending || loading || Boolean(loadError)} onApprove={() => void approve(plan)} />}
        </> : <p>{t.noPlan}</p>}
        {approvalError && <Alert type="error">{approvalError}</Alert>}
        {action && <p role="status" className="lifecycle-outcome"><strong>{t.statuses[action.status]}</strong></p>}
        {action?.recovery?.samples.length ? <RecoveryTable caseId={caseId} samples={action.recovery.samples} t={t} format={format} /> : null}
      </Step>
      <Step title={t.prevention}>
        {prevention.length ? prevention.map(item => <article className="lifecycle-recommendation" key={item.recommendation_id}>
          <p><strong>{t.proposed}</strong> · {item.title}</p>
          <p>{item.summary}</p>
          <ul className="lifecycle-lines">{item.steps.map(line => <li key={line}>{line}</li>)}</ul>
          <p>{item.verification}</p><p>{item.limitation}</p>
          <ul className="lifecycle-lines" aria-label={t.supporting}>{item.incident_ids.map(incidentId => <li key={incidentId}>
            <a href={`#/errors/${id(incidentId)}`}>{incidentId}</a>
          </li>)}</ul>
        </article>) : <p>{t.waiting}</p>}
      </Step>
    </ol>
  </section></>;
}

import { useEffect, useRef, useState, type FormEvent } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import Spinner from '@cloudscape-design/components/spinner';
import { api, id } from './api';
import type { Language } from './i18n';
import './triageWorkflow.css';

export type TriageIncident = { id: string; title: string; start: string; end: string };
type Evidence = { id: string; title: string; detail: string; source_url?: string };
type Candidate = { service: string; summary: string; evidence_ids: string[]; next_check: string };
export type Findings = TriageIncident & { candidates: Candidate[]; evidence: Evidence[]; limits: string[] };

const translations = {
  en: {
    title: 'Investigate an incident',
    subtitle: 'See where to look first, what supports it, and what to check next.',
    source: 'Recorded TrainTicket incidents',
    choose: 'Incident',
    placeholder: 'Select a recorded incident',
    loading: 'Loading incidents',
    investigate: 'Review incident',
    loadingFindings: 'Loading recorded findings',
    findings: 'Investigation findings',
    lead: 'Check first',
    why: 'Why this service',
    next: 'Suggested next check',
    evidence: 'Supporting records',
    other: 'Other candidates',
    limits: 'What is still uncertain',
    publicSource: 'View dataset source',
    empty: 'No recorded incidents are available.',
    loadError: 'Could not load the incident list.',
    reviewError: 'Could not load findings for this incident. Try reviewing it again.',
    noCandidate: 'The recorded evidence is insufficient to prioritize a service.',
    retry: 'Reload incidents',
    recordLanguage: '',
  },
  zh: {
    title: '调查事故',
    subtitle: '查看优先排查的服务、相关依据和下一步检查。',
    source: 'TrainTicket 事故回放',
    choose: '事故',
    placeholder: '选择一个事故记录',
    loading: '正在加载事故',
    investigate: '查看调查',
    loadingFindings: '正在读取调查结果',
    findings: '调查结果',
    lead: '优先排查',
    why: '排查依据',
    next: '下一步建议',
    evidence: '支持证据',
    other: '其他候选',
    limits: '待确认事项',
    publicSource: '查看数据来源',
    empty: '暂无事故记录。',
    loadError: '无法加载事故列表。',
    reviewError: '无法读取这次调查的结果，请重试。',
    noCandidate: '现有记录不足以确定优先排查的服务。',
    retry: '重新加载事故',
    recordLanguage: '实验记录保留英文。',
  },
} as const;

type Copy = typeof translations[Language];

function dateTime(value: string, language: Language) {
  return new Intl.DateTimeFormat(language === 'zh' ? 'zh-CN' : 'en', {
    dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC', hourCycle: 'h23',
  }).format(new Date(value));
}

function EvidenceList({ candidate, evidence, copy }: { candidate: Candidate; evidence: Map<string, Evidence>; copy: Copy }) {
  const records = candidate.evidence_ids.flatMap(ref => {
    const record = evidence.get(ref);
    return record ? [record] : [];
  });
  if (records.length === 0) return null;
  return <details className="triage-disclosure">
    <summary>{copy.evidence} <span className="triage-count">{records.length}</span></summary>
    <ul className="triage-records">{records.map(record => <li key={record.id}>
      <h4>{record.title}</h4>
      <p>{record.detail}</p>
      {record.source_url?.startsWith('https://') && <a href={record.source_url} target="_blank" rel="noreferrer">{copy.publicSource} ↗</a>}
    </li>)}</ul>
  </details>;
}

export function TriageResults({ findings, language }: { findings: Findings; language: Language }) {
  const copy = translations[language];
  const [lead, ...others] = findings.candidates;
  const evidence = new Map(findings.evidence.map(record => [record.id, record]));
  return <section className="triage-results" aria-label={copy.findings}>
    {copy.recordLanguage && <p className="triage-record-language">{copy.recordLanguage}</p>}
    {lead ? <div className="triage-lead">
      <span className="triage-kicker">{copy.lead}</span>
      <h2>{lead.service}</h2>
      <div className="triage-lead-body">
        <div>
          <h3>{copy.why}</h3>
          <p>{lead.summary}</p>
        </div>
        <div className="triage-next">
          <h3>{copy.next}</h3>
          <p>{lead.next_check}</p>
        </div>
      </div>
      <EvidenceList candidate={lead} evidence={evidence} copy={copy} />
    </div> : <Alert type="info">{copy.noCandidate}</Alert>}
    {(others.length > 0 || findings.limits.length > 0) && <div className="triage-secondary">
      {others.length > 0 && <details className="triage-disclosure">
        <summary>{copy.other} <span className="triage-count">{others.length}</span></summary>
        <ul className="triage-records">{others.map(candidate => <li key={candidate.service}>
          <h4>{candidate.service}</h4>
          <p>{candidate.summary}</p>
          <p><strong>{copy.next}</strong> · {candidate.next_check}</p>
          <EvidenceList candidate={candidate} evidence={evidence} copy={copy} />
        </li>)}</ul>
      </details>}
      {findings.limits.length > 0 && <details className="triage-disclosure">
        <summary>{copy.limits}</summary>
        <ul className="triage-limits">{findings.limits.map(limit => <li key={limit}>{limit}</li>)}</ul>
      </details>}
    </div>}
  </section>;
}

export default function TriageWorkflow({ language }: { language: Language }) {
  const copy = translations[language];
  const [catalog, setCatalog] = useState<TriageIncident[] | null>(null);
  const [catalogError, setCatalogError] = useState(false);
  const [reload, setReload] = useState(0);
  const [selected, setSelected] = useState('');
  const [findings, setFindings] = useState<Findings | null>(null);
  const [loading, setLoading] = useState(false);
  const [reviewError, setReviewError] = useState(false);
  const pendingReview = useRef<AbortController | null>(null);

  useEffect(() => {
    const request = new AbortController();
    setCatalogError(false);
    void api<{ cases: TriageIncident[] }>('/triage', { signal: request.signal }).then(value => {
      if (!request.signal.aborted) setCatalog(value.cases);
    }).catch(() => {
      if (!request.signal.aborted) setCatalogError(true);
    });
    return () => request.abort();
  }, [reload]);

  useEffect(() => () => pendingReview.current?.abort(), []);

  const incident = catalog?.find(item => item.id === selected);

  async function review(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!incident || pendingReview.current) return;
    const request = new AbortController();
    pendingReview.current = request;
    setLoading(true);
    setReviewError(false);
    setFindings(null);
    try {
      const value = await api<{ case: Findings }>(`/triage/${id(incident.id)}`, { signal: request.signal });
      if (value.case.id !== incident.id) throw new Error('Incorrect incident response');
      if (!request.signal.aborted) setFindings(value.case);
    } catch {
      if (!request.signal.aborted) setReviewError(true);
    } finally {
      if (pendingReview.current === request) {
        pendingReview.current = null;
        if (!request.signal.aborted) setLoading(false);
      }
    }
  }

  return <div className="triage-workflow">
    <div className="page-title">
      <div>
        <div className="eyebrow">{copy.source}</div>
        <h1>{copy.title}</h1>
        <p>{copy.subtitle}</p>
      </div>
    </div>
    {catalogError ? <Alert type="error" action={<Button onClick={() => setReload(value => value + 1)}>{copy.retry}</Button>}>{copy.loadError}</Alert>
      : catalog === null ? <p className="triage-loading" role="status"><Spinner /> {copy.loading}</p>
      : catalog.length === 0 ? <Alert type="info">{copy.empty}</Alert>
      : <form className="triage-picker" onSubmit={review}>
        <div>
          <label htmlFor="triage-incident">{copy.choose}</label>
          <select id="triage-incident" value={selected} onChange={event => {
            pendingReview.current?.abort();
            pendingReview.current = null;
            setSelected(event.target.value);
            setLoading(false);
            setReviewError(false);
            setFindings(null);
          }}>
            <option value="" disabled>{copy.placeholder}</option>
            {catalog.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}
          </select>
          {incident && <p className="triage-window">{dateTime(incident.start, language)} → {dateTime(incident.end, language)} UTC</p>}
        </div>
        <Button variant="primary" formAction="submit" disabled={!incident || loading}>{copy.investigate}</Button>
      </form>}
    <div aria-live="polite">
      {loading && <p className="triage-loading" role="status"><Spinner /> {copy.loadingFindings}</p>}
      {reviewError && <Alert type="error">{copy.reviewError}</Alert>}
      {findings && <TriageResults key={findings.id} findings={findings} language={language} />}
    </div>
  </div>;
}

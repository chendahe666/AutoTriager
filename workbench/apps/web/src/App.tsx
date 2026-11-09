import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import Checkbox from '@cloudscape-design/components/checkbox';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import Spinner from '@cloudscape-design/components/spinner';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Tabs from '@cloudscape-design/components/tabs';
import Textarea from '@cloudscape-design/components/textarea';
import { applyMode, Mode } from '@cloudscape-design/global-styles';
import { api, errorText, getPage, id, setCsrfToken } from './api';
import { dictionary, type Language, type TranslationKey } from './i18n';
import { reconcileRun, reconcileRunList } from './snapshots';
import { inspectionPolicy } from './policies';
import Evaluation from './Evaluation';
import InvestigationSummary from './InvestigationSummary';
import { recordedModel } from './summaryProjection';
import { RequestScope } from './requestScope';
import Knowledge from './Knowledge';
import ProactiveReplay from './ProactiveReplay';
import TriageWorkflow from './TriageWorkflow';
import MonitoringConsole from './MonitoringConsole';
import IncidentDesk from './IncidentDesk';
import InvestorShowcase from './InvestorShowcase';
import { isTerminal, type Answer, type CancelReceipt, type Candidate, type Capabilities, type Evidence, type Incident, type Investigation, type JournalEvent, type QueryDetail, type Run, type RunState } from './types';

type Route = { view: 'monitoring' | 'errors' | 'triage' | 'list' | 'new' | 'detail' | 'coverage' | 'evaluation' | 'knowledge' | 'proactive-replay' | 'showcase'; investigationId?: string; runId?: string; memoryId?: string; memoryReferenceRevision?: number; initialCaseId?: string; initialHistoryId?: string; incidentHistory?: boolean; incidentAgent?: boolean; evaluationCases?: boolean };
type T = (key: TranslationKey) => string;
function readRoute(): Route {
  const parts = location.hash.replace(/^#\/?/, '').split('/');
  if (parts[0] === 'monitoring') return { view: 'monitoring' };
  if (!parts[0] || parts[0] === 'errors' || parts[0] === 'conversations') {
    const isId = (value: string | undefined) => /^[A-Za-z0-9_-]{1,96}$/.test(value ?? '');
    if (parts[1] === 'history') return { view: 'errors', incidentHistory: true, initialHistoryId: isId(parts[2]) ? parts[2] : undefined };
    if (parts[1] === 'agent') return { view: 'errors', incidentAgent: true, initialCaseId: isId(parts[2]) ? parts[2] : undefined };
    return { view: 'errors', initialCaseId: isId(parts[1]) ? parts[1] : undefined };
  }
  if (parts[0] === 'showcase') return { view: 'showcase' };
  if (parts[0] === 'triage') return { view: 'triage' };
  if (parts[0] === 'investigations' && parts[1] && parts[1] !== 'new') return { view: 'detail', investigationId: decodeURIComponent(parts[1]), runId: parts[2] === 'runs' && parts[3] ? decodeURIComponent(parts[3]) : undefined };
  if (parts[0] === 'new' || parts[1] === 'new') return { view: 'new' };
  if (parts[0] === 'knowledge') return { view: 'knowledge', memoryId: parts[1] ? decodeURIComponent(parts[1]) : undefined, memoryReferenceRevision: parts[2] === 'reference' && /^[1-9]\d*$/.test(parts[3] ?? '') ? Number(parts[3]) : undefined };
  if (parts[0] === 'evaluation') return { view: 'evaluation', evaluationCases: parts[1] === 'cases' };
  if (parts[0] === 'coverage' || parts[0] === 'proactive-replay') return { view: parts[0] };
  return { view: 'list' };
}
const navigate = (path: string) => { location.hash = path; };
const runPath = (inv: string, run?: string) => `/investigations/${id(inv)}${run ? `/runs/${id(run)}` : ''}`;
const date = (value: string) => value.replace('T', ' ').replace(/\.\d+Z$/, ' UTC').replace(/Z$/, ' UTC');
const windowText = (value: { start: string; end: string }) => `${date(value.start)} → ${date(value.end)}`;
const eventTypes = ['run.state_changed', 'tool.started', 'tool.finished', 'evidence.added', 'answer.received', 'validation.finished', 'usage.observed', 'warning', 'late_observation'];
const eventKeys: Record<string, TranslationKey> = { 'run.state_changed': 'eventState', 'tool.started': 'toolStarted', 'tool.finished': 'toolFinished', 'evidence.added': 'evidenceAdded', 'answer.received': 'answerReceived', 'validation.finished': 'validationFinished', 'usage.observed': 'usageObserved', warning: 'warning', late_observation: 'lateObservation' };

function Panel({ title, aside, children, className = '' }: { title?: ReactNode; aside?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`panel ${className}`}>{title && <div className="panel-heading"><h2>{title}</h2>{aside}</div>}<div className="panel-content">{children}</div></section>;
}
function ModeBadge({ mode, t }: { mode: string; t: T }) { return <span className={`mode-badge ${mode === 'fixture' ? 'synthetic' : ''}`}><span aria-hidden="true">◇</span> {t(mode === 'fixture' ? 'fixture' : 'replay')}</span>; }
function State({ value }: { value: RunState }) {
  const type = value === 'COMPLETED' ? 'success' : ['FAILED', 'TIMED_OUT', 'BUDGET_EXHAUSTED'].includes(value) ? 'error' : ['CANCELLED', 'CANCEL_REQUESTED'].includes(value) ? 'stopped' : value === 'RUNNING' || value === 'VALIDATING' ? 'in-progress' : 'pending';
  return <StatusIndicator type={type}>{value}</StatusIndicator>;
}
function ErrorNotice({ error, t }: { error: string; t: T }) { return <Alert type="error" header={t('failed')}>{error}</Alert>; }
function Fact({ label, children, mono = false }: { label: ReactNode; children: ReactNode; mono?: boolean }) { return <div className="fact"><dt>{label}</dt><dd className={mono ? 'mono' : ''}>{children}</dd></div>; }
function Empty({ title, children }: { title: string; children?: ReactNode }) { return <div className="empty"><div className="empty-mark" aria-hidden="true">⌕</div><h2>{title}</h2>{children}</div>; }

function Coverage({ incident, t, compact = false }: { incident: Incident; t: T; compact?: boolean }) {
  return <div className={compact ? 'coverage compact' : 'coverage'}>{(['metrics', 'logs', 'traces'] as const).map(key => {
    const item = incident.modalities[key];
    return <div className={`modality ${item.availability}`} key={key}><div className="modality-top"><strong>{t(key)}</strong><span className="availability"><i />{t(item.availability)}</span></div><p>{item.record_count === null ? t('unknown') : `${item.record_count.toLocaleString()} ${t('records')}`}</p>{!compact && <><div className="small muted">{t('originalTimestamp')}: <code>{item.original_timestamp_unit}</code></div><div className="small muted">{t('units')}: {item.value_units.join(', ') || '—'}</div>{item.reason && <p className="small source-text">{item.reason}</p>}</>}</div>;
  })}</div>;
}
function Readiness({ caps, t }: { caps: Capabilities; t: T }) {
  return <Panel title={t('readiness')}><dl className="facts vertical"><Fact label={t('model')}>{caps.model_configured ? caps.resolved_model_id ?? t('configured') : t('notConfigured')}</Fact><Fact label={t('engine')}>{t(caps.engine_runtime_verified ? 'verified' : 'notVerified')}</Fact><Fact label={t('inference')}>{t(caps.inference_authorized ? 'authorized' : 'notAuthorized')}</Fact></dl><p className="small muted">{t('fixtureReady')}</p>{caps.pending_gates.length > 0 && <details><summary>{t('pending')} ({caps.pending_gates.length})</summary><ul className="plain-list source-text">{caps.pending_gates.map(gate => <li key={gate}>{gate}</li>)}</ul></details>}</Panel>;
}

function InvestigationList({ incidents, t }: { incidents: Incident[]; t: T }) {
  const [items, setItems] = useState<Investigation[]>([]);
  const [latest, setLatest] = useState<Record<string, Run>>({});
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [stateFilter, setStateFilter] = useState('');
  const load = useCallback(async (next?: string | null) => {
    setLoading(true); setError('');
    try {
      const page = await getPage<Investigation>('/investigations', next);
      setItems(previous => next ? [...previous, ...page.items.filter(item => !previous.some(old => old.investigation_id === item.investigation_id))] : page.items);
      setCursor(page.next_cursor);
      const settled = await Promise.allSettled(page.items.filter(item => item.latest_run_id).map(async item => [item.investigation_id, await api<Run>(`/runs/${id(item.latest_run_id!)}`)] as const));
      const results: Record<string, Run> = {};
      for (const result of settled) { if (result.status === 'fulfilled') results[result.value[0]] = result.value[1]; else setError(errorText(result.reason)); }
      setLatest(previous => { const merged = next ? { ...previous } : {} as Record<string, Run>; for (const [key, value] of Object.entries(results)) merged[key] = reconcileRun(previous[key], value); return merged; });
    } catch (error) { setError(errorText(error)); } finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  const filtered = items.filter(item => `${item.title} ${item.incident_id} ${item.investigation_id}`.toLowerCase().includes(query.toLowerCase()) && (!stateFilter || latest[item.investigation_id]?.state === stateFilter));
  return <><div className="page-title"><div><div className="eyebrow">{t('investigations')}</div><h1>{t('title')}</h1><p>{t('subtitle')}</p></div><Button variant="primary" iconName="add-plus" onClick={() => navigate('/new')}>{t('new')}</Button></div>{error && <ErrorNotice error={error} t={t} />}<section className="panel list-panel"><div className="panel-heading"><h2>{t('history')} <span className="count">({items.length} {t('loaded')})</span></h2><Button iconName="refresh" disabled={loading} onClick={() => void load()}>{t('refresh')}</Button></div><div className="table-toolbar"><div className="search"><Input type="search" value={query} onChange={({ detail }) => setQuery(detail.value)} placeholder={t('search')} ariaLabel={t('search')} /></div><Select selectedOption={{ label: stateFilter || t('allStates'), value: stateFilter }} options={[{ label: t('allStates'), value: '' }, ...['CREATED', 'QUEUED', 'RUNNING', 'VALIDATING', 'COMPLETED', 'FAILED', 'TIMED_OUT', 'BUDGET_EXHAUSTED', 'CANCEL_REQUESTED', 'CANCELLED'].map(value => ({ label: value, value }))]} onChange={({ detail }) => setStateFilter(detail.selectedOption.value ?? '')} ariaLabel={t('state')} /></div><div className="table-scroll"><table><thead><tr><th>{t('investigations')}</th><th>{t('incident')}</th><th>{t('state')}</th><th>{t('mode')}</th><th>{t('updated')}</th></tr></thead><tbody>{filtered.map(item => <tr key={item.investigation_id}><td><a className="record-title" href={`#${runPath(item.investigation_id)}`}>{item.title}</a><div className="mono muted small">{item.investigation_id}</div></td><td><code>{item.incident_id}</code><div className="small muted">{incidents.find(incident => incident.incident_id === item.incident_id)?.system_id ?? '—'}</div></td><td>{latest[item.investigation_id] ? <State value={latest[item.investigation_id].state} /> : item.latest_run_id ? t('unknown') : '—'}</td><td>{incidents.find(incident => incident.incident_id === item.incident_id) ? <ModeBadge mode={incidents.find(incident => incident.incident_id === item.incident_id)!.mode} t={t} /> : latest[item.investigation_id] ? <ModeBadge mode={latest[item.investigation_id].mode} t={t} /> : '—'}</td><td className="small nowrap">{date(item.updated_at)}</td></tr>)}</tbody></table></div>{!loading && filtered.length === 0 && <Empty title={items.length ? t('noMatches') : t('noInvestigations')}><p>{t('noInvestigationsText')}</p>{items.length === 0 && <Button onClick={() => navigate('/new')}>{t('new')}</Button>}</Empty>}{loading && <div className="loading"><Spinner /> {t('loading')}</div>}{cursor && <div className="panel-footer"><Button disabled={loading} onClick={() => void load(cursor)}>{t('loadMore')}</Button></div>}</section></>;
}

function StartInvestigation({ incidents, caps, t, loadIncidents, moreIncidents }: { incidents: Incident[]; caps: Capabilities; t: T; loadIncidents: () => Promise<void>; moreIncidents: boolean }) {
  const [incidentId, setIncidentId] = useState(incidents[0]?.incident_id ?? '');
  const [title, setTitle] = useState(''); const [question, setQuestion] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const attempt = useRef<{ signature: string; investigationKey: string; runKey: string; investigation?: Investigation } | null>(null);
  const incident = incidents.find(item => item.incident_id === incidentId);
  const policy = inspectionPolicy(incident?.mode, caps.allowed_policy_ids);
  const allowed = Boolean(policy);
  async function submit() {
    if (!incident || !question.trim() || busy) return;
    const body = { incident_id: incident.incident_id, ...(title.trim() ? { title: title.trim() } : {}) };
    const signature = JSON.stringify({ body, question: question.trim() });
    if (!attempt.current || attempt.current.signature !== signature) attempt.current = { signature, investigationKey: crypto.randomUUID(), runKey: crypto.randomUUID() };
    const request = attempt.current;
    setBusy(true); setError('');
    try {
      const investigation = request.investigation ?? await api<Investigation>('/investigations', { method: 'POST', body, key: request.investigationKey });
      request.investigation = investigation;
      const run = await api<Run>(`/investigations/${id(investigation.investigation_id)}/runs`, { method: 'POST', body: { question: question.trim(), policy_id: policy }, key: request.runKey });
      navigate(runPath(investigation.investigation_id, run.run_id));
    } catch (error) { setError(errorText(error)); } finally { setBusy(false); }
  }
  return <><button className="back-link" onClick={() => navigate('/investigations')}>← {t('back')}</button><div className="page-title"><div><div className="eyebrow">{t('new')}</div><h1>{t('startTitle')}</h1><p>{t('startDescription')}</p></div></div><div className="start-layout"><Panel title={t('scope')}><form onSubmit={event => { event.preventDefault(); void submit(); }}><div className="form-stack"><FormField label={t('incident')}><Select selectedOption={incident ? { label: incident.display_name, value: incident.incident_id, description: incident.incident_id } : null} options={incidents.map(item => ({ label: item.display_name, value: item.incident_id, description: `${item.incident_id} · ${item.system_id}` }))} onChange={({ detail }) => setIncidentId(detail.selectedOption.value ?? '')} placeholder={t('selectIncident')} disabled={busy} /></FormField>{moreIncidents && <Button formAction="none" onClick={() => void loadIncidents()}>{t('loadMore')}</Button>}{incident && <><dl className="facts vertical"><Fact label={t('window')}>{windowText(incident.time_window)}</Fact><Fact label={t('services')} mono>{incident.service_ids.join(' · ')}</Fact></dl><Coverage incident={incident} t={t} /><p className="small muted">{t('missingDifferent')}</p>{incident.coverage_warnings.length > 0 && <Alert type="warning">{incident.coverage_warnings.map(warning => <div className="source-text" key={warning}>{warning}</div>)}</Alert>}</>}<FormField label={<>{t('investigationTitle')} <span className="muted">({t('optional')})</span></>}><Input value={title} onChange={({ detail }) => setTitle(detail.value)} disabled={busy} /></FormField><FormField label={t('question')} description={t('questionHint')}><Textarea value={question} onChange={({ detail }) => setQuestion(detail.value)} placeholder={t('questionPlaceholder')} rows={4} disabled={busy} /></FormField>{error && <><ErrorNotice error={error} t={t} /><p className="small muted">{t('retryDescription')}</p></>}{!allowed && incident && <Alert type="warning">{t('blocked')}</Alert>}<div className="form-action"><Button variant="primary" loading={busy} disabled={!incident || !question.trim() || question.length > 8000 || title.length > 200 || !allowed || !caps.allowed_policy_ids.length} formAction="submit">{t(incident?.mode === 'fixture' ? 'start' : 'startReplay')}</Button></div></div></form></Panel><Readiness caps={caps} t={t} /></div></>;
}

function EvidenceRefs({ ids, open, t }: { ids: string[]; open: (evidence: string) => void; t: T }) {
  const links = (values: string[]) => values.map(value => <button key={value} onClick={() => open(value)} className="evidence-link" title={`${t('inspect')}: ${value}`}><span aria-hidden="true">↗</span><code>{value}</code></button>);
  return ids.length ? <div className="evidence-links">{links(ids.slice(0, 3))}{ids.length > 3 && <details><summary>+{ids.length - 3} {t('moreEvidence')}</summary>{links(ids.slice(3))}</details>}</div> : <span className="small muted">{t('noReferences')}</span>;
}
function StringList({ items, fallback }: { items: string[]; fallback?: string }) { return items.length ? <ul className="plain-list source-text">{items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul> : <span className="muted small">{fallback ?? '—'}</span>; }
function CandidateCard({ candidate, open, t }: { candidate: Candidate; open: (evidence: string) => void; t: T }) {
  return <div className="comparison-card"><div className="candidate-label"><span className="rank">{candidate.rank}</span><h3 className="mono">{candidate.service_id}</h3></div><div className="comparison-section"><h4>{t('hypothesis')}</h4><p className="source-text">{candidate.hypothesis}</p></div><div className="comparison-section"><h4>{t('support')}</h4><EvidenceRefs ids={candidate.supporting_evidence_ids} open={open} t={t} /></div><div className="comparison-section"><h4>{t('contradiction')}</h4><EvidenceRefs ids={candidate.contradicting_evidence_ids} open={open} t={t} /></div><div className="comparison-section"><h4>{t('missing')}</h4><StringList items={candidate.missing_checks} fallback={t('noReferences')} /></div></div>;
}
function Candidates({ answer, open, t, selected, setSelected, inspection = false }: { inspection?: boolean; answer: Answer; open: (evidence: string) => void; t: T; selected: string[]; setSelected: (value: string[]) => void }) {
  return <><Alert type="info">{t(inspection ? 'inspectionNote' : 'rankingNote')}</Alert>{answer.status === 'insufficient_evidence' ? <Panel><Empty title={t(inspection ? 'inspectionTitle' : 'insufficient')}><p>{t(inspection ? 'inspectionText' : 'insufficientText')}</p></Empty></Panel> : <Panel title={t('ranking')} aside={<span className="small muted">{t('compareHint')}</span>}><div className="candidate-list">{answer.candidates.map(candidate => <div className="candidate-row" key={candidate.service_id}><Checkbox checked={selected.includes(candidate.service_id)} disabled={selected.length >= 2 && !selected.includes(candidate.service_id)} onChange={({ detail }) => setSelected(detail.checked ? [...selected, candidate.service_id] : selected.filter(value => value !== candidate.service_id))}>{t('compare')} <span className="sr-only">{candidate.service_id}</span></Checkbox><span className="rank">{candidate.rank}</span><div><h3 className="mono">{candidate.service_id}</h3><p className="source-text">{candidate.hypothesis}</p><EvidenceRefs ids={candidate.supporting_evidence_ids} open={open} t={t} /></div></div>)}</div></Panel>}{selected.length > 0 && <Panel title={t('compare')}><div className="comparison-grid">{answer.candidates.filter(candidate => selected.includes(candidate.service_id)).map(candidate => <CandidateCard key={candidate.service_id} candidate={candidate} open={open} t={t} />)}</div></Panel>}<div className="two-col"><Panel title={t('observations')}>{answer.observations.map((observation, index) => <div className="observation" key={index}><p className="source-text">{observation.text}</p><EvidenceRefs ids={observation.evidence_ids} open={open} t={t} /></div>)}</Panel><Panel title={t('unknowns')}><StringList items={answer.unknowns} /></Panel></div></>;
}

function Timeline({ events, open, t }: { events: JournalEvent[]; open: (evidence: string) => void; t: T }) {
  return <Panel title={t('activity')} aside={<span className="mono small muted">{events.length} {t('loaded')}</span>}>{events.length === 0 ? <Empty title={t('noEvents')} /> : <ol className="timeline">{events.map(event => {
    const payload = event.payload;
    const evidenceIds = Array.isArray(payload.evidence_ids) ? payload.evidence_ids.filter((value): value is string => typeof value === 'string') : [];
    return <li key={event.event_id}><div className="timeline-marker">{event.sequence}</div><div className="event-body"><div className="event-top"><h3>{t(eventKeys[event.event_type] ?? 'event')}</h3><time className="small muted">{date(event.received_at)}</time></div>{event.event_type === 'run.state_changed' && <div className="state-transition"><code>{String(payload.from_state)}</code><span>→</span><code>{String(payload.to_state)}</code></div>}{payload.reason != null && <p className="source-text">{String(payload.reason)}</p>}{payload.tool_name != null && <p><code>{String(payload.tool_name)}</code></p>}{payload.status != null && <p className="small"><strong>{String(payload.status)}</strong>{payload.returned_count != null && <> · {t('returned')}: {String(payload.returned_count)}</>}{payload.truncated === true && <> · {t('truncated')}</>}</p>}{payload.message != null && <p className="source-text">{String(payload.message)}</p>}{event.upstream_call_id && <div className="small muted">{t('call')}: <code>{event.upstream_call_id}</code></div>}{payload.query_id != null && <div className="small muted">{t('query')}: <code>{String(payload.query_id)}</code></div>}{evidenceIds.length > 0 && <EvidenceRefs ids={evidenceIds} open={open} t={t} />}<details className="event-details"><summary>{t('rawPayload')}</summary><pre>{JSON.stringify(payload, null, 2)}</pre></details></div></li>;
  })}</ol>}</Panel>;
}

function useJournal(run: Run | null, refreshRun: (run: Run) => void) {
  const [events, setEvents] = useState<JournalEvent[]>([]); const [streamState, setStreamState] = useState<'connecting' | 'live' | 'reconnecting' | 'recorded' | 'error'>('connecting');
  const [streamError, setStreamError] = useState(''); const [retry, setRetry] = useState(0); const latestRun = useRef(run); latestRun.current = run;
  useEffect(() => {
    if (!run) return;
    let alive = true; let source: EventSource | null = null; let timer: ReturnType<typeof setTimeout> | undefined; let cursor = 0; let retries = 0;
    const runId = run.run_id;
    setEvents([]); setStreamError(''); setStreamState('connecting');
    const reconnect = () => { if (alive) timer = setTimeout(connect, Math.min(5000, 700 + retries * 500)); };
    const recover = async () => {
      source?.close();
      if (!alive) return;
      try {
        const current = await api<Run>(`/runs/${id(runId)}`);
        if (!alive) return;
        const admitted = reconcileRun(latestRun.current, current);
        refreshRun(admitted);
        if (isTerminal(admitted.state) && cursor >= admitted.last_sequence) { setStreamState('recorded'); setStreamError(''); return; }
      } catch (error) { if (alive) setStreamError(errorText(error)); }
      if (!alive) return;
      retries += 1;
      if (retries > 8) { setStreamState('error'); return; }
      setStreamState('reconnecting'); reconnect();
    };
    const receive = (message: MessageEvent) => {
      if (!alive) return;
      try {
        const event = JSON.parse(message.data) as JournalEvent;
        if (event.run_id !== runId || !Number.isInteger(event.sequence)) throw new Error('Invalid journal event identity.');
        if (event.sequence <= cursor) return;
        if (event.sequence !== cursor + 1) throw new Error('Journal sequence gap; replay is required.');
        cursor = event.sequence; retries = 0; setStreamError('');
        setEvents(previous => [...previous, event]);
        if (event.event_type === 'run.state_changed' || event.event_type === 'answer.received') void api<Run>(`/runs/${id(runId)}`).then(current => { if (alive) refreshRun(current); }).catch(error => { if (alive) setStreamError(errorText(error)); });
      } catch (error) { setStreamError(errorText(error)); void recover(); }
    };
    function connect() {
      if (!alive) return;
      source = new EventSource(`/api/v1/runs/${id(runId)}/events?after=${cursor}`, { withCredentials: true });
      source.onopen = () => { if (alive) setStreamState('live'); };
      for (const type of eventTypes) source.addEventListener(type, receive as EventListener);
      source.onerror = () => { void recover(); };
    }
    connect();
    return () => { alive = false; source?.close(); if (timer) clearTimeout(timer); };
    // A changed snapshot must not discard the replay cursor or restart a stream.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.run_id, retry]);
  return { events, streamState, streamError, retryStream: () => setRetry(value => value + 1) };
}

function Followup({ run, incident, investigation, caps, t, onCreated }: { run: Run | null; incident: Incident; investigation: Investigation; caps: Capabilities; t: T; onCreated: (run: Run) => void }) {
  const [question, setQuestion] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const attempt = useRef<{ signature: string; key: string } | null>(null);
  const requestScope = useRef(new RequestScope());
  const scopeKey = JSON.stringify([investigation.investigation_id, run?.run_id ?? null]);
  useLayoutEffect(() => {
    const ticket = requestScope.current.enter(scopeKey);
    return () => requestScope.current.invalidate(ticket);
  }, [scopeKey]);
  const policy = inspectionPolicy(incident.mode, caps.allowed_policy_ids);
  const allowed = Boolean(policy) && (!run || isTerminal(run.state));
  useEffect(() => { setQuestion(''); setError(''); setBusy(false); attempt.current = null; }, [run?.run_id]);
  async function submit() {
    if (!question.trim() || !allowed || busy) return;
    const ticket = requestScope.current.capture();
    if (!requestScope.current.isCurrent(ticket) || ticket?.scope !== scopeKey) return;
    const body = { question: question.trim(), ...(run ? { parent_run_id: run.run_id } : {}), policy_id: policy };
    const signature = JSON.stringify(body);
    if (attempt.current?.signature !== signature) attempt.current = { signature, key: crypto.randomUUID() };
    setBusy(true); setError('');
    try { const child = await api<Run>(`/investigations/${id(investigation.investigation_id)}/runs`, { method: 'POST', body, key: attempt.current!.key }); if (requestScope.current.isCurrent(ticket)) onCreated(child); } catch (error) { if (requestScope.current.isCurrent(ticket)) setError(errorText(error)); } finally { if (requestScope.current.isCurrent(ticket)) setBusy(false); }
  }
  return <Panel title={t(run ? 'followup' : 'newRun')} className="followup"><p className="small muted">{t(run ? 'followupNote' : 'emptyRuns')}</p><form onSubmit={event => { event.preventDefault(); void submit(); }}><FormField label={t('question')}><Textarea value={question} onChange={({ detail }) => setQuestion(detail.value)} placeholder={t('followupPlaceholder')} rows={3} disabled={busy} /></FormField>{run && !isTerminal(run.state) && <p className="small muted">{t('terminalParent')}</p>}{error && <div className="mt"><ErrorNotice error={error} t={t} /></div>}<div className="form-action"><span className="small mono muted">{t('incident')}: {investigation.incident_id}</span><Button variant="primary" formAction="submit" loading={busy} disabled={!allowed || !question.trim() || question.length > 8000 || !caps.allowed_policy_ids.length}>{t(run ? 'submitFollowup' : 'newRun')}</Button></div></form></Panel>;
}

function Detail({ route, caps, t }: { route: Route; caps: Capabilities; t: T }) {
  const [investigation, setInvestigation] = useState<Investigation | null>(null); const [incident, setIncident] = useState<Incident | null>(null); const [runs, setRuns] = useState<Run[]>([]); const [run, setRun] = useState<Run | null>(null); const [cursor, setCursor] = useState<string | null>(null); const [loading, setLoading] = useState(true); const [historyLoading, setHistoryLoading] = useState(false); const [error, setError] = useState(''); const [tab, setTab] = useState('summary'); const [selected, setSelected] = useState<string[]>([]); const [evidenceLocation, setEvidenceLocation] = useState<{ runId: string; evidenceId: string } | null>(null); const [cancelBusy, setCancelBusy] = useState(false); const [receipt, setReceipt] = useState<CancelReceipt | null>(null); const [now, setNow] = useState(Date.now());
  const requestScope = useRef(new RequestScope());
  const scopeKey = JSON.stringify([route.investigationId, route.runId ?? null]);
  useLayoutEffect(() => {
    const ticket = requestScope.current.enter(scopeKey);
    return () => requestScope.current.invalidate(ticket);
  }, [scopeKey]);
  const updateRun = useCallback((value: Run) => { setRun(previous => previous?.run_id === value.run_id ? reconcileRun(previous, value) : previous); setRuns(previous => previous.map(item => item.run_id === value.run_id ? reconcileRun(item, value) : item)); }, []);
  const journal = useJournal(run, updateRun);
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  useEffect(() => {
    let active = true; setLoading(true); setHistoryLoading(false); setCancelBusy(false); setError(''); setRun(null); setReceipt(null); setSelected([]); setEvidenceLocation(null);
    void (async () => {
      try {
        const investigation = await api<Investigation>(`/investigations/${id(route.investigationId!)}`);
        const [incident, history] = await Promise.all([api<Incident>(`/incidents/${id(investigation.incident_id)}`), getPage<Run>(`/investigations/${id(investigation.investigation_id)}/runs`)]);
        const selectedRunId = route.runId ?? investigation.latest_run_id ?? history.items[history.items.length - 1]?.run_id;
        const selectedRun = selectedRunId ? await api<Run>(`/runs/${id(selectedRunId)}`) : null;
        if (selectedRun && selectedRun.investigation_id !== investigation.investigation_id) throw new Error('The selected run does not belong to this investigation.');
        if (!active) return;
        setInvestigation(investigation); setIncident(incident); setRuns(selectedRun ? reconcileRunList(history.items, [selectedRun], true) : history.items); setCursor(history.next_cursor); setRun(selectedRun);
      } catch (error) { if (active) setError(errorText(error)); } finally { if (active) setLoading(false); }
    })();
    return () => { active = false; };
  }, [route.investigationId, route.runId]);
  async function moreRuns() {
    if (!investigation || !cursor || historyLoading || investigation.investigation_id !== route.investigationId) return;
    const ticket = requestScope.current.capture();
    if (!requestScope.current.isCurrent(ticket) || ticket?.scope !== scopeKey) return;
    setHistoryLoading(true);
    try {
      const page = await getPage<Run>(`/investigations/${id(investigation.investigation_id)}/runs`, cursor);
      if (!requestScope.current.isCurrent(ticket)) return;
      setRuns(previous => reconcileRunList(previous, page.items, true)); setCursor(page.next_cursor);
    } catch (error) { if (requestScope.current.isCurrent(ticket)) setError(errorText(error)); }
    finally { if (requestScope.current.isCurrent(ticket)) setHistoryLoading(false); }
  }
  async function cancel() {
    if (!run || cancelBusy || run.investigation_id !== route.investigationId) return;
    const ticket = requestScope.current.capture();
    if (!requestScope.current.isCurrent(ticket) || ticket?.scope !== scopeKey) return;
    setCancelBusy(true); setError('');
    try {
      const value = await api<CancelReceipt>(`/runs/${id(run.run_id)}/cancel`, { method: 'POST' });
      if (!requestScope.current.isCurrent(ticket)) return;
      setReceipt(value);
      const current = await api<Run>(`/runs/${id(run.run_id)}`);
      if (requestScope.current.isCurrent(ticket)) updateRun(current);
    } catch (error) { if (requestScope.current.isCurrent(ticket)) setError(errorText(error)); }
    finally { if (requestScope.current.isCurrent(ticket)) setCancelBusy(false); }
  }
  const openEvidence = (value: string) => { if (run) setEvidenceLocation({ runId: run.run_id, evidenceId: value }); };
  if (loading) return <div className="loading large"><Spinner size="big" /> {t('loading')}</div>;
  if (!investigation || !incident) return <><button className="back-link" onClick={() => navigate('/investigations')}>← {t('back')}</button><ErrorNotice error={error || t('noData')} t={t} /></>;
  const elapsed = run ? Math.max(0, Math.round(((isTerminal(run.state) ? Date.parse(run.updated_at) : now) - Date.parse(run.created_at)) / 1000)) : 0;
  const usage = [...journal.events].reverse().find(event => event.event_type === 'usage.observed')?.payload;
  const selectedModel = recordedModel(run);
  const selectedModelText = selectedModel.kind === 'recorded' ? selectedModel.modelId : t(selectedModel.kind === 'no_model' ? 'summaryNoModelUsed' : selectedModel.kind === 'no_run' ? 'summaryNoSelectedRun' : 'summaryModelNotRecorded');
  return <><button className="back-link" onClick={() => navigate('/investigations')}>← {t('back')}</button><div className="page-title detail-title"><div><div className="eyebrow mono">{investigation.investigation_id}</div><h1>{investigation.title}</h1><div className="header-meta"><ModeBadge mode={incident.mode} t={t} /><span className="mono">{incident.incident_id}</span><span>{incident.system_id}</span></div></div>{run && <div className="run-actions"><State value={run.state} /><Button disabled={isTerminal(run.state) || run.state === 'CANCEL_REQUESTED'} loading={cancelBusy} onClick={() => void cancel()}>{t('cancel')}</Button></div>}</div>{error && <ErrorNotice error={error} t={t} />}<div className="scope-strip"><div><span>{t('window')}</span><strong>{windowText(incident.time_window)}</strong></div><div><span>{t('services')}</span><strong className="mono">{incident.service_ids.join(' · ')}</strong></div><div><span>{t('summaryAnalysisModel')}</span><strong>{selectedModelText}</strong></div></div><Coverage incident={incident} t={t} compact /><div className="investigation-record"><div className="run-toolbar"><div className="run-selector"><label>{t('runs')}</label><Select selectedOption={run ? { label: `${run.run_id}${run.parent_run_id ? ` · ${t('child')}` : ''}`, value: run.run_id, description: run.state } : null} options={runs.map(item => ({ label: `${item.run_id}${item.parent_run_id ? ` · ${t('child')}` : ''}`, value: item.run_id, description: `${item.state} · ${date(item.created_at)}` }))} onChange={({ detail }) => navigate(runPath(investigation.investigation_id, detail.selectedOption.value))} ariaLabel={t('selectRun')} placeholder={t('selectRun')} />{cursor && <Button loading={historyLoading} onClick={() => void moreRuns()}>{t('loadMore')}</Button>}</div>{run && <div className="journal-state"><span className={`connection-dot ${journal.streamState}`} /><span>{t(journal.streamState === 'live' ? 'live' : journal.streamState === 'recorded' ? 'journalRead' : journal.streamState === 'error' ? 'networkFailure' : 'reconnecting')}</span><span className="mono small">{t('sequence')}: {journal.events.at(-1)?.sequence ?? 0}</span><span className="small">{t('elapsed')}: {elapsed}{t('seconds')}</span></div>}</div>{journal.streamError && <Alert type="warning" header={t('replayError')}>{journal.streamError}<div className="mt"><Button onClick={journal.retryStream}>{t('retry')}</Button></div></Alert>}{receipt && <Alert type={receipt.billing_may_continue ? 'warning' : 'info'} header={t('cancellation')}><div className="receipt-grid">{(['logical_fence_active', 'provider_dispatch_pending', 'billing_may_continue'] as const).map((key, index) => <span key={key}>{t((['fence', 'providerPending', 'billing'] as const)[index])}: <strong>{t(receipt[key] ? 'yes' : 'no')}</strong></span>)}</div><p className="small">{t('cancellationNote')}</p></Alert>}{run && <><div className="detail-content"><Tabs activeTabId={tab} onChange={({ detail }) => setTab(detail.activeTabId)} tabs={[{ id: 'summary', label: t('summary'), content: <InvestigationSummary investigation={investigation} incident={incident} run={run} t={t} openEvidence={openEvidence} /> }, { id: 'timeline', label: t('timeline'), content: <Timeline events={journal.events} open={openEvidence} t={t} /> }, { id: 'candidates', label: t('candidates'), content: run.answer ? <Candidates inspection={run.config.policy_id === 'offline_inspection'} answer={run.answer} open={openEvidence} t={t} selected={selected} setSelected={setSelected} /> : <Panel><Empty title={t(isTerminal(run.state) ? 'noAnswer' : 'waiting')}><p>{t(isTerminal(run.state) ? 'noAnswerText' : 'waitingText')}</p>{run.termination_code && <code>{run.termination_code}</code>}</Empty></Panel> }, { id: 'checks', label: t('checks'), content: <Panel title={t('checks')}><p className="small muted">{t('nextChecksNote')}</p><StringList items={run.answer?.next_checks ?? []} fallback={t('noChecks')} />{run.answer && <><h3 className="subheading">{t('unknowns')}</h3><StringList items={run.answer.unknowns} /></>}</Panel> }]} /></div><div className="run-footer-details"><details><summary>{t('frozenConfig')}</summary><dl className="facts"><Fact label={t('run')} mono>{run.run_id}</Fact><Fact label={t('parent')} mono>{run.parent_run_id ?? t('none')}</Fact><Fact label={t('policy')} mono>{run.config.policy_id} / {run.config.policy_version}</Fact><Fact label={t('revision')} mono>{run.config.data_revision}</Fact><Fact label={t('semantic')}>{t('notAssessed')}</Fact><Fact label={t('termination')} mono>{run.termination_code ?? '—'}</Fact><Fact label={t('modelCalls')}>{usage?.model_calls === undefined || usage.model_calls === null ? t('unknown') : String(usage.model_calls)}</Fact><Fact label={t('toolCalls')}>{usage?.tool_calls === undefined || usage.tool_calls === null ? t('unknown') : String(usage.tool_calls)}</Fact></dl><h4>{t('originalQuestion')}</h4><p className="source-text">{run.question}</p><pre>{JSON.stringify(run.config, null, 2)}</pre></details></div></>}<details className="separate-analysis"><summary>{t('chatMoreAnalysis')}</summary><Followup run={run} incident={incident} investigation={investigation} caps={caps} t={t} onCreated={child => navigate(runPath(investigation.investigation_id, child.run_id))} /></details></div>{evidenceLocation && <EvidenceDrawer evidenceId={evidenceLocation.evidenceId} runId={evidenceLocation.runId} t={t} close={() => setEvidenceLocation(null)} open={value => setEvidenceLocation({ ...evidenceLocation, evidenceId: value })} />}</>;
}

function EvidenceDrawer({ evidenceId, runId, t, close, open }: { evidenceId: string; runId: string; t: T; close: () => void; open: (value: string) => void }) {
  const dialog = useRef<HTMLDialogElement>(null); const opener = useRef<HTMLElement | null>(null); const [evidence, setEvidence] = useState<Evidence | null>(null); const [query, setQuery] = useState<QueryDetail | null>(null); const [error, setError] = useState(''); const [loading, setLoading] = useState(true); const [retry, setRetry] = useState(0);
  useEffect(() => { opener.current = document.activeElement as HTMLElement; dialog.current?.showModal(); return () => { if (opener.current?.isConnected) opener.current.focus(); }; }, []);
  useEffect(() => {
    let active = true; setLoading(true); setEvidence(null); setQuery(null); setError('');
    void api<Evidence>(`/runs/${id(runId)}/evidence/${id(evidenceId)}`).then(async value => { if (!active) return; setEvidence(value); const detail = await api<QueryDetail>(`/runs/${id(runId)}/queries/${id(value.query_id)}`); if (active) setQuery(detail); }).catch(error => { if (active) setError(errorText(error)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [evidenceId, runId, retry]);
  return <dialog ref={dialog} className="evidence-drawer" aria-labelledby="evidence-title" onCancel={event => { event.preventDefault(); close(); }} onClick={event => { if (event.target === dialog.current) { const rect = dialog.current.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close(); } }}><div className="drawer-heading"><div><div className="eyebrow">{t('evidence')}</div><h2 id="evidence-title" className="mono">{evidenceId}</h2></div><Button iconName="close" variant="icon" ariaLabel={t('close')} onClick={close} /></div><div className="drawer-body"><div className="small muted mb">{t('evidenceNotice')}</div>{loading && <div className="loading"><Spinner /> {t('loading')}</div>}{error && <><ErrorNotice error={error} t={t} /><div className="mt"><Button onClick={() => setRetry(value => value + 1)}>{t('retry')}</Button></div></>}{evidence && <><div className="evidence-badges"><span className="tag">{t(evidence.kind)}</span><span className="tag">{evidence.modality}</span><span className="mono small">{evidence.service_id ?? '—'}</span></div><dl className="facts vertical"><Fact label={t('incident')} mono>{evidence.incident_id}</Fact><Fact label={t('run')} mono>{evidence.run_id}</Fact><Fact label={t('locator')} mono>{evidence.record_locator}</Fact><Fact label={t('observed')}>{windowText(evidence.observed_window)}</Fact><Fact label={t('preserved')}>{date(evidence.preserved_at)}</Fact><Fact label={t('units')} mono>{evidence.unit}</Fact></dl><h3>{t('original')}</h3><pre className="record-json" data-testid="original-record">{JSON.stringify(evidence.record, null, 2)}</pre><h3>{t('limitations')}</h3><StringList items={evidence.limitations} fallback={t('noReferences')} />{evidence.derivation && <div className="lineage"><h3>{t('derivation')}</h3><dl className="facts vertical"><Fact label={t('method')} mono>{evidence.derivation.method_id} / {evidence.derivation.method_version}</Fact><Fact label={t('missingPolicy')} mono>{evidence.derivation.missing_value_policy}</Fact></dl><h4>{t('parents')}</h4><EvidenceRefs ids={evidence.derivation.parent_evidence_ids} open={open} t={t} /></div>}<details className="source-digests"><summary>{t('source')}</summary><dl className="facts vertical"><Fact label={t('digest')} mono>{evidence.content_digest}</Fact><Fact label={t('artifactDigest')} mono>{evidence.source_artifact_digest}</Fact></dl></details></>}{query && <><h3>{t('queryArgs')}</h3><dl className="facts"><Fact label={t('query')} mono>{query.query.query_id}</Fact><Fact label={t('rowLimit')}>{query.query.limit}</Fact><Fact label={t('byteLimit')}>{query.query.max_bytes}</Fact></dl><pre>{JSON.stringify(query.query, null, 2)}</pre><h3>{t('queryResult')}</h3>{query.result ? <><div className={`result-state ${query.result.truncated ? 'truncated' : ''}`}><strong>{query.result.status}</strong><span>{t('returned')}: {query.result.returned_count}</span><span>{t('matched')}: {query.result.matched_count ?? t('unknown')}</span><span>{t('truncated')}: {t(query.result.truncated ? 'yes' : 'no')}</span><span>{t('resultComplete')}: {t(query.result.result_complete ? 'yes' : 'no')}</span></div><StringList items={query.result.limitations} /><pre>{JSON.stringify(query.result, null, 2)}</pre></> : <Alert type="info">{t('pendingResult')}</Alert>}</>}</div><div className="drawer-footer"><Button onClick={close}>{t('close')}</Button></div></dialog>;
}

function WorkspaceNav({ view, t, operations }: { view: Route['view']; t: T; operations: boolean }) {
  const focused = operations ? view === 'monitoring' || view === 'errors' : view === 'triage';
  const advancedLinks = [
    { path: '/triage', key: 'triageHome', active: view === 'triage', symbol: '▤' },
    { path: '/investigations', key: 'investigations', active: ['list', 'new', 'detail'].includes(view), symbol: '▤' },
    { path: '/knowledge', key: 'knowledge', active: view === 'knowledge', symbol: '▱' },
    { path: '/coverage', key: 'coverage', active: view === 'coverage', symbol: '▦' },
    { path: '/evaluation', key: 'evaluation', active: view === 'evaluation', symbol: '⌁' },
    { path: '/showcase', key: 'showcase', active: false, symbol: '◉' },
    { path: '/proactive-replay', key: 'proactiveReplay', active: view === 'proactive-replay', symbol: '◈' },
  ] as const;

  return <aside className="sidebar">
    <div className="nav-label">{t('workspace')}</div>
    <nav aria-label={t('workspace')}>
      {operations ? <><a href="#/monitoring" className={view === 'monitoring' ? 'active' : ''} aria-current={view === 'monitoring' ? 'page' : undefined}>
        <span aria-hidden="true">⌁</span>{t('monitoringHome')}
      </a>
      <a href="#/errors" className={view === 'errors' ? 'active' : ''} aria-current={view === 'errors' ? 'page' : undefined}>
        <span aria-hidden="true">▤</span>{t('incidentDeskHome')}
      </a></> : <a href="#/triage" className={view === 'triage' ? 'active' : ''} aria-current={view === 'triage' ? 'page' : undefined}>
        <span aria-hidden="true">▤</span>{t('triageHome')}
      </a>}
    </nav>
    <details key={focused ? 'focused' : 'advanced'} className="triage-advanced" open={!focused}>
      <summary>{t('advanced')}</summary>
      <nav aria-label={t('advanced')}>
        {advancedLinks.filter(item => operations || item.path !== '/triage').map(item => <a href={`#${item.path}`} className={item.active ? 'active' : ''} aria-current={item.active ? 'page' : undefined} aria-label={t(item.key)} key={item.key}>
          <span aria-hidden="true">{item.symbol}</span>{t(item.key)}
        </a>)}
      </nav>
    </details>
    {!focused && <>
      <div className="sidebar-objective"><div className="nav-label">{t('objective')}</div><p>{t('objectiveText')}</p><span className="unmeasured">{t('unmeasured')}</span></div>
      <div className="sidebar-footer"><span className="mono">v0.2 / LOCAL</span><p>{t('shellNote')}</p></div>
    </>}
  </aside>;
}

export default function App() {
  const [language, setLanguage] = useState<Language>(() => localStorage.getItem('autotriager.language') === 'zh' ? 'zh' : 'en');
  const t: T = key => dictionary[language][key];
  const [route, setRoute] = useState<Route>(readRoute); const [caps, setCaps] = useState<Capabilities | null>(null); const [incidents, setIncidents] = useState<Incident[]>([]); const [incidentCursor, setIncidentCursor] = useState<string | null>(null); const [error, setError] = useState(''); const [boot, setBoot] = useState(0);
  useEffect(() => { const listener = () => { setRoute(readRoute()); window.scrollTo({ top: 0 }); }; window.addEventListener('hashchange', listener); return () => window.removeEventListener('hashchange', listener); }, []);
  useEffect(() => { document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en'; localStorage.setItem('autotriager.language', language); }, [language]);
  useEffect(() => {
    let active = true; setError('');
    void Promise.all([api<Capabilities>('/capabilities'), getPage<Incident>('/incidents')]).then(([caps, page]) => { if (!active) return; setCsrfToken(caps.csrf_token); setCaps(caps); setIncidents(page.items); setIncidentCursor(page.next_cursor); }).catch(error => { if (active) setError(errorText(error)); });
    return () => { active = false; };
  }, [boot]);
  const darkWorkspace = caps?.operations_views === true && (route.view === 'errors' || route.view === 'monitoring');
  useLayoutEffect(() => {
    document.documentElement.classList.toggle('ops-dark', darkWorkspace);
    applyMode(darkWorkspace ? Mode.Dark : Mode.Light);
  }, [darkWorkspace]);
  if (route.view === 'showcase') return <InvestorShowcase language={language} onToggleLanguage={() => setLanguage(value => value === 'en' ? 'zh' : 'en')} />;
  async function moreIncidents() { try { const page = await getPage<Incident>('/incidents', incidentCursor); setIncidents(previous => [...previous, ...page.items.filter(item => !previous.some(old => old.incident_id === item.incident_id))]); setIncidentCursor(page.next_cursor); } catch (error) { setError(errorText(error)); } }
  const modes = new Set(incidents.map(incident => incident.mode));
  const operations = caps?.operations_views === true;
  const view = !operations && (route.view === 'monitoring' || route.view === 'errors') ? 'triage' : route.view;
  const focused = operations ? view === 'monitoring' || view === 'errors' : view === 'triage';
  return <div className="app"><a className="skip-link" href="#main-content" onClick={event => { event.preventDefault(); document.getElementById('main-content')?.focus(); }}>{t('workbench')}</a><header className="topbar"><a href={operations ? "#/errors" : "#/triage"} className="brand" aria-label={t('home')}><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span><strong>AutoTriager</strong><span className="brand-divider" /><span className="brand-description">{t('workbench')}</span></a><div className="topbar-right"><span className="local-indicator"><i />{t('local')}</span><button className="language-toggle" onClick={() => setLanguage(language === 'en' ? 'zh' : 'en')} aria-label={language === 'en' ? 'Switch interface to Chinese' : '切换界面为英文'}>{language === 'en' ? '中文' : 'English'}<span aria-hidden="true">⇄</span></button></div></header>{view !== 'errors' && <WorkspaceNav view={view} t={t} operations={operations} />}<div className={view === 'errors' ? "workspace workspace-incidents" : "workspace"}>{!focused && <div className="mode-banner"><span aria-hidden="true">◇</span>{caps && caps.allowed_policy_ids.length === 0 ? t('qualificationNotice') : modes.has('offline_replay') ? t('mixedNotice') : t('fixtureNotice')}</div>}<main id="main-content" tabIndex={-1}>{!caps ? error ? <><ErrorNotice error={error} t={t} /><div className="mt"><Button onClick={() => setBoot(value => value + 1)}>{t('retry')}</Button></div></> : <div className="loading large"><Spinner size="big" /> {t('session')}</div> : <>{error && <ErrorNotice error={error} t={t} />}{view === 'monitoring' && <MonitoringConsole language={language} />}{view === 'errors' && <IncidentDesk language={language} initialCaseId={route.initialCaseId} initialHistoryId={route.initialHistoryId} historyView={route.incidentHistory} agentView={route.incidentAgent} canSave={caps.allowed_policy_ids.length > 0} />}{view === 'triage' && <TriageWorkflow language={language} />}{view === 'list' && <InvestigationList incidents={incidents} t={t} />}{view === 'new' && <StartInvestigation incidents={incidents} caps={caps} t={t} loadIncidents={moreIncidents} moreIncidents={Boolean(incidentCursor)} />}{view === 'detail' && <Detail route={route} caps={caps} t={t} />}{view === 'coverage' && <><div className="page-title"><div><div className="eyebrow">{t('coverage')}</div><h1>{t('coverageTitle')}</h1><p>{t('coverageDescription')}</p></div></div><div className="start-layout"><div className="panel-stack">{incidents.length === 0 && <Panel><Empty title={t('emptyCatalog')} /></Panel>}{incidents.map(incident => <Panel key={incident.incident_id} title={<><code>{incident.incident_id}</code> · {incident.display_name}</>} aside={<ModeBadge mode={incident.mode} t={t} />}><dl className="facts vertical"><Fact label={t('window')}>{windowText(incident.time_window)}</Fact><Fact label={t('services')} mono>{incident.service_ids.join(' · ')}</Fact><Fact label={t('revision')} mono>{incident.data_revision}</Fact></dl><Coverage incident={incident} t={t} /><p className="small muted">{t('missingDifferent')}</p><StringList items={incident.coverage_warnings} /></Panel>)}{incidentCursor && <Button onClick={() => void moreIncidents()}>{t('loadMore')}</Button>}</div><Readiness caps={caps} t={t} /></div></>}{view === 'evaluation' && <Evaluation t={t} language={language} cases={route.evaluationCases} />}{view === 'proactive-replay' && <ProactiveReplay language={language} />}{view === 'knowledge' && <Knowledge memoryId={route.memoryId} referenceRevision={route.memoryReferenceRevision} t={t} />}</>}</main>{!focused && <footer className="workspace-footer"><span>AutoTriager</span><span>{t('shellNote')}</span><code>autotriager.api.v1</code></footer>}</div></div>;
}

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { api, errorText, id } from './api';
import type { Language } from './i18n';
import { groupWorkflowEvidence, groupWorkflowEvents, workflowCheckAction } from './incidentWorkflowPresentation';
import './incidentWorkflow.css';

type Evidence = {
  evidence_id: string;
  modality: string;
  service_id?: string | null;
  observed_window: { start: string; end: string };
  unit?: string | null;
  record: { label?: string; detail?: string; source?: string; [key: string]: unknown };
  derivation: unknown;
  limitations: string[];
};
type Check = {
  check_id: string; kind: string; title: string; purpose: string; service: string;
  verification_only: boolean;
  status: 'proposed' | 'running' | 'completed' | 'insufficient' | 'failed';
  result_summary?: string; evidence_ids: string[]; started_at?: string; completed_at?: string;
  limitations: string[]; selection_reason?: string;
};
type Workflow = {
  incident_id: string; revision: number;
  status: 'idle' | 'running' | 'waiting_for_review' | 'failed';
  summary: { title: string; source: string; start: string; end: string; impact: string; focus: string; next_step: string };
  hypotheses: {
    hypothesis_id: string; title: string; statement: string;
    status: 'candidate' | 'supported' | 'weakened' | 'unknown';
    model_assessment?: 'candidate' | 'supported' | 'weakened' | 'unknown';
    verification_status?: string; verification_note?: string;
    supporting_evidence_ids: string[]; contradicting_evidence_ids: string[];
  }[];
  checks: Check[];
  evidence: Evidence[];
  events: { id: string; kind: string; title: string; at: string; summary: string; check_id?: string; evidence_ids: string[] }[];
  recovery: { status: string; summary: string; evidence_ids: string[]; executed_action: boolean };
  cause: { status: string; summary: string };
  active_turn_id?: string;
  enabled: boolean;
  disabled_reason?: string;
};

const labels = {
  en: {
    title: 'Investigation', compact: 'Shared investigation', loading: 'Loading investigation state…',
    impact: 'What happened', focus: 'Current focus', next: 'Next check', investigate: 'Investigate',
    continue: 'Continue investigation', running: 'Investigating…', refresh: 'Refresh',
    timeline: 'Timeline', hypotheses: 'Hypotheses', evidence: 'Evidence', verification: 'Verification',
    observation: 'Source observation window', execution: 'Investigation time', timeNote: 'These timestamps record investigation actions. The original observation time is shown inside each evidence record.',
    emptyTimeline: 'No investigation actions have been recorded yet.', emptyEvidence: 'No evidence records have been returned.',
    emptyHypotheses: 'No hypotheses have been recorded yet.', proposed: 'Proposed', completed: 'Completed',
    insufficient: 'Insufficient evidence', failed: 'Failed', candidate: 'Candidate', supported: 'Supported',
    weakened: 'Weakened', unknown: 'Unknown', idle: 'Ready to investigate', waiting_for_review: 'Ready for review',
    support: 'Supporting evidence', contradict: 'Counterevidence', noSupport: 'No supporting record linked.',
    noContradict: 'No counterevidence linked. This does not confirm the hypothesis.',
    check: 'Run check', checking: 'Checking…', checkIntro: 'Checks inspect available records. They do not change the application.',
    why: 'Why this check', result: 'Result', limitations: 'Limits and missing information',
    source: 'Source', modality: 'Signal', service: 'Service', units: 'Unit', provenance: 'How this evidence was produced',
    raw: 'Returned source record', close: 'Close evidence', missing: 'Record not returned',
    openEvidence: 'Open evidence', relatedCheck: 'Related check', viewCheck: 'View check result',
    recovery: 'Recovery evidence', verify: 'Check later observations', verifying: 'Checking later observations…',
    verifyHint: 'Compare later recorded observations with the incident window. This does not execute a repair.',
    archiveNote: 'Later recorded latency can show normalization. It cannot establish that a repair was performed, that the whole application recovered, or that a root cause was confirmed.',
    cause: 'Root-cause status', unconfirmed: 'Unconfirmed', notRun: 'Not checked',
    none: 'Not provided', activity: 'Checks and results', detail: 'Inspect investigation details',
    prompt: 'Continue investigating this incident. Use the recorded evidence and completed checks to compare competing explanations. Propose the next discriminating read-only check and preserve unconfirmed causes.',
    disabled: 'Investigation is unavailable for this incident.',
    checkedEvidence: 'Evidence from executed checks', initialEvidence: 'Initial context records', noCheckedEvidence: 'No check has returned additional evidence yet. Initial incident context is available below.',
    moreEvidence: 'more evidence records', noVerification: 'No later-observation check is available for this incident.',
    earlierActivity: 'Earlier activity', latestActivity: 'Latest investigation', hypothesisNote: 'An explanation may fit the records while other causal mechanisms remain possible. Model support does not establish a verified root cause.',
    unverifiedExplanation: 'Unverified explanation', modelAssessment: 'Model assessment', modelInterpretation: 'Model interpretation · not a verified fact', interpretationNote: 'This is the preserved model assessment. Check its claims against source evidence and competing explanations.',
  },
  zh: {
    title: '调查', compact: '共享调查进度', loading: '正在加载调查进度…',
    impact: '发生了什么', focus: '当前调查重点', next: '下一步核查', investigate: '开始调查',
    continue: '继续调查', running: '正在调查…', refresh: '刷新',
    timeline: '时间线', hypotheses: '候选解释', evidence: '证据', verification: '恢复核查',
    observation: '原始记录时间窗', execution: '调查执行时间', timeNote: '这些时间表示调查操作发生的时间。每条证据内另列原始数据的观测时间。',
    emptyTimeline: '尚未记录调查操作。', emptyEvidence: '尚未返回证据记录。',
    emptyHypotheses: '尚未形成候选解释。', proposed: '待核查', completed: '已完成',
    insufficient: '证据不足', failed: '失败', candidate: '候选解释', supported: '支持',
    weakened: '支持减弱', unknown: '未知', idle: '可以开始调查', waiting_for_review: '待核查',
    support: '支持证据', contradict: '反向证据', noSupport: '尚未关联支持记录。',
    noContradict: '尚未关联反向证据，并不表示解释已被证实。',
    check: '执行核查', checking: '正在核查…', checkIntro: '核查只读取已有记录，不会修改应用。',
    why: '为什么检查', result: '核查结果', limitations: '限制与信息缺口',
    source: '来源', modality: '数据类型', service: '服务', units: '单位', provenance: '证据是如何产生的',
    raw: '返回的原始记录', close: '关闭证据', missing: '未返回此记录',
    openEvidence: '查看证据', relatedCheck: '关联核查', viewCheck: '查看核查结果',
    recovery: '恢复证据', verify: '核查后续观测', verifying: '正在核查后续观测…',
    verifyHint: '比较事故时间窗与后续已有记录，不执行修复。',
    archiveNote: '后续记录可以说明延迟回落，但不能证明已执行修复、整个应用已恢复，或根因已被确认。',
    cause: '根因状态', unconfirmed: '尚未确认', notRun: '尚未核查',
    none: '未提供', activity: '核查与结果', detail: '展开调查详情',
    prompt: '继续调查这条事故。根据已有证据和已完成的核查比较不同候选解释，提出下一个有区分力的只读核查项，不把未确认的原因当作事实。',
    disabled: '当前事故无法启动调查。',
    checkedEvidence: '已执行核查所关联的证据', initialEvidence: '初始上下文记录', noCheckedEvidence: '尚无核查返回新增证据。下方可展开初始事故记录。',
    moreEvidence: '条其他证据', noVerification: '这条事故尚无可用的后续观测核查。',
    earlierActivity: '此前调查记录', latestActivity: '最新一轮调查', hypothesisNote: '一种解释与记录相符，仍可能存在其他致因机制。模型的支持判断不等于根因已被证实。',
    unverifiedExplanation: '尚未验证的解释', modelAssessment: '模型判断', modelInterpretation: '模型解释 · 并非已验证事实', interpretationNote: '此处保留模型原始判断。需要对照原始证据与其他候选解释进行核查。',
  },
} as const;
type Copy = typeof labels[Language];
type Tab = 'timeline' | 'hypotheses' | 'evidence' | 'verification';

function time(value: string | undefined, language: Language) {
  if (!value) return '—';
  const stamp = Date.parse(value);
  return Number.isFinite(stamp) ? new Intl.DateTimeFormat(language === 'zh' ? 'zh-CN' : 'en-US', {
    dateStyle: 'short', timeStyle: 'medium', timeZone: 'UTC', hourCycle: 'h23',
  }).format(stamp) + ' UTC' : value;
}

function status(value: string, copy: Copy) {
  return value in copy ? copy[value as keyof Copy] : value.replaceAll('_', ' ');
}

function EvidenceLinks({ ids, state, onSelect, copy }: { ids: string[]; state: Workflow; onSelect: (key: string) => void; copy: Copy }) {
  const renderLink = (key: string) => {
    const index = state.evidence.findIndex(record => record.evidence_id === key);
    return index >= 0 ? <button key={key} type="button" onClick={() => onSelect(key)} title={state.evidence[index].record.label ?? key}>
      <span>E{index + 1}</span>{state.evidence[index].record.label ?? key}
    </button> : <span key={key} className="incident-workflow-missing"><code>{key}</code> · {copy.missing}</span>;
  };
  return <div className="incident-workflow-links">{(ids.length > 4 ? ids.slice(0, 3) : ids).map(renderLink)}
    {ids.length > 4 && <details className="incident-workflow-more-evidence"><summary>{ids.length - 3} {copy.moreEvidence}</summary><div className="incident-workflow-links">{ids.slice(3).map(renderLink)}</div></details>}
  </div>;
}

function EvidenceDetail({ evidence, language, copy, onClose }: { evidence: Evidence; language: Language; copy: Copy; onClose: () => void }) {
  const box = useRef<HTMLElement>(null);
  useEffect(() => { box.current?.focus({ preventScroll: true }); box.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }, [evidence.evidence_id]);
  return <section ref={box} tabIndex={-1} className="incident-workflow-proof" aria-label={`${copy.evidence}: ${evidence.record.label ?? evidence.evidence_id}`}>
    <div className="incident-workflow-proof-heading"><h3>{evidence.record.label ?? evidence.evidence_id}</h3><button type="button" onClick={onClose} aria-label={copy.close}>×</button></div>
    <code className="incident-workflow-proof-id">{evidence.evidence_id}</code>
    {evidence.record.detail && <p>{evidence.record.detail}</p>}
    <dl className="incident-workflow-metadata">
      <div><dt>{copy.source}</dt><dd>{String(evidence.record.source ?? copy.none)}</dd></div>
      <div><dt>{copy.modality}</dt><dd>{evidence.modality}</dd></div>
      <div><dt>{copy.service}</dt><dd>{evidence.service_id ?? copy.none}</dd></div>
      {evidence.unit && <div><dt>{copy.units}</dt><dd>{evidence.unit}</dd></div>}
      <div className="incident-workflow-wide"><dt>{copy.observation}</dt><dd><time dateTime={evidence.observed_window.start}>{time(evidence.observed_window.start, language)}</time> → <time dateTime={evidence.observed_window.end}>{time(evidence.observed_window.end, language)}</time></dd></div>
    </dl>
    <details><summary>{copy.provenance}</summary><pre>{typeof evidence.derivation === 'string' ? evidence.derivation : JSON.stringify(evidence.derivation ?? null, null, 2)}</pre></details>
    {evidence.limitations.length > 0 && <div className="incident-workflow-limits"><h4>{copy.limitations}</h4><ul>{evidence.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul></div>}
    <details><summary>{copy.raw}</summary><pre>{JSON.stringify(evidence.record, null, 2)}</pre></details>
  </section>;
}

function CheckCard({ check, state, copy, language, busy, onRun, onSelect }: { check: Check; state: Workflow; copy: Copy; language: Language; busy: boolean; onRun: () => void; onSelect: (key: string) => void }) {
  return <article className="incident-workflow-check" data-status={check.status}>
    <div className="incident-workflow-check-heading"><h4>{check.title}</h4><span className="incident-workflow-tag" data-status={check.status}>{status(check.status, copy)}</span></div>
    <p>{check.purpose}</p>
    {check.result_summary && <p className="incident-workflow-check-result"><strong>{copy.result}: </strong>{check.result_summary}</p>}
    <EvidenceLinks ids={check.evidence_ids} state={state} onSelect={onSelect} copy={copy} />
    <div className="incident-workflow-check-footer">
      <details><summary>{copy.why}</summary>
        <p>{check.selection_reason ?? check.purpose}</p>
        <dl className="incident-workflow-metadata"><div><dt>{copy.service}</dt><dd>{check.service || copy.none}</dd></div>
          {check.started_at && <div><dt>{copy.execution}</dt><dd>{time(check.started_at, language)}{check.completed_at && <> → {time(check.completed_at, language)}</>}</dd></div>}
        </dl>
        {check.limitations.length > 0 && <ul>{check.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul>}
      </details>
      {check.status === 'proposed' && <button type="button" className="incident-workflow-secondary" disabled={busy} onClick={onRun}>{check.verification_only ? copy.verify : copy.check} ↗</button>}
    </div>
  </article>;
}

export default function IncidentWorkflow({ incidentId, language, compact = false, refreshKey = '', onActivity }: {
  incidentId: string; language: Language; compact?: boolean; refreshKey?: string; onActivity?: () => void;
}) {
  const copy = labels[language];
  const panelId = useId();
  const path = `/incident-workflow/${id(incidentId)}`;
  const [stored, setStored] = useState<Workflow>();
  const state = stored?.incident_id === incidentId ? stored : undefined;
  const [error, setError] = useState('');
  const [operation, setOperation] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [tab, setTab] = useState<Tab>('timeline');
  const [selectedEvidence, setSelectedEvidence] = useState('');
  const [selectedCheck, setSelectedCheck] = useState('');
  const read = useRef<AbortController | null>(null);
  const mutation = useRef<AbortController | null>(null);
  const currentIncident = useRef(incidentId);
  const attempts = useRef(new Map<string, string>());
  const checkSection = useRef<HTMLElement>(null);
  const busy = !!operation || state?.status === 'running';
  const accept = useCallback((next: Workflow) => {
    if (next.incident_id !== incidentId) throw new Error('Unexpected incident in investigation response.');
    if (currentIncident.current !== incidentId) return;
    setStored(previous => previous?.incident_id === incidentId && previous.revision > next.revision ? previous : next);
  }, [incidentId]);
  const refresh = useCallback(async () => {
    if (read.current || mutation.current) return;
    const controller = new AbortController();
    read.current = controller;
    setRefreshing(true);
    try {
      const result = await api<Workflow>(path, { signal: controller.signal });
      if (!controller.signal.aborted) { accept(result); setError(''); }
    } catch (cause) { if (!controller.signal.aborted) setError(errorText(cause)); }
    finally { if (read.current === controller) { read.current = null; if (!controller.signal.aborted) setRefreshing(false); } }
  }, [path, accept]);

  useEffect(() => {
    currentIncident.current = incidentId;
    setStored(undefined); setError(''); setOperation(''); setSelectedEvidence(''); setSelectedCheck('');
    attempts.current.clear();
    void refresh();
    return () => { currentIncident.current = ''; read.current?.abort(); read.current = null; mutation.current?.abort(); mutation.current = null; };
  }, [incidentId, refresh]);
  useEffect(() => { void refresh(); }, [refreshKey, refresh]);
  useEffect(() => {
    if (state?.status !== 'running') return;
    const timer = setInterval(() => void refresh(), 1800);
    return () => clearInterval(timer);
  }, [state?.status, refresh]);

  async function run(action: 'continue' | 'verify' | `checks/${string}/run`) {
    if (!state || busy || mutation.current || (action === 'continue' && !state.enabled)) return;
    read.current?.abort(); read.current = null; setRefreshing(false);
    const controller = new AbortController(); mutation.current = controller;
    const actionKey = `${incidentId}:${action}`;
    if (!attempts.current.has(actionKey)) attempts.current.set(actionKey, crypto.randomUUID());
    setOperation(action); setError('');
    try {
      const result = await api<Workflow | { turn: unknown }>(`${path}/${action}`, {
        method: 'POST', key: attempts.current.get(actionKey), signal: controller.signal,
        body: action === 'continue' ? { question: copy.prompt } : {},
      });
      if (controller.signal.aborted || currentIncident.current !== incidentId) return;
      if ('incident_id' in result) accept(result);
      attempts.current.delete(actionKey);
      onActivity?.();
    } catch (cause) { if (!controller.signal.aborted && currentIncident.current === incidentId) setError(errorText(cause)); }
    finally {
      if (mutation.current === controller) {
        mutation.current = null;
        if (!controller.signal.aborted && currentIncident.current === incidentId) { setOperation(''); void refresh(); }
      }
    }
  }

  const selectEvidence = (key: string) => setSelectedEvidence(key);
  const evidence = state?.evidence.find(item => item.evidence_id === selectedEvidence);
  const evidenceGroups = state ? groupWorkflowEvidence(state.evidence, state.checks, state.recovery.evidence_ids) : { checked: [], initial: [] };
  const eventGroups = groupWorkflowEvents(state?.events ?? []);
  const checks = selectedCheck ? state?.checks.filter(item => item.check_id === selectedCheck) : state?.checks;
  const selectCheck = (checkId: string) => {
    setSelectedCheck(checkId);
    requestAnimationFrame(() => { checkSection.current?.focus({ preventScroll: true }); checkSection.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); });
  };
  const evidenceCards = (records: Evidence[]) => state && <div className="incident-workflow-evidence-list">{records.map(record => {
    const index = state.evidence.findIndex(item => item.evidence_id === record.evidence_id);
    return <button type="button" key={record.evidence_id} aria-expanded={selectedEvidence === record.evidence_id} onClick={() => selectEvidence(record.evidence_id)}>
      <span className="incident-workflow-evidence-number">E{index + 1}</span><span><strong>{record.record.label ?? record.evidence_id}</strong><small>{record.modality}{record.service_id && ` · ${record.service_id}`}</small><span>{record.record.detail}</span></span><span aria-hidden="true">↗</span>
    </button>;
  })}</div>;
  const eventCards = (events: Workflow['events']) => state && <ol className="incident-workflow-events">{events.map(event => <li key={event.id}>
    <div className="incident-workflow-event-time"><time dateTime={event.at}>{time(event.at, language)}</time><span>{event.kind.replaceAll('_', ' ')}</span></div>
    <article><h4>{event.title}</h4>{event.kind === 'analysis_completed' && <span className="incident-workflow-model-label">{copy.modelInterpretation}</span>}<p>{event.summary}</p>
      {event.kind === 'analysis_completed' && <p className="incident-workflow-note">{copy.interpretationNote}</p>}
      <EvidenceLinks ids={event.evidence_ids} state={state} onSelect={selectEvidence} copy={copy} />
      {event.check_id && <button type="button" className="incident-workflow-text-button" onClick={() => selectCheck(event.check_id ?? '')}>{copy.viewCheck} ↓</button>}
    </article>
  </li>)}</ol>;
  const content = state && <>
    <dl className="incident-workflow-overview">
      <div><dt>{copy.impact}</dt><dd>{state.summary.impact}</dd></div>
      <div><dt>{copy.focus}</dt><dd>{state.summary.focus}</dd></div>
      <div><dt>{copy.next}</dt><dd>{state.summary.next_step}</dd></div>
    </dl>
    <p className="incident-workflow-source"><span>{state.summary.source}</span> · {copy.observation}: {time(state.summary.start, language)} → {time(state.summary.end, language)}</p>
    <div className="incident-workflow-tabs" role="tablist" aria-label={copy.title}>
      {(['timeline', 'hypotheses', 'evidence', 'verification'] as const).map(value => <button type="button" key={value}
        role="tab" aria-selected={tab === value} aria-controls={`${panelId}-panel`} id={`${panelId}-${value}`}
        tabIndex={tab === value ? 0 : -1}
        onKeyDown={event => {
          const tabs: Tab[] = ['timeline', 'hypotheses', 'evidence', 'verification'];
          let index = tabs.indexOf(value);
          if (event.key === 'ArrowRight') index = (index + 1) % tabs.length;
          else if (event.key === 'ArrowLeft') index = (index + tabs.length - 1) % tabs.length;
          else if (event.key === 'Home') index = 0;
          else if (event.key === 'End') index = tabs.length - 1;
          else return;
          event.preventDefault(); setTab(tabs[index]); setSelectedCheck(''); document.getElementById(`${panelId}-${tabs[index]}`)?.focus();
        }}
        onClick={() => { setTab(value); setSelectedCheck(''); }}>{copy[value]}{value === 'evidence' && <span>{state.evidence.length}</span>}</button>)}
    </div>
    <div className="incident-workflow-tabpanel" role="tabpanel" id={`${panelId}-panel`} aria-labelledby={`${panelId}-${tab}`}>
      {tab === 'timeline' && <>
        <p className="incident-workflow-note">{copy.timeNote}</p>
        {!state.events.length ? <p className="incident-workflow-empty">{copy.emptyTimeline}</p> : <>
          {eventGroups.earlier.length > 0 && <details className="incident-workflow-earlier"><summary>{copy.earlierActivity} ({eventGroups.earlier.length})</summary>{eventCards(eventGroups.earlier)}</details>}
          {eventGroups.earlier.length > 0 && <h3 className="incident-workflow-latest-heading">{copy.latestActivity}</h3>}
          {eventCards(eventGroups.current)}
        </>}
        {!!state.checks.length && <section className="incident-workflow-checks" ref={checkSection} tabIndex={-1}><div className="incident-workflow-section-heading"><h3>{copy.activity}</h3>{selectedCheck && <button type="button" className="incident-workflow-text-button" onClick={() => setSelectedCheck('')}>{language === 'zh' ? '全部核查' : 'All checks'}</button>}</div>
          <p className="incident-workflow-note">{copy.checkIntro}</p>
          {checks?.map(check => <CheckCard key={check.check_id} check={check} state={state} copy={copy} language={language} busy={busy} onRun={() => void run(workflowCheckAction(check))} onSelect={selectEvidence} />)}
        </section>}
      </>}
      {tab === 'hypotheses' && <>
        <p className="incident-workflow-note">{copy.hypothesisNote}</p>
        {!state.hypotheses.length && <p className="incident-workflow-empty">{copy.emptyHypotheses}</p>}
        {state.hypotheses.map(hypothesis => <article className="incident-workflow-hypothesis" key={hypothesis.hypothesis_id}>
          <div className="incident-workflow-check-heading"><h3>{hypothesis.title}</h3><span className="incident-workflow-tag" data-status="unknown">{copy.unverifiedExplanation}</span></div>
          <p className="incident-workflow-note">{copy.modelAssessment}: {status(hypothesis.model_assessment ?? hypothesis.status, copy)}</p>
          <p>{hypothesis.statement}</p>
          {hypothesis.verification_note && <p className="incident-workflow-note">{hypothesis.verification_note}</p>}
          <h4>{copy.support}</h4>
          {hypothesis.supporting_evidence_ids.length ? <EvidenceLinks ids={hypothesis.supporting_evidence_ids} state={state} onSelect={selectEvidence} copy={copy} /> : <p className="incident-workflow-note">{copy.noSupport}</p>}
          <h4>{copy.contradict}</h4>{hypothesis.contradicting_evidence_ids.length ? <EvidenceLinks ids={hypothesis.contradicting_evidence_ids} state={state} onSelect={selectEvidence} copy={copy} /> : <p className="incident-workflow-note">{copy.noContradict}</p>}
        </article>)}
      </>}
      {tab === 'evidence' && <>
        {!state.evidence.length && <p className="incident-workflow-empty">{copy.emptyEvidence}</p>}
        <h3>{copy.checkedEvidence}</h3>
        {evidenceGroups.checked.length ? evidenceCards(evidenceGroups.checked) : <p className="incident-workflow-note">{copy.noCheckedEvidence}</p>}
        {evidenceGroups.initial.length > 0 && <details className="incident-workflow-initial-evidence"><summary>{copy.initialEvidence} · {evidenceGroups.initial.length}</summary>{evidenceCards(evidenceGroups.initial)}</details>}
      </>}
      {tab === 'verification' && <section className="incident-workflow-recovery">
        <h3>{copy.recovery}</h3><span className="incident-workflow-tag">{status(state.recovery.status, copy)}</span><p>{state.recovery.summary}</p>
        <EvidenceLinks ids={state.recovery.evidence_ids} state={state} onSelect={selectEvidence} copy={copy} />
        <p className="incident-workflow-note">{copy.verifyHint}</p>
        <button type="button" className="incident-workflow-secondary" disabled={busy || !state.checks.some(check => check.verification_only)} onClick={() => void run('verify')}>{operation === 'verify' ? copy.verifying : copy.verify}</button>
        {!state.checks.some(check => check.verification_only) && <p className="incident-workflow-note">{copy.noVerification}</p>}
        <div className="incident-workflow-cause"><h4>{copy.cause}: {status(state.cause.status, copy)}</h4><p>{state.cause.summary}</p></div>
        {!state.recovery.executed_action && <p className="incident-workflow-note">{copy.archiveNote}</p>}
      </section>}
    </div>
    {evidence && <EvidenceDetail evidence={evidence} copy={copy} language={language} onClose={() => setSelectedEvidence('')} />}
  </>;

  return <section className={`incident-workflow${compact ? ' incident-workflow-compact' : ''}`} aria-label={copy.title}>
    <div className="incident-workflow-heading">
      <div><h2>{compact ? copy.compact : copy.title}</h2>{state && <span className="incident-workflow-status" data-status={state.status}>{status(state.status, copy)}</span>}</div>
      <div className="incident-workflow-actions">
        <button type="button" className="incident-workflow-refresh" disabled={!!operation || refreshing} onClick={() => void refresh()} aria-label={copy.refresh} title={copy.refresh}>↻</button>
        <button type="button" className="incident-workflow-primary" disabled={!state?.enabled || busy} onClick={() => void run('continue')}>
          {state?.status === 'running' || operation === 'continue' ? copy.running : state?.events.some(event => event.kind === 'analysis_completed') ? copy.continue : copy.investigate}<span aria-hidden="true">↗</span>
        </button>
      </div>
    </div>
    {error && <p className="incident-workflow-error" role="alert">{error}</p>}
    {!state && !error && <p className="incident-workflow-empty" role="status">{copy.loading}</p>}
    {state && !state.enabled && <p className="incident-workflow-note">{state.disabled_reason ?? copy.disabled}</p>}
    {compact ? state && <details className="incident-workflow-expand"><summary>{copy.detail}</summary>{content}</details> : content}
  </section>;
}

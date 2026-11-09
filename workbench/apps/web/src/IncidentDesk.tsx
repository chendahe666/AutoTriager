import { useCallback, useEffect, useRef, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import Spinner from '@cloudscape-design/components/spinner';
import { api, id } from './api';
import IncidentLifecycle, { lifecycleEvidenceAnchor, showEvidence } from './IncidentLifecycle';
import IncidentEvidenceViews from './IncidentEvidenceViews';
import IncidentEventTimeline from './IncidentEventTimeline';
import type { TopologyEdge } from './IncidentTopology';
import { buildIncidentEvents, type IncidentTimelineSource } from './incidentEvents';
import IncidentAgent from './IncidentAgent';
import IncidentWorkflow from './IncidentWorkflow';
import { IncidentQueue, orderIncidentQueue, type IncidentQueueEntry } from './IncidentQueue';
import MonitoringConsole from './MonitoringConsole';
import type { Language } from './i18n';
import { TriageResults, type Findings, type TriageIncident } from './TriageWorkflow';
import './incidentDesk.css';

type HistoryItem = {
  id: string;
  case_id: string;
  title: string;
  reviewed_at: string;
  summary: string;
};
type HistoryReference = HistoryItem & { reason: string };
type DeskIncident = TriageIncident & { source?: 'live'; triggered_at?: string; resolution?: 'unresolved' | 'resolved' };
type MetricEvidence = {
  service: string;
  metric: string;
  value: number;
  unit: string;
  threshold: number;
  start: string;
  end: string;
  source: string;
};
type ReplayMethod = 'B1' | 'Metric BARO' | 'Multi-source BARO';
type ReplayCandidate = { service: string; indicator?: string; evidence_id: string; evidence_ids?: string[]; score?: number };
type AnalysisWindow = { reference_start: string; reference_end?: string; incident_start: string; incident_end: string };
type DeskEvidence = Findings['evidence'][number] & { observed_at?: string; kind?: 'metric' | 'log' | 'trace' };
type BaroReplay = {
  method: ReplayMethod;
  primary_case_id?: string;
  methods?: { case_id: string; label: ReplayMethod }[];
  source_commit?: string;
  model_id?: string;
  input_window: AnalysisWindow;
  ranking: ReplayCandidate[];
  analyzed_at: string;
};
type LiveBaro = {
  status: 'waiting' | 'insufficient' | 'completed';
  method: 'Metric BARO';
  source_commit?: string;
  input_window?: AnalysisWindow;
  ranking?: ReplayCandidate[];
  evidence?: DeskEvidence[];
  reference_samples?: number;
  incident_samples?: number;
  message?: string;
  analyzed_at?: string;
};
type DeskFindings = Findings & DeskIncident & IncidentTimelineSource & {
  evidence: DeskEvidence[];
  metric_evidence?: MetricEvidence;
  baro?: BaroReplay;
  live_baro?: LiveBaro;
  topology_edges?: TopologyEdge[];
};
type IncidentResponse = { case: DeskFindings; exact?: HistoryItem | null; similar?: HistoryReference[] };
type HistoryResponse = { item: HistoryItem; case: DeskFindings; similar: HistoryReference[] };

const translations = {
  en: {
    title: 'Incidents',
    monitoring: 'Monitoring',
    alerts: 'Incident queue',
    history: 'History',
    timeline: 'Investigation timeline',
    evidence: 'Evidence',
    next: 'Next steps',
    liveAlert: 'Live alert',
    triggeredAt: 'Triggered at',
    recordedWindow: 'Recorded window',
    savedAt: 'Saved',
    chooseAlert: 'Select an incident to view its investigation.',
    chooseHistory: 'Select a saved investigation to reopen it.',
    alertDetails: 'Alert details',
    historyDetails: 'Saved investigation details',
    emptyAlerts: 'No recorded alerts are available.',
    emptyHistory: 'No investigations have been saved. Open an alert and select Save to history.',
    emptyHistoryReadOnly: 'No saved investigations are available in this view.',
    loading: 'Loading records',
    error: 'Could not load records.',
    retry: 'Try again',
    save: 'Save to history',
    saveError: 'Could not save this investigation. Try again.',
    openSaved: 'View saved record',
    allEvidence: 'All recorded evidence',
    noEvidence: 'This case has no evidence records.',
    source: 'View dataset source',
    similar: 'Similar past investigations',
    similarNote: 'Use these records to guide your review. Check the current evidence before applying a past explanation.',
    referencesLanguage: '',
    replay: 'Recorded RCAEval replay',
    ranking: 'Ranked candidates',
    replayCompleted: 'Analysis recorded',
    referenceWindow: 'Reference window',
    diagnosisWindow: 'Incident window',
    sourceRevision: 'Method revision',
    model: 'Model',
    rankNote: 'Services appear in the order returned by the saved analysis. A ranked service is a candidate for investigation.',
    b1RankNote: 'B1 used fixed telemetry summaries. Its citations open the observations referenced by the saved answer.',
    b1Count: (count: number) => `${count} ${count === 1 ? 'candidate' : 'candidates'} returned by B1`,
    metricRankNote: 'Metrics determine the ranking. Logs and traces provide investigation context.',
    multiRankNote: 'The method can rank indicators across available sources. The evidence list also includes investigation context.',
    observationNote: 'The observation timestamps mark when the archived telemetry was recorded. The analysis timestamp marks when the selected method ran.',
    observedCall: 'Observed service call',
    observedLog: 'Archived log observation',
    openEvidence: 'View evidence',
    closeEvidence: 'Close evidence',
    rawEvidence: 'Archived source record',
    openRaw: 'Open raw record',
    rankUnavailable: 'No candidates were returned.',
    analysisMethod: 'Analysis method',
    evaluation: 'View benchmark evaluation',
    nextCheck: 'Next check',
    liveRanking: 'Live metric analysis',
    liveBaroNote: 'Metric BARO ranks observed metric changes to prioritize investigation. Recovery actions require operator approval.',
    liveBaroStatus: { waiting: 'Collecting metric samples', insufficient: 'Insufficient metric evidence', completed: 'Ranking available' },
    liveMetricRecord: 'Captured metric evidence',
    samples: 'samples',
    methodScore: 'Score',
  },
  zh: {
    title: '事故',
    monitoring: '监控台',
    alerts: '事故列表',
    history: '历史记录',
    timeline: '调查时间线',
    evidence: '证据',
    next: '下一步操作',
    liveAlert: '实时报警',
    triggeredAt: '触发时间',
    recordedWindow: '记录时间窗',
    savedAt: '保存时间',
    chooseAlert: '选择一条事故，查看调查详情。',
    chooseHistory: '选择一条历史记录，打开已保存的调查。',
    alertDetails: '报警详情',
    historyDetails: '已保存的调查详情',
    emptyAlerts: '暂无已记录的报警。',
    emptyHistory: '暂无历史记录。打开一条报警后，可选择「保存到历史」。',
    emptyHistoryReadOnly: '当前视图暂无已保存的调查。',
    loading: '正在加载记录',
    error: '无法加载记录。',
    retry: '重试',
    save: '保存到历史',
    saveError: '无法保存这次调查，请重试。',
    openSaved: '查看已保存的记录',
    allEvidence: '全部证据记录',
    noEvidence: '此案例暂无证据记录。',
    source: '查看数据来源',
    similar: '相似历史调查',
    similarNote: '这些记录可以帮助确定排查方向。沿用历史解释前，请核对本次事故的证据。',
    referencesLanguage: '证据和匹配理由保留英文。',
    replay: 'RCAEval 归档事故回放',
    ranking: '候选服务排序',
    replayCompleted: '分析记录时间',
    referenceWindow: '参考时间窗',
    diagnosisWindow: '事故时间窗',
    sourceRevision: '方法版本',
    model: '模型',
    rankNote: '按已保存结果的顺序展示。排名用于确定值得进一步排查的候选服务。',
    b1RankNote: 'B1 使用固定的遥测摘要。点击证据可查看原答案引用的观测。',
    b1Count: (count: number) => `B1 返回了 ${count} 个候选服务`,
    metricRankNote: '指标决定排名。日志和调用链用于提供排查上下文。',
    multiRankNote: '此方法可使用多种可用信号为指标排序。证据列表也包含排查上下文。',
    observationNote: '观测时间显示遥测数据的原始记录时间。分析时间显示所选方法的运行时间。',
    observedCall: '观测到的服务调用',
    observedLog: '归档日志观测',
    openEvidence: '查看证据',
    closeEvidence: '关闭证据',
    rawEvidence: '归档来源记录',
    openRaw: '打开原始记录',
    rankUnavailable: '此次分析没有返回候选服务。',
    analysisMethod: '分析方法',
    evaluation: '查看基准评估',
    nextCheck: '下一步检查',
    liveRanking: '实时指标分析',
    liveBaroNote: 'Metric BARO 按观测到的指标变化为排查排序。恢复操作需要操作员批准。',
    liveBaroStatus: { waiting: '正在采集指标样本', insufficient: '指标证据不足', completed: '排名已生成' },
    liveMetricRecord: '已采集的指标证据',
    samples: '个样本',
    methodScore: '得分',
  },
} as const;

type Copy = typeof translations[Language];
type Remote<T> = { path: string; status: 'ready'; value: T } | { path: string; status: 'error' };

function useRecords<T>(path: string | null, pollMilliseconds?: number, shouldPoll?: (value: T) => boolean) {
  const [result, setResult] = useState<Remote<T>>();
  const [attempt, setAttempt] = useState(0);
  const refresh = useCallback(() => setAttempt(value => value + 1), []);
  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function load(route: string, refresh = false) {
      let again = !shouldPoll || refresh;
      try {
        const value = await api<T>(route, { signal: controller.signal });
        if (!controller.signal.aborted) setResult({ path: route, status: 'ready', value });
        again = shouldPoll?.(value) ?? true;
      } catch {
        if (!controller.signal.aborted && !(refresh && shouldPoll)) setResult({ path: route, status: 'error' });
      }
      if (pollMilliseconds && again && !controller.signal.aborted) timer = setTimeout(() => void load(route, true), pollMilliseconds);
    }
    void load(path);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [path, attempt, pollMilliseconds, shouldPoll]);

  return {
    result: result?.path === path ? result : undefined,
    refresh,
    retry: () => {
      setResult(undefined);
      refresh();
    },
  };
}

const collectingLiveBaro = (result: IncidentResponse) => result.case.source === 'live' && result.case.live_baro?.status === 'waiting';

function Time({ value, language }: { value: string; language: Language }) {
  const label = new Intl.DateTimeFormat(language === 'zh' ? 'zh-CN' : 'en', {
    dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC', hourCycle: 'h23',
  }).format(new Date(value));
  return <time dateTime={value}>{label} UTC</time>;
}

function Loading({ copy }: { copy: Copy }) {
  return <p className="incident-desk-state" role="status"><Spinner /> {copy.loading}</p>;
}

function LoadError({ copy, retry }: { copy: Copy; retry: () => void }) {
  return <Alert type="error" action={<Button onClick={retry}>{copy.retry}</Button>}>{copy.error}</Alert>;
}

const methodLinkId = (caseId: string) => `replay-method-${id(caseId)}`;
const incidentTitleId = (caseId: string) => `incident-detail-title-${id(caseId)}`;

function focusIncident(caseId: string) {
  const title = document.getElementById(incidentTitleId(caseId));
  title?.focus({ preventScroll: true });
  title?.scrollIntoView({ block: 'start' });
}

function rankingEvidence(candidate: ReplayCandidate) {
  return [...new Set(candidate.evidence_ids?.length ? candidate.evidence_ids : [candidate.evidence_id])];
}

function RankedServices({ ranking, language, openEvidence }: {
  ranking: ReplayCandidate[]; language: Language; openEvidence: (evidenceId: string) => void;
}) {
  const t = translations[language];
  const format = new Intl.NumberFormat(language === 'zh' ? 'zh-CN' : 'en', { maximumSignificantDigits: 5 });
  if (!ranking.length) return <p>{t.rankUnavailable}</p>;
  return <ol className="baro-ranking-list">
    {ranking.slice(0, 3).map((candidate, index) => <li key={`${candidate.service}-${index}`}>
      <span className="baro-rank-number" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
      <div className="baro-rank-label"><strong>{candidate.service}</strong>{candidate.indicator && <code>{candidate.indicator}</code>}</div>
      {typeof candidate.score === 'number' && Number.isFinite(candidate.score) && <span className="baro-rank-score">{t.methodScore} {format.format(candidate.score)}</span>}
      <div className="baro-rank-evidence">{rankingEvidence(candidate).map((evidenceId, evidenceIndex, references) => {
        const number = references.length > 1 ? ` ${evidenceIndex + 1}` : '';
        return <button type="button" key={evidenceId} className="incident-viz-evidence" aria-label={`${t.openEvidence}${number} · ${candidate.service}`}
          onClick={() => openEvidence(evidenceId)}>{t.openEvidence}{number}<span aria-hidden="true"> ↗</span></button>;
      })}</div>
    </li>)}
  </ol>;
}

function BaroRanking({ replay, caseId, sectionId, language, openEvidence, onMethodNavigate, nextCheck }: {
  replay: BaroReplay; caseId: string; sectionId: string; language: Language; openEvidence: (evidenceId: string) => void;
  onMethodNavigate?: (caseId: string) => void; nextCheck?: string;
}) {
  const t = translations[language];
  const methodNote = { 'B1': t.b1RankNote, 'Metric BARO': t.metricRankNote, 'Multi-source BARO': t.multiRankNote }[replay.method];
  return <section id={sectionId} tabIndex={-1} className="baro-ranking" aria-labelledby={`${sectionId}-heading`}>
    <header>
      <h2 id={`${sectionId}-heading`}>{t.ranking}</h2>
      {replay.methods?.length ? <nav className="baro-ranking-methods" aria-label={t.analysisMethod}>
        {replay.methods.map(method => <a key={method.case_id} id={methodLinkId(method.case_id)} className="baro-method-link"
          href={`#/errors/${id(method.case_id)}`} aria-current={method.case_id === caseId ? 'page' : undefined}
          onClick={event => {
            if (method.case_id !== caseId && event.button === 0 && !event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey) {
              onMethodNavigate?.(method.case_id);
            }
          }}>{method.label}</a>)}
      </nav> : <span className="baro-replay-badge">{replay.method}</span>}
    </header>
    <p>{t.rankNote} {methodNote}</p>
    {replay.method === 'B1' && <p className="baro-ranking-count">{t.b1Count(replay.ranking.length)}</p>}
    <RankedServices ranking={replay.ranking} language={language} openEvidence={openEvidence} />
    {nextCheck && <div className="baro-ranking-next"><h3>{t.nextCheck}</h3><p>{nextCheck}</p></div>}
    <a className="baro-evaluation-link" href="#/evaluation">{t.evaluation} <span aria-hidden="true">↗</span></a>
  </section>;
}

function LiveBaroRanking({ analysis, sectionId, language, openEvidence }: {
  analysis: LiveBaro; sectionId: string; language: Language; openEvidence: (evidenceId: string) => void;
}) {
  const t = translations[language];
  const window = analysis.input_window;
  return <section id={sectionId} tabIndex={-1} className="baro-ranking" aria-labelledby={`${sectionId}-heading`}>
    <header>
      <h2 id={`${sectionId}-heading`}>{t.liveRanking}</h2>
      <span className="baro-replay-badge">{analysis.method}</span>
    </header>
    <p>{t.liveBaroNote}</p>
    <p className="baro-live-status" data-status={analysis.status} role="status">{t.liveBaroStatus[analysis.status]}</p>
    {analysis.message && <p>{analysis.message}</p>}
    {analysis.status === 'completed' && <RankedServices ranking={analysis.ranking ?? []} language={language} openEvidence={openEvidence} />}
    {(window || analysis.analyzed_at || analysis.source_commit) && <dl className="baro-live-window">
      {window && <>
        <div><dt>{t.referenceWindow}</dt><dd><Time value={window.reference_start} language={language} /> → <Time value={window.reference_end ?? window.incident_start} language={language} />
          {typeof analysis.reference_samples === 'number' && <> · {analysis.reference_samples} {t.samples}</>}</dd></div>
        <div><dt>{t.diagnosisWindow}</dt><dd><Time value={window.incident_start} language={language} /> → <Time value={window.incident_end} language={language} />
          {typeof analysis.incident_samples === 'number' && <> · {analysis.incident_samples} {t.samples}</>}</dd></div>
      </>}
      {analysis.analyzed_at && <div><dt>{t.replayCompleted}</dt><dd><Time value={analysis.analyzed_at} language={language} /></dd></div>}
      {analysis.source_commit && <div><dt>{t.sourceRevision}</dt><dd><code title={analysis.source_commit}>{analysis.source_commit.slice(0, 12)}</code></dd></div>}
    </dl>}
  </section>;
}

function BaroEvidenceDrawer({ caseId, evidence, language, close, live = false }: {
  caseId: string; evidence: DeskEvidence; language: Language; close: () => void; live?: boolean;
}) {
  const copy = translations[language];
  const dialog = useRef<HTMLDialogElement>(null);
  const path = `/baro-evidence/${id(caseId)}/${id(evidence.id)}`;
  const { result, retry } = useRecords<unknown>(path);

  useEffect(() => {
    const element = dialog.current;
    const trigger = document.activeElement;
    element?.showModal();
    return () => {
      element?.close();
      if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus({ preventScroll: true });
    };
  }, []);

  return <dialog ref={dialog} className="evidence-drawer baro-evidence-drawer" aria-labelledby="baro-evidence-title"
    onCancel={event => { event.preventDefault(); close(); }}>
    <div className="drawer-heading">
      <div><div className="eyebrow">{copy.evidence} · {evidence.id}</div><h2 id="baro-evidence-title">{evidence.title}</h2></div>
      <Button iconName="close" variant="icon" ariaLabel={copy.closeEvidence} onClick={close} />
    </div>
    <div className="drawer-body">
      <p>{evidence.detail}</p>
      <h3>{live ? copy.liveMetricRecord : copy.rawEvidence}</h3>
      {!result ? <Loading copy={copy} /> : result.status === 'error' ? <LoadError copy={copy} retry={retry} /> :
        <pre className="record-json">{JSON.stringify(result.value, null, 2)}</pre>}
      <a className="incident-viz-evidence" href={`/api/v1${path}`} target="_blank" rel="noreferrer">{copy.openRaw} <span aria-hidden="true">↗</span></a>
    </div>
  </dialog>;
}

function CaseDetails({ findings, language, record, showLifecycle = false, onMethodNavigate, onVerified }: {
  findings: DeskFindings; language: Language; record?: HistoryItem; showLifecycle?: boolean; onMethodNavigate?: (caseId: string) => void;
  onVerified?: () => void;
}) {
  const copy = translations[language];
  const live = findings.source === 'live';
  const replay = findings.baro;
  const liveBaro = live ? findings.live_baro : undefined;
  const liveEvidence = new Map((liveBaro?.evidence ?? []).map(evidence => [evidence.id, evidence]));
  const evidenceRecords = [...findings.evidence, ...[...liveEvidence.values()].filter(evidence => !findings.evidence.some(row => row.id === evidence.id))];
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string>();
  const selectedEvidence = evidenceRecords.find(evidence => evidence.id === selectedEvidenceId);
  const hasRawEvidence = (evidenceId: string) => Boolean(replay) || liveEvidence.has(evidenceId);
  const openEvidence = (evidenceId: string) => hasRawEvidence(evidenceId) ? setSelectedEvidenceId(evidenceId) : showEvidence(findings.id, evidenceId);
  const rankingId = `incident-ranking-${findings.id}`;
  const timelineId = `incident-timeline-${findings.id}`;
  const evidenceId = `incident-records-${findings.id}`;
  const nextId = `incident-next-${findings.id}`;
  const moveTo = (sectionId: string, fallback?: string) => {
    const target = document.getElementById(sectionId) ?? (fallback ? document.getElementById(fallback) : null);
    if (target instanceof HTMLDetailsElement) target.open = true;
    target?.focus({ preventScroll: true });
    target?.scrollIntoView({ block: 'start' });
  };
  const recordedEvents = buildIncidentEvents(findings, language);
  if (replay) {
    for (const edge of findings.topology_edges ?? []) {
      recordedEvents.push({ id: `baro-trace-${edge.id}`, at: edge.at, kind: 'evidence', title: copy.observedCall,
        detail: `${edge.from} → ${edge.to}`, evidenceIds: [edge.id] });
    }
    for (const record of findings.evidence) {
      if (record.kind === 'log' && record.observed_at) {
        recordedEvents.push({ id: `baro-log-${record.id}`, at: record.observed_at, kind: 'evidence', title: copy.observedLog,
          detail: record.title, evidenceIds: [record.id] });
      }
    }
    recordedEvents.push({ id: 'baro-analysis-completed', at: replay.analyzed_at, kind: 'analysis', title: copy.replayCompleted,
      detail: replay.method, evidenceIds: replay.ranking.slice(0, 3).flatMap(rankingEvidence) });
  }
  const triageResults = <div id={nextId} tabIndex={-1}><TriageResults findings={findings} language={language} /></div>;
  return <>
    <header className="incident-desk-heading">
      <p className="incident-desk-kicker">{record ? copy.historyDetails : copy.title} <span> / </span> {replay?.primary_case_id ?? findings.id}</p>
      <h1 id={incidentTitleId(findings.id)} tabIndex={-1}>{findings.title}</h1>
      {replay && <span className="baro-replay-badge">{copy.replay}</span>}
      {live && findings.triggered_at && <p>{copy.liveAlert} · {copy.triggeredAt} · <Time value={findings.triggered_at} language={language} /></p>}
      {replay ? <>
        <p>{copy.referenceWindow} · <Time value={replay.input_window.reference_start} language={language} /> → <Time value={replay.input_window.incident_start} language={language} /></p>
        <p>{copy.diagnosisWindow} · <Time value={replay.input_window.incident_start} language={language} /> → <Time value={replay.input_window.incident_end} language={language} /></p>
        <p>{copy.replayCompleted} · <Time value={replay.analyzed_at} language={language} />
          {replay.source_commit && <> · {copy.sourceRevision} <code title={replay.source_commit}>{replay.source_commit.slice(0, 12)}</code></>}
          {replay.model_id && <> · {copy.model} <code>{replay.model_id}</code></>}
        </p>
      </> : <p>{copy.recordedWindow} · <Time value={findings.start} language={language} /> → <Time value={findings.end} language={language} /></p>}
      {record && <p>{copy.savedAt} · <Time value={record.reviewed_at} language={language} /></p>}
    </header>
    {(live || replay) && <nav className="incident-desk-sections" aria-label={copy.alertDetails}>
      {replay && <button type="button" onClick={() => moveTo(rankingId)}>{copy.ranking}</button>}
      {liveBaro && <button type="button" onClick={() => moveTo(rankingId)}>{copy.liveRanking}</button>}
      <button type="button" onClick={() => moveTo(timelineId)}>{copy.timeline}</button>
      <button type="button" onClick={() => moveTo(evidenceId)}>{copy.evidence} <span>{evidenceRecords.length}</span></button>
      {live && <button type="button" onClick={() => moveTo(nextId, timelineId)}>{copy.next}</button>}
    </nav>}
    {!live && !replay && <IncidentWorkflow key={findings.id} incidentId={findings.id} language={language} />}
    {replay && <BaroRanking replay={replay} caseId={findings.id} sectionId={rankingId} language={language} openEvidence={openEvidence}
      onMethodNavigate={onMethodNavigate} nextCheck={findings.candidates[0]?.next_check} />}
    {liveBaro && <LiveBaroRanking analysis={liveBaro} sectionId={rankingId} language={language} openEvidence={openEvidence} />}
    <IncidentEvidenceViews incident={findings} language={language} onEvidence={replay ? openEvidence : undefined} showMetricSummaryGap={!replay}>
    <section className="incident-desk-investigation" id={timelineId} tabIndex={-1} aria-label={copy.timeline}>
      <h2>{live || replay ? copy.timeline : language === 'zh' ? '原始事故上下文' : 'Original incident context'}</h2>
      {replay ? <>
        <p className="baro-timeline-note">{copy.observationNote}</p>
        <IncidentEventTimeline events={recordedEvents} language={language} onEvidence={openEvidence} />
      </> : live && showLifecycle ? <IncidentLifecycle caseId={findings.id} incident={findings} language={language} fallback={triageResults} onVerified={onVerified} /> : <>
        <details className="triage-disclosure">
          <summary>{language === 'zh' ? '初始事故记录与已有分析' : 'Initial incident record and existing analysis'}</summary>
          <IncidentEventTimeline events={recordedEvents} language={language} onEvidence={openEvidence} />
          {triageResults}
        </details>
      </>}
    </section>
    </IncidentEvidenceViews>
    <details id={evidenceId} tabIndex={-1} className="incident-desk-evidence triage-disclosure" open={live || replay ? true : undefined}>
      <summary>{copy.allEvidence} <span className="triage-count">{evidenceRecords.length}</span></summary>
      {evidenceRecords.length === 0 && <p>{copy.noEvidence}</p>}
      <ul className="triage-records">{evidenceRecords.map(record => <li key={record.id} id={lifecycleEvidenceAnchor(findings.id, record.id)} tabIndex={-1}>
        <h4>{record.title}</h4>
        <p>{record.detail}</p>
        {hasRawEvidence(record.id) && <button type="button" className="incident-viz-evidence" onClick={() => openEvidence(record.id)}>{copy.openEvidence}<span aria-hidden="true"> ↗</span></button>}
        {record.source_url?.startsWith('https://') && <a href={record.source_url} target="_blank" rel="noreferrer">{copy.source} ↗</a>}
      </li>)}</ul>
    </details>
    {selectedEvidence && <BaroEvidenceDrawer key={selectedEvidence.id} caseId={findings.id} evidence={selectedEvidence} language={language} live={live} close={() => setSelectedEvidenceId(undefined)} />}
  </>;
}

function SimilarHistory({ items, language, onOpen }: {
  items?: HistoryReference[]; language: Language; onOpen: (recordId: string) => void;
}) {
  if (!items?.length) return null;
  const copy = translations[language];
  return <details className="incident-desk-evidence triage-disclosure">
    <summary>{copy.similar} <span className="triage-count">{items.length}</span></summary>
    <p>{copy.similarNote}</p>
    {copy.referencesLanguage && <p>{copy.referencesLanguage}</p>}
    <ul className="triage-records">{items.map(item => <li key={item.id}>
      <h4>{item.title}</h4>
      <p>{item.reason}</p>
      <Button variant="inline-link" onClick={() => onOpen(item.id)}>{copy.openSaved}</Button>
    </li>)}</ul>
  </details>;
}

function AlertDetail({ caseId, canSave, language, onOpenHistory, result, retry, onMethodNavigate, onVerified }: {
  caseId: string; canSave: boolean; language: Language; onOpenHistory: (recordId: string) => void;
  result?: Remote<IncidentResponse>; retry: () => void; onMethodNavigate: (caseId: string) => void; onVerified: () => void;
}) {
  const copy = translations[language];
  const [saved, setSaved] = useState<HistoryItem>();
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(false);

  async function save() {
    if (saving) return;
    setSaving(true);
    setSaveError(false);
    try {
      const response = await api<HistoryResponse>('/incident-history', { method: 'POST', body: { case_id: caseId } });
      if (response.item.case_id !== caseId) throw new Error('Unexpected history record');
      setSaved(response.item);
    } catch {
      setSaveError(true);
    } finally {
      setSaving(false);
    }
  }

  if (!result) return <Loading copy={copy} />;
  if (result.status === 'error' || result.value.case.id !== caseId) return <LoadError copy={copy} retry={retry} />;
  const existing = saved ?? result.value.exact;
  return <>
    <CaseDetails findings={result.value.case} language={language} showLifecycle={canSave} onMethodNavigate={onMethodNavigate} onVerified={onVerified} />
    {saveError && <Alert type="error">{copy.saveError}</Alert>}
    {existing ? <div className="incident-desk-actions">
      <Button onClick={() => onOpenHistory(existing.id)}>{copy.openSaved}</Button>
    </div> : canSave && !result.value.case.baro && <div className="incident-desk-actions">
      <Button loading={saving} onClick={() => void save()}>{copy.save}</Button>
    </div>}
    <SimilarHistory items={result.value.similar} language={language} onOpen={onOpenHistory} />
  </>;
}

function HistoryDetail({ recordId, language, onOpenHistory }: {
  recordId: string; language: Language; onOpenHistory: (recordId: string) => void;
}) {
  const copy = translations[language];
  const { result, retry } = useRecords<HistoryResponse>(`/incident-history/${id(recordId)}`);
  if (!result) return <Loading copy={copy} />;
  if (result.status === 'error' || result.value.item.id !== recordId || result.value.case.id !== result.value.item.case_id) {
    return <LoadError copy={copy} retry={retry} />;
  }
  return <>
    <CaseDetails findings={result.value.case} language={language} record={result.value.item} />
    <SimilarHistory items={result.value.similar} language={language} onOpen={onOpenHistory} />
  </>;
}

export default function IncidentDesk({ language, canSave = true, initialCaseId, initialHistoryId, historyView = false, agentView = false }: {
  language: Language; canSave?: boolean; initialCaseId?: string; initialHistoryId?: string; historyView?: boolean; agentView?: boolean;
}) {
  const copy = translations[language];
  const [defaultCaseId, setDefaultCaseId] = useState<string>();
  const [lastViewedCaseId, setLastViewedCaseId] = useState<string>();
  const [lastAttachedCaseId, setLastAttachedCaseId] = useState<string>();
  const [dropTarget, setDropTarget] = useState<'tab' | 'panel' | null>(null);
  const pendingMethodFocus = useRef<string | undefined>(undefined);
  const pendingAlertFocus = useRef<string | undefined>(undefined);
  const monitoring = useRef<HTMLDetailsElement>(null);
  const showEmbeddedMonitoring = !historyView && !agentView;
  const alerts = useRecords<{ cases: DeskIncident[] }>(historyView ? null : '/triage', 15_000);
  const history = useRecords<{ items: HistoryItem[] }>(historyView ? '/incident-history' : null);
  const historyPath = (recordId: string) => `#/errors/history/${id(recordId)}`;
  const openHistory = (recordId: string) => { location.hash = historyPath(recordId); };
  const current = historyView ? history : alerts;
  const entries: IncidentQueueEntry[] = historyView
    ? (history.result?.status === 'ready' ? history.result.value.items : []).map(item => ({
      id: item.id, agentCaseId: item.case_id, title: item.title, time: item.reviewed_at, href: historyPath(item.id), status: 'recorded',
    }))
    : (alerts.result?.status === 'ready' ? alerts.result.value.cases : []).map(item => ({
      id: item.id, agentCaseId: item.id, title: item.title, time: item.triggered_at ?? item.start, href: `#/errors/${id(item.id)}`,
      status: item.source === 'live' ? (item.resolution ?? 'recorded') : 'recorded',
    }));
  const firstId = orderIncidentQueue(entries)[0]?.id;
  const remembered = entries.some(entry => entry.id === defaultCaseId) ? defaultCaseId : undefined;
  const selectedId = historyView ? (initialHistoryId ?? firstId) : (initialCaseId ?? remembered ?? firstId);
  const detail = useRecords<IncidentResponse>(selectedId && !historyView && !agentView ? `/triage/${id(selectedId)}` : null, 2_000, collectingLiveBaro);
  const loadedCase = detail.result?.status === 'ready' && detail.result.value.case.id === selectedId ? detail.result.value.case : undefined;
  const queueSelectionId = loadedCase?.baro?.primary_case_id ?? selectedId;
  const currentCaseId = (entries.find(entry => entry.agentCaseId === lastViewedCaseId) ?? entries.find(entry => entry.id === queueSelectionId))?.agentCaseId;
  const agentPath = (caseId?: string) => `#/errors/agent${caseId ? `/${id(caseId)}` : ''}`;
  const attach = (caseId: string) => { location.hash = agentPath(caseId); };

  const openMonitoredIncident = (caseId: string) => {
    if (loadedCase?.id === caseId) {
      focusIncident(caseId);
    } else {
      pendingAlertFocus.current = caseId;
      location.hash = `#/errors/${id(caseId)}`;
    }
  };

  const focusMonitoring = () => {
    const panel = monitoring.current;
    if (!panel) return;
    panel.open = true;
    panel.querySelector('summary')?.focus({ preventScroll: true });
    panel.scrollIntoView({ block: 'start' });
  };

  useEffect(() => {
    if (!historyView && !agentView && !initialCaseId && firstId) setDefaultCaseId(previous => previous ?? firstId);
    if (!historyView && !agentView && queueSelectionId) setLastViewedCaseId(queueSelectionId);
    if (agentView && initialCaseId) setLastAttachedCaseId(initialCaseId);
  }, [historyView, agentView, initialCaseId, firstId, queueSelectionId]);

  useEffect(() => {
    if (pendingAlertFocus.current !== selectedId) pendingAlertFocus.current = undefined;
    else if (loadedCase && pendingAlertFocus.current) {
      focusIncident(loadedCase.id);
      pendingAlertFocus.current = undefined;
    }
  }, [loadedCase, selectedId]);

  useEffect(() => {
    if (!pendingMethodFocus.current) return;
    if (pendingMethodFocus.current !== selectedId) pendingMethodFocus.current = undefined;
    else if (loadedCase) {
      document.getElementById(methodLinkId(loadedCase.id))?.focus();
      pendingMethodFocus.current = undefined;
    }
  }, [loadedCase, selectedId]);

  return <div className="incident-desk">
    <aside className="incident-desk-sidebar" aria-label={historyView ? copy.history : copy.alerts}>
      {showEmbeddedMonitoring
        ? <button type="button" className="incident-desk-monitoring" aria-controls="incident-workspace-monitoring" onClick={focusMonitoring}>{copy.monitoring}</button>
        : <a className="incident-desk-monitoring" href="#/monitoring">‹ {copy.monitoring}</a>}
      <div className="incident-desk-sidebar-title">{copy.title}</div>
      <nav className="incident-desk-views" aria-label={copy.title}>
        <a href="#/errors" aria-current={!historyView && !agentView ? 'page' : undefined}>{copy.alerts}</a>
        <a href="#/errors/history" aria-current={historyView ? 'page' : undefined}>{copy.history}</a>
        <a href={agentPath(lastAttachedCaseId)} aria-current={agentView ? 'page' : undefined} data-agent-target="tab"
          data-agent-drop={dropTarget === 'tab' || undefined}>Agent</a>
      </nav>
      {!current.result ? <Loading copy={copy} /> : current.result.status === 'error' ? <LoadError copy={copy} retry={current.retry} />
        : entries.length === 0 ? <p className="incident-desk-state">{historyView ? (canSave ? copy.emptyHistory : copy.emptyHistoryReadOnly) : copy.emptyAlerts}</p>
        : <IncidentQueue entries={entries} selectedId={agentView ? initialCaseId : queueSelectionId} language={language} onAttach={attach} onDragTarget={setDropTarget} />}
    </aside>
    <section className="incident-desk-detail" aria-label={agentView ? 'Agent' : historyView ? copy.historyDetails : copy.alertDetails}
      data-agent-drop={dropTarget === 'panel' || undefined} data-agent-target={agentView ? 'panel' : undefined}>
      {showEmbeddedMonitoring && <details id="incident-workspace-monitoring" className="incident-desk-monitoring-panel" ref={monitoring} open>
        <summary>{copy.monitoring}</summary>
        <MonitoringConsole language={language} embedded selectedIncidentId={selectedId} onOpenIncident={openMonitoredIncident} />
      </details>}
      {agentView ? <IncidentAgent key={initialCaseId ?? 'unattached'} incidentId={initialCaseId} language={language}
        attachCurrent={currentCaseId ? () => attach(currentCaseId) : undefined} />
        : !selectedId ? <p className="incident-desk-placeholder">{historyView ? copy.chooseHistory : copy.chooseAlert}</p>
        : historyView ? <HistoryDetail key={selectedId} recordId={selectedId} language={language} onOpenHistory={openHistory} />
        : <AlertDetail key={selectedId} caseId={selectedId} canSave={canSave} language={language} onOpenHistory={openHistory}
          result={detail.result} retry={detail.retry} onVerified={alerts.refresh} onMethodNavigate={caseId => { pendingMethodFocus.current = caseId; }} />}
    </section>
  </div>;
}

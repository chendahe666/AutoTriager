import { useCallback, useEffect, useRef, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import Spinner from '@cloudscape-design/components/spinner';
import { api, errorText, id } from './api';
import type { Language } from './i18n';
import IncidentWorkflow from './IncidentWorkflow';
import './incidentAgent.css';

type AgentTurn = {
  turn_id: string;
  incident_id: string;
  question: string;
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED';
  created_at: string;
  answer: {
    text: string;
    citations: { evidence_id: string; label: string; detail: string }[];
    limitations: string[];
    next_checks: string[];
  } | null;
  error: { code: string; message: string } | null;
};

type AgentSession = {
  incident_id: string;
  context: { title: string; source: string; start: string; end: string; evidence_count: number; available_modalities: string[] };
  enabled: boolean;
  disabled_reason: string | null;
  turns: AgentTurn[];
};

const labels = {
  en: {
    title: 'Incident investigation', description: 'Ask about evidence, compare affected services, and identify the next check.',
    drop: 'Drop an incident here', dropHint: 'Drag an incident from the queue, or use its Attach to Agent button.',
    attach: 'Attach current incident', selected: 'Incident selected', attached: 'Incident attached', open: 'Open incident', loading: 'Loading incident context',
    refresh: 'Refresh', unavailable: 'The incident Agent is unavailable.', evidence: 'Evidence', records: 'records',
    modalities: 'Available signals', none: 'None reported', start: 'Ask your first question about this incident.',
    question: 'Question for Agent', placeholder: 'Which service should I check first, and what evidence supports it?',
    send: 'Send', submitting: 'Sending', you: 'You', waiting: 'Reviewing incident evidence', failed: 'Agent could not answer this question.',
    limits: 'Evidence gaps', next: 'Suggested checks', metrics: 'Metrics', logs: 'Logs', traces: 'Traces',
    sourceLive: 'Live', sourceRecorded: 'Recorded', other: 'Incident data',
  },
  zh: {
    title: '事故调查', description: '询问证据、比较受影响的服务，并明确下一步该核查什么。',
    drop: '拖入一条事故', dropHint: '从队列拖入事故，或点击该事故旁的「添加到 Agent」。',
    attach: '添加当前事故', selected: '已选择事故', attached: '已关联事故', open: '打开事故详情', loading: '正在加载事故上下文',
    refresh: '刷新', unavailable: '当前无法使用事故 Agent。', evidence: '证据', records: '条记录',
    modalities: '可用数据', none: '暂无记录', start: '就这条事故提出第一个问题。',
    question: '向 Agent 提问', placeholder: '应该先检查哪个服务？有哪些证据支持？',
    send: '发送', submitting: '发送中', you: '你', waiting: '正在核查事故证据', failed: 'Agent 暂时无法回答这个问题。',
    limits: '缺少的证据', next: '建议核查项', metrics: '指标', logs: '日志', traces: '调用链',
    sourceLive: '实时', sourceRecorded: '已记录', other: '事故数据',
  },
} as const;

type Copy = typeof labels[Language];
const MAX_QUESTION = 4000;
const isActive = (turn: AgentTurn) => turn.status === 'QUEUED' || turn.status === 'RUNNING';

function shortTime(value: string, language: Language) {
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return value;
  return new Intl.DateTimeFormat(language === 'zh' ? 'zh-CN' : 'en-US', {
    dateStyle: 'short', timeStyle: 'medium', timeZone: 'UTC', hourCycle: 'h23',
  }).format(time) + ' UTC';
}

function signalLabel(signal: string, copy: Copy) {
  return signal === 'metrics' || signal === 'logs' || signal === 'traces' ? copy[signal] : signal;
}

function Turn({ turn, copy, language }: { turn: AgentTurn; copy: Copy; language: Language }) {
  return <li className="incident-agent-turn">
    <article className="incident-agent-question">
      <div className="incident-agent-message-meta"><strong>{copy.you}</strong><time dateTime={turn.created_at}>{shortTime(turn.created_at, language)}</time></div>
      <p>{turn.question}</p>
    </article>
    <article className="incident-agent-answer">
      <div className="incident-agent-message-meta"><strong>Agent</strong></div>
      {isActive(turn) ? <p role="status"><Spinner /> {copy.waiting}</p> : turn.answer ? <>
        <p>{turn.answer.text}</p>
        {turn.answer.citations.length > 0 && <div className="incident-agent-citations">
          <h3>{copy.evidence}</h3>
          {turn.answer.citations.map((evidence, index) => <details key={evidence.evidence_id}>
            <summary><span className="incident-agent-citation-index">{index + 1}</span>{evidence.label}</summary>
            <p>{evidence.detail}</p><code>{evidence.evidence_id}</code>
          </details>)}
        </div>}
        {turn.answer.limitations.length > 0 && <details className="incident-agent-disclosure">
          <summary>{copy.limits}</summary><ul>{turn.answer.limitations.map((limit, index) => <li key={index}>{limit}</li>)}</ul>
        </details>}
        {turn.answer.next_checks.length > 0 && <details className="incident-agent-disclosure">
          <summary>{copy.next}</summary><ul>{turn.answer.next_checks.map((check, index) => <li key={index}>{check}</li>)}</ul>
        </details>}
      </> : <p role="alert">{turn.error?.message ?? copy.failed}</p>}
    </article>
  </li>;
}

export default function IncidentAgent({ incidentId, language, attachCurrent }: {
  incidentId?: string; language: Language; attachCurrent?: () => void;
}) {
  const copy = labels[language];
  const path = incidentId ? `/incident-agent/${id(incidentId)}` : null;
  const [session, setSession] = useState<AgentSession>();
  const [error, setError] = useState('');
  const [question, setQuestion] = useState('');
  const [sending, setSending] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const active = session?.turns.some(isActive) ?? false;
  const alive = useRef(false);
  const request = useRef<AbortController>(null);
  const dispatching = useRef(false);
  const attempt = useRef<{ question: string; key: string }>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const follow = useRef(true);

  const refresh = useCallback(async () => {
    if (!path || request.current || dispatching.current) return;
    const controller = new AbortController();
    request.current = controller;
    setRefreshing(true);
    try {
      const result = await api<AgentSession>(path, { signal: controller.signal });
      if (result.incident_id !== incidentId || result.turns.some(turn => turn.incident_id !== incidentId)) {
        throw new Error('Unexpected incident in Agent response.');
      }
      if (!controller.signal.aborted) { setSession(result); setError(''); }
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorText(cause));
    } finally {
      if (request.current === controller) {
        request.current = null;
        if (!controller.signal.aborted) setRefreshing(false);
      }
    }
  }, [path, incidentId]);

  useEffect(() => {
    alive.current = true;
    void refresh();
    return () => {
      alive.current = false;
      request.current?.abort();
      request.current = null;
    };
  }, [refresh]);

  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => void refresh(), 1800);
    return () => clearInterval(timer);
  }, [active, refresh]);

  const latest = session?.turns.at(-1);
  useEffect(() => {
    if (follow.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [latest?.turn_id, latest?.status]);

  async function send() {
    const trimmed = question.trim();
    if (!path || !session?.enabled || active || dispatching.current || !trimmed || trimmed.length > MAX_QUESTION) return;
    dispatching.current = true;
    request.current?.abort();
    request.current = null;
    setRefreshing(false);
    if (attempt.current?.question !== trimmed) attempt.current = { question: trimmed, key: crypto.randomUUID() };
    setSending(true);
    setError('');
    try {
      const { turn } = await api<{ turn: AgentTurn }>(`${path}/turns`, {
        method: 'POST', body: { question: trimmed }, key: attempt.current.key,
      });
      if (!alive.current) return;
      if (turn.incident_id !== incidentId || turn.question !== trimmed) throw new Error('Unexpected Agent turn.');
      setSession(value => {
        if (!value) return value;
        const turns = value.turns.filter(saved => saved.turn_id !== turn.turn_id);
        return { ...value, turns: [...turns, turn] };
      });
      follow.current = true;
      setQuestion('');
      attempt.current = null;
    } catch (cause) {
      if (alive.current) setError(errorText(cause));
    } finally {
      dispatching.current = false;
      if (alive.current) setSending(false);
    }
  }

  return <div className="incident-agent">
    <header className="incident-agent-heading">
      <p className="incident-desk-kicker">AGENT</p>
      <h1>{copy.title}</h1>
      <p>{copy.description}</p>
    </header>
    {!incidentId ? <div className="incident-agent-dropzone">
      <svg aria-hidden="true" width="42" height="42" viewBox="0 0 42 42" fill="none" stroke="currentColor" strokeWidth="1.5">
        <path d="M8 7h26v20H21l-7 6v-6H8z" strokeLinejoin="round" />
        <path d="M15 17h12m-6-6v12" strokeLinecap="round" />
      </svg>
      <h2>{copy.drop}</h2><p>{copy.dropHint}</p>
      {attachCurrent && <Button onClick={attachCurrent}>{copy.attach}</Button>}
    </div> : <div className="incident-agent-frame">
      <div className="incident-agent-context">
        <div>
          <span className="incident-agent-context-label"><i aria-hidden="true" />{session ? copy.attached : copy.selected}</span>
          <h2>{session?.context.title ?? incidentId}</h2>
          <p><code>{incidentId}</code>{session && <> · {session.context.source === 'live' ? copy.sourceLive : session.context.source === 'recorded' ? copy.sourceRecorded : copy.other}</>}</p>
          {session && <p className="incident-agent-window">{shortTime(session.context.start, language)} → {shortTime(session.context.end, language)}</p>}
        </div>
        <a href={`#/errors/${id(incidentId)}`}>{copy.open} ↗</a>
      </div>
      {session && <div className="incident-agent-signals" aria-label={copy.modalities}>
        {session.context.available_modalities.length ? session.context.available_modalities.map(signal => <span key={signal}>{signalLabel(signal, copy)}</span>) : <span>{copy.none}</span>}
        <span>{session.context.evidence_count.toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US')} {copy.records}</span>
      </div>}
      {error && <div className="incident-agent-notice"><Alert type="error" action={<Button disabled={sending} loading={refreshing} onClick={() => void refresh()}>{copy.refresh}</Button>}>{error}</Alert></div>}
      {session && !session.enabled && <div className="incident-agent-notice"><Alert type="info" header={copy.unavailable}>{session.disabled_reason}</Alert></div>}
      <div className="incident-agent-transcript" ref={scroll} aria-live="polite"
        onScroll={() => { const node = scroll.current; if (node) follow.current = node.scrollHeight - node.scrollTop - node.clientHeight < 72; }}>
        {session && <IncidentWorkflow key={incidentId} incidentId={incidentId} language={language} compact refreshKey={`${latest?.turn_id ?? ''}:${latest?.status ?? ''}`} onActivity={() => void refresh()} />}
        {!session && !error ? <p className="incident-agent-empty" role="status"><Spinner /> {copy.loading}</p>
          : session?.turns.length ? <ol>{session.turns.map(turn => <Turn key={turn.turn_id} turn={turn} copy={copy} language={language} />)}</ol>
          : session?.enabled ? <p className="incident-agent-empty">{copy.start}</p> : null}
      </div>
      <form className="incident-agent-compose" onSubmit={event => { event.preventDefault(); void send(); }}>
        <label htmlFor="incident-agent-question">{copy.question}</label>
        <textarea id="incident-agent-question" rows={3} maxLength={MAX_QUESTION} value={question}
          placeholder={copy.placeholder} disabled={!session?.enabled || sending || active} onChange={event => setQuestion(event.target.value)}
          onKeyDown={event => {
            if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.nativeEvent.isComposing) {
              event.preventDefault(); void send();
            }
          }} />
        <Button formAction="submit" variant="primary" loading={sending} disabled={!session?.enabled || active || !question.trim()}>{sending ? copy.submitting : copy.send}</Button>
      </form>
    </div>}
  </div>;
}

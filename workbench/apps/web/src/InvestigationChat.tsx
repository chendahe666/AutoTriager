import { useCallback, useEffect, useRef, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import FormField from '@cloudscape-design/components/form-field';
import Spinner from '@cloudscape-design/components/spinner';
import Textarea, { type TextareaProps } from '@cloudscape-design/components/textarea';
import { api, errorText, id } from './api';
import { activeConversationTurn, conversationBlocked, reconcileConversation, scopedCitations } from './conversation';
import { investigationExport } from './conversationExport';
import { conversationCitations } from './conversationCitations';
import HistoricalMemorySources from './HistoricalMemorySources';
import type { ConversationCapabilities, ConversationHistory, ConversationTurn } from './conversationTypes';
import type { TranslationKey } from './i18n';
import type { Run } from './types';

type T = (key: TranslationKey) => string;
const time = (value: string) => value.replace('T', ' ').replace(/\.\d+Z$/, ' UTC').replace(/Z$/, ' UTC');

function Turn({ turn, t, openEvidence }: { turn: ConversationTurn; t: T; openEvidence: (runId: string, evidenceId: string) => void }) {
  const references = scopedCitations(turn);
  const numbered = conversationCitations(turn);
  return <li className="chat-turn" data-turn-id={turn.turn_id}>
    <article className="chat-message chat-user">
      <div className="chat-message-heading"><strong>{t('chatYou')}</strong><time>{time(turn.created_at)}</time></div>
      <p className="source-text">{turn.question}</p>
    </article>
    <article className={`chat-message chat-assistant chat-${turn.status.toLowerCase()}`}>
      <div className="chat-message-heading"><strong>AutoTriager</strong><span className={`chat-status ${turn.status.toLowerCase()}`}>{t(turn.status === 'QUEUED' ? 'chatQueued' : turn.status === 'RUNNING' ? 'chatGenerating' : turn.status === 'COMPLETED' ? 'chatGenerated' : 'chatFailed')}</span></div>
      <div className="chat-turn-source"><span>{t('chatSourceRun')}</span><code title={turn.context_run_id}>{turn.context_run_id}</code></div>
      {turn.status === 'QUEUED' || turn.status === 'RUNNING' ? <div className="chat-progress" role="status"><Spinner /> <span>{t(turn.status === 'QUEUED' ? 'chatQueuedText' : 'chatGeneratingText')}</span></div> : turn.status === 'FAILED' ? <><p>{t('chatFailedText')}</p>{turn.error && <p className="chat-turn-error source-text"><code>{turn.error.code}</code>: {turn.error.message}</p>}</> : turn.answer ? <>
        <p className="chat-answer source-text">{numbered.fragments.map((fragment, index) => fragment.kind === 'text' ? fragment.text : <button type="button" className="chat-inline-citation" key={index} title={`${t('chatSource')} ${fragment.number}: ${fragment.evidence_id}`} aria-label={`${t('chatSource')} ${fragment.number}: ${fragment.evidence_id}`} onClick={() => openEvidence(fragment.run_id, fragment.evidence_id)}>[{fragment.number}]</button>)}</p>
        {numbered.sources.length > 0 && <div className="chat-references"><h4>{t('chatSources')}</h4><div className="evidence-links">{numbered.sources.map(reference => <button type="button" className="evidence-link" key={reference.evidence_id} onClick={() => openEvidence(reference.run_id, reference.evidence_id)} title={`${t('inspect')}: ${reference.evidence_id}`}><span aria-hidden="true">↗</span>{t('chatSource')} {reference.number}</button>)}</div></div>}
        <HistoricalMemorySources answer={turn.answer} t={t} />
        {references.rejected > 0 && <Alert type="error">{t('chatInvalidReferences')}</Alert>}
        {numbered.unknownInlineIds.length > 0 && <Alert type="warning">{t('chatUnlinkedInlineReferences')}</Alert>}
        {turn.answer.limitations.length > 0 && <details className="chat-answer-details"><summary>{t('chatLimitations')}</summary><ul className="plain-list source-text">{turn.answer.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
        {turn.answer.next_checks.length > 0 && <details className="chat-answer-details"><summary>{t('chatNextChecks')}</summary><ul className="plain-list source-text">{turn.answer.next_checks.map((item, index) => <li key={index}>{item}</li>)}</ul><p className="small muted">{t('nextChecksNote')}</p></details>}
      </> : <p>{t('chatNoAnswer')}</p>}
      <details className="chat-turn-provenance"><summary>{t('chatContext')}</summary><dl><div><dt>{t('run')}</dt><dd><code>{turn.context_run_id}</code></dd></div>{turn.answer?.model_id && <div><dt>{t('model')}</dt><dd><code>{turn.answer.model_id}</code></dd></div>}<div><dt>{t('chatTurn')}</dt><dd><code>{turn.turn_id}</code></dd></div>{turn.context_digest && <div><dt>{t('chatContextDigest')}</dt><dd><code>{turn.context_digest}</code></dd></div>}{numbered.sources.map(reference => <div key={reference.evidence_id}><dt>{t('chatSource')} {reference.number}</dt><dd><code>{reference.run_id} / {reference.evidence_id}</code></dd></div>)}</dl>{turn.answer && <details><summary>{t('chatOriginalAnswer')}</summary><p className="source-text">{turn.answer.text}</p></details>}<p>{t('chatEvidenceBoundary')}</p></details>
    </article>
  </li>;
}

export default function InvestigationChat({ investigationId, contextRun, t, openEvidence, focusOnLoad = false }: {
  investigationId: string;
  contextRun: Run | null;
  t: T;
  openEvidence: (runId: string, evidenceId: string) => void;
  focusOnLoad?: boolean;
}) {
  const [caps, setCaps] = useState<ConversationCapabilities | null>(null);
  const [turns, setTurns] = useState<ConversationTurn[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [question, setQuestion] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const turnsRef = useRef<ConversationTurn[]>([]);
  const alive = useRef(true);
  const reading = useRef(false);
  const dispatching = useRef(false);
  const attempt = useRef<{ signature: string; key: string } | null>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const input = useRef<TextareaProps.Ref>(null);
  const focusApplied = useRef(false);
  const followBottom = useRef(true);
  const path = `/investigations/${id(investigationId)}/conversation`;

  const refresh = useCallback(async () => {
    if (reading.current) return;
    reading.current = true;
    setRefreshing(true);
    try {
      const [capabilities, history] = await Promise.all([
        api<ConversationCapabilities>('/conversation-capabilities'),
        api<ConversationHistory>(path),
      ]);
      if (!alive.current) return;
      if (history.investigation_id !== investigationId) throw new Error('The conversation belongs to a different investigation.');
      const next = reconcileConversation(turnsRef.current, history.turns, investigationId);
      turnsRef.current = next;
      setTurns(next);
      setCaps(capabilities);
      setTruncated(history.truncated === true);
      setError('');
    } catch (cause) {
      if (alive.current) setError(errorText(cause));
    } finally {
      reading.current = false;
      if (alive.current) { setLoading(false); setRefreshing(false); }
    }
  }, [investigationId, path]);

  useEffect(() => {
    alive.current = true;
    void refresh();
    return () => { alive.current = false; };
  }, [refresh]);
  const active = activeConversationTurn(turns);
  useEffect(() => {
    if (focusOnLoad && !focusApplied.current && !loading && !active) { input.current?.focus(); focusApplied.current = true; }
  }, [focusOnLoad, loading, active?.turn_id]);
  useEffect(() => {
    if (!active) return;
    // Refresh saved state only. Reading the conversation never dispatches inference.
    const timer = setInterval(() => void refresh(), 1800);
    return () => clearInterval(timer);
  }, [active?.turn_id, refresh]);
  const latest = turns.at(-1);
  useEffect(() => {
    if (followBottom.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [turns.length, latest?.status]);

  const reason = conversationBlocked(caps, contextRun, turns, question);
  const maxChars = Math.min(caps?.max_question_chars ?? 4000, 4000);
  const blockedText: Record<string, TranslationKey> = {
    unavailable: 'chatUnavailable', no_context: 'chatChooseContext', fixture: 'chatFixtureBlocked',
    running_context: 'chatContextPending', active: 'chatActiveNotice', too_long: 'chatQuestionTooLong',
  };

  async function submit() {
    if (reason || dispatching.current || !contextRun) return;
    dispatching.current = true;
    const body = { question: question.trim(), context_run_id: contextRun.run_id };
    const signature = JSON.stringify(body);
    if (attempt.current?.signature !== signature) attempt.current = { signature, key: crypto.randomUUID() };
    setSending(true); setError('');
    try {
      const result = await api<{ turn: ConversationTurn }>(`${path}/turns`, { method: 'POST', body, key: attempt.current.key });
      if (!alive.current) return;
      if (result.turn.context_run_id !== body.context_run_id || result.turn.question !== body.question) throw new Error('The returned conversation turn does not match the submitted question and context.');
      const next = reconcileConversation(turnsRef.current, [result.turn], investigationId);
      turnsRef.current = next;
      setTurns(next);
      followBottom.current = true;
      setQuestion('');
      attempt.current = null;
      void refresh();
    } catch (cause) {
      if (alive.current) setError(errorText(cause));
    } finally {
      dispatching.current = false;
      if (alive.current) setSending(false);
    }
  }

  function exportRecord() {
    try {
      const record = investigationExport(investigationId, contextRun, turns, truncated, new Date().toISOString());
      const url = URL.createObjectURL(new Blob([JSON.stringify(record, null, 2)], { type: 'application/json' }));
      const link = document.createElement('a');
      link.href = url;
      link.download = `autotriager-${investigationId.replace(/[^A-Za-z0-9_-]/g, '_')}-record.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) { setError(errorText(cause)); }
  }

  return <aside className="investigation-chat panel" aria-labelledby="investigation-chat-title" data-testid="investigation-chat">
    <div className="panel-heading chat-heading"><div><div className="eyebrow">{t('chatEyebrow')}</div><h2 id="investigation-chat-title">{t('chatTitle')}</h2></div><Button variant="icon" iconName="refresh" ariaLabel={t('chatRefresh')} loading={refreshing} onClick={() => void refresh()} /></div>
    <div className="chat-scope"><span className="chat-source-mark" aria-hidden="true" /><div><strong>{t('chatRecordedScope')}</strong><p>{t('chatScopeText')}</p></div></div>
    <div className="chat-scroll" ref={scroll} onScroll={() => { const node = scroll.current; if (node) followBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 70; }}>
      {loading ? <div className="loading"><Spinner /> {t('chatLoading')}</div> : <>
        {truncated && <p className="chat-history-warning">{t('chatTruncated')}</p>}
        {turns.length === 0 ? <div className="chat-empty"><div className="chat-empty-icon" aria-hidden="true">↗</div><h3>{t('chatEmptyTitle')}</h3><p>{t('chatEmptyText')}</p><span>{t('chatSavedHistory')}</span></div> : <ol className="chat-transcript" role="log" aria-label={t('chatHistory')} aria-live="polite" aria-relevant="additions text">{turns.map(turn => <Turn key={turn.turn_id} turn={turn} t={t} openEvidence={openEvidence} />)}</ol>}
      </>}
    </div>
    <div className="chat-composer">
      {error && <div className="chat-request-error"><Alert type="error" header={t('chatRequestFailed')}>{error}<p className="small">{t('chatRetryNote')}</p></Alert></div>}
      {!loading && caps && !caps.enabled && <Alert type="info" header={t('chatUnavailable')}><span className="source-text">{caps.disabled_reason || t('chatUnavailableText')}</span></Alert>}
      <div className="chat-context-label"><span>{t('chatUsingContext')}</span><strong title={contextRun?.run_id}>{contextRun?.run_id ?? t('none')}</strong>{contextRun && <span className="chat-context-state">{contextRun.state}</span>}</div>
      <form onSubmit={event => { event.preventDefault(); void submit(); }}>
        <div className="chat-suggestions" aria-label={t('chatSuggestions')}>{(['chatSuggestEvidence', 'chatSuggestUnknowns', 'chatSuggestCheck'] as const).map(key => <button type="button" key={key} disabled={sending || Boolean(active)} onClick={() => setQuestion(t(key))}>{t(key)}</button>)}</div>
        <FormField label={t('chatQuestion')} description={t('chatInputDescription')} errorText={reason === 'too_long' ? t('chatQuestionTooLong') : undefined}>
          <Textarea ref={input} value={question} onChange={({ detail }) => setQuestion(detail.value)} rows={3} placeholder={t('chatPlaceholder')} disabled={sending || Boolean(active) || loading} ariaLabel={t('chatQuestion')} />
        </FormField>
        {reason && !['unavailable', 'empty', 'too_long'].includes(reason) && <p className="chat-gate-note" role="status">{t(blockedText[reason])}</p>}
        <div className="chat-send-row"><span className={question.trim().length > maxChars ? 'chat-over-limit' : ''}>{question.trim().length.toLocaleString()} / {maxChars.toLocaleString()}</span><Button variant="primary" formAction="submit" loading={sending} disabled={Boolean(reason) || sending || loading}>{t('chatSend')}</Button></div>
      </form>
      <p className="chat-footer-note">{t('chatFooterNote')}</p>
      {caps && <div className="chat-provider-meta"><span>{caps.model_id ?? t('notConfigured')}</span><span>{t('chatObservedCalls')}: {caps.budget.used_calls} / {caps.budget.max_calls}</span></div>}
      <div className="chat-export"><Button variant="link" iconName="download" disabled={loading || (!contextRun && turns.length === 0)} onClick={exportRecord}>{t('chatExport')}</Button><span>{t('chatExportScope')}</span></div>
    </div>
  </aside>;
}

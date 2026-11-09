import { useCallback, useEffect, useRef, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import Input from '@cloudscape-design/components/input';
import Spinner from '@cloudscape-design/components/spinner';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { api, errorText } from './api';
import { conversationThreadPath, filterConversationThreads, parseConversationIndex, type ConversationIndex } from './conversationIndex';
import type { ConversationStatus } from './conversationTypes';
import type { TranslationKey } from './i18n';

type T = (key: TranslationKey) => string;
const time = (value: string) => value.replace('T', ' ').replace(/\.\d+Z$/, ' UTC').replace(/Z$/, ' UTC');
function Status({ value, t }: { value: ConversationStatus; t: T }) {
  return <StatusIndicator type={value === 'COMPLETED' ? 'success' : value === 'FAILED' ? 'error' : value === 'RUNNING' ? 'in-progress' : 'pending'}>{t(value === 'COMPLETED' ? 'chatGenerated' : value === 'FAILED' ? 'chatFailed' : value === 'RUNNING' ? 'chatGenerating' : 'chatQueued')}</StatusIndicator>;
}

export default function Conversations({ t }: { t: T }) {
  const [index, setIndex] = useState<ConversationIndex | null>(null);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const sequence = useRef(0);
  const alive = useRef(true);
  const refresh = useCallback(async () => {
    const request = ++sequence.current;
    setLoading(true);
    try {
      const value = parseConversationIndex(await api<unknown>('/conversations'));
      if (alive.current && sequence.current === request) { setIndex(value); setError(''); }
    } catch (cause) { if (alive.current && sequence.current === request) setError(errorText(cause)); }
    finally { if (alive.current && sequence.current === request) setLoading(false); }
  }, []);
  useEffect(() => { alive.current = true; void refresh(); return () => { alive.current = false; sequence.current += 1; }; }, [refresh]);
  const visible = filterConversationThreads(index?.threads ?? [], query);

  return <>
    <div className="page-title"><div><div className="eyebrow">{t('workspace')}</div><h1>{t('conversations')}</h1><p>{t('conversationIndexDescription')}</p></div><Button iconName="refresh" loading={loading} onClick={() => void refresh()}>{t('refresh')}</Button></div>
    {error && <Alert type="error" header={t('conversationIndexError')}>{error}{index && <p>{t('evalStaleRecord')}</p>}</Alert>}
    {index && !index.enabled && <Alert type="info" header={t('conversationIndexDisabled')}>{t('conversationIndexDisabledText')}</Alert>}
    <section className="panel list-panel conversation-index" aria-labelledby="conversation-index-title">
      <div className="panel-heading"><h2 id="conversation-index-title">{t('conversationSavedThreads')} <span className="count">({index?.threads.length ?? 0} {t('loaded')})</span></h2><span className="small muted">{t('conversationIndexReadOnly')}</span></div>
      <div className="conversation-filter"><Input type="search" value={query} disabled={!index || !index.enabled} onChange={({ detail }) => setQuery(detail.value)} placeholder={t('conversationFilter')} ariaLabel={t('conversationFilter')} /><p>{t(index?.truncated ? 'conversationLatest100' : 'conversationLoadedScope')}</p></div>
      {loading && !index ? <div className="loading large"><Spinner /> {t('chatLoading')}</div> : !index ? <div className="empty"><h2>{t('conversationHistoryNotLoaded')}</h2><p>{t('conversationHistoryNotLoadedText')}</p></div> : visible.length > 0 ? <div className="table-scroll"><table><thead><tr><th>{t('conversationThread')}</th><th>{t('conversationLastQuestion')}</th><th>{t('conversationReplyState')}</th><th>{t('conversationTurns')}</th><th>{t('updated')}</th></tr></thead><tbody>{visible.map(thread => <tr key={thread.investigation_id}>
        <td><a className="record-title" href={`#${conversationThreadPath(thread)}`}>{thread.title || thread.investigation_id}</a><div className="mono muted small">{thread.incident_id}</div></td>
        <td className="conversation-question-cell"><a className="conversation-question-link source-text" href={`#${conversationThreadPath(thread)}`} title={thread.last_question}>{thread.last_question}</a><span className="conversation-index-context"><span>{t('chatSourceRun')}</span><code>{thread.context_run_id}</code></span></td>
        <td className="conversation-status-cell"><Status value={thread.last_status} t={t} /></td><td className="conversation-turn-count">{thread.turn_count.toLocaleString('en-US')}</td><td className="small nowrap muted">{time(thread.updated_at)}</td>
      </tr>)}</tbody></table></div> : <div className="empty"><div className="empty-mark" aria-hidden="true">↗</div><h2>{t(query.trim() ? 'conversationNoMatches' : index?.enabled === false ? 'conversationIndexDisabled' : 'conversationNoThreads')}</h2><p>{t(query.trim() ? 'conversationNoMatchesText' : index?.enabled === false ? 'conversationIndexDisabledText' : 'conversationNoThreadsText')}</p>{!query.trim() && <Button href="#/investigations">{t('conversationOpenInvestigation')}</Button>}</div>}
    </section>
  </>;
}

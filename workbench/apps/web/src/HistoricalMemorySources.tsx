import type { ConversationAnswer } from './conversationTypes';
import { historicalMemorySources } from './historicalMemory';
import { knowledgePath, reviewKind } from './knowledgeModel';
import type { TranslationKey } from './i18n';

type T = (key: TranslationKey) => string;
export default function HistoricalMemorySources({ answer, t }: { answer: ConversationAnswer; t: T }) {
  const { snapshot, cited, rejected } = historicalMemorySources(answer);
  if (!snapshot && !answer.memory_citations?.length) return null;
  return <section className="chat-historical-memory" aria-label={t('memoryHistoricalSources')}>
    <h4>{t('memoryHistoricalSources')}</h4><p>{t('memoryHistoricalMeaning')}</p>
    {cited.map(({ citation, item, number }) => <div className="chat-memory-source" key={`${citation.memory_id}-${citation.revision}`}>
      <a className="chat-memory-link" href={`#${knowledgePath(citation.memory_id, citation.revision)}`} title={`${t('memoryOpenCurrentRecord')}: ${citation.memory_id} · ${t('memoryRevision')} ${citation.revision}`}>{t('memoryHistoricalLabel')} {number} · {t('memoryRevision')} {citation.revision} <span aria-hidden="true">↗</span></a>
      <strong>{item.title}</strong><span className="chat-memory-quality">{t(({ policy: 'memoryPolicyReview', human: 'memoryHumanReview', other: 'memoryOtherReview', none: 'memoryNoReview' } as const)[reviewKind(item.review?.actor_type)])}</span>
      <span className="chat-memory-origin">{t('memorySource')}: <code>{citation.source_incident_id ?? item.source.kind}</code>{citation.source_run_id && <> · <code>{citation.source_run_id}</code></>}</span>
      <details><summary>{t('memoryRetrievalReason')}</summary><p className="source-text">{typeof item.match_reason === 'string' ? item.match_reason : JSON.stringify(item.match_reason, null, 2)}</p>{typeof item.score === 'number' && Number.isFinite(item.score) && <p>{t('memoryRetrievalScore')}: {item.score.toFixed(3)}</p>}</details>
    </div>)}
    {rejected.length > 0 && <p className="chat-memory-warning">{t('memoryUnboundReference')}</p>}
    {snapshot && <details className="chat-memory-trace"><summary>{t('memoryRetrievalTrace')} · {snapshot.items.length} {t('memoryRetrievedCount')}</summary><code>{snapshot.snapshot_id}</code>{!snapshot.items.length && <p>{t('memoryNoneRetrieved')}</p>}{snapshot.items.map(item => <div className="chat-memory-trace-item" key={`${item.memory_id}-${item.revision}`}><code>{item.memory_id} · {t('memoryRevision')} {item.revision}</code><p className="source-text">{item.summary}</p><span>{item.scope ? `${item.scope.system_id} · ${item.scope.service_ids.join(', ')}` : t('memoryScopeInRecord')}</span></div>)}<ul>{snapshot.limitations.map((value, index) => <li className="source-text" key={index}>{value}</li>)}</ul><p>{t('memorySnapshotStatus')}</p></details>}
  </section>;
}

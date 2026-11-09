import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import Checkbox from '@cloudscape-design/components/checkbox';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import Spinner from '@cloudscape-design/components/spinner';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Textarea from '@cloudscape-design/components/textarea';
import { api, errorText, id } from './api';
import { filterMemories, knowledgePath, memoryActions, memoryDecision, memoryStates, parseKnowledgeIndex, parseMemoryEntry, reconcileMemoryEntry, reviewKind } from './knowledgeModel';
import type { KnowledgeIndex, MemoryEntry, MemoryState, MemorySummary } from './knowledgeTypes';
import type { TranslationKey } from './i18n';

type T = (key: TranslationKey) => string;
const stateKeys: Record<MemoryState, TranslationKey> = { CANDIDATE: 'memoryCandidate', REVIEWED: 'memoryReviewed', PUBLISHED: 'memoryPublished', QUARANTINED: 'memoryQuarantined', REVOKED: 'memoryRevoked' };
const time = (value: string | null | undefined) => value ? value.replace('T', ' ').replace(/\.\d+Z$/, ' UTC').replace(/Z$/, ' UTC') : '—';
const reviewLabel = (entry: Pick<MemorySummary, 'review'>, t: T) => t(({ policy: 'memoryPolicyReview', human: 'memoryHumanReview', other: 'memoryOtherReview', none: 'memoryNoReview' } as const)[reviewKind(entry.review?.actor_type)]);
function Panel({ title, children, aside }: { title: ReactNode; children: ReactNode; aside?: ReactNode }) { return <section className="panel"><div className="panel-heading"><h2>{title}</h2>{aside}</div><div className="panel-content">{children}</div></section>; }
function Fact({ label, children }: { label: string; children: ReactNode }) { return <div className="fact"><dt>{label}</dt><dd>{children}</dd></div>; }
function State({ state, t }: { state: MemoryState; t: T }) { return <StatusIndicator type={state === 'PUBLISHED' ? 'success' : state === 'QUARANTINED' ? 'warning' : state === 'REVOKED' ? 'stopped' : 'pending'}>{t(stateKeys[state])}</StatusIndicator>; }

function MemoryList({ t }: { t: T }) {
  const [index, setIndex] = useState<KnowledgeIndex | null>(null);
  const [query, setQuery] = useState(''); const [state, setState] = useState('');
  const [loading, setLoading] = useState(true); const [error, setError] = useState('');
  const [runId, setRunId] = useState(''); const [capturing, setCapturing] = useState(false);
  const alive = useRef(true); const sequence = useRef(0); const dispatching = useRef(false);
  const attempt = useRef<{ signature: string; key: string } | null>(null);
  const refresh = useCallback(async () => {
    const request = ++sequence.current; setLoading(true);
    try {
      const value = parseKnowledgeIndex(await api<unknown>('/knowledge'));
      if (alive.current && request === sequence.current) { setIndex(value); setError(''); }
    } catch (cause) { if (alive.current && request === sequence.current) setError(errorText(cause)); }
    finally { if (alive.current && request === sequence.current) setLoading(false); }
  }, []);
  useEffect(() => { alive.current = true; void refresh(); return () => { alive.current = false; sequence.current += 1; }; }, [refresh]);
  async function capture() {
    if (!runId.trim() || !index?.enabled || dispatching.current) return;
    const body = { run_id: runId.trim() }; const signature = JSON.stringify(body);
    if (attempt.current?.signature !== signature) attempt.current = { signature, key: crypto.randomUUID() };
    dispatching.current = true; setCapturing(true); setError('');
    try {
      const result = await api<{ entry: unknown }>('/knowledge/capture', { method: 'POST', body, key: attempt.current.key });
      const entry = parseMemoryEntry(result.entry);
      if (alive.current) location.hash = knowledgePath(entry.memory_id);
    } catch (cause) { if (alive.current) setError(errorText(cause)); }
    finally { dispatching.current = false; if (alive.current) setCapturing(false); }
  }
  const items = filterMemories(index?.items ?? [], query, state);
  return <>
    <div className="page-title"><div><div className="eyebrow">{t('knowledge')}</div><h1>{t('memoryPageTitle')}</h1><p>{t('memoryPageDescription')}</p></div><Button iconName="refresh" loading={loading} onClick={() => void refresh()}>{t('refresh')}</Button></div>
    <Alert type="info" header={t('memoryGrowthTitle')}>{t('memoryGrowthText')}</Alert>
    {index?.enabled && <Alert type="info" header={t(index.answer_injection_enabled === true ? 'memoryAnswerInjectionOnTitle' : index.answer_injection_enabled === false ? 'memoryAnswerInjectionOffTitle' : 'memoryAnswerInjectionUnknownTitle')}>{t(index.answer_injection_enabled === true ? 'memoryAnswerInjectionOn' : index.answer_injection_enabled === false ? 'memoryAnswerInjectionOff' : 'memoryAnswerInjectionUnknown')}</Alert>}
    {error && <Alert type="error" header={t('memoryReadError')}>{error}{index && <p>{t('evalStaleRecord')}</p>}</Alert>}
    {index && !index.enabled && <Alert type="info" header={t('memoryUnavailable')}>{index.reason ?? t('memoryUnavailableText')}</Alert>}
    {index?.enabled && <div className="memory-counts" aria-label={t('memoryOwnerTotals')}>{memoryStates.map(value => <button type="button" className={state === value ? 'selected' : ''} key={value} onClick={() => setState(state === value ? '' : value)}><span>{t(stateKeys[value])}</span><strong>{index.counts[value] ?? 0}</strong></button>)}</div>}
    <section className="panel list-panel memory-index"><div className="panel-heading"><h2>{t('memoryRecords')} <span className="count">({index?.items.length ?? 0} {t('loaded')})</span></h2><span className="small muted">{t('memoryOwnerTotals')}</span></div><div className="table-toolbar"><Input type="search" value={query} disabled={!index?.enabled} onChange={({ detail }) => setQuery(detail.value)} placeholder={t('memoryFilter')} ariaLabel={t('memoryFilter')} /><Select selectedOption={{ label: state ? t(stateKeys[state as MemoryState]) : t('memoryAllStates'), value: state }} options={[{ label: t('memoryAllStates'), value: '' }, ...memoryStates.map(value => ({ label: t(stateKeys[value]), value }))]} onChange={({ detail }) => setState(detail.selectedOption.value ?? '')} ariaLabel={t('memoryAllStates')} /></div><p className="memory-loaded-note">{t(index?.truncated ? 'memoryLatest100' : 'memoryLoadedScope')}</p>
      {loading && !index ? <div className="loading large"><Spinner /> {t('loading')}</div> : !index ? <div className="empty"><h2>{t('memoryNotLoaded')}</h2><p>{t('memoryNotLoadedText')}</p></div> : items.length ? <div className="table-scroll"><table><thead><tr><th>{t('memoryRecord')}</th><th>{t('memoryLifecycle')}</th><th>{t('memorySource')}</th><th>{t('memoryReviewMode')}</th><th>{t('memoryCaptured')}</th></tr></thead><tbody>{items.map(entry => <tr key={entry.memory_id}><td><a className="record-title" href={`#${knowledgePath(entry.memory_id)}`}>{entry.title || entry.memory_id}</a><p className="memory-list-summary">{entry.summary}</p><span className="small muted">{entry.fact_count} {t('memoryFacts')} · {t('memoryRevision')} {entry.revision}</span></td><td className="nowrap"><State state={entry.state} t={t} /></td><td><code>{entry.source.incident_id ?? entry.source.kind}</code><div className="small muted">{entry.scope.system_id}</div><div className="memory-producer">{t(entry.producer === 'deterministic' ? 'memoryDeterministic' : 'memoryAIProposal')}</div></td><td className="small">{reviewLabel(entry, t)}</td><td className="small nowrap">{time(entry.capture.at)}<div className="memory-producer">{t(entry.capture.automatic ? 'memoryAutomatic' : 'memoryExplicitCapture')}</div></td></tr>)}</tbody></table></div> : <div className="empty"><h2>{t(index.enabled ? query || state ? 'memoryNoMatches' : 'memoryEmpty' : 'memoryUnavailable')}</h2><p>{t(index.enabled ? 'memoryEmptyText' : 'memoryUnavailableText')}</p></div>}
    </section>
    <details className="memory-capture"><summary>{t('memoryCaptureExisting')}</summary><p className="small muted">{t('memoryCaptureExistingText')}</p><form onSubmit={event => { event.preventDefault(); void capture(); }}><FormField label={t('memoryRecordedRunId')}><Input value={runId} disabled={capturing || !index?.enabled} onChange={({ detail }) => setRunId(detail.value)} /></FormField><Button variant="normal" formAction="submit" loading={capturing} disabled={!runId.trim() || !index?.enabled}>{t('memoryCapture')}</Button></form></details>
  </>;
}

function MemoryDetail({ memoryId, referenceRevision, t }: { memoryId: string; referenceRevision?: number; t: T }) {
  const [entry, setEntry] = useState<MemoryEntry | null>(null); const [loading, setLoading] = useState(true);
  const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const [note, setNote] = useState(''); const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false); const alive = useRef(true); const sequence = useRef(0); const dispatching = useRef(false);
  const attempt = useRef<{ signature: string; key: string } | null>(null);
  const currentEntry = useRef<MemoryEntry | null>(null);
  const accept = useCallback((value: MemoryEntry) => {
    if (value.memory_id !== memoryId) throw new Error('The knowledge response belongs to another record.');
    const next = reconcileMemoryEntry(currentEntry.current, value);
    if (currentEntry.current && next.revision !== currentEntry.current.revision) setAcknowledged(false);
    currentEntry.current = next; setEntry(next);
    return next;
  }, [memoryId]);
  const refresh = useCallback(async () => {
    const request = ++sequence.current; setLoading(true);
    try {
      const response = await api<{ entry: unknown }>(`/knowledge/${id(memoryId)}`);
      const value = parseMemoryEntry(response.entry);
      if (value.memory_id !== memoryId) throw new Error('The knowledge response belongs to another record.');
      if (alive.current && request === sequence.current) { accept(value); setError(''); }
    } catch (cause) { if (alive.current && request === sequence.current) setError(errorText(cause)); }
    finally { if (alive.current && request === sequence.current) setLoading(false); }
  }, [memoryId, accept]);
  useEffect(() => { alive.current = true; void refresh(); return () => { alive.current = false; sequence.current += 1; }; }, [refresh]);
  async function decide(action: 'review' | 'publish' | 'revoke') {
    if (!entry || dispatching.current || !note.trim() || note.trim().length > 1000 || (action !== 'revoke' && !acknowledged) || !memoryActions(entry)[action]) return;
    const body = memoryDecision(entry, action, note);
    const signature = JSON.stringify([memoryId, action, body]);
    if (attempt.current?.signature !== signature) attempt.current = { signature, key: crypto.randomUUID() };
    dispatching.current = true; setBusy(true); setError(''); setNotice('');
    // Prevent a slower pre-action GET from replacing the decision response.
    sequence.current += 1;
    try {
      const response = await api<{ entry: unknown }>(`/knowledge/${id(memoryId)}/${action}`, { method: 'POST', body, key: attempt.current.key });
      const value = parseMemoryEntry(response.entry);
      if (value.memory_id !== memoryId) throw new Error('The decision response belongs to another record.');
      if (alive.current) { const next = accept(value); setNotice(`${value.revision < next.revision ? `${t('memoryOlderReceipt')} ` : ''}${t(stateKeys[next.state])}`); setNote(''); setAcknowledged(false); attempt.current = null; setLoading(false); }
    } catch (cause) { if (alive.current) { setError(errorText(cause)); setLoading(false); } }
    finally { dispatching.current = false; if (alive.current) setBusy(false); }
  }
  if (loading && !entry) return <div className="loading large"><Spinner /> {t('loading')}</div>;
  if (!entry) return <><a className="back-link" href="#/knowledge">← {t('memoryBack')}</a><Alert type="error" header={t('memoryReadError')}>{error || t('noData')}</Alert></>;
  const actions = memoryActions(entry);
  const action = actions.review ? 'review' : actions.publish ? 'publish' : actions.revoke ? 'revoke' : null;
  return <>
    <a className="back-link" href="#/knowledge">← {t('memoryBack')}</a><div className="page-title"><div><div className="eyebrow mono">{entry.memory_id} · {t('memoryRevision')} {entry.revision}</div><h1>{entry.title}</h1><p>{entry.summary}</p></div><div className="memory-detail-actions"><State state={entry.state} t={t} /><Button iconName="refresh" loading={loading} disabled={busy} onClick={() => void refresh()}>{t('refresh')}</Button></div></div>
    {error && <Alert type="error" header={t('memoryActionError')}>{error}<p>{t('memoryConflictHint')}</p></Alert>}
    {notice && <Alert type="info" header={t('memoryDecisionSaved')}>{notice}</Alert>}
    {referenceRevision !== undefined && <Alert type="info" header={t('memoryPinnedReference')}>{t('memoryReferencedRevision')} {referenceRevision}. {t('memoryCurrentRevision')} {entry.revision} · {t(stateKeys[entry.state])}. {t('memoryOldTurnsUnchanged')}</Alert>}
    <Alert type={entry.state === 'QUARANTINED' ? 'warning' : 'info'} header={t('memoryMeaningTitle')}>{t('memoryMeaningText')}</Alert>
    <div className="memory-detail-layout"><div>
      <Panel title={t('memorySourceFacts')} aside={<span className="small muted">{entry.facts.length} {t('memoryFacts')}</span>}><p className="small muted">{t(entry.producer === 'deterministic' ? 'memoryFactMeaning' : 'memoryProposalMeaning')}</p>{entry.facts.map(fact => <details className="memory-fact" key={fact.fact_id}><summary><span>{fact.modality}</span><code>{fact.service_id ?? t('unknown')}</code><span>{fact.fact_id}</span></summary><dl className="facts"><Fact label={t('units')}><code>{fact.unit ?? t('unknown')}</code></Fact><Fact label={t('evidence')}><code>{fact.evidence_id ?? '—'}</code></Fact><Fact label={t('locator')}><code>{fact.record_locator ?? '—'}</code></Fact><Fact label={t('artifactDigest')}><code>{fact.source_artifact_digest ?? '—'}</code></Fact></dl><pre className="record-json">{JSON.stringify(fact.record, null, 2)}</pre></details>)}{!entry.facts.length && <p className="small muted">{t('memoryNoFacts')}</p>}</Panel>
      <Panel title={t('memoryChecks')}><p className="small muted">{t('memoryChecksMeaning')}</p><ul className="memory-checks">{entry.checks.map((check, i) => <li key={`${check.id}-${i}`}><StatusIndicator type={check.status === 'pass' ? 'success' : check.status === 'fail' ? 'error' : 'pending'}>{check.status}</StatusIndicator><div><code>{check.id}</code><p className="source-text">{check.detail}</p></div></li>)}</ul></Panel>
      <Panel title={t('memorySourceIdentity')}><dl className="facts"><Fact label={t('memorySourceKind')}>{entry.source.kind}</Fact><Fact label={t('incident')}><code>{entry.source.incident_id ?? '—'}</code></Fact><Fact label={t('run')}><code>{entry.source.run_id ?? '—'}</code></Fact><Fact label={t('revision')}><code>{entry.source.data_revision ?? '—'}</code></Fact><Fact label={t('memorySourceDigest')}><code>{entry.source.digest}</code></Fact><Fact label={t('memoryGroup')}><code>{entry.source.group_id ?? t('unknown')}</code></Fact></dl>{entry.source.investigation_id && entry.source.run_id && <Button href={`#/investigations/${id(entry.source.investigation_id)}/runs/${id(entry.source.run_id)}`}>{t('memoryOpenSourceRun')}</Button>}<details className="memory-reference-details"><summary>{t('memoryOriginalReferences')}</summary><pre>{JSON.stringify({ evidence_refs: entry.source.evidence_refs, source_artifact_digests: entry.source.source_artifact_digests }, null, 2)}</pre></details></Panel>
    </div><aside>
      <Panel title={t('memoryReuseScope')}><dl className="facts vertical"><Fact label={t('summarySystem')}>{entry.scope.system_id}</Fact><Fact label={t('services')}><span className="source-text">{entry.scope.service_ids.join(' · ') || '—'}</span></Fact><Fact label={t('revision')}><code>{entry.scope.data_revision ?? t('unknown')}</code></Fact><Fact label={t('memoryProducer')}>{t(entry.producer === 'deterministic' ? 'memoryDeterministic' : 'memoryAIProposal')}</Fact><Fact label={t('memoryClaimKind')}>{entry.claim_kind === 'recorded_observation' ? t('memoryRecordedObservation') : entry.claim_kind}</Fact><Fact label={t('memoryCausalStatus')}>{entry.causal_status === 'not_assessed' ? t('notAssessed') : entry.causal_status}</Fact></dl><p className="small muted">{t(entry.state === 'PUBLISHED' ? 'memoryEligibleText' : entry.state === 'REVOKED' ? 'memoryRevokedText' : 'memoryNotEligibleText')}</p></Panel>
      <Panel title={t('memoryDecisions')}><dl className="facts vertical"><Fact label={t('memoryCaptured')}>{time(entry.capture.at)}<br />{t(entry.capture.automatic ? 'memoryAutomatic' : 'memoryExplicitCapture')}</Fact><Fact label={t('memoryReviewMode')}>{reviewLabel(entry, t)}</Fact>{entry.review && <><Fact label={t('memoryReviewTime')}>{time(entry.review.at)}</Fact><Fact label={t('memoryReviewNote')}><span className="source-text">{entry.review.note}</span></Fact></>}{entry.publication && <><Fact label={t('memoryPublishedAt')}>{time(entry.publication.published_at)}</Fact><Fact label={t('memoryRevokedAt')}>{time(entry.publication.revoked_at)}</Fact><Fact label={t('memoryDecisionReason')}><span className="source-text">{entry.publication.reason}</span></Fact></>}</dl>{entry.review && <details><summary>{t('memoryReviewBasis')}</summary><pre>{JSON.stringify(entry.review.basis, null, 2)}</pre></details>}</Panel>
      {action && <Panel title={t(action === 'review' ? 'memoryReviewAction' : action === 'publish' ? 'memoryPublishAction' : 'memoryRevokeAction')}><p className="small muted">{t(action === 'review' ? 'memoryReviewInstructions' : action === 'publish' ? 'memoryPublishInstructions' : 'memoryRevokeInstructions')}</p><form onSubmit={event => { event.preventDefault(); void decide(action); }}><FormField label={t('memoryDecisionReason')} description={t('memoryReasonLimit')}><Textarea value={note} onChange={({ detail }) => setNote(detail.value)} disabled={busy} rows={3} /></FormField>{action !== 'revoke' && <div className="memory-acknowledgment"><Checkbox checked={acknowledged} disabled={busy} onChange={({ detail }) => setAcknowledged(detail.checked)}>{t('memoryReviewAcknowledgment')}</Checkbox></div>}<div className="form-action"><Button variant="primary" formAction="submit" loading={busy} disabled={!note.trim() || note.trim().length > 1000 || (action !== 'revoke' && !acknowledged)}>{t(action === 'review' ? 'memoryReviewButton' : action === 'publish' ? 'memoryPublishButton' : 'memoryRevokeButton')}</Button></div></form></Panel>}
    </aside></div>
  </>;
}

export default function Knowledge({ memoryId, referenceRevision, t }: { memoryId?: string; referenceRevision?: number; t: T }) {
  return memoryId ? <MemoryDetail key={memoryId} memoryId={memoryId} referenceRevision={referenceRevision} t={t} /> : <MemoryList t={t} />;
}

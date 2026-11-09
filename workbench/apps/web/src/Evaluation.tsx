import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import Spinner from '@cloudscape-design/components/spinner';
import { api, errorText } from './api';
import { nominalUsd, parseEvaluationSummary, percentage, seconds, type EvaluationSummary } from './evaluationSummary';
import type { Language, TranslationKey } from './i18n';
import SymptomComparison from './SymptomComparison';

type T = (key: TranslationKey) => string;
function Panel({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return <section className="panel"><div className="panel-heading"><h2>{title}</h2>{aside}</div><div className="panel-content">{children}</div></section>;
}
function Fact({ label, children }: { label: string; children: ReactNode }) { return <div className="fact"><dt>{label}</dt><dd>{children}</dd></div>; }

export default function Evaluation({ t, language, cases }: { t: T; language: Language; cases?: boolean }) {
  return cases ? <SymptomComparison language={language} /> : <RecordedEvaluation t={t} language={language} />;
}

type RcaevalComparison = { available: false } | { available: true; cohort: string; methods: {
  label: string; case_count: number; top_hits: [number, number, number]; avg_at_5: number;
}[] };

function BaroComparison({ language }: { language: Language }) {
  const [comparison, setComparison] = useState<RcaevalComparison>();
  const zh = language === 'zh';
  useEffect(() => {
    const controller = new AbortController();
    void api<RcaevalComparison>('/rcaeval-comparison', { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setComparison(result); })
      .catch(() => { if (!controller.signal.aborted) setComparison({ available: false }); });
    return () => controller.abort();
  }, []);
  const title = zh ? 'RCAEval 三种方法对比' : 'RCAEval method comparison';
  return <Panel title={title} aside={<Button href="#/errors/rcaeval-baro-0001" variant="inline-link">
    {zh ? '查看同一事故回放' : 'Open the incident replay'}</Button>}>
    {!comparison ? <p role="status">{zh ? '正在读取评估记录…' : 'Loading evaluation record…'}</p>
      : comparison.available ? <>
        <p>{zh ? '同一批 RE2 Online Boutique 开发案例，使用已知故障时间和同一服务定位评分规则。B1 使用已保存的模型输出。' :
          'The methods share the RE2 Online Boutique development cases, known fault onset, and service-localization scoring. B1 uses saved model outputs.'}</p>
        <div className="table-scroll eval-table"><table aria-label={title}>
          <thead><tr><th scope="col">{zh ? '方法' : 'Method'}</th><th scope="col">Top 1</th><th scope="col">Top 3</th><th scope="col">Top 5</th><th scope="col">Avg@5</th></tr></thead>
          <tbody>{comparison.methods.map(row => <tr key={row.label}><th scope="row">{row.label}</th>
            {row.top_hits.map((hit, index) => <td key={index}>{hit}/{row.case_count}</td>)}<td>{percentage(row.avg_at_5)}</td></tr>)}</tbody>
        </table></div>
      </> : <p>{zh ? '本地评估记录暂不可用。' : 'The local evaluation record is unavailable.'}</p>}
  </Panel>;
}

function RecordedEvaluation({ t, language }: { t: T; language: Language }) {
  const [summary, setSummary] = useState<EvaluationSummary | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const sequence = useRef(0);
  const alive = useRef(true);

  const refresh = useCallback(async () => {
    const request = ++sequence.current;
    setLoading(true);
    const current = () => alive.current && sequence.current === request;
    try {
      const evaluation = parseEvaluationSummary(await api<unknown>('/evaluation-summary'));
      if (current()) { setSummary(evaluation); setError(''); }
    } catch (cause) {
      if (current()) setError(errorText(cause));
    } finally {
      if (current()) setLoading(false);
    }
  }, []);
  useEffect(() => { alive.current = true; void refresh(); return () => { alive.current = false; sequence.current += 1; }; }, [refresh]);

  const recorded = summary?.available ? summary : null;
  return <>
    <div className="page-title"><div><div className="eyebrow">{t('evaluation')}</div><h1>{t('evalPageTitle')}</h1><p>{t('evalPageDescription')}</p></div><div className="evaluation-page-actions"><Button href="#/evaluation/cases" iconName="angle-right" iconAlign="right">{t('evalMatchedCases')}</Button><Button iconName="refresh" loading={loading} onClick={() => void refresh()}>{t('refresh')}</Button></div></div>
    <Alert type="info" header={t('evalScopeTitle')}>{t('evalScopeText')}</Alert>
    <div className="evaluation-layout">
      <div className="evaluation-record">
        <BaroComparison language={language} />
        {error && <Alert type="error" header={t('evalReadError')}>{error}{summary && <p>{t('evalStaleRecord')}</p>}</Alert>}
        {loading && !summary && <div className="loading"><Spinner /> {t('evalLoading')}</div>}
        {summary && !summary.available && <Panel title={t('evalUnavailable')}><p className="source-text muted small">{summary.reason}</p></Panel>}
        {recorded && <>
          <Panel title={t('evalRecordedExperiment')} aside={<span className="tag">{t('evalFrozen')}</span>}>
            <dl className="facts eval-metadata"><Fact label={t('evalCohort')}>{recorded.cohort_size} · RE2-OB</Fact><Fact label={t('model')}><code>{recorded.model_id}</code></Fact><Fact label={t('evalCompletedAt')}><time>{recorded.completed_at.replace('T', ' ').replace(/Z$/, ' UTC')}</time></Fact></dl>
            <p className="small muted eval-metric-definition">{t('evalMetricDefinition')}</p>
            <div className="table-scroll eval-table"><table><thead><tr><th>{t('evalMethod')}</th><th>{t('evalCompleted')}</th><th>AC@1</th><th>AC@3</th><th>AC@5</th><th>Avg@5</th><th>{t('evalNominalCost')}</th><th>{t('evalMedian')}</th></tr></thead><tbody>{recorded.arms.map(arm => <tr key={arm.id}>
              <td><strong>{arm.id}</strong><span className="eval-method-description">{arm.id === 'B1' ? t('evalB1') : arm.id === 'B2' ? t('evalB2') : t('evalRetainedMethod')}</span></td>
              <td><strong>{arm.completed} / {arm.planned}</strong><span className="eval-case-status">{arm.failed} {t('evalFailedCases')} · {arm.attempted} {t('evalAttemptedCases')}</span></td>
              <td className="eval-primary-metric">{percentage(arm.ac_at_1)}</td><td>{percentage(arm.ac_at_3)}</td><td>{percentage(arm.ac_at_5)}</td><td>{percentage(arm.avg_at_5)}</td><td>{nominalUsd(arm.nominal_usd)}</td><td>{seconds(arm.median_seconds)}</td>
            </tr>)}</tbody></table></div>
            <p className="eval-table-note">{t('evalDenominatorNote')}</p>
          </Panel>
          <Panel title={t('evalRecordedUsage')}>
            <dl className="facts"><Fact label={t('evalGenerationResponses')}>{recorded.usage.generations.toLocaleString('en-US')}</Fact><Fact label={t('evalInputTokens')}>{recorded.usage.input_tokens.toLocaleString('en-US')}</Fact><Fact label={t('evalOutputTokens')}>{recorded.usage.output_and_thinking_tokens.toLocaleString('en-US')}</Fact><Fact label={t('evalNominalCost')}>{nominalUsd(recorded.usage.nominal_usd)}</Fact><Fact label={t('evalUnresolved')}>{recorded.usage.unresolved_calls}</Fact></dl>
            <p className="small muted">{t('evalUsageDefinition')}</p>
          </Panel>
          <Panel title={t('evalLimits')}><ul className="plain-list source-text">{recorded.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></Panel>
          <Panel title={t('evalProvenance')}><p className="small muted">{t('evalProvenanceText')}</p><ol className="eval-sources">{recorded.provenance.map(source => <li key={`${source.path}-${source.sha256}`}><code>{source.path}</code><span>SHA-256</span><code className="eval-source-hash">{source.sha256}</code></li>)}</ol></Panel>
        </>}
      </div>
      <aside className="evaluation-allowance">
        <Panel title={t('evalInterpretation')}><ul className="plain-list"><li>{t('evalInterpretAccuracy')}</li><li>{t('evalInterpretCost')}</li><li>{t('evalInterpretTime')}</li></ul></Panel>
      </aside>
    </div>
  </>;
}

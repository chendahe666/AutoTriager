import { useEffect, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import Select from '@cloudscape-design/components/select';
import Spinner from '@cloudscape-design/components/spinner';
import type { Language } from './i18n';
import { parseSymptomComparison, type ComparisonArm, type ComparisonCase, type SymptomComparisonRecord } from './symptomComparisonRecord';
import './symptomComparison.css';

const comparisonPath = '/benchmark/symptom-comparison.json';

const labels = {
  en: {
    title: 'Same symptoms, different causes', intro: 'Compare three investigation methods on matched cases from the same benchmark.',
    back: 'Recorded evaluation', loading: 'Loading matched case results',
    missing: 'No matched-case result is installed.', failed: 'The saved matched-case result could not be read.',
    scope: 'RCAEval fault-injection benchmark', archive: 'Reanalysis of archived model predictions. Opening this page makes no model calls.',
    source: 'Benchmark source', sourceDate: 'Archived runs', selectionDate: 'Case selection',
    select: 'Select a matched pair', pair: 'Pair', reveal: 'Reveal scoring labels', hide: 'Hide scoring labels',
    overview: 'Shared symptoms', evidence: 'Observed telemetry', outputs: 'Recorded rankings', labels: 'Scoring label',
    review: 'Evaluator observation',
    top: 'Top three candidates', rest: 'Other ranked candidates', noCandidates: 'No candidate returned',
    outcomes: { completed: 'Completed', failed: 'Failed', missing: 'No saved result' },
    match: 'Ranked first', lower: 'Labeled service ranked', missed: 'Labeled service absent from displayed ranks',
    metrics: 'Matched cohort results', method: 'Method', answered: 'Completed', bothTop1: 'Both in pair at #1',
    denominator: 'Each accuracy denominator includes every case in the matched cohort, including missing or failed results.',
    protocol: 'Selection and limits', download: 'Download scored case data',
    armNames: { B0: 'Metrics only', B1: 'Structured evidence', B2: 'HolmesGPT investigation' },
  },
  zh: {
    title: '症状相似，根因不同', intro: '对照同一基准中匹配的案例，查看三种调查方法的表现。',
    back: '已记录评估', loading: '正在加载匹配案例结果',
    missing: '当前工作台尚未安装匹配案例结果。', failed: '无法读取已保存的匹配案例结果。',
    scope: 'RCAEval 故障注入基准', archive: '重新分析已归档的模型预测。打开此页面不会调用模型。',
    source: '基准来源', sourceDate: '归档运行日期', selectionDate: '案例选择日期',
    select: '选择一对匹配案例', pair: '案例对', reveal: '揭示评分标签', hide: '隐藏评分标签',
    overview: '共同症状', evidence: '实测遥测', outputs: '已记录排名', labels: '评分标签',
    review: '评分方复核',
    top: '前三名候选', rest: '其他已排名候选', noCandidates: '未返回候选',
    outcomes: { completed: '已完成', failed: '失败', missing: '没有已保存结果' },
    match: '正确服务排第一', lower: '正确服务排名', missed: '展示的名次中没有正确服务',
    metrics: '匹配案例组结果', method: '方法', answered: '已完成', bothTop1: '同组两例均为第 1 名',
    denominator: '准确率以该案例组的全部案例为分母，包括失败或缺失的结果。',
    protocol: '选例方法与限制', download: '下载已评分的案例数据',
    armNames: { B0: '只看指标', B1: '结构化证据', B2: 'HolmesGPT 调查' },
  },
} as const;

type Labels = (typeof labels)[Language];

function CaseColumn({ entry, arms, revealed, t }: { entry: ComparisonCase; arms: ComparisonArm[]; revealed: boolean; t: Labels }) {
  return <article className="symptom-case" aria-label={entry.id}>
    <header className="symptom-case-header">
      <span className="symptom-kicker">{entry.system}</span>
      <h3><code>{entry.id}</code></h3>
      <p>{entry.signal}</p>
      {revealed && <div className="symptom-case-truth"><span>{t.labels}</span><code>{entry.truth}</code>{entry.fault && <span>· {entry.fault}</span>}</div>}
    </header>
    {entry.evidence.length > 0 && <details className="symptom-evidence">
      <summary>{t.evidence} · {entry.evidence.length}</summary>
      <dl>{entry.evidence.map(({ source, detail }, index) => <div key={`${source}-${index}`}><dt>{source}</dt><dd>{detail}</dd></div>)}</dl>
    </details>}
    <div className="symptom-predictions">
      <h4>{t.outputs}</h4>
      {arms.map(arm => {
        const prediction = entry.predictions.find(item => item.arm_id === arm.id)!;
        const rank = prediction.reference_rank ?? (prediction.ranking.indexOf(entry.truth) + 1);
        const ranked = prediction.ranking.slice(0, 3);
        const assessment = prediction.status !== 'completed' ? t.outcomes[prediction.status] : rank === 1 ? t.match : rank ? `${t.lower} #${rank}` : t.missed;
        return <div className="symptom-prediction" key={arm.id}>
          <div className="symptom-prediction-method"><code>{arm.id}</code> {t.armNames[arm.id]}</div>
          {ranked.length ? <ol className="symptom-ranking" aria-label={t.top}>
            {ranked.map(service => <li key={service} className={revealed && service === entry.truth ? 'symptom-hit' : undefined}><code>{service}</code></li>)}
          </ol> : <p className="small muted">{prediction.status === 'completed' ? t.noCandidates : t.outcomes[prediction.status]}</p>}
          {prediction.ranking.length > 3 && <details>
            <summary>{t.rest}</summary>
            <ol className="symptom-ranking" start={4}>{prediction.ranking.slice(3).map(service => <li key={service} className={revealed && service === entry.truth ? 'symptom-hit' : undefined}><code>{service}</code></li>)}</ol>
          </details>}
          {revealed && <>
            <span className={`symptom-assessment ${rank === 1 ? 'symptom-hit' : ''}`}>{assessment}</span>
            {prediction.observation && <p className="small symptom-observation">{prediction.observation}</p>}
          </>}
        </div>;
      })}
    </div>
    {revealed && entry.review_note && <aside className="symptom-review"><h4>{t.review}</h4><p>{entry.review_note}</p></aside>}
  </article>;
}

function CohortResults({ record, t }: { record: SymptomComparisonRecord; t: Labels }) {
  const fraction = (count: number) => `${(100 * count / record.cohort_size).toFixed(1)}%`;
  const pairScoreAvailable = record.arms.some(arm => arm.both_services_top1 !== undefined);
  return <section className="panel symptom-results" aria-label={t.metrics}>
    <div className="panel-heading"><h2>{t.metrics} <span className="count">n = {record.cohort_size}</span></h2></div>
    <div className="panel-content">
      <div className="table-scroll eval-table"><table>
        <thead><tr><th scope="col">{t.method}</th><th scope="col">{t.answered}</th><th scope="col">AC@1</th><th scope="col">AC@3</th>{pairScoreAvailable && <th scope="col">{t.bothTop1}</th>}</tr></thead>
        <tbody>{record.arms.map(arm => <tr key={arm.id}>
          <td><strong>{arm.id} · {t.armNames[arm.id]}</strong><span className="eval-method-description">{arm.label}</span>{arm.run_mode && <span className="eval-case-status">{arm.run_mode}</span>}</td>
          <td>{arm.completed} / {record.cohort_size}</td>
          <td><strong>{fraction(arm.top1_correct)}</strong><span className="eval-case-status">{arm.top1_correct} / {record.cohort_size}</span></td>
          <td><strong>{fraction(arm.top3_correct)}</strong><span className="eval-case-status">{arm.top3_correct} / {record.cohort_size}</span></td>
          {pairScoreAvailable && <td>{arm.both_services_top1 === undefined ? '—' : `${arm.both_services_top1} / ${Math.floor(record.cohort_size / 2)}`}</td>}
        </tr>)}</tbody>
      </table></div>
      <p className="eval-table-note">{t.denominator}</p>
    </div>
  </section>;
}

export default function SymptomComparison({ language }: { language: Language }) {
  const t = labels[language];
  const [record, setRecord] = useState<SymptomComparisonRecord | null>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'missing' | 'failed'>('loading');
  const [selectedPair, setSelectedPair] = useState('');
  const [revealed, setRevealed] = useState(false);

  useEffect(() => {
    const abort = new AbortController();
    void (async () => {
      try {
        const response = await fetch(comparisonPath, { signal: abort.signal });
        if (abort.signal.aborted) return;
        if (response.status === 404) { setStatus('missing'); return; }
        if (!response.ok) throw new Error('Could not load the comparison record.');
        const value = parseSymptomComparison(await response.json());
        if (abort.signal.aborted) return;
        setRecord(value);
        setSelectedPair(value.featured_pair_id ?? value.pairs[0].id);
        setStatus('ready');
      } catch {
        if (!abort.signal.aborted) setStatus('failed');
      }
    })();
    return () => abort.abort();
  }, []);

  const pair = record?.pairs.find(item => item.id === selectedPair);
  const options = record?.pairs.map((item, index) => ({ value: item.id, label: `${t.pair} ${index + 1}`, description: item.symptom })) ?? [];
  return <div className="symptom-comparison">
    <a className="back-link" href="#/evaluation">← {t.back}</a>
    <div className="page-title"><div><div className="eyebrow">{t.scope}</div><h1>{t.title}</h1><p>{t.intro}</p></div></div>
    {status === 'loading' && <div className="loading"><Spinner /> {t.loading}</div>}
    {status === 'missing' && <Alert type="info">{t.missing}</Alert>}
    {status === 'failed' && <Alert type="error">{t.failed}</Alert>}
    {record && pair && <>
      <div className="symptom-provenance">
        <div><span className="tag">{record.dataset_label}</span><span>{t.archive}</span></div>
        <a href={record.dataset_url} target="_blank" rel="noreferrer">{t.source} ↗</a>
      </div>
      <section className="panel symptom-pair" aria-label={t.overview}>
        <div className="symptom-controls">
          <Select ariaLabel={t.select} options={options} selectedOption={options.find(option => option.value === selectedPair) ?? null} onChange={({ detail }) => {
            const next = detail.selectedOption.value;
            if (next) { setSelectedPair(next); setRevealed(false); }
          }} />
          <Button onClick={() => setRevealed(value => !value)}>{revealed ? t.hide : t.reveal}</Button>
        </div>
        <div className="symptom-common"><span className="symptom-kicker">{t.overview}</span><h2>{pair.symptom}</h2><p>{pair.match_reason}</p></div>
        <div className="symptom-columns">{pair.cases.map(entry => <CaseColumn key={entry.id} entry={entry} arms={record.arms} revealed={revealed} t={t} />)}</div>
      </section>
      <CohortResults record={record} t={t} />
      <section className="panel symptom-protocol" aria-label={t.protocol}>
        <div className="panel-content">
          <details><summary>{t.protocol}</summary>
            <p>{record.methodology}</p>
            <ul className="plain-list">{record.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul>
            {(record.source_run_date || record.selection_date) && <p className="small muted">{[record.source_run_date && `${t.sourceDate} ${record.source_run_date}`, record.selection_date && `${t.selectionDate} ${record.selection_date}`].filter(Boolean).join(' · ')}</p>}
            <a href={comparisonPath} download>{t.download}</a>
          </details>
        </div>
      </section>
    </>}
  </div>;
}

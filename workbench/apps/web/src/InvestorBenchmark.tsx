import { useEffect, useState, type CSSProperties } from 'react';
import type { Language } from './i18n';
import { parseSymptomComparison, type SymptomComparisonRecord } from './symptomComparisonRecord';
import './investorBenchmark.css';

const copy = {
  en: {
    eyebrow: 'MEASURED ON PUBLIC DATA',
    title: 'Same symptom. Different fault.',
    description: 'We compared archived results on the same cases, pairing similar frontend slowdowns with different injected fault services.',
    methods: { B0: 'Metrics only', B1: 'Structured evidence', B2: 'HolmesGPT investigation' },
    implementation: { B0: 'RCAEval NSigma', B1: 'Fixed-context Gemini', B2: 'HolmesGPT + Gemini' },
    method: 'Method', top: 'Top-1 accuracy', complete: 'Valid rankings', paired: 'Both first in pair',
    cases: 'cases', pairs: 'matched pairs',
    detail: 'Explore cases and evidence', loading: 'Loading recorded results…', unavailable: 'Recorded results are currently unavailable.',
    note: 'Top-1 counts cases with the injected service ranked first. Failed investigations count as misses. This retrospective RE2-OB slice measures service localization.',
    unavailableValue: 'Not reported',
  },
  zh: {
    eyebrow: '公开数据实测',
    title: '相似的症状，不同的故障服务',
    description: '在同一批案例上对照三种方案的归档结果。每对的前端延迟症状相近，故障注入的服务不同。',
    methods: { B0: '只看指标', B1: '结构化证据', B2: 'HolmesGPT 调查' },
    implementation: { B0: 'RCAEval NSigma', B1: '固定证据 Gemini', B2: 'HolmesGPT + Gemini' },
    method: '方案', top: '首位命中率', complete: '有效排名', paired: '同对两例均命中首位',
    cases: '案例', pairs: '匹配对',
    detail: '查看案例与证据', loading: '正在加载实测结果…', unavailable: '暂时无法读取实测结果。',
    note: '首位命中率统计注入故障的服务排第一的案例比例，未完成也计入分母。这是 RCAEval RE2-OB 的回溯切片，评价故障服务定位。',
    unavailableValue: '未报告',
  },
} as const;

type Result = { status: 'loading' | 'unavailable' } | { status: 'ready'; record: SymptomComparisonRecord };

export default function InvestorBenchmark({ language }: { language: Language }) {
  const t = copy[language];
  const [result, setResult] = useState<Result>({ status: 'loading' });

  useEffect(() => {
    const request = new AbortController();
    async function load() {
      try {
        const response = await fetch('/benchmark/symptom-comparison.json', { signal: request.signal });
        if (!response.ok) throw new Error('Could not load benchmark results.');
        const record = parseSymptomComparison(await response.json());
        if (!request.signal.aborted) setResult({ status: 'ready', record });
      } catch {
        if (!request.signal.aborted) setResult({ status: 'unavailable' });
      }
    }
    void load();
    return () => request.abort();
  }, []);

  const record = result.status === 'ready' ? result.record : undefined;
  const pairCount = record?.pairs.length ?? 0;
  const completePairList = record?.cohort_size === 2 * pairCount;
  const percent = new Intl.NumberFormat(language === 'zh' ? 'zh-CN' : 'en-US', { style: 'percent', maximumFractionDigits: 1 });

  return <section className="investor-benchmark" aria-labelledby="investor-benchmark-title">
    <div className="investor-benchmark-heading">
      <div>
        <p className="investor-benchmark-kicker"><span aria-hidden="true" />{t.eyebrow}</p>
        <h2 id="investor-benchmark-title">{t.title}</h2>
        <p className="investor-benchmark-description">{t.description}</p>
      </div>
      {record && <dl className="investor-benchmark-cohort">
        <div><dt>{t.cases}</dt><dd>{record.cohort_size}</dd></div>
        <div><dt>{t.pairs}</dt><dd>{pairCount}</dd></div>
      </dl>}
    </div>
    {!record && <p className="investor-benchmark-loading" role="status">{result.status === 'loading' ? t.loading : t.unavailable}</p>}
    {record && <div className="investor-benchmark-table-scroll">
      <table className="investor-benchmark-table">
        <thead><tr><th scope="col">{t.method}</th><th scope="col">{t.top}</th><th scope="col">{t.complete}</th><th scope="col">{t.paired}</th></tr></thead>
        <tbody>{record.arms.map(arm => {
          const score = arm.top1_correct / record.cohort_size;
          return <tr key={arm.id} data-method={arm.id}>
            <th scope="row"><div className="investor-benchmark-method"><span className="investor-benchmark-arm">{arm.id}</span><span><strong>{t.methods[arm.id]}</strong><small>{t.implementation[arm.id]}</small></span></div></th>
            <td>
              <div className="investor-benchmark-value"><strong>{percent.format(score)}</strong><span>{arm.top1_correct} / {record.cohort_size}</span></div>
              <div className="investor-benchmark-meter" aria-hidden="true" style={{ '--benchmark-grid-size': `${100 / record.cohort_size}%` } as CSSProperties}>
                <span style={{ width: `${score * 100}%` }} />
              </div>
            </td>
            <td><span className="investor-benchmark-count">{arm.completed}<small> / {record.cohort_size}</small></span></td>
            <td>{!completePairList || arm.both_services_top1 === undefined ? t.unavailableValue : <span className="investor-benchmark-count">{arm.both_services_top1}<small> / {pairCount}</small></span>}</td>
          </tr>;
        })}</tbody>
      </table>
    </div>}
    <div className="investor-benchmark-footer">
      <p>{t.note}</p>
      <a href="#/evaluation/cases">{t.detail}<span aria-hidden="true">↗</span></a>
    </div>
  </section>;
}

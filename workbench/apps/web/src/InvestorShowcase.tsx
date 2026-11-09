import { useEffect, useState } from 'react';
import type { Language } from './i18n';
import InvestorBenchmark from './InvestorBenchmark';
import incident from './showcaseIncident.json';
import './investorShowcase.css';

const copy = {
  en: {
    nav: 'Incident response', research: 'Evaluation', back: 'Workbench', replay: 'Play the incident', pause: 'Pause replay',
    restart: 'Replay incident', eyebrow: 'INCIDENT RESPONSE / 01', title: 'The next move, backed by evidence.',
    intro: 'AutoTriager brings the alert, the investigation and the recovery check into one workspace for on-call engineers.',
    stage: 'Incident replay', source: 'Recorded controlled lab', scenario: 'Checkout degradation',
    location: 'ONLINE STORE  /  PAYMENTS', chart: 'Checkout latency', chartNote: 'Recorded request samples',
    threshold: 'Threshold', before: 'At alert', after: 'After recovery', sample: 'Request',
    payment: 'Time in payment call', checkout: 'Total checkout time', recovery: 'Fresh requests passed',
    recap: 'Verified occurrences', configured: 'Payment delay setting',
    next: 'Next', complete: 'Recovery verified', chartSource: 'One request at alert and three fresh requests after the approved change.',
    data: 'Two evidence sources. Two different questions.',
    dataNote: 'The controlled lab shows recovery. Public fault injections test whether an investigation ranks the correct service.',
    watch: 'ALERT', find: 'INVESTIGATE', fix: 'RECOVER', learn: 'PREVENT',
    steps: [
      ['The alert opens the investigation.', 'A Prometheus rule observed checkout above its configured latency threshold. The investigation starts with the measured value and the rule that fired.'],
      ['Follow the time spent in payment.', 'The matching checkout request contains the observed payment call. It gives the engineer a specific dependency to investigate.'],
      ['Approve one change. Check fresh requests.', 'In the recorded lab run, an operator approved restoring the payment delay setting. AutoTriager checked three new requests against the original threshold.'],
      ['Two recoveries make a case for prevention.', 'Two independent episodes with verified recovery support a recommendation to monitor payment degradation and rehearse containment.'],
    ],
  },
  zh: {
    nav: '事故响应', research: '实验评估', back: '工作台', replay: '播放事故', pause: '暂停回放',
    restart: '重新回放', eyebrow: '事故响应 / 01', title: '下一步怎么做，证据说了算。',
    intro: 'AutoTriager 把报警、调查和恢复核查放进同一个工作台，帮助值班工程师做出有依据的决定。',
    stage: '事故回放', source: '受控实验实测回放', scenario: '结账请求变慢',
    location: '在线商店  /  支付', chart: '结账延迟', chartNote: '实测请求样本',
    threshold: '报警阈值', before: '触发报警', after: '缓解之后', sample: '请求',
    payment: '支付调用耗时', checkout: '结账总耗时', recovery: '新请求通过',
    recap: '已验证的独立事故', configured: '支付延迟设置',
    next: '下一步', complete: '已验证恢复', chartSource: '报警时的一个请求，以及批准变更后的三个新请求。',
    data: '两组证据回答两个问题。',
    dataNote: '受控实验验证恢复闭环。公开故障注入数据检验调查能否把正确的服务排在前面。',
    watch: '发现报警', find: '调查证据', fix: '验证恢复', learn: '降低复发',
    steps: [
      ['报警自动打开调查。', 'Prometheus 发现结账延迟超过预设阈值。调查从触发规则和当时的实测值开始。'],
      ['沿着请求找到支付调用。', '同一笔结账请求保留了支付调用的耗时，为值班工程师提供一个明确的调查方向。'],
      ['批准一项变更，检查新请求。', '在记录的实验里，操作员批准恢复支付延迟设置。随后用三个新请求和原来的阈值核查恢复。'],
      ['用两次已验证的事故提出预防建议。', '两次独立事故都验证恢复后，系统才提出监控支付性能退化、演练隔离措施的建议。'],
    ],
  },
} as const;

type Copy = (typeof copy)[Language];

function SignalMark() {
  return <svg aria-hidden="true" viewBox="0 0 28 28" width="28" height="28"><path d="M3 18 10 11l5 4 9-10M18 5h6v6" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinejoin="round" strokeLinecap="round"/><path d="M3 24h22" stroke="currentColor" opacity=".35" /></svg>;
}

function LatencyChart({ t, phase }: { t: Copy; phase: number }) {
  const y = (value: number) => 224 - value * 0.65;
  const recoveryX = [353, 447, 541];
  const revealRecovery = phase >= 2;
  return <figure className="sc-chart">
    <div className="sc-chart-title"><div><h3>{t.chart}</h3><span>{t.chartNote}</span></div><span className="sc-chart-legend"><i />{t.threshold}</span></div>
    <svg viewBox="0 0 606 270" role="img" aria-label={`${t.chart}. ${t.before}: ${incident.alertMs.toFixed(2)} ms. ${t.threshold}: ${incident.thresholdMs} ms.${revealRecovery ? ` ${t.after}: ${incident.recoveryMs.map(n => n.toFixed(2)).join(', ')} ms.` : ''}`}>
      <defs><radialGradient id="sc-alert-halo"><stop stopColor="#ff8b73" stopOpacity=".42"/><stop offset="1" stopColor="#ff8b73" stopOpacity="0"/></radialGradient></defs>
      {[0, 100, 200, 300].map(tick => <g key={tick}><line x1="63" x2="580" y1={y(tick)} y2={y(tick)} className={tick === incident.thresholdMs ? 'sc-threshold' : 'sc-gridline'}/><text x="51" y={y(tick) + 4} textAnchor="end">{tick}</text></g>)}
      <text x="14" y="15">ms</text>
      <line x1="251" x2="251" y1="29" y2="224" className="sc-chart-divider"/>
      <circle cx="152" cy={y(incident.alertMs)} r="42" fill="url(#sc-alert-halo)" />
      <circle cx="152" cy={y(incident.alertMs)} r="6" className="sc-alert-dot" />
      <text x="152" y={y(incident.alertMs) - 16} textAnchor="middle" className="sc-point-alert">{incident.alertMs.toFixed(1)}</text>
      <text x="152" y="250" textAnchor="middle">{t.before}</text>
      {revealRecovery ? <g className="sc-recovery-points">
        <polyline fill="none" points={incident.recoveryMs.map((value, i) => `${recoveryX[i]},${y(value)}`).join(' ')} className="sc-recovery-line" />
        {incident.recoveryMs.map((value, i) => <g key={i}>
          <circle cx={recoveryX[i]} cy={y(value)} r="6" className="sc-recovery-dot" />
          <text x={recoveryX[i]} y={y(value) - 16} textAnchor="middle" className="sc-point-recovery">{value.toFixed(1)}</text>
          <text x={recoveryX[i]} y="250" textAnchor="middle">{t.sample} {i + 1}</text>
        </g>)}
      </g> : <text x="446" y="129" textAnchor="middle" className="sc-muted-point">{t.after}</text>}
    </svg>
    <figcaption>{t.chartSource}</figcaption>
  </figure>;
}

export default function InvestorShowcase({ language, onToggleLanguage }: { language: Language; onToggleLanguage: () => void }) {
  const t = copy[language];
  const [phase, setPhase] = useState(0);
  const [playing, setPlaying] = useState(false);
  const steps = [t.watch, t.find, t.fix, t.learn];

  useEffect(() => {
    if (!playing) return;
    const timer = window.setTimeout(() => {
      if (phase === steps.length - 1) setPlaying(false);
      else setPhase(phase + 1);
    }, 6500);
    return () => window.clearTimeout(timer);
  }, [phase, playing, steps.length]);

  function select(index: number) {
    setPhase(index);
    setPlaying(false);
  }

  const cta = playing ? t.pause : phase === 0 ? t.replay : t.restart;
  return <div className="showcase" lang={language}>
    <header className="sc-nav"><a href="#/showcase" className="sc-wordmark"><SignalMark />AutoTriager</a>
      <nav aria-label="Showcase"><a href="#/evaluation/cases">{t.research}</a><a href="#/monitoring">{t.back}</a><button type="button" onClick={onToggleLanguage} aria-label={language === 'en' ? 'Switch interface to Chinese' : '切换界面为英文'}>{language === 'en' ? '中文' : 'EN'}</button></nav>
    </header>
    <main className="sc-content" id="showcase-main">
      <section className="sc-hero" aria-labelledby="sc-title">
        <div><p className="sc-kicker"><span /> {t.eyebrow}</p><h1 id="sc-title">{t.title}</h1></div>
        <div className="sc-hero-aside"><p>{t.intro}</p><button className="sc-primary" type="button" onClick={() => {
          if (playing) setPlaying(false);
          else { setPhase(0); setPlaying(true); }
        }}><span aria-hidden="true">{playing ? 'Ⅱ' : '▶'}</span>{cta}</button></div>
      </section>
      <section className="sc-desk" aria-label={t.stage}>
        <div className="sc-desk-header"><div className="sc-desk-name"><span className="sc-scenario-dot" data-verified={phase >= 2}/><strong>{t.scenario}</strong><span className="sc-location">{t.location}</span></div><span className="sc-source">{t.source}</span></div>
        <div className="sc-phase-nav" aria-label={t.stage}>{steps.map((step, index) => <button type="button" key={step} className={phase === index ? 'active' : ''} aria-pressed={phase === index} onClick={() => select(index)}><span>0{index + 1}</span>{step}</button>)}</div>
        <div className="sc-desk-body">
          <div className="sc-stage-copy" key={phase} aria-live="polite">
            <p className="sc-kicker">0{phase + 1} / 04</p><h2>{t.steps[phase][0]}</h2><p className="sc-stage-detail">{t.steps[phase][1]}</p>
            <div className="sc-measure">
              {phase === 0 && <><span>{t.checkout}</span><strong className="sc-warm">{incident.alertMs.toFixed(1)} <small>ms</small></strong></>}
              {phase === 1 && <><span>{t.payment}</span><strong className="sc-warm">{incident.paymentMs.toFixed(1)} <small>ms</small></strong><meter className="sc-payment-share" min={0} max={incident.alertMs} value={incident.paymentMs} aria-label={t.payment} /><span className="sc-small-fact">{t.checkout} · {incident.alertMs.toFixed(1)} ms</span></>}
              {phase === 2 && <><span>{t.configured}</span><strong>{incident.restoreFromMs} <i>→</i> {incident.restoreToMs} <small>ms</small></strong><span className="sc-small-fact">{t.recovery} · {incident.recoveryMs.length}/{incident.recoveryMs.length}</span></>}
              {phase === 3 && <><span>{t.recap}</span><strong>0{incident.episodes}</strong><span className="sc-small-fact">{incident.relatedAlertMs.map(n => `${n.toFixed(1)} ms`).join('  /  ')}</span></>}
            </div>
            {phase < 3 ? <button type="button" className="sc-next" onClick={() => select(phase + 1)}>{t.next} <span aria-hidden="true">↗</span></button> : <span className="sc-verified"><i />{t.complete}</span>}
          </div>
          <LatencyChart t={t} phase={phase} />
        </div>
      </section>
      <section className="sc-evaluation" aria-labelledby="sc-evaluation-title">
        <div className="sc-eval-intro"><p className="sc-kicker"><span /> 02 / {t.research.toUpperCase()}</p><h2 id="sc-evaluation-title">{t.data}</h2><p>{t.dataNote}</p></div>
        <InvestorBenchmark language={language} />
      </section>
    </main>
    <footer className="sc-footer"><span>AutoTriager</span><span>{incident.sourceRun}</span><a href="#/evaluation/cases">{t.research} ↗</a></footer>
  </div>;
}

import { useEffect, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import Tabs from '@cloudscape-design/components/tabs';
import { api, id } from './api';
import type { Language } from './i18n';
import './monitoringConsole.css';

type Frame = {
  start: string;
  closed_at: string;
  target_end: string;
  value: number;
  forecast: number | null;
  state: 'observed_breach' | 'forecast_risk' | 'below_threshold';
  incident_id?: string;
};
type Score = {
  pairs: number;
  mae: { ar1: number | null; persistence: number | null };
  eligible_crossings: number;
  crossings_warned: number;
  false_forecast_warnings: number;
};
type LiveAlert = {
  incident_id: string;
  triggered_at: string;
  service: string;
  metric: string;
  value: number;
  threshold: number;
  unit: string;
};
type AlertConnection = 'connected' | 'unavailable' | 'connecting';
type Series = {
  mode: 'replay' | 'live';
  service: string;
  metric: string;
  unit: string;
  bin_seconds: number;
  threshold: number;
  freshness?: 'fresh' | 'stale' | 'gap';
  source_url?: string;
  source_paper?: string;
  alerts?: LiveAlert[];
  alert_connection?: AlertConnection;
  cases: { id: string; frames: Frame[]; source_url?: string }[];
  evaluation?: { training_bins: number; healthy_holdout: Score; incident_replay: Score };
};
type Monitoring = Series | { mode: 'live_unavailable' | 'replay_unavailable' | 'replay_invalid'; alerts?: LiveAlert[]; alert_connection?: AlertConnection };

const labels = {
  en: {
    title: 'Monitoring', metrics: 'Data monitoring', warnings: 'Warnings', loading: 'Loading monitoring data…',
    error: 'The monitoring API could not be reached.', live_unavailable: 'The configured live metrics source is unavailable. Try again after checking the connection.',
    replay_unavailable: 'The PetShop replay archive is unavailable. See the setup instructions to download the public recording.', replay_invalid: 'The PetShop archive could not be verified.',
    replay: 'Recorded replay', live: 'Live metrics', replayNote: 'Public PetShop testbed recording. The controls advance the recorded clock.',
    liveNote: 'Monitoring a configured Prometheus series. Updates arrive after each completed sample interval.',
    stale: 'The latest completed interval is missing. Forecast warnings are paused. Recorded breaches are still visible.',
    gap: 'A sampling interval is missing before the latest sample. Forecast warnings resume after consecutive samples arrive.',
    paused: 'Paused pending fresh samples',
    case: 'Recording', play: 'Play replay', pause: 'Pause', next: 'Next sample', reset: 'Restart', refresh: 'Refresh',
    current: 'Latest observation', predicted: 'Next interval forecast', threshold: 'Threshold', meanLatency: 'Mean latency',
    observed_breach: 'Observed breach', forecast_risk: 'Forecast risk', below_threshold: 'At or below threshold',
    noWarnings: 'No warning at this point.', data: 'Observed and forecast values', waiting: 'Warming up',
    scope: 'Signal location', scopeNote: 'The observed service and metric establish where the signal appeared. Locating the cause requires more evidence.',
    evidence: 'Evidence at decision time', latest: 'Completed interval', horizon: 'Forecast interval ends', raw: 'Open recorded measurements',
    checks: 'Suggested checks', check1: 'Confirm the reading on the source dashboard and check for missing or stale samples.',
    check2: 'Compare request volume, errors, and latency over the same interval. Look for a concurrent change in traffic or deployments.',
    check3: 'Inspect traces for slow upstream or downstream calls. Carry the time and service into incident investigation if impact is confirmed.',
    action: 'Read-only recommendations. The engineer decides what to investigate or change.',
    evaluation: 'Forecast validation', training: 'Earlier healthy samples used for model fit', group: 'Evaluation set', pairs: 'Forecasts',
    ar1: 'AR(1) MAE', persistence: 'Persistence MAE', crossing: 'Crossings warned / eligible', falseAlerts: 'False warnings',
    healthyHoldout: 'Later healthy recording', incidentReplay: 'Three injected cases', evaluationNote: 'A warning requires an observed value at or below the threshold and a forecast strictly above it. This pilot tests next-interval threshold crossings in one metric. Its threshold is the 95th percentile of the earlier healthy data.',
    sources: 'Public recording', paper: 'Dataset paper', noData: 'No completed samples are available yet.', window: 'seconds per sample',
    experimental: 'Experimental forecast',
    openInvestigation: 'Open investigation',
    liveAlerts: 'Received live alerts', alertSource: 'Prometheus firing rule · observed threshold breach',
    triggeredAt: 'Triggered at', observation: 'Observed value', showLiveAlerts: 'View received live alerts',
    alertConnecting: 'Waiting for the first Prometheus alert check.',
    alertUnavailable: 'Prometheus alert rules could not be checked. Received alerts remain available.',
    selectedAlert: 'Selected alert', selectedAlertTime: 'Selected alert time',
    liveNotConfigured: 'Live monitoring is not configured for this workspace.',
  },
  zh: {
    title: '监控台', metrics: '数据监控', warnings: '预警信息', loading: '正在加载监控数据…',
    error: '无法连接监控服务。', live_unavailable: '当前配置的实时指标源不可用，请检查连接后重试。',
    replay_unavailable: 'PetShop 回放归档尚不可用。请按安装说明下载公开记录。', replay_invalid: 'PetShop 归档校验失败。',
    replay: '历史回放', live: '实时指标', replayNote: '来源为 PetShop 公开测试环境的实测记录，可逐步推进回放时间。',
    liveNote: '正在监控已配置的 Prometheus 指标。每个采样区间结束后更新。',
    stale: '最近一个完整区间的采样缺失。暂停预测预警，已记录的越线仍可查看。',
    gap: '最新样本与上一条之间有缺口。收到连续样本后恢复预测预警。',
    paused: '等待新样本，暂时暂停',
    case: '选择记录', play: '播放', pause: '暂停', next: '下一条', reset: '重新开始', refresh: '刷新',
    current: '最新观测', predicted: '下一区间预测', threshold: '阈值', meanLatency: '平均延迟',
    observed_breach: '已观测超阈', forecast_risk: '预测超阈风险', below_threshold: '未超阈',
    noWarnings: '截至当前无预警。', data: '观测与预测值', waiting: '历史样本积累中',
    scope: '信号位置', scopeNote: '服务与指标说明异常出现在哪里。判断根因还需其他证据。',
    evidence: '决策时可用的证据', latest: '已完成采样区间', horizon: '预测区间结束', raw: '查看原始测量记录',
    checks: '建议排查步骤', check1: '先在指标源确认读数，检查样本是否缺失或过期。',
    check2: '查看同期请求量、错误率和延迟，核对流量或部署是否同时发生变化。',
    check3: '查看调用追踪，识别慢调用。如果确认用户受到影响，将服务与时间范围带入事故调查。',
    action: '建议仅供核查。工程师决定下一步调查或操作。',
    evaluation: '预测验证', training: '用于拟合的早期健康样本', group: '评估数据', pairs: '预测次数',
    ar1: 'AR(1) 平均绝对误差', persistence: '沿用最新值的平均绝对误差', crossing: '成功预警 / 可预警跨阈', falseAlerts: '误报',
    healthyHoldout: '后半段健康记录', incidentReplay: '三个注入故障案例', evaluationNote: '只有当前值未超阈且预测值超阈才会发出预警。本轮评价仅覆盖单指标的下一区间跨阈，阈值为早期健康数据的第 95 百分位。',
    sources: '公开记录', paper: '数据集论文', noData: '暂时没有完整采样。', window: '秒 / 样本',
    experimental: '实验性预测',
    openInvestigation: '打开报错调查',
    liveAlerts: '收到的实时报警', alertSource: 'Prometheus 报警规则 · 实测越线',
    triggeredAt: '触发时间', observation: '实测值', showLiveAlerts: '查看收到的实时报警',
    alertConnecting: '正在等待首次检查 Prometheus 报警规则。',
    alertUnavailable: '暂时无法检查 Prometheus 报警规则，仍可查看已收到的报警。',
    selectedAlert: '当前事故报警', selectedAlertTime: '当前事故触发时间',
    liveNotConfigured: '当前工作空间未配置实时监控。',
  },
} as const;

type Copy = typeof labels[Language];
type OpenIncident = (caseId: string) => void;
const time = (value: string) => value.replace('T', ' ').replace('+00:00', 'Z');
const number = (value: number, language: Language) => value.toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US', { maximumFractionDigits: 3 });

function OpenIncidentButton({ caseId, copy, onOpenIncident }: { caseId: string; copy: Copy; onOpenIncident?: OpenIncident }) {
  return <Button href={`#/errors/${id(caseId)}`} onClick={event => {
    const { button, ctrlKey, shiftKey, altKey, metaKey } = event.detail;
    if (!onOpenIncident || button !== 0 || ctrlKey || shiftKey || altKey || metaKey) return;
    event.preventDefault();
    onOpenIncident(caseId);
  }}>{copy.openInvestigation}</Button>;
}

function InvestigationLink({ frame, copy, onOpenIncident }: { frame: Frame; copy: Copy; onOpenIncident?: OpenIncident }) {
  if (frame.state !== 'observed_breach' || !frame.incident_id) return null;
  return <div className="monitoring-investigation"><OpenIncidentButton caseId={frame.incident_id} copy={copy} onOpenIncident={onOpenIncident} /></div>;
}

function AlertReading({ alert, copy, language }: { alert: LiveAlert; copy: Copy; language: Language }) {
  return <>{copy.observation} {number(alert.value, language)} {alert.unit} · {copy.threshold} {number(alert.threshold, language)} {alert.unit}</>;
}

function LiveAlerts({ alerts, copy, language, selectedIncidentId, embedded, onOpenIncident }: {
  alerts: LiveAlert[]; copy: Copy; language: Language; selectedIncidentId?: string; embedded?: boolean; onOpenIncident?: OpenIncident;
}) {
  if (alerts.length === 0) return null;
  const sorted = [...alerts].sort((a, b) => Date.parse(b.triggered_at) - Date.parse(a.triggered_at));
  return <section className="monitoring-panel monitoring-alerts">
    {embedded ? <h3>{copy.liveAlerts}</h3> : <h2>{copy.liveAlerts}</h2>}
    <ul>{sorted.map(alert => <li key={alert.incident_id} data-selected={alert.incident_id === selectedIncidentId || undefined}>
      <div>
        <strong>{alert.service} · {alert.metric}</strong>
        <p>{copy.alertSource}</p>
        <p>{copy.triggeredAt} · <time dateTime={alert.triggered_at}>{time(alert.triggered_at)}</time></p>
        <p><AlertReading alert={alert} copy={copy} language={language} /></p>
      </div>
      <div className="monitoring-alert-actions">
        {alert.incident_id === selectedIncidentId && <span className="monitoring-selected-label" aria-current="true">{copy.selectedAlert}</span>}
        <OpenIncidentButton caseId={alert.incident_id} copy={copy} onOpenIncident={onOpenIncident} />
      </div>
    </li>)}</ul>
  </section>;
}

function AlertConnectionNotice({ state, copy }: { state?: AlertConnection; copy: Copy }) {
  if (state === 'unavailable') return <Alert type="warning">{copy.alertUnavailable}</Alert>;
  if (state === 'connecting') return <Alert type="info">{copy.alertConnecting}</Alert>;
  return null;
}

function Timeline({ frames, data, copy, language, selectedAlert }: {
  frames: Frame[]; data: Series; copy: Copy; language: Language; selectedAlert?: LiveAlert;
}) {
  const last = frames.at(-1)!;
  const duration = Date.parse(last.target_end) - Date.parse(frames[0].closed_at);
  const values = [data.threshold, last.forecast ?? last.value, ...frames.map(frame => frame.value)];
  const minimum = Math.min(0, ...values);
  const maximum = Math.max(0.001, ...values);
  const range = (maximum - minimum) * 1.1;
  const x = (when: string) => 65 + (Date.parse(when) - Date.parse(frames[0].closed_at)) * 525 / duration;
  const y = (value: number) => 143 - (value - minimum) / range * 125;
  const selectedTime = selectedAlert?.triggered_at;
  const selectedX = selectedTime && Date.parse(selectedTime) <= Date.parse(last.closed_at) ? x(selectedTime) : undefined;
  const showSelected = selectedTime !== undefined && selectedX !== undefined && selectedX >= 65;
  return <>
    <svg className="monitoring-chart" viewBox="0 0 620 178" role="img" aria-label={copy.data}>
      {[minimum, minimum + range / 2, minimum + range].map(tick => <g key={tick}>
        <line x1="65" x2="590" y1={y(tick)} y2={y(tick)} className="monitoring-chart-grid" />
        <text x="57" y={y(tick) + 4} textAnchor="end">{number(tick, language)}</text>
      </g>)}
      <line x1="65" x2="590" y1={y(data.threshold)} y2={y(data.threshold)} className="monitoring-threshold" />
      {showSelected && <line x1={selectedX} x2={selectedX} y1="18" y2="143" className="monitoring-selected-mark">
        <title>{`${copy.selectedAlertTime} · ${time(selectedTime)}`}</title>
      </line>}
      {frames.slice(1).map((frame, i) => {
        const previous = frames[i];
        return frame.start === previous.closed_at && <line key={frame.start} className="monitoring-observed" x1={x(previous.closed_at)} x2={x(frame.closed_at)} y1={y(previous.value)} y2={y(frame.value)} />;
      })}
      {frames.map(frame => <circle key={frame.start} cx={x(frame.closed_at)} cy={y(frame.value)} r="3" className="monitoring-observed-dot">
        <title>{`${time(frame.closed_at)} · ${number(frame.value, language)} ${data.unit}`}</title>
      </circle>)}
      {last.forecast !== null && <g>
        <line className="monitoring-forecast" x1={x(last.closed_at)} x2={x(last.target_end)} y1={y(last.value)} y2={y(last.forecast)} />
        <circle className="monitoring-forecast-dot" cx={x(last.target_end)} cy={y(last.forecast)} r="4">
          <title>{`${time(last.target_end)} · ${number(last.forecast, language)} ${data.unit}`}</title>
        </circle>
      </g>}
      <text x="65" y="165">{frames[0].closed_at.slice(11, 16)}</text>
      <text x="590" y="165" textAnchor="end">{last.target_end.slice(11, 16)} UTC</text>
    </svg>
    <ul className="monitoring-legend"><li>{copy.current}</li><li>{copy.predicted}</li><li>{copy.threshold}</li>
      {showSelected && <li className="monitoring-selected-legend">{copy.selectedAlertTime}</li>}
    </ul>
  </>;
}

function Validation({ data, copy, language }: { data: Series; copy: Copy; language: Language }) {
  if (!data.evaluation) return null;
  const { training_bins, healthy_holdout, incident_replay } = data.evaluation;
  return <details className="monitoring-validation">
    <summary>{copy.evaluation}</summary>
    <p>{copy.training} · {training_bins}</p>
    <div className="monitoring-table-wrap"><table>
      <thead><tr>{[copy.group, copy.pairs, copy.ar1, copy.persistence, copy.crossing, copy.falseAlerts].map(label => <th scope="col" key={label}>{label}</th>)}</tr></thead>
      <tbody>{([[copy.healthyHoldout, healthy_holdout], [copy.incidentReplay, incident_replay]] as const).map(([group, score]) =>
        <tr key={group}><th scope="row">{group}</th><td>{score.pairs}</td>
          <td>{score.mae.ar1 === null ? '—' : `${number(score.mae.ar1, language)} ${data.unit}`}</td>
          <td>{score.mae.persistence === null ? '—' : `${number(score.mae.persistence, language)} ${data.unit}`}</td>
          <td>{score.crossings_warned} / {score.eligible_crossings}</td><td>{score.false_forecast_warnings}</td></tr>)}</tbody>
    </table></div>
    <p>{copy.evaluationNote}</p>
  </details>;
}

export default function MonitoringConsole({ language, embedded = false, selectedIncidentId, onOpenIncident }: {
  language: Language; embedded?: boolean; selectedIncidentId?: string; onOpenIncident?: OpenIncident;
}) {
  const copy = labels[language];
  const className = `monitoring-console${embedded ? ' monitoring-embedded' : ''}`;
  const [data, setData] = useState<Monitoring>();
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  const [tab, setTab] = useState('metrics');
  const [caseId, setCaseId] = useState('');
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [selectedWarning, setSelectedWarning] = useState<string>();

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let live = false;
    let pollMilliseconds = 15_000;
    async function load() {
      try {
        const response = await api<Monitoring>('/monitoring', { signal: controller.signal });
        if (controller.signal.aborted) return;
        live = response.mode === 'live' || response.mode === 'live_unavailable';
        if (response.mode === 'live') {
          pollMilliseconds = response.bin_seconds >= 2 && response.bin_seconds <= 15 ? 3_000 : 15_000;
        }
        setData(response);
        setError(false);
      } catch {
        if (controller.signal.aborted) return;
        setError(true);
      }
      if (live) timer = setTimeout(load, pollMilliseconds);
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [reload]);

  const activeCase = data && (data.mode === 'replay' || data.mode === 'live')
    ? data.cases.find(item => item.id === caseId) ?? data.cases[0] : undefined;
  const count = activeCase?.frames.length ?? 0;
  useEffect(() => {
    if (!playing || cursor >= count - 1) return;
    const timer = setTimeout(() => {
      setCursor(cursor + 1);
      if (cursor + 1 === count - 1) setPlaying(false);
    }, 1500);
    return () => clearTimeout(timer);
  }, [playing, cursor, count]);

  const unrelatedReplay = embedded && selectedIncidentId && data?.mode === 'replay';
  if (error || !data || unrelatedReplay || (data.mode !== 'replay' && data.mode !== 'live')) return <section className={className} aria-label={copy.title}>
    {!embedded && <h1>{copy.title}</h1>}
    <Alert type={error || data?.mode === 'replay_invalid' ? 'error' : 'info'}>
      {error ? copy.error : unrelatedReplay ? copy.liveNotConfigured : !data || data.mode === 'replay' || data.mode === 'live' ? copy.loading : copy[data.mode]}
    </Alert>
    <div className="monitoring-refresh"><Button onClick={() => setReload(value => value + 1)}>{copy.refresh}</Button></div>
    <AlertConnectionNotice state={data?.alert_connection} copy={copy} />
    {(data?.mode === 'live' || data?.mode === 'live_unavailable') &&
      <LiveAlerts alerts={data.alerts ?? []} copy={copy} language={language} selectedIncidentId={selectedIncidentId} embedded={embedded} onOpenIncident={onOpenIncident} />}
  </section>;

  const replay = data.mode === 'replay';
  const liveAlerts = replay ? [] : data.alerts ?? [];
  const selectedAlert = liveAlerts.find(alert => alert.incident_id === selectedIncidentId);
  const stale = data.freshness === 'stale' || data.freshness === 'gap' ? data.freshness : null;
  const frames = activeCase?.frames.slice(0, replay ? cursor + 1 : undefined) ?? [];
  const last = frames.at(-1);
  const warnings = frames.filter(frame => frame.state === 'observed_breach' ||
    (frame.state === 'forecast_risk' && (replay || (!stale && frame === last)))).reverse();
  const selected = warnings.find(frame => frame.start === selectedWarning) ?? warnings[0];
  const metric = replay ? copy.meanLatency : data.metric;
  const format = (value: number) => `${number(value, language)} ${data.unit}`;

  return <section className={className} aria-label={copy.title}>
    <header className="monitoring-header">
      <div><span className={`monitoring-mode ${replay ? '' : 'monitoring-live'}`}>{replay ? copy.replay : copy.live}</span>
        {!embedded && <h1>{copy.title}</h1>}{(!embedded || replay) && <p>{replay ? copy.replayNote : copy.liveNote}</p>}</div>
      {replay && <div className="monitoring-controls">
        <label>{copy.case}<select value={activeCase?.id ?? ''} onChange={event => {
          setCaseId(event.target.value); setPlaying(false); setCursor(0); setSelectedWarning(undefined);
        }}>{data.cases.map(item => <option value={item.id} key={item.id}>{item.id}</option>)}</select></label>
        <Button onClick={() => { setPlaying(false); setCursor(0); setSelectedWarning(undefined); }} disabled={cursor === 0 && !playing}>{copy.reset}</Button>
        <Button onClick={() => setCursor(value => Math.min(value + 1, count - 1))} disabled={playing || cursor >= count - 1}>{copy.next}</Button>
        <Button variant="primary" onClick={() => setPlaying(value => !value)} disabled={cursor >= count - 1}>{playing ? copy.pause : copy.play}</Button>
      </div>}
    </header>
    <AlertConnectionNotice state={data.alert_connection} copy={copy} />
    {stale && <Alert type="warning">{copy[stale]}</Alert>}
    <Tabs activeTabId={tab} onChange={event => setTab(event.detail.activeTabId)} tabs={[
      { id: 'metrics', label: copy.metrics, content: <>
        {embedded && selectedAlert && <div className="monitoring-selected-summary">
          <strong className="monitoring-selected-label">{copy.selectedAlert}</strong>
          <time dateTime={selectedAlert.triggered_at}>{time(selectedAlert.triggered_at)}</time>
          <span><AlertReading alert={selectedAlert} copy={copy} language={language} /></span>
        </div>}
        {last ? <>
        <div className="monitoring-overview">
          <div className="monitoring-values">
            <div><span>{copy.current}</span><strong>{format(last.value)}</strong><small>{data.service} · {metric}</small></div>
            <div><span>{copy.predicted}</span><strong>{last.forecast === null ? '—' : format(last.forecast)}</strong><small>{stale ? copy.paused : last.forecast === null ? copy.waiting : `${copy.experimental} · ${time(last.target_end)}`}</small></div>
            <div><span>{copy.threshold}</span><strong>{format(data.threshold)}</strong><small className={stale ? '' : `monitoring-state ${last.state}`}>{stale ? copy.paused : copy[last.state]}</small></div>
          </div>
          <section className="monitoring-panel">
            <div className="monitoring-panel-head">{embedded ? <h3>{metric}</h3> : <h2>{metric}</h2>}<span>{data.bin_seconds} {copy.window} {replay && `· ${cursor + 1}/${count}`}</span></div>
            <Timeline frames={frames} data={data} copy={copy} language={language} selectedAlert={embedded ? selectedAlert : undefined} />
            {!replay && <InvestigationLink frame={last} copy={copy} onOpenIncident={onOpenIncident} />}
          </section>
        </div>
        {!embedded && liveAlerts.length > 0 && <div className="monitoring-investigation"><Button onClick={() => setTab('warnings')}>{copy.showLiveAlerts}</Button></div>}
        <Validation data={data} copy={copy} language={language} />
        </> : <><p>{copy.noData}</p>{!embedded && liveAlerts.length > 0 && <Button onClick={() => setTab('warnings')}>{copy.showLiveAlerts}</Button>}</>}
      </> },
      { id: 'warnings', label: copy.warnings, content: <>
      <LiveAlerts alerts={liveAlerts} copy={copy} language={language} selectedIncidentId={selectedIncidentId} embedded={embedded} onOpenIncident={onOpenIncident} />
      {selected ? <div className="monitoring-warning-grid">
        <ul className="monitoring-warning-list">{warnings.map(frame => <li key={frame.start}>
          <button type="button" aria-pressed={frame.start === selected.start} onClick={() => setSelectedWarning(frame.start)}>
            <span className={`monitoring-state ${frame.state}`}>{copy[frame.state]}</span>
            <strong>{data.service} · {metric}</strong><time dateTime={frame.closed_at}>{time(frame.closed_at)}</time>
          </button>
        </li>)}</ul>
        <article className="monitoring-panel monitoring-warning-detail">
          {embedded ? <h3>{copy[selected.state]}</h3> : <h2>{copy[selected.state]}</h2>}
          <h3>{copy.scope}</h3><strong>{data.service} · {metric}</strong><p>{copy.scopeNote}</p>
          <h3>{copy.evidence}</h3>
          <dl><div><dt>{copy.latest}</dt><dd>{time(selected.start)} → {time(selected.closed_at)}</dd></div>
            <div><dt>{copy.current}</dt><dd>{format(selected.value)}</dd></div><div><dt>{copy.threshold}</dt><dd>{format(data.threshold)}</dd></div>
            {selected.forecast !== null && <><div><dt>{copy.predicted}</dt><dd>{format(selected.forecast)}</dd></div>
              <div><dt>{copy.horizon}</dt><dd>{time(selected.target_end)}</dd></div></>}
          </dl>
          {activeCase?.source_url && <p><a href={activeCase.source_url} target="_blank" rel="noreferrer">{copy.raw}</a></p>}
          {!replay && <InvestigationLink frame={selected} copy={copy} onOpenIncident={onOpenIncident} />}
          <h3>{copy.checks}</h3><ol><li>{copy.check1}</li><li>{copy.check2}</li><li>{copy.check3}</li></ol><p>{copy.action}</p>
        </article>
      </div> : liveAlerts.length === 0 && <Alert type="info">{stale ? copy.paused : copy.noWarnings}</Alert>}
      </> },
    ]} />
    {data.source_url && <footer className="monitoring-sources"><a href={data.source_url} target="_blank" rel="noreferrer">{copy.sources}</a>
      {data.source_paper && <a href={data.source_paper} target="_blank" rel="noreferrer">{copy.paper}</a>}</footer>}
  </section>;
}

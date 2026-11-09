import type { Language } from './i18n';
import './incidentEventTimeline.css';

export type IncidentEvent = {
  id: string;
  at: string;
  kind: 'alert' | 'route' | 'evidence' | 'approval' | 'action' | 'verification' | 'window' | 'analysis';
  title: string;
  detail?: string;
  evidenceIds?: string[];
};

const copy = {
  en: {
    empty: 'No timestamped events have been recorded for this incident.',
    evidenceLink: 'View evidence',
    kinds: { alert: 'Alert', route: 'Routing', evidence: 'Evidence', approval: 'Approval', action: 'Action', verification: 'Verification', window: 'Observation window', analysis: 'Replay analysis' },
  },
  zh: {
    empty: '此事故暂无带时间戳的事件记录。',
    evidenceLink: '查看证据',
    kinds: { alert: '报警', route: '分流', evidence: '证据', approval: '批准', action: '执行', verification: '验证', window: '观测窗口', analysis: '回放分析' },
  },
} as const;

export default function IncidentEventTimeline({ events, language, onEvidence }: {
  events: IncidentEvent[];
  language: Language;
  onEvidence: (id: string) => void;
}) {
  const labels = copy[language];
  const ordered = events.flatMap(event => {
    const timestamp = Date.parse(event.at);
    const zoned = /(?:\.(\d+))?(?:Z|[+-]\d{2}:\d{2})$/i.exec(event.at);
    if (!Number.isFinite(timestamp) || !zoned) return [];
    const fraction = zoned[1] ? Number(`0.${zoned[1]}`) : 0;
    return [{ event, timestamp, fraction }];
  }).sort((left, right) => left.timestamp - right.timestamp || left.fraction - right.fraction);

  if (!ordered.length) return <p className="incident-event-timeline-empty">{labels.empty}</p>;

  const locale = language === 'zh' ? 'zh-CN' : 'en';
  const clock = new Intl.DateTimeFormat(locale, {
    hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3, timeZone: 'UTC', hourCycle: 'h23',
  });
  const day = new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeZone: 'UTC' });

  return <ol className="incident-event-timeline">
    {ordered.map(({ event, timestamp }) => <li key={event.id} className="incident-event-row" data-kind={event.kind}>
      <time className="incident-event-time" dateTime={event.at} title={event.at}>
        <span>{clock.format(timestamp)} UTC</span>
        <span>{day.format(timestamp)}</span>
      </time>
      <div className="incident-event-rail" aria-hidden="true"><span /></div>
      <article className="incident-event-card">
        <span className="incident-event-kind">{labels.kinds[event.kind]}</span>
        <h3>{event.title}</h3>
        {event.detail && <p>{event.detail}</p>}
        {event.evidenceIds?.length ? <ul className="incident-event-evidence">
          {[...new Set(event.evidenceIds)].map(evidenceId => <li key={evidenceId}>
            <button type="button" onClick={() => onEvidence(evidenceId)}>
              {labels.evidenceLink}<code>{evidenceId}</code><span aria-hidden="true">↗</span>
            </button>
          </li>)}
        </ul> : null}
      </article>
    </li>)}
  </ol>;
}

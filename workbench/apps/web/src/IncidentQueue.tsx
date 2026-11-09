import { useEffect, useId, useRef } from 'react';
import type { PointerEvent } from 'react';
import type { Language } from './i18n';
import './incidentQueue.css';

export type IncidentQueueEntry = {
  id: string;
  agentCaseId?: string;
  title: string;
  time: string;
  href: string;
  status: 'unresolved' | 'resolved' | 'recorded';
};

type Props = {
  entries: readonly IncidentQueueEntry[];
  selectedId?: string;
  language: Language;
  onAttach?: (caseId: string) => void;
  onDragTarget?: (target: 'tab' | 'panel' | null) => void;
};

function targetAt(x: number, y: number) {
  const target = document.elementFromPoint(x, y)?.closest('[data-agent-target]')?.getAttribute('data-agent-target');
  return target === 'tab' || target === 'panel' ? target : null;
}

const copy = {
  en: { title: 'Incident queue', unresolved: 'Unresolved', resolved: 'Resolved', recorded: 'Recorded', empty: 'No incidents yet.', timeUnavailable: 'Time unavailable', attach: 'Attach to Agent' },
  zh: { title: '事故队列', unresolved: '未解决', resolved: '已解决', recorded: '已归档', empty: '暂无事故。', timeUnavailable: '时间未知', attach: '添加到 Agent' },
};

const statusOrder = { unresolved: 0, resolved: 1, recorded: 2 } as const;
const statuses = ['unresolved', 'resolved', 'recorded'] as const;

function timestamp(value: string) {
  const result = Date.parse(value);
  return Number.isFinite(result) ? result : Number.NEGATIVE_INFINITY;
}

export function orderIncidentQueue(entries: readonly IncidentQueueEntry[]) {
  return [...entries].sort((left, right) => {
    if (left.status !== right.status) return statusOrder[left.status] - statusOrder[right.status];
    const leftTime = timestamp(left.time);
    const rightTime = timestamp(right.time);
    if (leftTime !== rightTime) return rightTime - leftTime;
    return left.id.localeCompare(right.id);
  });
}

export function IncidentQueue({ entries, selectedId, language, onAttach, onDragTarget }: Props) {
  const id = useId();
  const cleanup = useRef<() => void>(undefined);
  useEffect(() => () => cleanup.current?.(), []);

  function beginDrag(event: PointerEvent<HTMLAnchorElement>, caseId: string) {
    if (!onAttach || event.button !== 0 || event.pointerType === 'touch') return;
    cleanup.current?.();
    const { pointerId, clientX, clientY } = event;
    const moved = (e: globalThis.PointerEvent) => Math.hypot(e.clientX - clientX, e.clientY - clientY) > 7;
    const move = (e: globalThis.PointerEvent) => {
      if (e.pointerId === pointerId && moved(e)) onDragTarget?.(targetAt(e.clientX, e.clientY));
    };
    const finish = (e: globalThis.PointerEvent) => {
      if (e.pointerId !== pointerId) return;
      const destination = moved(e) && targetAt(e.clientX, e.clientY);
      clear();
      if (destination) onAttach(caseId);
    };
    const clear = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', finish);
      document.removeEventListener('pointercancel', clear);
      window.removeEventListener('blur', clear);
      onDragTarget?.(null);
      cleanup.current = undefined;
    };
    cleanup.current = clear;
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', finish);
    document.addEventListener('pointercancel', clear);
    window.addEventListener('blur', clear);
  }
  const labels = copy[language];
  const formatter = new Intl.DateTimeFormat(language === 'zh' ? 'zh-CN' : 'en-US', {
    dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC', hourCycle: 'h23',
  });
  const ordered = orderIncidentQueue(entries);
  return <nav className="incident-queue" aria-labelledby={`${id}-title`}>
    <header className="incident-queue-header">
      <span className="incident-queue-signal" aria-hidden="true"><i /><i /><i /></span>
      <h2 id={`${id}-title`}>{labels.title}</h2>
      <span className="incident-queue-total" aria-hidden="true">{ordered.length}</span>
    </header>
    <div className="incident-queue-groups">
      {ordered.length === 0 && <p className="incident-queue-empty">{labels.empty}</p>}
      {statuses.map(status => {
        const group = ordered.filter(entry => entry.status === status);
        if (!group.length) return null;
        return <section key={status} className="incident-queue-group" aria-labelledby={`${id}-${status}`} data-status={status}>
          <div className="incident-queue-group-label">
            <span className="incident-queue-status" aria-hidden="true" />
            <h3 id={`${id}-${status}`}>{labels[status]}</h3>
            <span aria-hidden="true">{group.length}</span>
          </div>
          <ul>
            {group.map(entry => {
              const occurredAt = timestamp(entry.time);
              const caseId = entry.agentCaseId;
              const attachable = Boolean(caseId && onAttach);
              return <li key={entry.id} className={attachable ? 'incident-queue-attachable' : undefined}>
                <a href={entry.href} aria-current={entry.id === selectedId ? 'page' : undefined} draggable={false}
                  onPointerDown={caseId ? event => beginDrag(event, caseId) : undefined}>
                  <span className="incident-queue-name">{entry.title}</span>
                  {Number.isFinite(occurredAt)
                    ? <time dateTime={entry.time}>{formatter.format(occurredAt)} UTC</time>
                    : <span className="incident-queue-time">{labels.timeUnavailable}</span>}
                  <span className="incident-queue-arrow" aria-hidden="true">›</span>
                </a>
                {caseId && onAttach && <button className="incident-queue-attach" type="button" title={labels.attach}
                  aria-label={`${labels.attach}: ${entry.title} (${caseId})`} onClick={() => onAttach(caseId)}>
                  <svg aria-hidden="true" width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4">
                    <path d="M2.5 2.75h11v8h-5l-3 2.5v-2.5h-3z" strokeLinejoin="round" />
                    <path d="M5.75 6.75h4.5m-2.25-2.25V9" strokeLinecap="round" />
                  </svg>
                </button>}
              </li>;
            })}
          </ul>
        </section>;
      })}
    </div>
  </nav>;
}

export default IncidentQueue;

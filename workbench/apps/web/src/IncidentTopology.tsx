import { useId, useRef, useState } from 'react';
import type { Language } from './i18n';
import './incidentTopology.css';

export type TopologyEdge = {
  id: string;
  from: string;
  to: string;
  fromRoute?: string;
  toRoute?: string;
  at: string;
  latency?: number;
};

type Link = { id: string; from: string; to: string; observations: TopologyEdge[] };
type Point = { x: number; y: number };

const copy = {
  en: {
    title: 'Topology', services: 'services', connections: 'connections',
    empty: 'The incident records contain no verified caller-to-callee observation.',
    note: 'Select a connection to inspect its recorded calls.',
    zoomIn: 'Zoom in', zoomOut: 'Zoom out', reset: 'Reset zoom', expand: 'Expand topology', close: 'Close topology',
    evidence: 'View evidence', calls: 'observed calls', diagram: 'Directed topology of observed calls',
    choose: 'Choose an observed connection',
  },
  zh: {
    title: '服务拓扑', services: '个服务', connections: '条依赖',
    empty: '本次事故的记录里没有可核对的调用方与被调用方。',
    note: '选择一条连线，查看对应的调用记录。',
    zoomIn: '放大', zoomOut: '缩小', reset: '重置缩放', expand: '展开拓扑', close: '关闭拓扑',
    evidence: '查看证据', calls: '条调用记录', diagram: '已观测调用的有向拓扑',
    choose: '选择一条已观测依赖',
  },
} as const;

const nodeWidth = 156;
const nodeHeight = 42;
const columnStep = 254;
const rowStep = 66;
const marginX = 54;

function project(edges: TopologyEdge[]) {
  const nodes = [...new Set(edges.flatMap(edge => [edge.from, edge.to]))].sort();
  const links = new Map<string, Link>();
  const adjacency = new Map(nodes.map(node => [node, new Set<string>()]));
  const incoming = new Set<string>();
  for (const edge of edges) {
    const id = JSON.stringify([edge.from, edge.to]);
    const link = links.get(id) ?? { id, from: edge.from, to: edge.to, observations: [] };
    link.observations.push(edge);
    links.set(id, link);
    if (edge.from !== edge.to) {
      adjacency.get(edge.from)?.add(edge.to);
      incoming.add(edge.to);
    }
  }

  const depths = new Map<string, number>();
  function place(roots: string[]) {
    const queue = [...roots];
    for (const root of roots) depths.set(root, 0);
    for (let index = 0; index < queue.length; index++) {
      const current = queue[index];
      for (const child of [...(adjacency.get(current) ?? [])].sort()) {
        if (depths.has(child)) continue;
        depths.set(child, depths.get(current)! + 1);
        queue.push(child);
      }
    }
  }

  place(nodes.filter(node => !incoming.has(node)));
  for (const node of nodes) {
    if (!depths.has(node)) place([node]);
  }

  const columns: string[][] = [];
  for (const node of nodes) (columns[depths.get(node)!] ??= []).push(node);
  const height = Math.max(184, ...columns.map(column => column.length * rowStep + 68));
  const width = Math.max(380, marginX * 2 + nodeWidth + Math.max(0, columns.length - 1) * columnStep);
  const points = new Map<string, Point>();
  const left = (width - (nodeWidth + Math.max(0, columns.length - 1) * columnStep)) / 2;
  columns.forEach((column, index) => column.forEach((node, row) => points.set(node, {
    x: left + index * columnStep,
    y: (height - nodeHeight - (column.length - 1) * rowStep) / 2 + row * rowStep,
  })));
  return { nodes, links: [...links.values()], points, width, height };
}

function connectionPath(from: Point, to: Point, self: boolean, reverseInset: number) {
  if (self) {
    const x = from.x + nodeWidth;
    return `M ${x} ${from.y + 11} C ${x + 54} ${from.y - 14}, ${x + 54} ${from.y + 56}, ${x + 3} ${from.y + 31}`;
  }
  if (to.x <= from.x) {
    const lane = to.x - reverseInset;
    const above = Math.min(from.y, to.y) - 18 - reverseInset / 4;
    return `M ${from.x + 36} ${from.y} Q ${from.x + 36} ${above} ${from.x + 4} ${above} H ${lane + 8} Q ${lane} ${above} ${lane} ${above + 8} V ${to.y + nodeHeight / 2} H ${to.x - 4}`;
  }
  const startX = from.x + nodeWidth;
  const endX = to.x - 4;
  const midX = (startX + endX) / 2;
  const startY = from.y + nodeHeight / 2;
  const endY = to.y + nodeHeight / 2;
  return `M ${startX} ${startY} C ${midX} ${startY}, ${midX} ${endY}, ${endX} ${endY}`;
}

function Graph({ model, labels, selected, setSelected, zoom }: {
  model: ReturnType<typeof project>;
  labels: (typeof copy)[Language];
  selected: Link;
  setSelected: (link: Link) => void;
  zoom: number;
}) {
  const marker = useId();
  const fitWidth = Math.min(820, model.width + 52);
  const reverseGroups = new Map<number, string[]>();
  for (const link of model.links) {
    const from = model.points.get(link.from)!;
    const to = model.points.get(link.to)!;
    if (link.from === link.to || to.x > from.x) continue;
    const group = reverseGroups.get(to.x) ?? [];
    group.push(link.id);
    reverseGroups.set(to.x, group);
  }
  const reverseLanes = new Map<string, { inset: number; hitWidth: number }>();
  for (const [toX, group] of reverseGroups) {
    // Separate shared approaches in the gutter between the service columns.
    const spacing = Math.min(16, (Math.min(72, toX - 8) - 18) / Math.max(1, group.length - 1));
    group.sort().forEach((id, index) => reverseLanes.set(id, {
      inset: group.length === 1 ? 44 : 18 + index * spacing,
      hitWidth: group.length === 1 ? 20 : Math.min(20, spacing * 1.7),
    }));
  }
  return <div className="incident-topology-canvas" tabIndex={0} aria-label={labels.diagram}>
    <svg viewBox={`0 0 ${model.width} ${model.height}`} aria-label={labels.diagram}
      style={{ width: `min(${zoom * 100}%, ${fitWidth * zoom}px)`, minWidth: Math.min(fitWidth, 300) * zoom }}>
      <defs>
        <marker id={marker} viewBox="0 0 8 8" refX="6.5" refY="4" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M 0 0 L 8 4 L 0 8 Z" fill="context-stroke" />
        </marker>
      </defs>
      {model.links.map(link => {
        const lane = reverseLanes.get(link.id);
        const path = connectionPath(model.points.get(link.from)!, model.points.get(link.to)!, link.from === link.to, lane?.inset ?? 44);
        const name = `${link.from} → ${link.to} · ${link.observations.length} ${labels.calls}`;
        return <g key={link.id} role="button" tabIndex={0} aria-label={name} aria-pressed={link.id === selected.id}
          className="incident-topology-link" onClick={() => setSelected(link)} onKeyDown={event => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              setSelected(link);
            }
          }}>
          <title>{name}</title>
          <path d={path} className="incident-topology-hit" style={lane ? { strokeWidth: lane.hitWidth } : undefined} />
          <path d={path} className="incident-topology-line" markerEnd={`url(#${marker})`} />
        </g>;
      })}
      {model.nodes.map(node => {
        const point = model.points.get(node)!;
        const active = selected.from === node || selected.to === node;
        const shortened = node.length > 18 ? `${node.slice(0, 16)}…` : node;
        return <g key={node} transform={`translate(${point.x} ${point.y})`} className="incident-topology-node" data-active={active}>
          <title>{node}</title>
          <rect width={nodeWidth} height={nodeHeight} rx="8" />
          <path d="M 14 21 l 5 -5 5 5 -5 5 z" />
          <text x="33" y="21" dominantBaseline="central">{shortened}</text>
        </g>;
      })}
    </svg>
  </div>;
}

export default function IncidentTopology({ edges, language, onEvidence }: {
  edges: TopologyEdge[];
  language: Language;
  onEvidence: (id: string) => void;
}) {
  const labels = copy[language];
  const heading = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [selectedId, setSelectedId] = useState<string>();
  const [zoom, setZoom] = useState(1);
  const model = project(edges);
  const selected = model.links.find(link => link.id === selectedId) ?? model.links[0];
  const formatNumber = new Intl.NumberFormat(language === 'zh' ? 'zh-CN' : 'en-US', { maximumFractionDigits: 3 });
  const formatTime = new Intl.DateTimeFormat(language === 'zh' ? 'zh-CN' : 'en-US', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'UTC', hourCycle: 'h23',
  });

  function inspectEvidence(id: string) {
    if (dialog.current?.open) {
      dialog.current.close();
      requestAnimationFrame(() => onEvidence(id));
    } else onEvidence(id);
  }

  function surface(isExpanded: boolean) {
    return <>
      <div className="incident-topology-toolbar">
        <div className="incident-topology-key">{model.nodes.length} {labels.services}<span aria-hidden="true">·</span>{model.links.length} {labels.connections}</div>
        <div className="incident-topology-controls">
          <button type="button" title={labels.zoomOut} aria-label={labels.zoomOut} disabled={zoom <= .75}
            onClick={() => setZoom(current => Math.max(.75, current - .25))}>−</button>
          <button type="button" title={labels.reset} aria-label={labels.reset} onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</button>
          <button type="button" title={labels.zoomIn} aria-label={labels.zoomIn} disabled={zoom >= 2.5}
            onClick={() => setZoom(current => Math.min(2.5, current + .25))}>+</button>
          {!isExpanded && <button type="button" title={labels.expand} aria-label={labels.expand} onClick={() => {
            dialog.current?.showModal();
            setExpanded(true);
          }}>
            <svg aria-hidden="true" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M6 2H2v4m8-4h4v4M2 10v4h4m8-4v4h-4" /></svg>
          </button>}
        </div>
      </div>
      <Graph model={model} labels={labels} selected={selected} setSelected={link => setSelectedId(link.id)} zoom={zoom} />
      <div className="incident-topology-inspector">
        <div className="incident-topology-inspector-heading">
          <label className="incident-topology-select" aria-label={labels.choose}>
            <select aria-label={labels.choose} value={selected.id} onChange={event => setSelectedId(event.target.value)}>
              {model.links.map(link => <option value={link.id} key={link.id}>{link.from} → {link.to}</option>)}
            </select>
          </label>
          <span>{selected.observations.length} {labels.calls}</span>
        </div>
        <ul>{selected.observations.map(edge => {
          const timestamp = new Date(edge.at);
          return <li key={edge.id}>
            <div className="incident-topology-observation">
              {(edge.fromRoute || edge.toRoute) && <code>{edge.fromRoute ?? edge.from} → {edge.toRoute ?? edge.to}</code>}
              {!Number.isNaN(timestamp.getTime()) && <time dateTime={edge.at}>{formatTime.format(timestamp)} UTC</time>}
              {typeof edge.latency === 'number' && Number.isFinite(edge.latency) && <strong>{formatNumber.format(edge.latency)} ms</strong>}
            </div>
            <button type="button" onClick={() => inspectEvidence(edge.id)}>{labels.evidence} <span aria-hidden="true">↗</span></button>
          </li>;
        })}</ul>
      </div>
    </>;
  }

  return <section className="incident-topology" aria-labelledby={heading}>
    <header><h3 id={heading}>{labels.title}</h3>{selected && <p>{labels.note}</p>}</header>
    {selected ? surface(false) : <p className="incident-topology-empty">{labels.empty}</p>}
    <dialog ref={dialog} className="incident-topology-modal" aria-labelledby={`${heading}-expanded`} onClose={() => setExpanded(false)}>
      <header><h3 id={`${heading}-expanded`}>{labels.title}</h3>
        <button type="button" className="incident-topology-close" onClick={() => dialog.current?.close()}>{labels.close}</button>
      </header>
      {expanded && selected && surface(true)}
    </dialog>
  </section>;
}

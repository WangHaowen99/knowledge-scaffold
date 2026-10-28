import { BookOpen, Crosshair, Maximize, PinOff, Search, Shuffle, X, ZoomIn, ZoomOut } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent } from 'react';
import { rpc } from './rpc';
import { errorText, useToast } from './toast';
import type { WikiGraph, WikiPage, WikiPageType } from './types';
import { Button, Empty, Spinner } from './ui';

const TYPE_LABEL: Record<WikiPageType, string> = { index: '索引', summary: '文档', entity: '实体', concept: '概念' };
const NODE_COLOR: Record<WikiPageType, string> = {
  index: 'var(--color-text-secondary)',
  summary: 'var(--color-feedback-warning)',
  entity: 'var(--color-action-primary)',
  concept: 'var(--color-feedback-success)',
};
const LEGEND: WikiPageType[] = ['summary', 'entity', 'concept'];
const LINK_RE = /\[\[([^[\]|\n]+?)(?:\|([^[\]\n]+?))?\]\]/g;
const plain = (text: string) => text.replace(LINK_RE, (_, slug: string, label?: string) => label ?? slug);

// Force parameters, in world units (one unit is one screen pixel at zoom 1).
const CHARGE = 220;
const LINK_DISTANCE = 70;
// split across the busier end, so hubs are not crushed by their own links (as d3-force does)
const LINK_STRENGTH = 0.4;
const GRAVITY = 0.02; // pulls every component toward the origin so islands stay in view
const COLLIDE_GAP = 16;
const DAMPING = 0.6;
const ALPHA_MIN = 0.005;
const MIN_ZOOM = 0.2;
const MAX_ZOOM = 4;
const FIT_MAX_ZOOM = 1.2;
const LABEL_ZOOM = 1.25; // above this zoom every label shows
const LABEL_SHARE = 0.35; // below it, only the best-linked share of nodes is labelled

type SimNode = {
  slug: string;
  title: string;
  type: WikiPageType;
  degree: number;
  r: number;
  x: number;
  y: number;
  vx: number;
  vy: number;
  pinned: boolean;
};
type SimEdge = { source: SimNode; target: SimNode; strength: number };
type View = { x: number; y: number; k: number };

function buildSim(graph: WikiGraph) {
  const nodes: SimNode[] = graph.nodes.map((n, i) => {
    // sunflower seeding gives an even, deterministic start without overlaps
    const angle = i * 2.39996;
    const radius = 18 * Math.sqrt(i + 1);
    return {
      slug: n.slug,
      title: n.title,
      type: n.page_type,
      degree: n.degree,
      r: 5 + Math.min(9, Math.sqrt(n.degree) * 2),
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius,
      vx: 0,
      vy: 0,
      pinned: false,
    };
  });
  const bySlug = new Map(nodes.map((n) => [n.slug, n]));
  const edges: SimEdge[] = [];
  for (const e of graph.edges) {
    const source = bySlug.get(e.source);
    const target = bySlug.get(e.target);
    if (source && target && source !== target) edges.push({ source, target, strength: 0 });
  }
  const links = new Map<SimNode, number>();
  for (const e of edges) {
    links.set(e.source, (links.get(e.source) ?? 0) + 1);
    links.set(e.target, (links.get(e.target) ?? 0) + 1);
  }
  for (const e of edges) e.strength = LINK_STRENGTH / Math.min(links.get(e.source)!, links.get(e.target)!);
  const neighbors = new Map(nodes.map((n) => [n.slug, new Set<string>()]));
  for (const e of edges) {
    neighbors.get(e.source.slug)!.add(e.target.slug);
    neighbors.get(e.target.slug)!.add(e.source.slug);
  }
  const ranked = [...nodes].sort((a, b) => b.degree - a.degree);
  const labelDegree = ranked[Math.min(ranked.length - 1, Math.floor(ranked.length * LABEL_SHARE))]?.degree ?? 0;
  return { nodes, edges, bySlug, neighbors, labelDegree };
}

function step(nodes: SimNode[], edges: SimEdge[], alpha: number) {
  for (let i = 0; i < nodes.length; i += 1) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j += 1) {
      const b = nodes[j];
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      const d2 = dx * dx + dy * dy || 0.01;
      const push = (CHARGE * alpha) / d2;
      a.vx += dx * push;
      a.vy += dy * push;
      b.vx -= dx * push;
      b.vy -= dy * push;
      const d = Math.sqrt(d2);
      const min = a.r + b.r + COLLIDE_GAP;
      if (d < min) {
        const share = ((min - d) / d) * 0.5;
        a.x += dx * share;
        a.y += dy * share;
        b.x -= dx * share;
        b.y -= dy * share;
      }
    }
  }
  for (const { source, target, strength } of edges) {
    const dx = target.x - source.x;
    const dy = target.y - source.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 1;
    const pull = ((d - LINK_DISTANCE) / d) * strength * alpha;
    source.vx += dx * pull;
    source.vy += dy * pull;
    target.vx -= dx * pull;
    target.vy -= dy * pull;
  }
  for (const n of nodes) {
    if (n.pinned) {
      n.vx = 0;
      n.vy = 0;
      continue;
    }
    n.vx = (n.vx - n.x * GRAVITY * alpha) * DAMPING;
    n.vy = (n.vy - n.y * GRAVITY * alpha) * DAMPING;
    n.x += n.vx;
    n.y += n.vy;
  }
}

// A gentle arc from the source to the rim of the target, so the arrow head stays visible.
function edgePath({ source, target }: SimEdge): string {
  const cx = (source.x + target.x) / 2 - (target.y - source.y) * 0.12;
  const cy = (source.y + target.y) / 2 + (target.x - source.x) * 0.12;
  const d = Math.hypot(target.x - cx, target.y - cy) || 1;
  const ex = target.x - ((target.x - cx) / d) * (target.r + 3);
  const ey = target.y - ((target.y - cy) / d) * (target.r + 3);
  return `M${source.x},${source.y} Q${cx},${cy} ${ex},${ey}`;
}

function NodeCard({
  kbId,
  node,
  neighbors,
  onClose,
  onOpen,
  onPick,
  onFocus,
}: {
  kbId: string;
  node: SimNode;
  neighbors: SimNode[];
  onClose: () => void;
  onOpen: () => void;
  onPick: (slug: string) => void;
  onFocus: () => void;
}) {
  const [summary, setSummary] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setSummary(null);
    rpc
      .call<WikiPage>('wiki.page', { kb_id: kbId, slug: node.slug })
      .then((page) => live && setSummary(plain(page.summary || '')))
      .catch(() => live && setSummary(''));
    return () => {
      live = false;
    };
  }, [kbId, node.slug]);
  return (
    <div className="graph-panel graph-card">
      <button className="icon-btn graph-card-close" onClick={onClose} aria-label="关闭">
        <X size={14} />
      </button>
      <span className="graph-card-type" style={{ color: NODE_COLOR[node.type] }}>
        {TYPE_LABEL[node.type]}
      </span>
      <div className="graph-card-title">{node.title}</div>
      <div className="graph-card-summary">{summary === null ? <Spinner size={12} /> : summary || '暂无摘要'}</div>
      <div className="graph-card-meta">
        链接 <b>{neighbors.length}</b>
      </div>
      {neighbors.length ? (
        <div className="graph-card-neighbors">
          {neighbors.slice(0, 10).map((n) => (
            <button key={n.slug} onClick={() => onPick(n.slug)}>
              {n.title}
            </button>
          ))}
        </div>
      ) : null}
      <div className="graph-card-actions">
        <Button variant="primary" icon={<BookOpen size={13} />} onClick={onOpen}>
          打开页面
        </Button>
        <Button icon={<Crosshair size={13} />} onClick={onFocus}>
          聚焦邻居
        </Button>
      </div>
    </div>
  );
}

export default function WikiGraphView({ kbId, onOpen }: { kbId: string; onOpen: (slug: string) => void }) {
  const notify = useToast();
  const [graph, setGraph] = useState<WikiGraph | null>(null);
  const [hidden, setHidden] = useState<Set<WikiPageType>>(new Set());
  const [hover, setHover] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [, setPinTick] = useState(0);

  useEffect(() => {
    setGraph(null);
    setSelected(null);
    rpc
      .call<WikiGraph>('wiki.graph', { kb_id: kbId, limit: 150 })
      .then(setGraph)
      .catch((err) => notify(errorText(err), true));
  }, [kbId, notify]);

  const sim = useMemo(() => (graph ? buildSim(graph) : null), [graph]);
  const svgRef = useRef<SVGSVGElement>(null);
  const viewportRef = useRef<SVGGElement>(null);
  const nodeEls = useRef(new Map<string, SVGGElement>());
  const labelEls = useRef(new Map<string, SVGGElement>());
  const edgeEls = useRef<(SVGPathElement | null)[]>([]);
  const view = useRef<View>({ x: 0, y: 0, k: 1 });
  const alpha = useRef(1);
  const alphaTarget = useRef(0);
  const fitted = useRef(false);
  const tween = useRef(0);
  const hiddenRef = useRef(hidden);
  hiddenRef.current = hidden;

  const visibleNodes = useCallback(
    () => (sim ? sim.nodes.filter((n) => !hiddenRef.current.has(n.type)) : []),
    [sim],
  );

  const applyView = useCallback(() => {
    const { x, y, k } = view.current;
    const vp = viewportRef.current;
    if (!vp) return;
    vp.setAttribute('transform', `translate(${x},${y}) scale(${k})`);
    // labels and their halo keep a constant on-screen size
    vp.style.setProperty('--label-scale', String(1 / Math.max(k, 0.6)));
    svgRef.current?.classList.toggle('graph-zoomed', k > LABEL_ZOOM);
  }, []);

  const animateTo = useCallback(
    (target: View) => {
      const from = { ...view.current };
      const start = performance.now();
      cancelAnimationFrame(tween.current);
      const frame = (now: number) => {
        const p = Math.min(1, (now - start) / 380);
        const e = 1 - (1 - p) ** 3;
        view.current = { x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e, k: from.k + (target.k - from.k) * e };
        applyView();
        if (p < 1) tween.current = requestAnimationFrame(frame);
      };
      tween.current = requestAnimationFrame(frame);
    },
    [applyView],
  );

  const fit = useCallback(
    (subset?: SimNode[], smooth = true) => {
      const svg = svgRef.current;
      const list = subset ?? visibleNodes();
      if (!svg || !list.length) return;
      const box = svg.getBoundingClientRect();
      if (!box.width || !box.height) return;
      const xs = list.map((n) => n.x);
      const ys = list.map((n) => n.y);
      // extra room on the right for labels
      const x0 = Math.min(...xs) - 40;
      const x1 = Math.max(...xs) + 140;
      const y0 = Math.min(...ys) - 40;
      const y1 = Math.max(...ys) + 40;
      const k = Math.min(FIT_MAX_ZOOM, Math.min(box.width / (x1 - x0), box.height / (y1 - y0)) * 0.92);
      const target = { k, x: box.width / 2 - ((x0 + x1) / 2) * k, y: box.height / 2 - ((y0 + y1) / 2) * k };
      if (smooth) animateTo(target);
      else {
        view.current = target;
        applyView();
      }
    },
    [visibleNodes, animateTo, applyView],
  );

  const reheat = useCallback((to = 0.5) => {
    alpha.current = Math.max(alpha.current, to);
  }, []);

  // simulation loop: positions are written straight to the DOM, React only owns classes
  useEffect(() => {
    if (!sim) return;
    alpha.current = 1;
    fitted.current = false;
    let raf = 0;
    const frame = () => {
      if (alpha.current > ALPHA_MIN || alphaTarget.current) {
        const live = sim.nodes.filter((n) => !hiddenRef.current.has(n.type));
        const liveEdges = sim.edges.filter((e) => !hiddenRef.current.has(e.source.type) && !hiddenRef.current.has(e.target.type));
        step(live, liveEdges, alpha.current);
        alpha.current += (alphaTarget.current - alpha.current) * 0.02;
        if (!fitted.current && alpha.current < 0.08) {
          fitted.current = true;
          fit(undefined, false);
        }
        for (const n of live) {
          const at = `translate(${n.x},${n.y})`;
          nodeEls.current.get(n.slug)?.setAttribute('transform', at);
          labelEls.current.get(n.slug)?.setAttribute('transform', at);
        }
        sim.edges.forEach((e, i) => edgeEls.current[i]?.setAttribute('d', edgePath(e)));
      }
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [sim, fit]);

  useEffect(() => reheat(0.3), [hidden, reheat]);

  // wheel zoom needs a non-passive listener to stop the page from scrolling
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      cancelAnimationFrame(tween.current);
      zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX, e.clientY);
    };
    svg.addEventListener('wheel', onWheel, { passive: false });
    return () => svg.removeEventListener('wheel', onWheel);
  });

  const toWorld = (clientX: number, clientY: number) => {
    const box = svgRef.current!.getBoundingClientRect();
    const { x, y, k } = view.current;
    return { x: (clientX - box.left - x) / k, y: (clientY - box.top - y) / k };
  };

  function zoomAt(factor: number, clientX?: number, clientY?: number) {
    const box = svgRef.current!.getBoundingClientRect();
    const cx = clientX ?? box.left + box.width / 2;
    const cy = clientY ?? box.top + box.height / 2;
    const k = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, view.current.k * factor));
    const w = toWorld(cx, cy);
    view.current = { k, x: cx - box.left - w.x * k, y: cy - box.top - w.y * k };
    applyView();
  }

  const drag = useRef<
    | { kind: 'node'; node: SimNode; dx: number; dy: number; wasPinned: boolean; moved: boolean }
    | { kind: 'pan'; sx: number; sy: number; moved: boolean }
    | null
  >(null);

  const nodeFromEvent = (target: EventTarget) => {
    const el = (target as Element).closest?.('[data-slug]');
    return el ? sim?.bySlug.get(el.getAttribute('data-slug')!) ?? null : null;
  };

  const onPointerDown = (e: PointerEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    cancelAnimationFrame(tween.current);
    const node = nodeFromEvent(e.target);
    if (node) {
      const w = toWorld(e.clientX, e.clientY);
      drag.current = { kind: 'node', node, dx: node.x - w.x, dy: node.y - w.y, wasPinned: node.pinned, moved: false };
      node.pinned = true;
      alphaTarget.current = 0.3;
      reheat(0.3);
    } else {
      drag.current = { kind: 'pan', sx: e.clientX - view.current.x, sy: e.clientY - view.current.y, moved: false };
      svgRef.current?.classList.add('graph-panning');
    }
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e: PointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d) {
      const node = nodeFromEvent(e.target);
      setHover(node?.slug ?? null);
      return;
    }
    d.moved = true;
    if (d.kind === 'pan') {
      view.current = { ...view.current, x: e.clientX - d.sx, y: e.clientY - d.sy };
      applyView();
    } else {
      const w = toWorld(e.clientX, e.clientY);
      d.node.x = w.x + d.dx;
      d.node.y = w.y + d.dy;
    }
  };

  const onPointerUp = () => {
    const d = drag.current;
    drag.current = null;
    if (!d) return;
    if (d.kind === 'pan') {
      svgRef.current?.classList.remove('graph-panning');
      if (!d.moved) setSelected(null);
      return;
    }
    alphaTarget.current = 0;
    if (!d.moved) {
      d.node.pinned = d.wasPinned; // a click selects without pinning
      setSelected(d.node.slug);
    }
    setPinTick((t) => t + 1);
  };

  const unpinAll = () => {
    sim?.nodes.forEach((n) => (n.pinned = false));
    setPinTick((t) => t + 1);
    reheat();
  };

  const relayout = () => {
    if (!sim) return;
    sim.nodes.forEach((n) => {
      const angle = Math.random() * Math.PI * 2;
      n.x = Math.cos(angle) * 200;
      n.y = Math.sin(angle) * 200;
      n.pinned = false;
    });
    setPinTick((t) => t + 1);
    alpha.current = 1;
    fitted.current = false;
  };

  const focusNode = (slug: string) => {
    const node = sim?.bySlug.get(slug);
    const svg = svgRef.current;
    if (!node || !svg) return;
    if (hidden.has(node.type)) setHidden((cur) => new Set([...cur].filter((t) => t !== node.type)));
    setSelected(slug);
    const box = svg.getBoundingClientRect();
    const k = Math.max(view.current.k, 1.4);
    // leave room for the detail card on the right
    animateTo({ k, x: box.width / 2 - node.x * k - 120, y: box.height / 2 - node.y * k });
  };

  if (!graph || !sim) {
    return (
      <div className="empty">
        <Spinner size={18} />
      </div>
    );
  }
  if (!sim.nodes.length) return <Empty title="还没有页面" />;

  const focus = selected ?? hover;
  const near = focus ? new Set([focus, ...(sim.neighbors.get(focus) ?? [])]) : null;
  const q = query.trim();
  const match = q ? sim.nodes.find((n) => n.title.includes(q) && !hidden.has(n.type)) ?? null : null;
  const selectedNode = selected ? sim.bySlug.get(selected) : undefined;
  const nodeState = (n: SimNode) =>
    [
      near && !near.has(n.slug) ? 'wiki-node-dim' : '',
      n.slug === focus ? 'wiki-node-focus' : '',
      n.slug === selected ? 'wiki-node-selected' : '',
      n === match ? 'wiki-node-match' : '',
      n.pinned ? 'wiki-node-pinned' : '',
      n.degree < sim.labelDegree && !near?.has(n.slug) && n !== match ? 'wiki-node-minor' : '',
    ]
      .filter(Boolean)
      .join(' ');
  const track = (els: Map<string, SVGGElement>, n: SimNode, el: SVGGElement | null) => {
    if (!el) {
      els.delete(n.slug);
      return;
    }
    els.set(n.slug, el);
    el.setAttribute('transform', `translate(${n.x},${n.y})`);
  };
  const counts = LEGEND.map((t) => [t, sim.nodes.filter((n) => n.type === t).length] as const);

  return (
    <div className="wiki-graph">
      <svg
        ref={svgRef}
        role="img"
        aria-label="Wiki 链接图谱"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={() => !drag.current && setHover(null)}
        onDoubleClick={(e) => {
          const node = nodeFromEvent(e.target);
          if (node) onOpen(node.slug);
          else unpinAll();
        }}
      >
        <defs>
          <marker id="wiki-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,1 L9,5 L0,9 z" fill="var(--color-action-primary)" />
          </marker>
        </defs>
        <g ref={viewportRef}>
          <g>
            {sim.edges.map((e, i) => {
              if (hidden.has(e.source.type) || hidden.has(e.target.type)) return null;
              const lit = !!focus && (e.source.slug === focus || e.target.slug === focus);
              return (
                <path
                  key={i}
                  ref={(el) => {
                    edgeEls.current[i] = el;
                    el?.setAttribute('d', edgePath(e));
                  }}
                  className={`wiki-edge ${lit ? 'wiki-edge-lit' : focus ? 'wiki-edge-dim' : ''}`}
                  markerEnd={lit ? 'url(#wiki-arrow)' : undefined}
                />
              );
            })}
          </g>
          <g>
            {sim.nodes.map((n) => {
              if (hidden.has(n.type)) return null;
              return (
                <g
                  key={n.slug}
                  data-slug={n.slug}
                  ref={(el) => track(nodeEls.current, n, el)}
                  className={`wiki-node ${nodeState(n)}`}
                  style={{ color: NODE_COLOR[n.type] }}
                >
                  <circle className="wiki-node-halo" r={n.r + 8} />
                  <circle className="wiki-node-body" r={n.r} />
                  <circle className="wiki-node-gloss" r={n.r * 0.35} cx={-n.r * 0.3} cy={-n.r * 0.35} />
                </g>
              );
            })}
          </g>
          {/* labels sit in their own layer so no node is drawn over them */}
          <g>
            {sim.nodes.map((n) => {
              if (hidden.has(n.type)) return null;
              return (
                <g key={n.slug} ref={(el) => track(labelEls.current, n, el)} className={`wiki-label ${nodeState(n)}`}>
                  <text x={n.r + 5} y={4}>
                    {n.title.length > 18 ? `${n.title.slice(0, 18)}…` : n.title}
                  </text>
                </g>
              );
            })}
          </g>
        </g>
      </svg>

      <div className="graph-panel graph-search">
        <Search size={14} />
        <input
          placeholder="搜索节点，回车定位"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && match && focusNode(match.slug)}
        />
      </div>
      <div className="graph-panel graph-legend">
        {counts.map(([t, n]) => (
          <button
            key={t}
            className={`graph-legend-item ${hidden.has(t) ? 'graph-legend-off' : ''}`}
            title={hidden.has(t) ? `显示${TYPE_LABEL[t]}节点` : `隐藏${TYPE_LABEL[t]}节点`}
            onClick={() => {
              setHidden((cur) => {
                const next = new Set(cur);
                if (next.has(t)) next.delete(t);
                else next.add(t);
                return next;
              });
              if (selectedNode?.type === t) setSelected(null);
            }}
          >
            <i style={{ background: NODE_COLOR[t], color: NODE_COLOR[t] }} />
            {TYPE_LABEL[t]}
            <em>{n}</em>
          </button>
        ))}
      </div>
      <div className="graph-panel graph-tools">
        <button className="icon-btn" title="放大" onClick={() => zoomAt(1.3)}>
          <ZoomIn size={16} />
        </button>
        <button className="icon-btn" title="缩小" onClick={() => zoomAt(1 / 1.3)}>
          <ZoomOut size={16} />
        </button>
        <button className="icon-btn" title="适应画布" onClick={() => fit()}>
          <Maximize size={16} />
        </button>
        <span className="graph-tools-sep" />
        <button className="icon-btn" title="重新布局" onClick={relayout}>
          <Shuffle size={16} />
        </button>
        <button className="icon-btn" title="释放所有固定节点" onClick={unpinAll}>
          <PinOff size={16} />
        </button>
      </div>
      <div className="graph-panel graph-tip">
        <span><kbd>拖动</kbd>节点/画布</span>
        <span><kbd>滚轮</kbd>缩放</span>
        <span><kbd>双击</kbd>打开页面</span>
      </div>
      {selectedNode ? (
        <NodeCard
          kbId={kbId}
          node={selectedNode}
          neighbors={[...(sim.neighbors.get(selectedNode.slug) ?? [])].map((s) => sim.bySlug.get(s)!).filter((n) => !hidden.has(n.type))}
          onClose={() => setSelected(null)}
          onOpen={() => onOpen(selectedNode.slug)}
          onPick={focusNode}
          onFocus={() => fit([selectedNode, ...[...(sim.neighbors.get(selectedNode.slug) ?? [])].map((s) => sim.bySlug.get(s)!)])}
        />
      ) : null}
    </div>
  );
}

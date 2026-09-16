import { Fragment, useCallback, useEffect, useId, useRef, useState } from 'react';
import { Maximize2, Pencil, Scan, ZoomIn, ZoomOut } from 'lucide-react';
import { displayPackageEntryName } from '../../legacyArchiveFilename.js';
import { actions, store } from '../lib/appStore.js';
import { displayEvidenceReferences } from '../lib/evidence.js';
import { useStore } from '../lib/store.js';
import { flow as demoFlow } from '../lib/mock/fixtures.js';
import LegacyFlowView from './LegacyFlowView.jsx';
import { parseMmdPresentation, renderNativeMmd } from './mmdPresentation.js';

const selNode = (s) => s.focus.node;
const selFocus = (s) => s.focus;
const selDemo = (s) => s.demo;
const selContents = (s) => s.fileContents;
const selMaterials = (s) => s.materials;

/**
 * 按宽度贴合视口，高度只用来限制"放多大"，不参与缩小——
 * 视口本身纵向可滚动，用高度去压会把长流程图缩成一团。
 * 上限跟放大按钮一致给到 1.8：窄长的图原尺寸只占画布一角，值得放大，但不该放到失真。
 */
export function fitMermaidZoom(
  viewWidth,
  availableWidth,
  viewHeight = 0,
  availableHeight = 0,
  { minZoom = 0.25, maxZoom = 1.8 } = {},
) {
  const diagramWidth = Number(viewWidth);
  const viewportWidth = Number(availableWidth);
  if (!Number.isFinite(diagramWidth) || diagramWidth <= 0
    || !Number.isFinite(viewportWidth) || viewportWidth <= 0) return 1;
  let fitted = viewportWidth / diagramWidth;
  const diagramHeight = Number(viewHeight);
  const viewportHeight = Number(availableHeight);
  if (Number.isFinite(diagramHeight) && diagramHeight > 0
    && Number.isFinite(viewportHeight) && viewportHeight > 0) {
    fitted = Math.min(fitted, Math.max(1, viewportHeight / diagramHeight));
  }
  return Math.max(minZoom, Math.min(maxZoom, Math.floor(fitted * 1_000) / 1_000));
}

/* Mermaid 按需加载；解析失败时保留轻量流程视图，保证中栏始终有可读内容。 */
export default function FlowView() {
  const focusNode = useStore(store, selNode);
  const focus = useStore(store, selFocus);
  const demo = useStore(store, selDemo);
  const contents = useStore(store, selContents);
  const materials = useStore(store, selMaterials);
  const source = contents[focus.target];
  const readableSource = displayEvidenceReferences(source, demo ? [] : materials);

  return (
    <div className="wb-flow-view">
      <MermaidPreview source={source} readableSource={readableSource} path={focus.target}
        focusNode={focusNode} fallbackFlow={demo ? demoFlow : undefined}
        note={`${focusNode ? `助手正指向节点 ${focusNode}` : '点左栏文件或消息上的定位入口可切换视图'}`} />
    </div>
  );
}

export function MermaidPreview({
  source,
  readableSource = source,
  path = '',
  focusNode,
  fallbackFlow,
  sourceLabel = '源码',
  sourceView = null,
  note = '',
}) {
  const [mode, setMode] = useState('diagram');
  const [presentation, setPresentation] = useState(null);
  const controller = useRef(null);
  const current = presentation?.source === source && presentation?.path === path ? presentation : null;
  const legacy = current?.kind === 'legacy';

  useEffect(() => {
    let active = true;
    if (!source) return;
    void parseMmdPresentation(source).then((result) => {
      if (active) setPresentation({ ...result, source, path });
    }).catch((error) => {
      if (active) setPresentation({ kind: 'error', source, path,
        message: error instanceof Error ? error.message : '流程图语法无法解析' });
    });
    return () => { active = false; };
  }, [source, path]);
  const [zoom, setZoom] = useState(1);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(null);
  const [fitZoom, setFitZoom] = useState(1);
  const flow = fallbackFlow || parseFlow(source, path);
  const box = useRef(null);
  const handleFit = useCallback((value) => {
    setFitZoom(value);
    setZoom(value);
  }, []);

  useEffect(() => {
    if (!focusNode) return;
    const el = [...(box.current?.querySelectorAll('[data-node]') || [])]
      .find((node) => node.dataset.node === focusNode);
    el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [focusNode]);

  let diagramContent;
  if (legacy) {
    diagramContent = <LegacyFlowView key={`${path}:${source}`} graph={current.graph} focusNode={focusNode}
      controller={controller} onZoom={setZoom} editable={editing} onDraftChange={setDraft} />;
  } else if (current?.kind === 'native') {
    diagramContent = <>
      <p className="t-sm muted">此图包含特殊结构或样式，保留原始图形展示。</p>
      <MermaidDiagram key={`${path}:${source}`} source={source} flow={flow} focusNode={focusNode}
        zoom={zoom} box={box} onFit={handleFit} />
    </>;
  } else {
    let fallbackContent;
    if (current?.kind === 'error') {
      fallbackContent = <div className="wb-flow-error" role="alert"><b>流程图暂时无法渲染</b>
        <span>请切换到源码检查语法。</span><span>{current.message}</span></div>;
    } else {
      fallbackContent = <><span className="t-sm muted" role="status">正在读取流程图…</span>
        <div className="wb-flow">{flow.nodes.map((node) => <Node key={node.id} node={node} first focus={focusNode} />)}</div></>;
    }
    diagramContent = <div className="wb-mermaid-viewport" ref={box}>{fallbackContent}</div>;
  }
  let sourceContent = sourceView;
  if (!sourceContent) {
    sourceContent = <pre className="wb-flow-source">
      {source === undefined ? '正在读取流程源码…' : readableSource}
    </pre>;
  }

  return (
    <>
      <div className="wb-flow-toolbar">
        <div className="seg" role="tablist" aria-label="流程图查看方式">
          <button role="tab" aria-selected={mode === 'diagram'} className={mode === 'diagram' ? 'on' : ''}
            onClick={() => setMode('diagram')}>图形</button>
          <button role="tab" aria-selected={mode === 'source'} className={mode === 'source' ? 'on' : ''}
            onClick={() => setMode('source')}>{sourceLabel}</button>
        </div>
        {mode === 'diagram' && <div className="wb-flow-zoom" aria-label="流程图缩放">
          <button className="btn btn-ghost" title="缩小" aria-label="缩小流程图" disabled={zoom <= (legacy ? 0.05 : 0.1)}
            onClick={() => legacy ? controller.current?.zoomOut() : setZoom((value) => Math.max(0.1, Number((value - 0.1).toFixed(2))))}><ZoomOut size={14} /></button>
          <span>{Math.round(zoom * 100)}%</span>
          <button className="btn btn-ghost" title="放大" aria-label="放大流程图" disabled={zoom >= 1.8}
            onClick={() => legacy ? controller.current?.zoomIn() : setZoom((value) => Math.min(1.8, Number((value + 0.1).toFixed(1))))}><ZoomIn size={14} /></button>
          <button className="btn btn-ghost" title="适合窗口" aria-label="流程图适合窗口"
            onClick={() => legacy ? controller.current?.fit() : setZoom(fitZoom)}><Maximize2 size={14} /></button>
          {legacy && <>
            <button className="btn btn-ghost" title="查看全图" aria-label="查看全图"
              onClick={() => controller.current?.overview()}><Scan size={14} /></button>
            <button className="btn btn-ghost wb-flow-reset" title="原始大小" aria-label="恢复 100%"
              onClick={() => controller.current?.reset()}>100%</button>
            {/* 编辑只改前端草稿，落盘要另点「保存为候选」，和其它变更一样过确认 */}
            <button className={`btn btn-ghost${editing ? ' is-on' : ''}`} aria-pressed={editing}
              title={editing ? '退出编辑' : '编辑流程图'} aria-label="编辑流程图"
              onClick={() => { setEditing((on) => !on); setDraft(null); }}><Pencil size={14} /></button>
            {editing && draft?.dirty && <button className="btn btn-p btn-sm" type="button"
              onClick={() => actions.saveFlowDraft?.(path, draft)}>保存为候选（{draft.edges.length} 条新连线）</button>}
          </>}
        </div>}
      </div>
      {mode === 'diagram' ? <>
        {diagramContent}
        {note && <p className="t-sm muted" style={{ textAlign: 'center', marginTop: 'var(--s-6)' }}>{note}</p>}
      </> : sourceContent}
    </>
  );
}

function MermaidDiagram({ source, flow, focusNode, zoom, box, onFit }) {
  const renderKey = useId().replaceAll(':', '');
  const [svg, setSvg] = useState('');
  const [error, setError] = useState('');
  const [dimensions, setDimensions] = useState(null);

  useEffect(() => {
    let active = true;
    if (!source) {
      setSvg('');
      setError('');
      setDimensions(null);
      return () => { active = false; };
    }
    void renderNativeMmd(`wbMermaid${renderKey}`, source).then((rendered) => {
        if (active) {
          setSvg(rendered.svg);
          setError('');
          setDimensions(null);
        }
    }).catch((renderError) => {
      if (active) {
        setSvg('');
        setError(renderError instanceof Error ? renderError.message : '流程图渲染器加载失败');
      }
    });
    return () => { active = false; };
  }, [renderKey, source]);

  useEffect(() => {
    if (!svg) return;
    let frame;
    const fit = () => {
      const svgNode = box.current?.querySelector('.wb-mermaid-svg svg');
      const viewWidth = svgNode?.viewBox?.baseVal?.width || 0;
      const viewHeight = svgNode?.viewBox?.baseVal?.height || 0;
      const available = Math.max(1, (box.current?.clientWidth || 0) - 40);
      const availableHeight = Math.max(1, (box.current?.clientHeight || 0) - 40);
      if (viewWidth > 0 && viewHeight > 0) {
        setDimensions((current) => current?.width === viewWidth && current?.height === viewHeight
          ? current : { width: viewWidth, height: viewHeight });
        onFit(fitMermaidZoom(viewWidth, available, viewHeight, availableHeight));
      }
    };
    frame = requestAnimationFrame(fit);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(fit);
    const layoutContainer = box.current?.closest('.wb-pane-c') || box.current;
    if (layoutContainer) observer?.observe(layoutContainer);
    return () => {
      cancelAnimationFrame(frame);
      observer?.disconnect();
    };
  }, [box, onFit, svg]);

  useEffect(() => {
    const nodes = box.current?.querySelectorAll('g.node') || [];
    let focused;
    for (const node of nodes) {
      const matches = Boolean(focusNode && (
        node.id === focusNode || node.id.includes(`-${focusNode}-`) || node.dataset.id === focusNode
      ));
      node.classList.toggle('wb-mermaid-focus', matches);
      if (matches) focused = node;
    }
    focused?.scrollIntoView({ block: 'center', inline: 'center', behavior: 'smooth' });
  }, [box, focusNode, svg, zoom]);

  const canvasStyle = svg && dimensions
    ? { width: dimensions.width * zoom, height: dimensions.height * zoom }
    : { transform: `scale(${zoom})` };

  return (
    <div className="wb-mermaid-viewport" ref={box} aria-label="Mermaid 流程图">
      {error && <div className="wb-flow-error"><b>图形暂时无法完整渲染</b><span>已显示简化流程，可切换到源码检查语法。</span></div>}
      <div className="wb-mermaid-scale" style={canvasStyle}>
        {svg
          ? <div className="wb-mermaid-svg" dangerouslySetInnerHTML={{ __html: svg }} />
          : <div className="wb-flow">
            {flow.nodes.map((node, index) => <Node key={node.id} node={node} first={index === 0} focus={focusNode} />)}
          </div>}
      </div>
    </div>
  );
}

function parseFlow(source = '', path = '') {
  const nodes = new Map();
  const edges = [];
  const nodePattern = /\b([A-Za-z0-9_]+)\s*(\[([^\]]+)\]|\{([^}]+)\}|\(([^)]+)\))/g;
  for (const match of source.matchAll(nodePattern)) {
    const id = match[1];
    const text = (match[3] || match[4] || match[5] || id).replace(/^"|"$/g, '');
    const kind = (() => { if (match[4]) return 'decision'; return match[5] ? 'terminal' : 'step'; })();
    nodes.set(id, { id, text, kind, tag: match[4] ? id : undefined });
  }
  // Mermaid 允许在连线两端直接写节点定义。先规整为节点 ID，避免
  // `A[开始] --> B{判断}` 被误认为没有连线。
  const graphSource = source.replace(nodePattern, '$1');
  const edgePattern = /([A-Za-z0-9_]+)\s*-->(?:\|([^|]+)\|)?\s*([A-Za-z0-9_]+)/g;
  for (const match of graphSource.matchAll(edgePattern)) edges.push({ from: match[1], to: match[3], label: match[2] });
  const ordered = [];
  const seen = new Set();
  let current = edges[0]?.from || nodes.keys().next().value;
  while (current && !seen.has(current)) {
    seen.add(current);
    const node = nodes.get(current);
    if (node) ordered.push(node);
    const edge = edges.find((item) => item.from === current && !seen.has(item.to));
    current = edge?.to;
    if (current && nodes.has(current)) nodes.get(current).edgeLabel = edge?.label;
  }
  for (const node of nodes.values()) if (!seen.has(node.id)) ordered.push(node);
  if (!ordered.length && source) ordered.push({ id: 'source', text: '流程源码暂无法图形化，请切到文件内容查看', kind: 'step' });
  return { title: displayPackageEntryName(path).replace(/\.mmd$/, '') || '流程', nodes: ordered };
}

function Node({ node, first, focus }) {
  const cls = ['wb-node', node.kind === 'decision' && 'decision', node.kind === 'terminal' && 'terminal',
    node.id === focus && 'hl', node.added && 'added'].filter(Boolean).join(' ');
  return (
    <>
      {!first && <div className="wb-edge" data-label={node.edgeLabel || undefined} />}
      <div className={cls} data-node={node.id}>
        {node.kind === 'decision'
          ? <span>{node.tag && <span className="tag">{node.tag}</span>}<NodeText text={node.text} /></span>
          : <NodeText text={node.text} />}
      </div>
    </>
  );
}

function NodeText({ text }) {
  return String(text || '').split(/<br\s*\/?>/i).map((line, index) => (
    <Fragment key={`${index}-${line}`}>
      {index > 0 && <br />}
      {line.replace(/<\/?(?:b|strong)>/gi, '')}
    </Fragment>
  ));
}

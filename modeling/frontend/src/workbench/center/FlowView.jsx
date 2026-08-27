import { Fragment, useCallback, useEffect, useId, useRef, useState } from 'react';
import { Maximize2, ZoomIn, ZoomOut } from 'lucide-react';
import { displayPackageEntryName } from '../../legacyArchiveFilename.js';
import { store } from '../lib/appStore.js';
import { displayEvidenceReferences } from '../lib/evidence.js';
import { useStore } from '../lib/store.js';
import { flow as demoFlow } from '../lib/mock/fixtures.js';

const selNode = (s) => s.focus.node;
const selFocus = (s) => s.focus;
const selDemo = (s) => s.demo;
const selContents = (s) => s.fileContents;
const selMaterials = (s) => s.materials;

export function fitMermaidZoom(viewWidth, availableWidth, minZoom = 0.25) {
  const diagramWidth = Number(viewWidth);
  const viewportWidth = Number(availableWidth);
  if (!Number.isFinite(diagramWidth) || diagramWidth <= 0
    || !Number.isFinite(viewportWidth) || viewportWidth <= 0) return 1;
  const fitted = Math.floor((viewportWidth / diagramWidth) * 1_000) / 1_000;
  return Math.max(minZoom, Math.min(1, fitted));
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
        note={`${focusNode ? `助手正指向节点 ${focusNode}` : '点左栏文件或消息上的定位入口可切换视图'} · 绿色节点为本轮新增`} />
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
  const [zoom, setZoom] = useState(1);
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
          <button className="btn btn-ghost" title="缩小" aria-label="缩小流程图" disabled={zoom <= 0.1}
            onClick={() => setZoom((value) => Math.max(0.1, Number((value - 0.1).toFixed(2))))}><ZoomOut size={14} /></button>
          <span>{Math.round(zoom * 100)}%</span>
          <button className="btn btn-ghost" title="放大" aria-label="放大流程图" disabled={zoom >= 1.8}
            onClick={() => setZoom((value) => Math.min(1.8, Number((value + 0.1).toFixed(1))))}><ZoomIn size={14} /></button>
          <button className="btn btn-ghost" title="适合窗口" aria-label="流程图适合窗口"
            onClick={() => setZoom(fitZoom)}><Maximize2 size={14} /></button>
        </div>}
      </div>
      {mode === 'diagram' ? <>
        <MermaidDiagram source={source} flow={flow} focusNode={focusNode} zoom={zoom} box={box}
          onFit={handleFit} />
        {note && <p className="t-sm muted" style={{ textAlign: 'center', marginTop: 'var(--s-6)' }}>{note}</p>}
      </> : (
        sourceView || <pre className="wb-flow-source">{source === undefined ? '正在读取流程源码…' : readableSource}</pre>
      )}
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
    void import('mermaid').then(async ({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        theme: 'base',
        themeVariables: {
          fontFamily: 'system-ui, sans-serif',
          primaryColor: '#ffffff',
          primaryTextColor: '#252a31',
          primaryBorderColor: '#aeb8c7',
          lineColor: '#8793a5',
          secondaryColor: '#f3f6fa',
          tertiaryColor: '#edf5ef',
        },
        flowchart: { htmlLabels: true, useMaxWidth: true, curve: 'basis' },
      });
      try {
        const rendered = await mermaid.render(`wbMermaid${renderKey}`, source);
        if (active) {
          setSvg(rendered.svg);
          setError('');
          setDimensions(null);
        }
      } catch (renderError) {
        if (active) {
          setSvg('');
          setError(renderError instanceof Error ? renderError.message : '流程图语法暂时无法渲染');
        }
      }
    }).catch((loadError) => {
      if (active) setError(loadError instanceof Error ? loadError.message : '流程图渲染器加载失败');
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
      if (viewWidth > 0 && viewHeight > 0) {
        setDimensions((current) => current?.width === viewWidth && current?.height === viewHeight
          ? current : { width: viewWidth, height: viewHeight });
        onFit(fitMermaidZoom(viewWidth, available));
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
    const kind = match[4] ? 'decision' : match[5] ? 'terminal' : 'step';
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

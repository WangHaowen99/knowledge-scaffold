// drawio 只读预览：用 /drawio/js/viewer-static.min.js 渲染（后端挂载的官方 webapp 自带），可平移、缩放、翻页
import { useEffect, useRef, useState } from 'react';
import { Button, Note, Select } from '../ui/index.jsx';
import '../styles/flowchart.css';

let viewerScriptPromise;
function loadViewer() {
  if (window.GraphViewer) return Promise.resolve();
  if (!viewerScriptPromise) {
    viewerScriptPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      const finish = (error) => {
        clearTimeout(timeout);
        script.onload = null;
        script.onerror = null;
        if (error) {
          script.remove();
          viewerScriptPromise = null;
          reject(error);
        } else resolve();
      };
      const timeout = setTimeout(() => finish(new Error('timeout')), 20000);
      script.src = '/drawio/js/viewer-static.min.js';
      script.onload = () => finish(window.GraphViewer ? null : new Error('unavailable'));
      script.onerror = () => finish(new Error('unavailable'));
      document.head.appendChild(script);
    });
  }
  return viewerScriptPromise;
}

export default function DrawioPreview({ xml, hint = '拖动画布查看 · 点击「编辑流程图」修改节点和连线' }) {
  const holderRef = useRef(null);
  const viewerRef = useRef(null);
  const fitRef = useRef(() => {});
  const [state, setState] = useState('loading');
  const [attempt, setAttempt] = useState(0);
  const [scale, setScale] = useState(100);
  const [pages, setPages] = useState([]);
  const [page, setPage] = useState(0);

  useEffect(() => {
    let active = true;
    let observer;
    let frame;
    const holder = holderRef.current;
    setState('loading');
    loadViewer().then(() => {
      if (!active) return;
      const el = document.createElement('div');
      el.className = 'dio-canvas';
      el.style.width = '100%';
      el.style.height = '100%';
      el.dataset.mxgraph = JSON.stringify({
        xml, nav: true, resize: false, responsive: false, 'auto-fit': false, 'auto-crop': false, center: true, border: 24,
      });
      holder.replaceChildren(el);
      window.GraphViewer.createViewerForElement(el, (viewer) => {
        if (!active) { viewer.graph?.destroy(); return; }
        viewerRef.current = viewer;
        const { graph } = viewer;
        // 视口大小由外层决定，缩放不能把父容器撑大
        graph.resizeContainer = false;
        graph.setPanning(true);
        graph.panningHandler.useLeftButtonForPanning = true;
        el.style.overflow = 'hidden';
        fitRef.current = () => {
          if (!active || !el.clientWidth || !el.clientHeight) return;
          graph.maxFitScale = 1.5;
          graph.fit(24, null, null, null, null, false);
          graph.center(true, true);
          setScale(Math.round(graph.view.scale * 100));
        };
        const scheduleFit = () => {
          cancelAnimationFrame(frame);
          frame = requestAnimationFrame(() => fitRef.current());
        };
        observer = new ResizeObserver(scheduleFit);
        observer.observe(holder);
        viewer.addListener('pageSelected', scheduleFit);
        const diagrams = Array.from(viewer.diagrams || []);
        setPages(diagrams.map((d, i) => ({ value: i, label: d.getAttribute('name') || `第 ${i + 1} 页` })));
        setPage(viewer.currentPage || 0);
        setState('ready');
        scheduleFit();
      });
    }).catch(() => active && setState('error'));
    return () => {
      active = false;
      observer?.disconnect();
      cancelAnimationFrame(frame);
      fitRef.current = () => {};
      viewerRef.current?.graph?.destroy();
      viewerRef.current = null;
      holder.replaceChildren();
    };
  }, [xml, attempt]);

  const zoom = (value) => {
    const graph = viewerRef.current?.graph;
    if (!graph) return;
    graph.zoomTo(Math.min(4, Math.max(0.1, value)));
    graph.center(true, true);
    setScale(Math.round(graph.view.scale * 100));
  };
  const ready = state === 'ready';

  return (
    <div className="dio-preview">
      <div className="dio-ptb">
        <span className="xs muted b">预览</span>
        {pages.length > 1 ? (
          <Select
            w={180} value={page} options={pages}
            onChange={(v) => { viewerRef.current?.selectPage(v); setPage(v); fitRef.current(); }}
          />
        ) : null}
        <span className="sp" />
        <Button size="sm" kind="ghost" icon="zoomout" title="缩小" disabled={!ready} onClick={() => zoom(scale / 120)} />
        <Button size="sm" kind="ghost" className="num" title="恢复实际大小" disabled={!ready} onClick={() => zoom(1)}>{scale}%</Button>
        <Button size="sm" kind="ghost" icon="zoomin" title="放大" disabled={!ready} onClick={() => zoom((scale * 1.2) / 100)} />
        <Button size="sm" icon="maximize" disabled={!ready} onClick={() => fitRef.current()}>适应窗口</Button>
      </div>
      <div className="dio-viewport">
        <div ref={holderRef} className="dio-viewer" />
        {!ready ? (
          <div className="dio-mask" role="status">
            {state === 'error' ? (
              <Note tone="amber">流程图预览加载失败。<Button size="xs" kind="ghost" icon="refresh" onClick={() => setAttempt((v) => v + 1)}>重试</Button></Note>
            ) : <><span className="spin" />正在加载流程图…</>}
          </div>
        ) : null}
      </div>
      {hint ? <div className="dio-hint">{hint}</div> : null}
    </div>
  );
}

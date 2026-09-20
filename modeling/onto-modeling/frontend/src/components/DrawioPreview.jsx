import { AimOutlined, MinusOutlined, PlusOutlined } from '@ant-design/icons';
import { Alert, Button, Select, Spin, Tooltip } from 'antd';
import { useEffect, useRef, useState } from 'react';

let viewerScriptPromise;
function loadViewer() {
  if (window.GraphViewer) return Promise.resolve();
  if (!viewerScriptPromise) {
    viewerScriptPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      const timeout = setTimeout(() => finish(new Error('timeout')), 20000);
      const finish = (error) => {
        clearTimeout(timeout);
        script.onload = script.onerror = null;
        if (error) {
          script.remove();
          viewerScriptPromise = null;
          reject(error);
        } else resolve();
      };
      script.src = '/drawio/js/viewer-static.min.js';
      script.onload = () => finish(window.GraphViewer ? null : new Error('unavailable'));
      script.onerror = () => finish(new Error('unavailable'));
      document.head.appendChild(script);
    });
  }
  return viewerScriptPromise;
}

export default function DrawioPreview({ xml }) {
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
      el.className = 'drawio-canvas';
      el.style.width = '100%';
      el.style.height = '100%';
      el.dataset.mxgraph = JSON.stringify({
        xml, nav: true, resize: false, responsive: false,
        'auto-fit': false, 'auto-crop': false, center: true, border: 24,
      });
      holder.replaceChildren(el);
      window.GraphViewer.createViewerForElement(el, (viewer) => {
        if (!active) { viewer.graph?.destroy(); return; }
        viewerRef.current = viewer;
        const graph = viewer.graph;
        // The viewport owns its size. Zooming must not grow the parent column.
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
        setPages(diagrams.map((diagram, index) => ({
          value: index, label: diagram.getAttribute('name') || `第 ${index + 1} 页`,
        })));
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

  return (
    <div className="drawio-preview">
      <div className="drawio-preview-toolbar">
        <span className="drawio-mode-label">预览</span>
        {pages.length > 1 && <Select size="small" value={page} options={pages}
          className="drawio-page-picker" onChange={(value) => {
            viewerRef.current?.selectPage(value);
            setPage(value);
            fitRef.current();
          }} />}
        <span className="drawio-toolbar-spacer" />
        <Tooltip title="缩小"><Button size="small" type="text" aria-label="缩小"
          icon={<MinusOutlined />} disabled={state !== 'ready'} onClick={() => zoom(scale / 120)} /></Tooltip>
        <Button size="small" type="text" className="drawio-scale" title="恢复实际大小"
          disabled={state !== 'ready'} onClick={() => zoom(1)}>{scale}%</Button>
        <Tooltip title="放大"><Button size="small" type="text" aria-label="放大"
          icon={<PlusOutlined />} disabled={state !== 'ready'} onClick={() => zoom(scale * 1.2 / 100)} /></Tooltip>
        <Button size="small" icon={<AimOutlined />} disabled={state !== 'ready'}
          onClick={() => fitRef.current()}>适应窗口</Button>
      </div>
      <div className="drawio-viewport">
        <div ref={holderRef} className="drawio-viewer" />
        {state !== 'ready' && <div className="drawio-loading" role="status">
          {state === 'error' ? <Alert type="warning" showIcon message="流程图预览加载失败"
            action={<Button size="small" onClick={() => setAttempt(attempt + 1)}>重试</Button>} />
            : <><Spin />正在加载流程图…</>}
        </div>}
      </div>
      <div className="drawio-hint">拖动画布查看 · 点击「编辑流程图」修改节点和连线</div>
    </div>
  );
}

import { Spin } from 'antd';
import { Application, Container, Graphics, Text } from 'pixi.js';
import { useEffect, useRef, useState } from 'react';

/** WebGL 大图运行时：PixiJS 渲染 + Worker 力导布局（方案 7.1 原班路径）。
 * 面向上千节点的本体：Canvas/WebGL 单画布绘制，滚轮缩放、拖拽平移、
 * 点击节点进条目，缩小时自动隐藏文字标签保持流畅。 */
export default function StudioBigGraph({ graph, kindMeta, query, selected, onNodeClick }) {
  const holderRef = useRef(null);
  const stateRef = useRef(null); // {app, world, nodeViews, edgesG, worker}
  const [ready, setReady] = useState(false);

  // 初始化 pixi 应用与力导 Worker（graph 变化时整体重建）
  useEffect(() => {
    let destroyed = false;
    const holder = holder0();
    if (!holder) return undefined;

    const css = getComputedStyle(document.documentElement);
    const cssColor = (name, fallback) => {
      const value = (css.getPropertyValue(name) || '').trim();
      return value || fallback;
    };
    const bg = cssColor('--surface-page', '#f3f5f8');
    const edgeColor = cssColor('--text-tertiary', '#6f7785');
    const textColor = cssColor('--text-primary', '#111827');

    const app = new Application();
    const worker = new Worker(new URL('./forceLayout.worker.js', import.meta.url), {
      type: 'module',
    });

    (async () => {
      await app.init({ background: bg, antialias: true, resizeTo: holder });
      if (destroyed) {
        app.destroy(true);
        return;
      }
      holder.appendChild(app.canvas);
      const world = new Container();
      app.stage.addChild(world);
      const edgesG = new Graphics();
      world.addChild(edgesG);

      // 节点视图：对象=圆、逻辑=圆角矩形；标签在低缩放时隐藏
      const nodeViews = new Map();
      for (const node of graph.nodes) {
        const meta = kindMeta[node.kind] || kindMeta.object;
        const view = new Container();
        const shape = new Graphics();
        if (node.kind === 'object') {
          shape.circle(0, 0, 34).fill(meta.soft).stroke({ width: 2, color: meta.color });
        } else {
          shape
            .roundRect(-52, -20, 104, 40, 10)
            .fill(meta.soft)
            .stroke({ width: 2, color: meta.color });
        }
        const label = new Text({
          text: node.id.length > 10 ? `${node.id.slice(0, 10)}…` : node.id,
          style: { fontSize: 13, fill: textColor, fontWeight: '600' },
        });
        label.anchor.set(0.5);
        view.addChild(shape, label);
        view.eventMode = 'static';
        view.cursor = 'pointer';
        view.on('pointertap', (e) => {
          if (!dragState.moved) onNodeClick?.(node.id);
        });
        world.addChild(view);
        nodeViews.set(node.id, { view, label, node });
      }

      const drawEdges = () => {
        edgesG.clear();
        for (const edge of graph.edges) {
          const a = nodeViews.get(edge.source)?.view;
          const b = nodeViews.get(edge.target)?.view;
          if (!a || !b) continue;
          edgesG.moveTo(a.x, a.y).lineTo(b.x, b.y);
        }
        edgesG.stroke({ width: 1, color: edgeColor, alpha: 0.35 });
      };

      const fitView = () => {
        if (!nodeViews.size) return;
        let minX = Infinity;
        let minY = Infinity;
        let maxX = -Infinity;
        let maxY = -Infinity;
        for (const { view } of nodeViews.values()) {
          minX = Math.min(minX, view.x);
          minY = Math.min(minY, view.y);
          maxX = Math.max(maxX, view.x);
          maxY = Math.max(maxY, view.y);
        }
        const w = Math.max(maxX - minX, 1) + 200;
        const h = Math.max(maxY - minY, 1) + 200;
        const scale = Math.min(app.screen.width / w, app.screen.height / h, 1.2);
        world.scale.set(scale);
        world.position.set(
          app.screen.width / 2 - ((minX + maxX) / 2) * scale,
          app.screen.height / 2 - ((minY + maxY) / 2) * scale,
        );
        updateLabelVisibility();
      };

      const updateLabelVisibility = () => {
        const show = world.scale.x > 0.45;
        for (const { label } of nodeViews.values()) label.visible = show;
      };

      // 平移（空白处拖拽）与滚轮缩放（围绕指针）
      const dragState = { active: false, moved: false, x: 0, y: 0 };
      app.stage.eventMode = 'static';
      app.stage.hitArea = app.screen;
      app.stage.on('pointerdown', (e) => {
        dragState.active = true;
        dragState.moved = false;
        dragState.x = e.global.x - world.x;
        dragState.y = e.global.y - world.y;
      });
      app.stage.on('pointermove', (e) => {
        if (!dragState.active) return;
        dragState.moved = true;
        world.position.set(e.global.x - dragState.x, e.global.y - dragState.y);
      });
      const endDrag = () => {
        dragState.active = false;
      };
      app.stage.on('pointerup', endDrag);
      app.stage.on('pointerupoutside', endDrag);
      app.canvas.addEventListener(
        'wheel',
        (e) => {
          e.preventDefault();
          const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
          const next = Math.min(2.5, Math.max(0.08, world.scale.x * factor));
          const rect = app.canvas.getBoundingClientRect();
          const px = e.clientX - rect.left;
          const py = e.clientY - rect.top;
          const worldX = (px - world.x) / world.scale.x;
          const worldY = (py - world.y) / world.scale.y;
          world.scale.set(next);
          world.position.set(px - worldX * next, py - worldY * next);
          updateLabelVisibility();
        },
        { passive: false },
      );

      let fitted = false;
      worker.onmessage = (event) => {
        for (const pos of event.data.positions) {
          const entry = nodeViews.get(pos.id);
          if (entry) entry.view.position.set(pos.x, pos.y);
        }
        drawEdges();
        if (event.data.done && !fitted) {
          fitted = true;
          fitView();
          setReady(true);
        }
      };
      worker.postMessage({
        nodes: graph.nodes.map((n) => ({ id: n.id, kind: n.kind })),
        edges: graph.edges,
      });

      stateRef.current = { app, world, nodeViews, edgesG, worker };
    })();

    function holder0() {
      return holderRef.current;
    }

    return () => {
      destroyed = true;
      setReady(false);
      worker.terminate();
      const state = stateRef.current;
      stateRef.current = null;
      if (state) state.app.destroy(true, { children: true });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph]);

  // 搜索淡化与选中高亮：不重建场景，只改透明度/描边
  useEffect(() => {
    const state = stateRef.current;
    if (!state) return;
    const keyword = (query || '').trim();
    for (const { view, node } of state.nodeViews.values()) {
      const dimmed = keyword ? !node.id.includes(keyword) : false;
      view.alpha = dimmed ? 0.16 : 1;
      view.scale.set(node.id === selected ? 1.18 : 1);
    }
  }, [query, selected, ready]);

  return (
    <div className="studio-biggraph" ref={holderRef}>
      {!ready && (
        <div className="studio-biggraph-loading">
          <Spin tip="Worker 布局计算中…" />
        </div>
      )}
    </div>
  );
}

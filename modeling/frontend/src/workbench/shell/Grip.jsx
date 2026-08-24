import { useCallback, useRef, useState } from 'react';
import { actions } from '../lib/appStore.js';

const LIMITS = { left: [180, 380], right: [300, 560] };

/** 分栏拖拽。宽度写在 DOM style 上并用 rAF 节流，拖动过程不触发 React 重渲染。 */
export default function Grip({ side, paneRef }) {
  const [dragging, setDragging] = useState(false);
  const raf = useRef(0);
  const last = useRef(0);

  const onDown = useCallback((e) => {
    const pane = paneRef.current;
    if (!pane) return;
    e.preventDefault();
    const startX = e.clientX;
    const startW = pane.getBoundingClientRect().width;
    const [min, max] = LIMITS[side];
    setDragging(true);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    const onMove = (ev) => {
      const delta = side === 'left' ? ev.clientX - startX : startX - ev.clientX;
      last.current = Math.min(max, Math.max(min, startW + delta));
      if (raf.current) return;
      raf.current = requestAnimationFrame(() => {
        raf.current = 0;
        pane.style.width = `${last.current}px`;
      });
    };
    const onUp = () => {
      cancelAnimationFrame(raf.current); raf.current = 0;
      setDragging(false);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      if (last.current) actions.setWidth(side, last.current);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  }, [side, paneRef]);

  return (
    <div
      className={`wb-grip wb-grip-${side === 'left' ? 'l' : 'r'}${dragging ? ' dragging' : ''}`}
      onPointerDown={onDown}
      role="separator"
      aria-orientation="vertical"
      aria-label={side === 'left' ? '调整左栏宽度' : '调整右栏宽度'}
    />
  );
}

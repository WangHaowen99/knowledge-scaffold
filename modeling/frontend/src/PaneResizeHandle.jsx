import React, { useEffect, useRef } from 'react';

import { resizePaneWidth } from './paneResize.js';

export default function PaneResizeHandle({
  label,
  value,
  min,
  max,
  direction = 1,
  defaultValue,
  onChange,
  className = '',
}) {
  const cleanupRef = useRef(null);
  useEffect(() => () => cleanupRef.current?.(), []);

  const startResize = (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = value;
    const move = (nextEvent) => {
      onChange(resizePaneWidth(startWidth, nextEvent.clientX - startX, direction, min, max));
    };
    const cleanup = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', cleanup);
      window.removeEventListener('pointercancel', cleanup);
      document.documentElement.classList.remove('pkw-resizing');
      cleanupRef.current = null;
    };
    cleanupRef.current?.();
    cleanupRef.current = cleanup;
    document.documentElement.classList.add('pkw-resizing');
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', cleanup, { once: true });
    window.addEventListener('pointercancel', cleanup, { once: true });
  };

  const onKeyDown = (event) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const pointerDelta = event.key === 'ArrowRight' ? 16 : -16;
    onChange(resizePaneWidth(value, pointerDelta, direction, min, max));
  };

  return (
    <div className={`pane-resize-handle ${className}`.trim()} role="separator" tabIndex={0}
      aria-label={label} aria-orientation="vertical" aria-valuemin={min} aria-valuemax={max}
      aria-valuenow={value} title={`${label}（最大 ${max}px，双击恢复默认）`}
      onPointerDown={startResize} onKeyDown={onKeyDown}
      onDoubleClick={() => onChange(resizePaneWidth(defaultValue ?? value, 0, 1, min, max))}>
      <span aria-hidden="true" />
    </div>
  );
}

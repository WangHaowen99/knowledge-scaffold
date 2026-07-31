export function clampPaneWidth(width, min, max) {
  const safeMin = Number.isFinite(min) ? min : 0;
  const safeMax = Math.max(safeMin, Number.isFinite(max) ? max : safeMin);
  return Math.min(safeMax, Math.max(safeMin, Math.round(width)));
}

export function resizePaneWidth(startWidth, pointerDelta, direction, min, max) {
  return clampPaneWidth(startWidth + pointerDelta * direction, min, max);
}

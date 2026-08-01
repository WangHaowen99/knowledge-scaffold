/**
 * 助手栏宽度按容器比例分配，而不是写死 360/520 这类像素值——
 * 固定值在 2560px 宽屏上会让助手栏显得局促、在 1366px 上又挤掉中栏编辑区。
 *
 * 唯一保留的绝对值是可读性下限：低于它消息会频繁折行、卡片里的按钮会换行堆叠。
 */
export const ASSISTANT_PANE_RATIO = { default: 0.26, min: 0.2, max: 0.38 };
export const ASSISTANT_PANE_MIN_PX = 280;

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), Math.max(min, max));
}

/**
 * 容器很窄时按比例算出的下限会低于可读性下限，此时以可读性下限为准；
 * 若连下限都放不下（超窄窗口），上下限收敛到同一个值，交给 CSS 的单列断点兜底。
 */
export function assistantPaneBounds(containerWidth) {
  const width = Number.isFinite(containerWidth) && containerWidth > 0 ? containerWidth : 0;
  if (!width) return { min: ASSISTANT_PANE_MIN_PX, max: ASSISTANT_PANE_MIN_PX };
  const min = Math.round(Math.max(ASSISTANT_PANE_MIN_PX, width * ASSISTANT_PANE_RATIO.min));
  const max = Math.round(Math.max(min, width * ASSISTANT_PANE_RATIO.max));
  return { min, max };
}

export function assistantPaneWidth(containerWidth, ratio = ASSISTANT_PANE_RATIO.default) {
  const width = Number.isFinite(containerWidth) && containerWidth > 0 ? containerWidth : 0;
  const bounds = assistantPaneBounds(width);
  if (!width) return bounds.min;
  const safeRatio = Number.isFinite(ratio) ? ratio : ASSISTANT_PANE_RATIO.default;
  return Math.round(clamp(width * safeRatio, bounds.min, bounds.max));
}

/** 拖拽产生的是像素宽度，换算回比例后窗口缩放时才能保持同样的版面观感 */
export function assistantPaneRatio(containerWidth, widthPx) {
  const width = Number.isFinite(containerWidth) && containerWidth > 0 ? containerWidth : 0;
  if (!width) return ASSISTANT_PANE_RATIO.default;
  const bounds = assistantPaneBounds(width);
  return clamp(widthPx, bounds.min, bounds.max) / width;
}

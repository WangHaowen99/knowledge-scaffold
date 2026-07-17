export const DEFAULT_BOTTOM_THRESHOLD_PX = 72;

export function distanceFromScrollBottom(metrics = {}) {
  const scrollTop = Number(metrics.scrollTop || 0);
  const scrollHeight = Number(metrics.scrollHeight || 0);
  const clientHeight = Number(metrics.clientHeight || 0);
  if (![scrollTop, scrollHeight, clientHeight].every(Number.isFinite)) return Infinity;
  return Math.max(0, scrollHeight - scrollTop - clientHeight);
}

export function isNearScrollBottom(metrics, threshold = DEFAULT_BOTTOM_THRESHOLD_PX) {
  return distanceFromScrollBottom(metrics) <= Math.max(0, Number(threshold) || 0);
}

export const clampSpotlightRect = (rect, viewport, padding = 8) => {
  const width = Math.max(0, Number(viewport?.width) || 0);
  const height = Math.max(0, Number(viewport?.height) || 0);
  const left = Math.max(4, Math.min(width - 4, rect.left - padding));
  const right = Math.max(left, Math.min(width - 4, rect.right + padding));
  const top = Math.max(4, Math.min(height - 4, rect.top - padding));
  const bottom = Math.max(top, Math.min(height - 4, rect.bottom + padding));
  return { x: left, y: top, w: right - left, h: bottom - top };
};

export const getOnboardingTipStyle = (rect, viewport, cardHeight) => {
  const viewportWidth = Math.max(0, Number(viewport?.width) || 0);
  const viewportHeight = Math.max(0, Number(viewport?.height) || 0);
  const margin = 12;
  const gap = 14;
  const width = Math.min(420, Math.max(280, viewportWidth - margin * 2));
  const measuredHeight = Math.max(0, Number(cardHeight) || 230);
  const height = Math.min(measuredHeight, Math.max(0, viewportHeight - margin * 2));
  const below = rect.y + rect.h + gap;
  const preferredTop = below + height <= viewportHeight - margin
    ? below
    : rect.y - height - gap;
  const top = Math.max(margin, Math.min(preferredTop, viewportHeight - height - margin));
  const left = Math.max(margin, Math.min(rect.x, viewportWidth - width - margin));
  return {
    position: 'fixed',
    left,
    top,
    width,
    maxHeight: Math.max(0, viewportHeight - margin * 2),
    overflowY: 'auto',
  };
};

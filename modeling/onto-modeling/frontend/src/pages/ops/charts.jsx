// 运维中心的小图表（设计稿 core.js 的 bars、spark）；几何计算在 overviewModel.barRects、obsModel.sparkPath
import { barRects } from './overviewModel.js';
import { sparkPath } from './obsModel.js';

export function Bars({ values, w = 240, h = 70, hi = -1, color = '#BFD4FE', title }) {
  const rects = barRects(values || [], { w, h, hi });
  return (
    <svg width={w} height={h} style={{ display: 'block', maxWidth: '100%' }}>
      {rects.map((r, i) => (
        <rect key={i} x={r.x} y={r.y} width={r.w} height={r.h} rx="3" fill={r.hi ? '#2563EB' : color}>
          {title ? <title>{title(i)}</title> : null}
        </rect>
      ))}
    </svg>
  );
}

export function Spark({ points, w = 120, h = 32, color = '#2563EB', fill = 'rgba(37,99,235,.10)' }) {
  const p = sparkPath(points, { w, h });
  if (!p) return <svg width={w} height={h} style={{ display: 'block' }} />;
  return (
    <svg width={w} height={h} style={{ display: 'block' }}>
      <path d={p.area} fill={fill} />
      <path d={p.line} fill="none" stroke={color} strokeWidth="1.8" strokeLinejoin="round" />
    </svg>
  );
}

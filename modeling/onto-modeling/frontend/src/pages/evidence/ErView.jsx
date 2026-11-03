// 证据浏览器中栏的 ER 视图（EV-28）：三栏排版同设计稿，蓝色是选中的表和它的相邻关系；点表选中它
import { useMemo } from 'react';
import { ER_W, fieldKey, layoutEr } from './evidenceModel.js';

const DASH = { inferred: '6 5', observed: '1.5 4' };

export function ErView({ er, focus, selected, selField, domainOf, zoom = 1, onPick }) {
  const lay = useMemo(() => layoutEr(er, focus, { domainOf, selected }), [er, focus, domainOf, selected]);
  const boxes = Object.values(lay.boxes);
  return (
    <svg
      className="evd-er"
      width={`${zoom * 100}%`}
      height={`${zoom * 100}%`}
      viewBox={`0 0 ${lay.width} ${lay.height}`}
      preserveAspectRatio="xMidYMid meet"
    >
      {lay.edges.map((e, i) => (
        <path
          key={i}
          d={e.d}
          fill="none"
          stroke={e.on ? '#2563EB' : '#94A3B8'}
          strokeWidth={e.on ? 1.8 : 1.4}
          strokeDasharray={DASH[e.confidence]}
          strokeLinecap={e.confidence === 'observed' ? 'round' : undefined}
        >
          <title>{`${e.confidence} · ${e.basis || ''}`}</title>
        </path>
      ))}
      {boxes.map(({ x, y, h, t }) => {
        const on = t.anchor === selected;
        const hi = on && selField ? t.fields.findIndex((f) => f.name === selField) : -1;
        const head = on ? '#EAF2FF' : '#F6F9FD';
        return (
          <g key={t.anchor} className="tb" onClick={() => onPick?.(t.anchor)}>
            <title>{`${t.name} · ${t.label || ''}`}</title>
            <rect x={x} y={y} width={ER_W} height={h} rx="10" fill="#fff" stroke={on ? '#2563EB' : '#D6DFEB'} strokeWidth={on ? 2 : 1} />
            <rect x={x} y={y} width={ER_W} height="24" rx="10" fill={head} />
            <rect x={x} y={y + 14} width={ER_W} height="10" fill={head} />
            <text x={x + 10} y={y + 16} className="tn">{t.name}</text>
            {t.fields.map((f, i) => {
              const k = fieldKey(f);
              const ty = y + 26 + 18 * i + 13;
              return (
                <g key={f.name}>
                  <text x={x + 10} y={ty} style={i === hi ? { fill: '#1D4ED8', fontWeight: 700 } : undefined}>{f.name}</text>
                  {k ? <text x={x + ER_W - 10} y={ty} textAnchor="end" className="k" style={k === '脱敏' ? { fill: '#4338CA' } : undefined}>{k}</text> : null}
                </g>
              );
            })}
            {hi >= 0 ? <rect x={x + 4} y={y + 26 + 18 * hi + 1} width={ER_W - 8} height="17" rx="5" fill="#2563EB" opacity=".08" /> : null}
          </g>
        );
      })}
    </svg>
  );
}

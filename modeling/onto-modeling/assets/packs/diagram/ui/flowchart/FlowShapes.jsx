// 流程图的图形：节点、连线、箭头、图例，以及只读的整图 SVG（drawio 降级预览、Studio 预览共用）。
// 画法照设计稿 fcCanvas：开始 / 结束是胶囊，业务步骤圆角矩形，判断菱形，数据对象折角矩形；
// 选中的节点加蓝色描边和浅蓝光晕，口径待定（pending）的节点用琥珀色虚线框。
import { useId } from 'react';
import { EDGE_STYLE, edgeGeometry, fitBox, nodeSize, textWidth } from './flowchartModel.js';

const SEL = '#2563EB';

export function NodeShape({ node, cx, cy, selected, handles = false }) {
  const { w, h } = nodeSize(node);
  const t = node.type;
  const pend = node.pending && !selected;
  const sw = selected ? 2.4 : 1.5;
  const label = node.label;
  if (t === 'start' || t === 'end') {
    return (
      <>
        {selected ? <rect x={cx - w / 2 - 8} y={cy - h / 2 - 7} width={w + 16} height={h + 14} rx={(h + 14) / 2} fill="rgba(37,99,235,.10)" /> : null}
        <rect
          x={cx - w / 2} y={cy - h / 2} width={w} height={h} rx={h / 2}
          fill={t === 'start' ? '#EFF5FF' : '#F1F0FF'} stroke={selected ? SEL : t === 'start' ? '#2563EB' : '#4F46E5'}
          strokeWidth={selected ? 2.4 : 1.5} strokeDasharray={pend ? '4 3' : undefined}
        />
        <text x={cx} y={cy + 4} textAnchor="middle" fontSize="12.5" fontWeight="600">{label}</text>
      </>
    );
  }
  if (t === 'decision') {
    return (
      <>
        <path
          d={`M${cx} ${cy - h / 2}L${cx + w / 2} ${cy}L${cx} ${cy + h / 2}L${cx - w / 2} ${cy}Z`}
          fill="#FFF8EB" stroke={selected ? SEL : '#F59E0B'} strokeWidth={sw} strokeDasharray={pend ? '4 3' : undefined}
        />
        <text x={cx} y={cy + 4} textAnchor="middle" fontSize="12.5" fontWeight={selected ? 700 : undefined} fill={selected ? '#1D4ED8' : undefined}>{label}</text>
      </>
    );
  }
  if (t === 'data') {
    return (
      <>
        <path
          d={`M${cx - w / 2} ${cy - h / 2}h${w - 12}l12 12v${h - 12}h-${w}z`}
          fill="#ECFAF7" stroke={selected ? SEL : '#0D9488'} strokeWidth={selected ? 2.4 : 1.4} strokeDasharray={pend ? '4 3' : undefined}
        />
        <text x={cx} y={cy + 5} textAnchor="middle" fontSize="12" fontWeight={selected ? 700 : undefined} fill={selected ? '#1D4ED8' : undefined}>{label}</text>
      </>
    );
  }
  return (
    <>
      {selected ? <rect x={cx - w / 2 - 8} y={cy - h / 2 - 7} width={w + 16} height={h + 14} rx="14" fill="rgba(37,99,235,.10)" /> : null}
      <rect
        x={cx - w / 2} y={cy - h / 2} width={w} height={h} rx="10"
        fill={pend ? '#FFFBF2' : '#fff'} stroke={selected ? SEL : pend ? '#F5C26B' : '#9BBEFB'} strokeWidth={sw}
        strokeDasharray={pend ? '4 3' : undefined}
      />
      <text
        x={cx} y={cy + 4} textAnchor="middle" fontSize="12.5"
        fontWeight={selected ? 700 : undefined} fill={selected ? '#1D4ED8' : pend ? '#B45309' : undefined}
      >
        {label}
      </text>
      {selected && handles ? [[cx - w / 2, cy], [cx + w / 2, cy], [cx, cy - h / 2], [cx, cy + h / 2]].map(([a, b]) => (
        <circle key={`${a},${b}`} cx={a} cy={b} r="4.5" fill="#fff" stroke={SEL} strokeWidth="1.6" />
      )) : null}
    </>
  );
}

export function EdgeMarkers({ prefix }) {
  return (
    <defs>
      {Object.entries(EDGE_STYLE).map(([k, { color }]) => (
        <marker key={k} id={`${prefix}-${k}`} markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto">
          <path d="M0 0 9 4.5 0 9z" fill={color} />
        </marker>
      ))}
    </defs>
  );
}

export function EdgeShape({ edge, geo, prefix, selected }) {
  const { color, dash } = EDGE_STYLE[edge.type] || EDGE_STYLE.seq;
  const label = edge.label;
  const lw = Math.max(56, Math.round(textWidth(label, 11) + 16));
  return (
    <>
      {selected ? <line x1={geo.x1} y1={geo.y1} x2={geo.x2} y2={geo.y2} stroke="rgba(37,99,235,.22)" strokeWidth="8" strokeLinecap="round" /> : null}
      <line
        x1={geo.x1} y1={geo.y1} x2={geo.x2} y2={geo.y2} stroke={color} strokeWidth="1.7"
        strokeDasharray={dash || undefined} markerEnd={`url(#${prefix}-${edge.type})`}
      />
      {label ? (
        <g>
          <rect x={geo.mx - lw / 2} y={geo.my - 11} width={lw} height="20" rx="6" fill="#fff" stroke={selected ? SEL : color} strokeWidth={selected ? 1.4 : 0.8} />
          <text x={geo.mx} y={geo.my + 3} textAnchor="middle" fontSize="11" fill={color}>{label}</text>
        </g>
      ) : null}
    </>
  );
}

// 只读整图：按 fitBox 取景，宽高跟容器走
export function FlowchartSvg({ model, selection, className }) {
  const prefix = `fcs${useId().replace(/[^a-z0-9]/gi, '')}`;
  const nodes = model?.nodes || [];
  const edges = model?.edges || [];
  const geo = edgeGeometry(nodes, edges);
  const box = fitBox(nodes);
  return (
    <svg className={className} viewBox={`${box.x} ${box.y} ${box.w || 1} ${box.h || 1}`} preserveAspectRatio="xMidYMid meet">
      <EdgeMarkers prefix={prefix} />
      {edges.map((e) => (geo.get(e.id) ? (
        <EdgeShape key={e.id} edge={e} geo={geo.get(e.id)} prefix={prefix} selected={selection?.type === 'edge' && selection.id === e.id} />
      ) : null))}
      {nodes.map((n) => (
        <g key={n.id}><NodeShape node={n} cx={n.x} cy={n.y} selected={selection?.type === 'node' && selection.id === n.id} handles /></g>
      ))}
    </svg>
  );
}

const LEGEND_NODES = [
  ['开始 / 结束', <span key="s" style={{ width: 20, height: 11, borderRadius: 6, border: '1.5px solid #2563EB', background: '#EFF5FF' }} />],
  ['业务步骤', <span key="s" style={{ width: 20, height: 12, borderRadius: 3, border: '1.5px solid #9BBEFB' }} />],
  ['判断', <span key="s" style={{ width: 10, height: 10, transform: 'rotate(45deg)', border: '1.5px solid #F59E0B', background: '#FFF8EB', margin: '0 3px' }} />],
  ['数据对象', <span key="s" style={{ width: 20, height: 12, border: '1.5px solid #0D9488', background: '#ECFAF7' }} />],
];
const LEGEND_EDGES = [['顺序流转', 'seq'], ['条件分支', 'cond'], ['异常流转', 'exc'], ['数据流转', 'data']];

export function FlowLegend() {
  return (
    <div className="fc-leg">
      <div className="lr"><b>节点</b>{LEGEND_NODES.map(([t, sw]) => <span key={t} className="r">{sw}{t}</span>)}</div>
      <div className="lr">
        <b>连线</b>
        {LEGEND_EDGES.map(([t, k]) => (
          <span key={t} className="r">
            <svg width="22" height="6"><line x1="0" y1="3" x2="22" y2="3" stroke={EDGE_STYLE[k].color} strokeWidth="1.8" strokeDasharray={EDGE_STYLE[k].dash || undefined} /></svg>
            {t}
          </span>
        ))}
      </div>
    </div>
  );
}

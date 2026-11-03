// 插件详情的三个页签内容：主线图（它往流程里加了什么）、包含什么、更新记录（设计稿 plugin-detail）
import { Chip, Icon } from '../../ui/index.jsx';
import { changelogView } from './detailModel.js';

// 主线图：坐标由 flowLayout 算好，这里按设计稿的颜色画；最大高度按设计稿 162 → 180 的比例，各个包的节点一样大
export function FlowMap({ layout: L, caption }) {
  return (
    <svg width="100%" viewBox={`0 0 ${L.width} ${L.height}`} style={{ display: 'block', maxHeight: Math.round((L.height * 10) / 9) }} role="img" aria-label={caption}>
      {L.edges.map((e) => (
        <g key={e.x1}>
          <line x1={e.x1} y1={e.y} x2={e.x2} y2={e.y} stroke="#93A1B5" strokeWidth="1.5" />
          <path d={`M${e.x2 - 6} ${e.y - 4} l6 4 -6 4`} fill="none" stroke="#93A1B5" strokeWidth="1.5" />
        </g>
      ))}
      {L.nodes.map((n) => (
        <g key={n.id}>
          <rect x={n.x} y={n.y} width={n.w} height={n.h} rx="12" fill={n.hi ? '#2563EB' : '#fff'} stroke={n.hi ? '#2563EB' : '#C3D8FD'} />
          <text x={n.x + n.w / 2} y={n.y + 28} textAnchor="middle" style={{ fontSize: 13, fontWeight: 600, fill: n.hi ? '#fff' : '#0B1A33' }}>{n.title}</text>
        </g>
      ))}
      {L.pills.map((p) => (
        <g key={p.id}>
          <line x1={p.cx} y1={p.lineFrom} x2={p.cx} y2={p.lineTo} stroke="#2563EB" strokeWidth="1.4" strokeDasharray="4 3" />
          <rect x={p.x} y={p.y} width={p.w} height={p.h} rx="11" fill="#2563EB" />
          <text x={p.cx} y={p.y + (p.sub ? 18 : 27)} textAnchor="middle" style={{ fontSize: 12.5, fontWeight: 600, fill: '#fff' }}>{p.title}</text>
          {p.sub ? <text x={p.cx} y={p.y + 35} textAnchor="middle" style={{ fontSize: 10.5, fill: '#DCE8FF' }}>{p.sub}</text> : null}
        </g>
      ))}
      {caption ? <text x={L.caption.x} y={L.caption.y} textAnchor="middle" style={{ fontSize: 12, fill: '#1D4ED8' }}>{caption}</text> : null}
    </svg>
  );
}

export function ContentsGrid({ groups }) {
  if (!groups.length) return <div className="sm muted">清单里没有列出内容。</div>;
  return (
    <div className="pl-cg">
      {groups.map((g) => (
        <div className="card" key={g.key}>
          <h5><Icon name={g.icon} size={14} />{g.title}<span className="n">{g.count}</span></h5>
          {g.items.map((x) => <span className="tag" key={x}>{x}</span>)}
          {g.count > g.items.length ? <span className="xs faint">等 {g.count} 项</span> : null}
          {g.note ? <div className="xs muted mt4">{g.note}</div> : null}
        </div>
      ))}
    </div>
  );
}

export function Changelog({ changelog }) {
  const rows = changelogView(changelog);
  return (
    <>
      {rows.length ? rows.map((v) => (
        <div className="pl-ver" key={v.version}>
          <div className="vv"><div className="b mono">{v.version}</div><div className="xs faint mt4">{v.date}</div></div>
          <div className="grow sm" style={{ color: 'var(--ink2)' }}>
            {v.notes}
            {v.chips.length ? <div className="mt4 row g4">{v.chips.map((c) => <Chip key={c.text} tone={c.tone} sm icon={c.icon || undefined}>{c.text}</Chip>)}</div> : null}
          </div>
        </div>
      )) : <div className="sm muted" style={{ padding: '12px 0' }}>还没有更新记录。</div>}
    </>
  );
}

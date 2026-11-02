// Studio 的 ER 视图（R23）：包选用了 er_view 时出现在 Studio 的视图切换里。对象画成实体框（属性列在框里），
// 对象之间的关系声明画成连线；点实体打开条目编辑。数据：ST-02（不带提及）+ 各对象的 ST-04。
import { useEffect, useMemo, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Empty } from '../../ui/index.jsx';
import { ER_BOX_W, ER_GEOMETRY, erData, erLayout } from './erModel.js';
import { gotoEntry } from './parts.jsx';

const { HEAD_H, ROW_H } = ER_GEOMETRY;

export default function StudioErView() {
  const { api } = useApp();
  const { pid, subscribe } = useProject();
  const [graph, setGraph] = useState(null);
  const [fields, setFields] = useState({});
  const [tick, setTick] = useState(0);
  const [zoom, setZoom] = useState(1);

  useEffect(() => subscribe((env) => { if (env.type === 'workspace.changed') setTick((t) => t + 1); }), [subscribe]);
  useEffect(() => {
    let alive = true;
    api.studioGraph(pid, { include_mentions: false }).then(async (g) => {
      if (!alive) return;
      setGraph(g);
      const objects = (g.nodes || []).filter((n) => n.kind === 'object');
      const rows = await Promise.all(objects.map((n) => api.studioEntry(pid, n.id).then((e) => [n.id, e.fields || []]).catch(() => [n.id, []])));
      if (alive) setFields(Object.fromEntries(rows));
    }).catch((e) => toast(errorText(e), 'alert'));
    return () => { alive = false; };
  }, [api, pid, tick]);

  const lay = useMemo(() => (graph ? erLayout(erData(graph, fields)) : null), [graph, fields]);
  if (!lay) return <div className="sd-empty">正在读取…</div>;
  if (!lay.boxes.length) return <div className="sd-er-empty"><Empty icon="graph" title="还没有对象" /></div>;
  return (
    <div className="sd-er">
      <svg width={lay.width * zoom} height={lay.height * zoom} viewBox={`0 0 ${lay.width} ${lay.height}`}>
        <defs>
          <marker id="sd-er-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0L10 5L0 10z" fill="#94A3B8" />
          </marker>
        </defs>
        {lay.edges.map((e) => (
          <g key={e.id} className="ed">
            <path d={e.d} fill="none" stroke="#94A3B8" strokeWidth="1.4" markerEnd="url(#sd-er-arrow)" />
            {e.label ? <text x={e.lx} y={e.ly + 4} textAnchor="middle" className="lb">{e.label}</text> : null}
          </g>
        ))}
        {lay.boxes.map((b) => (
          <g key={b.id} className="tb" onClick={() => gotoEntry(pid, b.id)}>
            <title>{b.name}</title>
            <rect x={b.x} y={b.y} width={ER_BOX_W} height={b.h} rx="10" fill="#fff" stroke="#D6DFEB" />
            <rect x={b.x} y={b.y} width={ER_BOX_W} height={HEAD_H} rx="10" fill="#EEF4FF" />
            <rect x={b.x} y={b.y + HEAD_H - 10} width={ER_BOX_W} height="10" fill="#EEF4FF" />
            <text x={b.x + 12} y={b.y + 19} className="tn">{b.name}</text>
            {b.fields.map((f, i) => (
              <g key={f.name}>
                <text x={b.x + 12} y={b.y + HEAD_H + ROW_H * i + 15}>{f.name}</text>
                {f.column ? <text x={b.x + ER_BOX_W - 12} y={b.y + HEAD_H + ROW_H * i + 15} textAnchor="end" className="k">{f.column}</text> : null}
              </g>
            ))}
            {b.more ? <text x={b.x + 12} y={b.y + HEAD_H + ROW_H * b.fields.length + 15} className="k">+{b.more}</text> : null}
          </g>
        ))}
      </svg>
      <div className="sd-er-zoom row g4">
        <Button size="xs" icon="zoomout" title="缩小" onClick={() => setZoom((z) => Math.max(0.5, +(z - 0.1).toFixed(1)))} />
        <span className="xs num muted" style={{ width: 36, textAlign: 'center' }}>{Math.round(zoom * 100)}%</span>
        <Button size="xs" icon="zoomin" title="放大" onClick={() => setZoom((z) => Math.min(2, +(z + 0.1).toFixed(1)))} />
      </div>
    </div>
  );
}

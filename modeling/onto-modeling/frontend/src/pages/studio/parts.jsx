// Studio 两页共用的小件：插件标记、类型点、锁标记与锁定条、状态列、流程图预览、跳转
import { useCallback, useEffect, useState } from 'react';
import { useApp, useProject } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { lockState } from '../../state/readonly.js';
import { Button, Chip, Icon, St } from '../../ui/index.jsx';
import { KIND_META, flowPreviewLayout, kindDotStyle, lockActionTarget, lockCopy, rowStatus } from './studioModel.js';

export function PlugMark({ label }) {
  return <span className="sn-pl" data-pop={`由「${label}」插件提供，未启用时不显示`}><Icon name="puzzle" size={12} /></span>;
}

export function KindDot({ kind, size = 12 }) {
  if (kind === 'flowchart') return <span style={{ color: 'var(--indigo)', display: 'inline-flex' }}><Icon name="branch" size={size + 6} /></span>;
  return <span className="d" style={kindDotStyle(kind, size)} />;
}

export function KindChip({ kind }) {
  const m = KIND_META[kind] || { label: kind, tone: 'gray' };
  return <Chip tone={m.tone} sm icon={m.icon}>{m.label}</Chip>;
}

export function RowStatus({ row }) {
  const s = rowStatus(row);
  if (s.chip) return <Chip tone={s.chip[1]} sm icon={s.chip[2]}>{s.chip[0]}</Chip>;
  return <St k={s.st} sm />;
}

// 图谱侧栏、流程图面板标题上的锁标记（「待确认」）
export function LockMark({ info }) {
  if (!info) return null;
  return <span className="sd-lk" data-pop={info.pop}><Icon name="lock" size={12} />{info.chip}</span>;
}

// 表格名称后、被谁引用列表里的小锁
export function LockIcon({ info }) {
  if (!info) return null;
  return <span className="stt-lk" data-pop={info.pop}><Icon name="lock" size={12} /></span>;
}

// 编辑器顶部的锁定条：运行中「回到对话」，待确认「去确认」（去 wb-l2 的本轮卡）
export function LockBar({ info, lock }) {
  const { pid } = useProject();
  if (!info) return null;
  const go = () => {
    const t = lockActionTarget(info.action, lock);
    if (t) navigate(pagePath(t.page, pid), t.query);
  };
  return (
    <div className="stt-lockbar">
      <Icon name="lock" size={14} />
      <div className="grow">
        <b>{info.title}</b>
        <div className="xs" style={{ marginTop: 3, color: '#92400E' }}>{info.sub}</div>
      </div>
      {info.action?.kind === 'confirm'
        ? <Button size="xs" kind="primary" icon="merge" onClick={go}>{info.action.label}</Button>
        : <Button size="xs" onClick={go}>{info.action?.label}</Button>}
    </div>
  );
}

/**
 * 写入范围锁的说法。等你决定的决策卡改动预览里列出的条目（S-05 open_questions[].affected）用更具体的说法。
 * 返回 (target, name) => lockCopy 结果
 */
export function useLockCopy() {
  const { api } = useApp();
  const { pid, scope } = useProject();
  const [affected, setAffected] = useState([]);
  const state = lockState(scope);
  const cid = scope?.conversation_id;
  useEffect(() => {
    if (state === 'none' || !cid) { setAffected([]); return undefined; }
    let alive = true;
    api.conversationState(pid, cid)
      .then((r) => { if (alive) setAffected((r.open_questions || []).flatMap((q) => (q.affected || []).map((a) => a.id).filter(Boolean))); })
      .catch(() => {});
    return () => { alive = false; };
  }, [api, pid, cid, state]);
  return useCallback((target, name) => lockCopy(scope, target, name, { affected }), [scope, affected]);
}

// 「问助手」：回到工作台，把条目作为引用带进对话（ref 是 EntryRef.id 或 <type>/<key>）
export function askAssistant(pid, ref) {
  navigate(pagePath('wb-l2', pid), ref ? { ref } : undefined);
}

export function gotoEntry(pid, id, page = 'studio-table') {
  navigate(pagePath(page, pid), id ? { entry: id } : undefined);
}

// 流程图预览：FlowchartModel 的节点按坐标画（形状同设计稿流程图弹窗）
const EDGE_STYLE = { seq: ['#7C8CA6', ''], cond: ['#F59E0B', ''], exc: ['#DC2626', '6 4'], data: ['#0D9488', '2 4'] };
export function FlowPreview({ model, idPrefix = 'fp' }) {
  const g = flowPreviewLayout(model);
  if (!g) return <div className="ph">还没有节点</div>;
  const shape = (n) => {
    const { x, y, hw, hh } = n;
    const pend = n.pending;
    if (n.type === 'start' || n.type === 'end') {
      return <rect x={x - hw} y={y - hh} width={hw * 2} height={hh * 2} rx={hh} fill={n.type === 'start' ? '#EFF5FF' : '#F1F0FF'} stroke={n.type === 'start' ? '#2563EB' : '#4F46E5'} strokeWidth="1.5" />;
    }
    if (n.type === 'decision') return <path d={`M${x} ${y - hh}L${x + hw} ${y}L${x} ${y + hh}L${x - hw} ${y}Z`} fill="#FFF8EB" stroke="#F59E0B" strokeWidth="1.5" />;
    if (n.type === 'data') return <path d={`M${x - hw} ${y - hh}h${hw * 2 - 12}l12 12v${hh * 2 - 12}h-${hw * 2}z`} fill="#ECFAF7" stroke="#0D9488" strokeWidth="1.4" />;
    return <rect x={x - hw} y={y - hh} width={hw * 2} height={hh * 2} rx="10" fill={pend ? '#FFFBF2' : '#fff'} stroke={pend ? '#F5C26B' : '#9BBEFB'} strokeDasharray={pend ? '4 3' : undefined} strokeWidth="1.5" />;
  };
  return (
    <svg viewBox={g.viewBox} preserveAspectRatio="xMidYMid meet">
      <defs>
        {Object.entries(EDGE_STYLE).map(([k, [c]]) => (
          <marker key={k} id={`${idPrefix}-${k}`} markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto"><path d="M0 0 9 4.5 0 9z" fill={c} /></marker>
        ))}
      </defs>
      {g.edges.map((e, i) => {
        const [c, dash] = EDGE_STYLE[e.type] || EDGE_STYLE.seq;
        const mx = (e.x1 + e.x2) / 2;
        const my = (e.y1 + e.y2) / 2;
        return (
          <g key={e.id || i}>
            <line x1={e.x1} y1={e.y1} x2={e.x2} y2={e.y2} stroke={c} strokeWidth="1.7" strokeDasharray={dash || undefined} markerEnd={`url(#${idPrefix}-${e.type in EDGE_STYLE ? e.type : 'seq'})`} />
            {e.label ? (
              <g>
                <rect x={mx - 28} y={my - 11} width="56" height="20" rx="6" fill="#fff" stroke={c} strokeWidth=".8" />
                <text x={mx} y={my + 3} textAnchor="middle" fontSize="11" fill={c}>{e.label}</text>
              </g>
            ) : null}
          </g>
        );
      })}
      {g.nodes.map((n) => (
        <g key={n.id}>
          {shape(n)}
          <text x={n.x} y={n.y + 4} textAnchor="middle" fontSize="12.5" fontWeight={n.type === 'start' || n.type === 'end' ? 600 : undefined} fill={n.pending ? '#B45309' : '#0B1A33'}>{n.label}</text>
        </g>
      ))}
    </svg>
  );
}

// 复制到剪贴板（失败也不打断，交给调用方提示）
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

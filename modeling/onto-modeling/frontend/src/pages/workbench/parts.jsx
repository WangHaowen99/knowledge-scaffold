// 对话里的基本件：与设计稿 pages-00-main.js 的 mU / mA / mSys / mDay / cc / wxProc / ct / srcRow / sl 对应
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { createContext, useContext, useLayoutEffect, useRef, useState } from 'react';
import { navigate, pagePath } from '../../app/router.js';
import { subagentStatus } from '../../state/conversation.js';
import { Icon, cx } from '../../ui/index.jsx';
import { inlineTokens, preprocessMd, stripLinks } from './md.js';
import { layoutThumb } from './thumb.js';

// 渲染上下文：项目 id、条目名 → EntryRef、当前块的出处编号 → 锚点
export const WbCtx = createContext({ pid: null, entries: {} });
const AnchorCtx = createContext({});

function gotoEntry(pid, ref, name) {
  navigate(pagePath('studio-table', pid), ref?.id ? { entry: ref.id } : { q: name });
}

export function Wl({ name }) {
  const { pid, entries } = useContext(WbCtx);
  const clean = stripLinks(name);
  return (
    <span className="wl" title="在 Studio 中查看和编辑" onClick={(e) => { e.stopPropagation(); gotoEntry(pid, entries[clean], clean); }}>
      {clean}
    </span>
  );
}

const isDb = (anchor) => /^db:/.test(anchor || '');

export function Ct({ n, anchor }) {
  const anchors = useContext(AnchorCtx);
  const a = anchor || anchors[n];
  return <sup className={cx('wx-ct', isDb(a) && 'db')} data-ev={a || undefined}>{n}</sup>;
}

const anchorMap = (list) => Object.fromEntries((list || []).map((x) => [x.n, x.anchor]));

// Markdown 正文；inline=true 时不包 <p>
export function Md({ text, anchors, inline }) {
  const comps = {
    a: ({ href, children }) => {
      if (href?.startsWith('#wl:')) return <Wl name={decodeURIComponent(href.slice(4))} />;
      if (href?.startsWith('#ct:')) return <Ct n={Number(href.slice(4))} />;
      return <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>;
    },
  };
  if (inline) comps.p = ({ children }) => <>{children}</>;
  return (
    <AnchorCtx.Provider value={anchorMap(anchors)}>
      <Markdown remarkPlugins={[remarkGfm]} components={comps}>{preprocessMd(text)}</Markdown>
    </AnchorCtx.Provider>
  );
}

// 单行文字：只认 [[ ]] 和 [^n]
export function Inline({ text, anchors }) {
  const map = anchorMap(anchors);
  return inlineTokens(text).map((t, i) => {
    if (t.t === 'link') return <Wl key={i} name={t.v} />;
    if (t.t === 'cite') return <Ct key={i} n={t.v} anchor={map[t.v]} />;
    return <span key={i}>{t.v}</span>;
  });
}

export function MDay({ label }) {
  return <div className="wx-day">{label}</div>;
}

export function MSys({ children, left }) {
  return (
    <div className="m-sys" style={left ? { justifyContent: 'flex-start', margin: '6px 0' } : undefined}>
      <span><span className="ck"><Icon name="check" /></span>{children}</span>
    </div>
  );
}

const FILE_KIND = { doc: ['doc', 'Word 文档'], xls: ['xls', 'Excel 表格'], pdf: ['pdf', 'PDF'] };
export function WxFile({ name, kind }) {
  const ext = (name.match(/\.(\w+)$/) || [])[1] || '';
  const k = kind && FILE_KIND[kind] ? kind : /docx?/i.test(ext) ? 'doc' : /xlsx?|csv/i.test(ext) ? 'xls' : /pdf/i.test(ext) ? 'pdf' : '';
  return (
    <div className="wx-file">
      <span className={cx('fi', k)}>{ext.toUpperCase().slice(0, 4)}</span>
      <div style={{ minWidth: 0 }}>
        <div className="nm">{name.replace(/\.\w+$/, '')}</div>
        <div className="mt">{FILE_KIND[k]?.[1] || (kind === 'image' ? '图片' : '文件')}</div>
      </div>
    </div>
  );
}

export function Orb() {
  return (
    <span className="wx-orb">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2" strokeLinecap="round">
        <path d="M12 12.4V6.4M12 12.4l-5.4 4M12 12.4l5.4 4" opacity=".8" />
        <circle cx="12" cy="4.8" r="2.7" fill="#fff" stroke="none" />
        <circle cx="5.2" cy="17.6" r="2.7" fill="#fff" stroke="none" />
        <circle cx="18.8" cy="17.6" r="2.7" fill="#fff" stroke="none" />
        <circle cx="12" cy="12.4" r="1.9" fill="#93C5FD" stroke="none" />
      </svg>
    </span>
  );
}

export function MA({ time, children }) {
  return (
    <div className="m-a">
      <div className="wx-who"><Orb /><b>建模助手</b>{time ? <span>{time}</span> : null}</div>
      <div className="bd">{children}</div>
    </div>
  );
}

export function CC({
  icon = 'filetext', tone, eyebrow, title, chip, sub, children, foot, acts, dbl, hint, className, onDoubleClick, id,
}) {
  return (
    <div className={cx('cc', dbl && 'dbl', className)} onDoubleClick={onDoubleClick} id={id}>
      <div className="cc-h">
        <span className={cx('ik', tone)}><Icon name={icon} size={16} /></span>
        <div className="grow" style={{ minWidth: 0 }}>
          {eyebrow ? <div className="eb">{eyebrow}</div> : null}
          <div className="row g8"><span className="ti">{title}</span>{chip}</div>
          {sub ? <div className="sb">{sub}</div> : null}
        </div>
        {dbl && hint !== false ? <span className="cc-hint"><Icon name="maximize" size={12} />双击打开</span> : null}
        {acts}
      </div>
      {children != null ? <div className="cc-b">{children}</div> : null}
      {foot ? <div className="cc-f">{foot}</div> : null}
    </div>
  );
}

export function fmtDuration(ms) {
  if (ms == null) return '';
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  return s % 60 ? `${m} 分 ${String(s % 60).padStart(2, '0')} 秒` : `${m} 分钟`;
}

export function hhmm(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// 有标题的进展组 → 可折叠「工作过程」；没有标题 → 时间线
export function WxProc({ group }) {
  const [open, setOpen] = useState(null);
  const run = group.state === 'running';
  const isOpen = open ?? !group.collapsed;
  return (
    <details className="wx-proc" open={isOpen}>
      <summary onClick={(e) => { e.preventDefault(); setOpen(!isOpen); }}>
        <span className="ck">{run ? <span className="spin" /> : <Icon name={group.state === 'failed' ? 'alert' : group.state === 'interrupted' ? 'pause' : 'check'} />}</span>
        <b>{group.title}</b>
        {group.desc ? <span className="ds">{group.desc}</span> : null}
        <span className="tm">{group.duration_ms ? fmtDuration(group.duration_ms) : ''}</span>
        <Icon name="chevd" size={12} className="cv" />
      </summary>
      <div className="wx-steps">
        {(group.steps || []).map((s) => (
          <div key={s.id} className={cx('r', s.state === 'running' && 'run', s.state === 'info' && 'say')}>
            <span className="t">{s.text}</span>
            <span className="m">{s.meta || ''}</span>
          </div>
        ))}
      </div>
    </details>
  );
}

const TL_DOT = {
  done: ['', 'check'], ok: ['', 'check'], waiting: ['wait', 'hand'], failed: ['err', 'alert'], err: ['err', 'alert'],
  info: ['', 'info'], interrupted: ['gray', 'pause'], gray: ['gray', 'pause'], dot: ['gray', 'dot'],
};
export function Tli({ k, t, m, children }) {
  const [cls, icon] = TL_DOT[k] || ['', null];
  return (
    <div className="tl-i">
      <span className={cx('tl-dot', cls)}>{k === 'running' ? <span className="spin" style={{ width: 10, height: 10 }} /> : <Icon name={icon || 'dot'} />}</span>
      <div className="grow">
        <div className="tl-t">{t}</div>
        {m ? <div className="tl-m">{m}</div> : null}
        {children}
      </div>
    </div>
  );
}

// 一段可滚动的列表：新内容到达时，停在底部附近就跟到底
function useStickBottom(dep) {
  const ref = useRef(null);
  const stick = useRef(true);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [dep]);
  const onScroll = () => {
    const el = ref.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };
  return { ref, onScroll };
}

const SUB_TITLE = { done: '子任务完成', failed: '子任务失败', interrupted: '子任务已中断' };
const SUB_ICON = { done: 'check', failed: 'alert', interrupted: 'pause' };

// 派出的子任务：时间线上单独一张卡（沿用旧版对话页的样式），第二行是它正在做的那一步或做完的小结；
// 会话已停下还没收到终态的显示已中断（ADR-0009）
export function SubagentCard({ s, ended = false }) {
  const status = subagentStatus(s, ended);
  const over = Boolean(SUB_ICON[status]);
  const line = over ? s.summary : (s.progress_text || '独立子代理执行中，主对话稍后汇总结果');
  return (
    <div className={cx('subagent-card', over ? status : 'running')}>
      <span className="subagent-icon">{over ? <Icon name={SUB_ICON[status]} size={14} /> : <span className="spin" />}</span>
      <span className="subagent-main">
        <span className="subagent-title">{SUB_TITLE[status] || '子任务运行中'} · {s.title}</span>
        {line ? <span className="subagent-hint">{line}</span> : null}
      </span>
    </div>
  );
}

// 助手在跑、别的都没动时的一行「思考中」；有思考片段时可以展开看
export function LiveRow({ thinking }) {
  const [open, setOpen] = useState(false);
  const box = useStickBottom(thinking);
  return (
    <div className="wb-live">
      <div className={cx('hd', thinking && 'tg')} onClick={thinking ? () => setOpen(!open) : undefined}>
        <span className="spin" /><span className="t">思考中</span>
        {thinking ? <Icon name="chevd" size={12} className={cx('cv', open && 'on')} /> : null}
      </div>
      {open && thinking ? <div className="bd" ref={box.ref} onScroll={box.onScroll}>{thinking}</div> : null}
    </div>
  );
}

export function TimelineGroup({ group }) {
  return (
    <div className="tl">
      {(group.steps || []).map((s) => <Tli key={s.id} k={s.state} t={s.text} m={s.meta} />)}
    </div>
  );
}

export function SrcRow({ sources }) {
  return (
    <div className="wx-src">
      <b>出处</b>
      {sources.map((s) => (
        <span key={s.n} data-ev={s.anchor}><i className={cx('wx-ct', (s.scheme === 'db' || isDb(s.anchor)) && 'db')}>{s.n}</i>{s.label}</span>
      ))}
    </div>
  );
}

const DIFF_CLS = { header: 'h', del: 'd', add: 'a', ctx: '', note: 'h' };
export function DiffBlock({ lines, style }) {
  return (
    <div className="diff" style={style}>
      {(lines || []).map((l, i) => <div key={i} className={DIFF_CLS[l.kind] || undefined}>{stripLinks(l.text)}</div>)}
    </div>
  );
}

export function FlowThumb({ thumbnail, height = 140 }) {
  const { nodes = [], edges = [] } = thumbnail || {};
  const { pos } = layoutThumb(nodes, edges);
  const lines = edges.map((e, i) => {
    const a = pos.get(e.from);
    const b = pos.get(e.to);
    if (!a || !b) return null;
    const y2 = b.cy === a.cy ? b.cy : b.cy + (b.cy < a.cy ? 2 : -2);
    return { key: i, x1: a.x + a.w, y1: a.cy, x2: b.x - 2, y2, label: e.label, ly: b.cy < a.cy ? b.cy + 18 : b.cy - 8 };
  }).filter(Boolean);
  return (
    <svg className="wb-thumb" viewBox={`0 0 780 ${height}`} height={height}>
      <defs><marker id="fa" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 8 4 0 8z" fill="#93A1B5" /></marker></defs>
      {lines.map((l) => <line key={l.key} x1={l.x1} y1={l.y1} x2={l.x2} y2={l.y2} stroke="#93A1B5" strokeWidth="1.5" markerEnd="url(#fa)" />)}
      {[...pos.values()].map(({ x, w, h, cy, n }) => {
        const pend = n.pending;
        const textStyle = pend ? { fill: '#B45309' } : undefined;
        let shape;
        if (n.type === 'start' || n.type === 'end') shape = <ellipse cx={x + w / 2} cy={cy} rx={w / 2} ry={18} fill="#fff" stroke="#93A1B5" />;
        else if (n.type === 'decision') shape = <path d={`M${x + w / 2} ${cy - 28} ${x + w} ${cy} ${x + w / 2} ${cy + 28} ${x}  ${cy}Z`} fill="#fff" stroke="#93A1B5" />;
        else shape = <rect x={x} y={cy - h / 2} width={w} height={h} rx="9" fill={pend ? '#FFFBF2' : '#fff'} stroke={pend ? '#F5C26B' : '#C3D8FD'} strokeDasharray={pend ? '4 3' : undefined} />;
        return (
          <g key={n.id}>
            {shape}
            <text x={x + w / 2} y={cy + 4} textAnchor="middle" style={textStyle}>{n.label}</text>
          </g>
        );
      })}
      {lines.filter((l) => l.label).map((l) => <text key={`t${l.key}`} x={l.x1 + 12} y={l.ly} style={{ fontSize: 10, fill: '#64748B' }}>{l.label}</text>)}
    </svg>
  );
}

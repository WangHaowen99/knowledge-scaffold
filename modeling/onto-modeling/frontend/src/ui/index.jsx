// 组件库：与设计稿 core.js 的组件函数一一对应，类名沿用 base.css
import {
  createContext, useCallback, useContext, useEffect, useRef, useState,
} from 'react';
import { createPortal } from 'react-dom';
import { ICONS } from './icons.js';

export const cx = (...parts) => parts.filter(Boolean).join(' ');

export function Icon({ name, size, className, style }) {
  return (
    <svg
      className={cx('i', size && `s${size}`, className)}
      viewBox="0 0 24 24"
      style={style}
      aria-hidden="true"
      dangerouslySetInnerHTML={{ __html: ICONS[name] || ICONS.dot }}
    />
  );
}

export function Button({
  children, kind, size, icon, after, className, disabled, type = 'button', ...rest
}) {
  const iconOnly = !children && icon;
  return (
    <button
      type={type}
      className={cx('btn', kind, size, className, iconOnly && 'icon')}
      disabled={disabled}
      {...rest}
    >
      {icon ? <Icon name={icon} size={size === 'xs' ? 12 : 14} /> : null}
      {children}
      {after}
    </button>
  );
}

export function Chip({ children, tone, sm, icon, dot, className, ...rest }) {
  return (
    <span className={cx('chip', tone, sm && 'sm', className)} {...rest}>
      {icon ? <Icon name={icon} /> : null}
      {dot ? <i className="dt" /> : null}
      {children}
    </span>
  );
}

// 节点 / 分段 / 条目的显示态：键与契约 DisplayState 一致，取值来自设计稿 ST 表
export const DISPLAY_STATES = {
  not_ready: ['未就绪', 'gray', null],
  ready: ['可开始', 'line', 'play'],
  running: ['进行中', 'st-run', null],
  pending: ['待确认', 'amber', null],
  confirmed: ['已确认', 'blue', 'check'],
  stale: ['过期', 'dash', 'refresh'],
  skipped: ['已跳过', 'gray', null],
  blocked: ['被挡住', 'red', 'lock'],
  needs_review: ['需复核', 'indigo', 'eye'],
  disabled: ['已停用', 'gray', 'ban'],
  conflict: ['冲突', 'red', 'alert'],
  draft: ['草稿', 'gray', null],
  none: ['未开始', 'gray', null],
  backfill: ['可补做', 'line', 'plus'],
  auto: ['自主完成', 'indigo', 'bot'],
  fail: ['失败', 'red', 'x'],
  pass: ['通过', 'teal', 'check'],
};

export function St({ k, sm, label }) {
  const [text, tone, icon] = DISPLAY_STATES[k] || [k, 'gray', null];
  if (k === 'running') {
    return <span className={cx('chip st-run', sm && 'sm')}><span className="spin" />{label || text}</span>;
  }
  if (k === 'pending') {
    return <span className={cx('chip amber', sm && 'sm')}><i className="dt" />{label || text}</span>;
  }
  return <Chip tone={tone} icon={icon} sm={sm}>{label || text}</Chip>;
}

export function Card({ title, icon, sub, extra, children, className, bodyClassName, ...rest }) {
  return (
    <div className={cx('card', className)} {...rest}>
      {title ? (
        <div className="card-h">
          {icon ? <span style={{ color: 'var(--blue)' }}><Icon name={icon} /></span> : null}
          <h3>{title}</h3>
          {sub ? <span className="sub">{sub}</span> : null}
          <span className="sp" />
          {extra}
        </div>
      ) : null}
      <div className={cx('card-b', bodyClassName)}>{children}</div>
    </div>
  );
}

export function Stat({ label, value, unit, icon, desc, className }) {
  return (
    <div className={cx('stat', className)}>
      <div className="lb">{icon ? <Icon name={icon} size={14} /> : null}{label}</div>
      <div className="v">{value}{unit ? <small>{unit}</small> : null}</div>
      {desc ? <div className="d">{desc}</div> : null}
    </div>
  );
}

export function Table({ cols, rows, compact, className, wrapClassName, rowClassName }) {
  return (
    <div className={cx('tbl-wrap', wrapClassName)}>
      <table className={cx('tbl', compact && 'compact', className)}>
        <thead>
          <tr>
            {cols.map((c, i) => (
              <th key={i} style={typeof c === 'object' && c.w ? { width: c.w } : undefined}>
                {typeof c === 'object' ? c.t : c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className={rowClassName ? rowClassName(i) : undefined}>
              {r.map((c, j) => <td key={j}>{c}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Field({ label, req, extra, hint, children, className }) {
  return (
    <div className={cx('field', className)}>
      <label>
        {label}
        {req ? <span style={{ color: 'var(--red)' }}>*</span> : null}
        {extra}
      </label>
      {children}
      {hint ? <div className="hint">{hint}</div> : null}
    </div>
  );
}

// 输入框：.inp 外壳 + 原生 input
export function Inp({
  icon, after, error, focus, className, style, w, inputRef, mono, ...inputProps
}) {
  return (
    <div className={cx('inp', error && 'err', focus && 'focus', className)} style={{ ...(w ? { width: w } : null), ...style }}>
      {icon ? <Icon name={icon} size={14} /> : null}
      <input ref={inputRef} className={mono ? 'mono' : undefined} {...inputProps} />
      {after}
    </div>
  );
}

export function Area({ minHeight, className, textareaStyle, ...props }) {
  return (
    <div className={cx('inp area', className)} style={minHeight ? { minHeight } : undefined}>
      <textarea style={textareaStyle} {...props} />
    </div>
  );
}

// 下拉选择：外观同设计稿 sel()，点开是 .menu
export function Select({ value, options, onChange, icon, w, className, disabled }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  const current = options.find((o) => o.value === value);
  return (
    <div ref={ref} style={{ position: 'relative', width: w }}>
      <div
        className={cx('inp sel', className)}
        style={{ cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.6 : 1 }}
        onClick={() => !disabled && setOpen((v) => !v)}
        role="button"
      >
        {icon ? <Icon name={icon} size={14} /> : null}
        <span className="grow ell">{current ? current.label : ''}</span>
      </div>
      {open ? (
        <div className="menu" style={{ position: 'absolute', top: 40, left: 0, right: 0, zIndex: 40 }}>
          {options.map((o) => (
            <div
              key={o.value}
              className="mi"
              onClick={() => { setOpen(false); onChange?.(o.value); }}
            >
              {o.icon ? <Icon name={o.icon} size={14} /> : null}
              <span>{o.label}</span>
              {o.value === value ? <span className="k"><Icon name="check" size={12} /></span> : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function Toggle({ on, onChange, disabled, title }) {
  return (
    <span
      className={cx('tog', on && 'on')}
      role="switch"
      aria-checked={!!on}
      title={title}
      style={disabled ? { opacity: 0.5, cursor: 'not-allowed' } : undefined}
      onClick={(e) => { e.preventDefault(); if (!disabled) onChange?.(!on); }}
    />
  );
}

export function Check({ on, onChange, label, disabled }) {
  return (
    <label className="row" style={{ cursor: disabled ? 'not-allowed' : 'pointer' }}>
      <span
        className={cx('chk', on && 'on')}
        role="checkbox"
        aria-checked={!!on}
        onClick={(e) => { e.preventDefault(); if (!disabled) onChange?.(!on); }}
      />
      {label ? <span onClick={() => !disabled && onChange?.(!on)}>{label}</span> : null}
    </label>
  );
}

export function Radio({ on, label, onChange }) {
  return (
    <label className="row" style={{ cursor: 'pointer' }} onClick={() => onChange?.()}>
      <span className={cx('rad', on && 'on')} />
      {label ? <span>{label}</span> : null}
    </label>
  );
}

export function Seg({ items, value, onChange, id }) {
  return (
    <div className="seg" id={id}>
      {items.map((x) => {
        const [k, t] = Array.isArray(x) ? x : [x, x];
        return (
          <button key={k} type="button" className={k === value ? 'on' : ''} onClick={() => onChange?.(k)}>
            {t}
          </button>
        );
      })}
    </div>
  );
}

export function Tabs({ items, value, onChange }) {
  return (
    <div className="tabs">
      {items.map(([k, t, n, icon]) => (
        <div key={k} className={cx('tab', k === value && 'on')} onClick={() => onChange?.(k)}>
          {icon ? <Icon name={icon} size={14} /> : null}
          {t}
          {n != null ? <span className="n">{n}</span> : null}
        </div>
      ))}
    </div>
  );
}

export function Prog({ p, tone, thin, w }) {
  return (
    <div className={cx('prog', tone, thin && 'thin')} style={w ? { width: w } : undefined}>
      <i style={{ width: `${Math.max(0, Math.min(100, p || 0))}%` }} />
    </div>
  );
}

export function SegBar({ states, style }) {
  return (
    <div className="segbar" style={style}>
      {states.map((s, i) => <i key={i} className={s || undefined} />)}
    </div>
  );
}

const AV_COLORS = [
  'linear-gradient(140deg,#60A5FA,#2563EB)',
  'linear-gradient(140deg,#5EEAD4,#0D9488)',
  'linear-gradient(140deg,#A5B4FC,#4F46E5)',
  'linear-gradient(140deg,#FCD34D,#D97706)',
];
export function Avatar({ text, size, i = 0, color }) {
  return <span className={cx('av', size)} style={{ background: color || AV_COLORS[i % AV_COLORS.length] }}>{text}</span>;
}

export function Note({ tone, icon, children, className, style }) {
  const name = icon || (tone === 'amber' || tone === 'red' ? 'alert' : 'info');
  return (
    <div className={cx('note', tone, className)} style={style}>
      <Icon name={name} />
      <div className="grow">{children}</div>
    </div>
  );
}

export function KV({ pairs, w = '110px', style }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: `${w} 1fr`, gap: '8px 12px', fontSize: '12.8px', ...style }}>
      {pairs.map(([k, v], i) => [
        <div key={`k${i}`} className="muted">{k}</div>,
        <div key={`v${i}`}>{v}</div>,
      ])}
    </div>
  );
}

export function Kbd({ children }) {
  return <span className="kbd">{children}</span>;
}

export function Empty({ icon, title, children, actions }) {
  return (
    <div className="empty">
      <div className="ic"><Icon name={icon} size={24} /></div>
      <h4>{title}</h4>
      <div style={{ maxWidth: 380 }}>{children}</div>
      {actions ? <div className="row mt8">{actions}</div> : null}
    </div>
  );
}

export function Ring({ p, r = 9, color = '#2563EB' }) {
  const c = 2 * Math.PI * r;
  const sz = r * 2 + 6;
  const pct = Math.max(0, Math.min(100, p || 0));
  return (
    <svg width={sz} height={sz} viewBox={`0 0 ${sz} ${sz}`} style={{ flex: 'none' }}>
      <circle cx={sz / 2} cy={sz / 2} r={r} fill="none" stroke="#E5EBF3" strokeWidth="3" />
      <circle
        cx={sz / 2} cy={sz / 2} r={r} fill="none" stroke={color} strokeWidth="3" strokeLinecap="round"
        strokeDasharray={`${(c * pct) / 100} ${c}`} transform={`rotate(-90 ${sz / 2} ${sz / 2})`}
      />
    </svg>
  );
}

export function Menu({ items, style, className, onPick }) {
  return (
    <div className={cx('menu', className)} style={style}>
      {items.map((x, i) => (x === '-' ? <hr key={i} /> : (
        <div
          key={i}
          className={cx('mi', x.danger && 'danger')}
          style={x.disabled ? { opacity: 0.45, cursor: 'not-allowed' } : undefined}
          title={x.title}
          onClick={(e) => { e.stopPropagation(); if (!x.disabled) { onPick?.(x); x.onClick?.(); } }}
        >
          {x.icon ? <Icon name={x.icon} size={14} /> : null}
          <span>{x.label}</span>
          {x.hint ? <span className="k">{x.hint}</span> : null}
        </div>
      )))}
    </div>
  );
}

// ---------- 弹层宿主：弹窗、抽屉挂到 .app 下（与设计稿 overlay 定位一致） ----------
export const OverlayHostCtx = createContext(null);

function OverlayPortal({ children }) {
  const host = useContext(OverlayHostCtx);
  if (host) return createPortal(children, host);
  return children;
}

// 弹层关掉时先播退场动画再卸载：关闭后的 LEAVE_MS 内保留最后一次打开时的内容（调用方此时多半已清空数据）
const LEAVE_MS = 140;
export function usePresence(open, props) {
  const last = useRef(props);
  if (open) last.current = props;
  const [leaving, setLeaving] = useState(false);
  const [prevOpen, setPrevOpen] = useState(open);
  // 渲染时就切到退场（不等 effect），否则关闭那一帧会先卸掉再挂回来闪一下
  if (prevOpen !== open) {
    setPrevOpen(open);
    setLeaving(!open);
  }
  useEffect(() => {
    if (!leaving) return undefined;
    const t = setTimeout(() => setLeaving(false), LEAVE_MS);
    return () => clearTimeout(t);
  }, [leaving]);
  return open ? { shown: true, leaving: false, props } : { shown: leaving, leaving, props: last.current };
}

// 调用方直接卸掉弹层（`{x ? <Modal open … /> : null}`、组件里 `if (!x) return null`）时 usePresence 拦不住：
// 卸载那一刻把弹层的 DOM 拷一份留在原处播退场，播完删掉。拷贝只是画面，不响应任何操作
function leaveGhost(el, parent) {
  if (!parent || el.classList.contains('mo-leave')) return;
  const ghost = el.cloneNode(true);
  ghost.classList.add('mo-leave');
  ghost.setAttribute('aria-hidden', 'true');
  ghost.inert = true;
  parent.appendChild(ghost);
  const from = el.querySelectorAll('.modal-b,.drawer-b');
  ghost.querySelectorAll('.modal-b,.drawer-b').forEach((box, i) => { box.scrollTop = from[i]?.scrollTop || 0; });
  setTimeout(() => ghost.remove(), LEAVE_MS + 40);
}

function useLeaveGhost() {
  return useCallback((el) => {
    if (!el) return undefined;
    const parent = el.parentNode;
    return () => leaveGhost(el, parent);
  }, []);
}

export function Modal(all) {
  useEscape(all.open, all.onClose);
  const { shown, leaving, props } = usePresence(all.open, all);
  const ghostRef = useLeaveGhost();
  if (!shown) return null;
  const {
    onClose, icon, danger, title, sub, w, children, foot, sticky, className,
  } = props;
  return (
    <OverlayPortal>
      <div
        ref={ghostRef}
        className={cx('overlay', className, leaving && 'mo-leave')}
        onMouseDown={(e) => { if (e.target === e.currentTarget && !sticky && !leaving) onClose?.(); }}
      >
        <div className="modal" style={w ? { width: w } : undefined} role="dialog" aria-modal="true">
          <div className="modal-h">
            {icon ? (
              <div
                style={{
                  width: 36, height: 36, borderRadius: 11, display: 'grid', placeItems: 'center', flex: 'none',
                  background: danger ? 'var(--r50)' : 'var(--b50)', color: danger ? 'var(--red)' : 'var(--blue)',
                }}
              >
                <Icon name={icon} size={18} />
              </div>
            ) : null}
            <div className="grow">
              <h3>{title}</h3>
              {sub ? <p>{sub}</p> : null}
            </div>
            <button type="button" className="icon-btn" onClick={onClose} aria-label="关闭"><Icon name="x" /></button>
          </div>
          <div className="modal-b">{children}</div>
          {foot ? <div className="modal-f">{foot}</div> : null}
        </div>
      </div>
    </OverlayPortal>
  );
}

export function Drawer(all) {
  useEscape(all.open, all.onClose);
  const { shown, leaving, props } = usePresence(all.open, all);
  const ghostRef = useLeaveGhost();
  if (!shown) return null;
  const {
    onClose, icon, title, sub, w, children, foot,
  } = props;
  return (
    <OverlayPortal>
      <div ref={ghostRef} className={cx('drawer', leaving && 'mo-leave')} style={w ? { width: w } : undefined} role="dialog">
        <div className="drawer-h">
          {icon ? <Icon name={icon} size={18} /> : null}
          <div className="grow">
            <div className="b lg">{title}</div>
            {sub ? <div className="muted sm">{sub}</div> : null}
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="关闭"><Icon name="x" /></button>
        </div>
        <div className="drawer-b">{children}</div>
        {foot ? <div className="drawer-f">{foot}</div> : null}
      </div>
    </OverlayPortal>
  );
}

// ---------- 证据锚点与条目链接 ----------
export function Ev({ anchor, label, scheme }) {
  const db = scheme === 'db' || /^db:/.test(anchor || '');
  const code = scheme === 'code' || /^code:/.test(anchor || '');
  return (
    <span className={cx('ev', db && 'db')} data-ev={anchor}>
      <Icon name={db ? 'db' : code ? 'code' : 'quote'} />
      {label}
    </span>
  );
}

// ---------- 工具 hooks ----------
export function useOutside(ref, onOutside, active = true) {
  useEffect(() => {
    if (!active) return undefined;
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) onOutside(); };
    document.addEventListener('mousedown', h);
    return () => document.removeEventListener('mousedown', h);
  }, [ref, onOutside, active]);
}

export function useEscape(active, onEscape) {
  useEffect(() => {
    if (!active || !onEscape) return undefined;
    const h = (e) => { if (e.key === 'Escape') onEscape(); };
    document.addEventListener('keydown', h);
    return () => document.removeEventListener('keydown', h);
  }, [active, onEscape]);
}

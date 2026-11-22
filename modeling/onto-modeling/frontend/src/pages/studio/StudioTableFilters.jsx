// Studio 表格的表头筛选：漏斗按钮 + 下拉面板（develop !611 用 antd 列筛选，这里换成本项目的菜单样式）。
// 面板挂到 body、按按钮位置固定定位，不被表格的圆角容器裁掉；改动点「确定」才生效，「重置」清掉这一列。
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Button, Check, Icon, Inp, cx, useEscape } from '../../ui/index.jsx';
import { studioUpdatedRange, studioUpdatedRangeKey } from './studioTable.js';

const PANEL_W = 232;

function placeAt(el) {
  const r = el.getBoundingClientRect();
  return { left: Math.max(8, Math.min(r.left - 8, window.innerWidth - PANEL_W - 8)), top: r.bottom + 6, width: PANEL_W };
}

export function ColumnFilter({ label, active, children }) {
  const [at, setAt] = useState(null);
  const buttonRef = useRef(null);
  const panelRef = useRef(null);
  const open = !!at;
  const close = () => setAt(null);
  useEscape(open, close);
  useEffect(() => {
    if (!open) return undefined;
    const down = (e) => {
      if (panelRef.current?.contains(e.target) || buttonRef.current?.contains(e.target)) return;
      setAt(null);
    };
    const move = (e) => {
      if (panelRef.current?.contains(e.target)) return;
      if (buttonRef.current) setAt(placeAt(buttonRef.current));
    };
    document.addEventListener('mousedown', down);
    document.addEventListener('scroll', move, true);
    window.addEventListener('resize', move);
    return () => {
      document.removeEventListener('mousedown', down);
      document.removeEventListener('scroll', move, true);
      window.removeEventListener('resize', move);
    };
  }, [open]);
  const toggle = (e) => {
    e.stopPropagation();
    setAt((current) => (current ? null : placeAt(buttonRef.current)));
  };
  return (
    <>
      <button ref={buttonRef} type="button" className={cx('stt-flb', active && 'on')} aria-label={`筛选${label}`} aria-expanded={open} onClick={toggle}>
        <Icon name="filter" size={11} />
      </button>
      {open ? createPortal(
        <div ref={panelRef} className="menu stt-flp" style={at} onClick={(e) => e.stopPropagation()}>{children(close)}</div>,
        document.body,
      ) : null}
    </>
  );
}

function FilterFoot({ onReset, onOk }) {
  return (
    <div className="stt-flf">
      <Button size="sm" onClick={onReset}>重置</Button>
      <Button size="sm" kind="primary" onClick={onOk}>确定</Button>
    </div>
  );
}

// 责任部门、维护人：可搜索的多选
export function OwnerFilter({ options, value, onChange, close }) {
  const [draft, setDraft] = useState(value || []);
  const [q, setQ] = useState('');
  const needle = q.trim().toLowerCase();
  const shown = needle ? options.filter((o) => o.text.toLowerCase().includes(needle)) : options;
  const toggle = (v) => setDraft((d) => (d.includes(v) ? d.filter((x) => x !== v) : [...d, v]));
  return (
    <>
      <Inp icon="search" placeholder="搜索" value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="stt-flo">
        {shown.map((o) => <Check key={o.value} on={draft.includes(o.value)} label={o.text} onChange={() => toggle(o.value)} />)}
        {!shown.length ? <div className="muted sm">没有匹配项</div> : null}
      </div>
      <FilterFoot onReset={() => { onChange([]); close(); }} onOk={() => { onChange(draft); close(); }} />
    </>
  );
}

// 最后更新时间：起止日期，任一端可留空
export function UpdatedRangeFilter({ value, onChange, close }) {
  const initial = studioUpdatedRange(value);
  const [start, setStart] = useState(initial.start);
  const [end, setEnd] = useState(initial.end);
  return (
    <>
      <label className="stt-fld"><span>从</span><Inp type="date" aria-label="起始日期" value={start} max={end || undefined} onChange={(e) => setStart(e.target.value)} /></label>
      <label className="stt-fld"><span>到</span><Inp type="date" aria-label="截止日期" value={end} min={start || undefined} onChange={(e) => setEnd(e.target.value)} /></label>
      <FilterFoot onReset={() => { onChange(''); close(); }} onOk={() => { onChange(studioUpdatedRangeKey(start, end)); close(); }} />
    </>
  );
}

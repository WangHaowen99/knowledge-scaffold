// 卡片、表格里弹出的下拉菜单：外层有 overflow 时用 fixed 定位，靠近窗口底部就往上弹，页面一滚动就收起（版本页 ⋯ 菜单、发布页「改为现有表」共用）
import { useEffect, useRef, useState } from 'react';
import { useOutside } from '../../ui/index.jsx';

/**
 * align='right'：菜单右边对齐按钮右边（⋯）；'left'：左边对齐、至少和按钮一样宽（下拉框）
 */
export function usePopMenu({ width = 190, height = 200, align = 'right' } = {}) {
  const [at, setAt] = useState(null);
  const ref = useRef(null);
  useOutside(ref, () => setAt(null), !!at);
  useEffect(() => {
    if (!at) return undefined;
    const close = (e) => { if (!ref.current?.contains(e.target)) setAt(null); };
    window.addEventListener('scroll', close, true);
    return () => window.removeEventListener('scroll', close, true);
  }, [at]);
  const toggle = (e) => {
    e.stopPropagation();
    if (at) { setAt(null); return; }
    const r = e.currentTarget.getBoundingClientRect();
    const w = align === 'left' ? Math.max(width, r.width) : width;
    const up = r.bottom + height + 8 > window.innerHeight;
    const left = align === 'left' ? r.left : r.right - w;
    const x = Math.max(8, Math.min(left, window.innerWidth - w - 8));
    setAt(up ? { left: x, bottom: window.innerHeight - r.top + 4, width: w } : { left: x, top: r.bottom + 4, width: w });
  };
  return { at, ref, toggle, close: () => setAt(null) };
}

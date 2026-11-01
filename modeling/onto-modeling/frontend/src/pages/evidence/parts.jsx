// 三个资料子页的共用件：页头插件标识（设计稿 evdPlg）、带 id 的抽屉（#evdWiz 的定位）、状态与置信度小标签、行菜单
import { useContext, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useApp } from '../../app/context.js';
import { navigate, pagePath } from '../../app/router.js';
import { Chip, Icon, Menu, OverlayHostCtx, St, useEscape } from '../../ui/index.jsx';
import { PluginOff, usePageEnabled } from '../plugin/parts.jsx';
import { CONF_TEXT, CONF_TONE, JOB_STATE, PACK, PACK_LABEL } from './evidenceModel.js';
import '../../styles/evidence.css';
import '../../styles/evidence-pages.css';

export function EvdPlg() {
  return (
    <span className="evd-plg" data-pop={`由「${PACK_LABEL}」插件提供；未启用时不显示这个页面`} onClick={() => navigate(pagePath('plugin-detail'), { name: PACK })}>
      <Icon name="puzzle" size={12} />插件 · {PACK_LABEL}
    </span>
  );
}

// 插件没启用（直接打开地址）时整页空状态；扩展项还没读到时什么都不画
export function EvidenceGate({ page, children }) {
  const enabled = usePageEnabled(page);
  if (enabled === null) return null;
  if (!enabled) return <PluginOff label={PACK_LABEL} what="证据出处" />;
  return children;
}

export const useIsAdmin = () => useApp()?.user?.role === 'admin';

// 同 ui 的 Drawer，多一个 id（设计稿 #evdWiz 的定位与宽度挂在 id 上）
export function EvdDrawer({ id, open, onClose, icon, title, sub, children, foot }) {
  const host = useContext(OverlayHostCtx);
  useEscape(open, onClose);
  if (!open) return null;
  const el = (
    <div className="drawer" id={id} role="dialog">
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
  );
  return host ? createPortal(el, host) : el;
}

export function JobChip({ state }) {
  if (state === 'running') return <St k="running" label="运行中" />;
  const [t, tone, icon] = JOB_STATE[state] || [state, 'gray', null];
  return <Chip tone={tone} icon={icon}>{t}</Chip>;
}

export function ConfChip({ k }) {
  if (!k) return null;
  return <Chip tone={CONF_TONE[k] || 'line'} sm data-pop={CONF_TEXT[k]}>{k}</Chip>;
}

// 行尾「⋯」：点开一列操作，点别处收起。菜单挂到 body、按按钮位置固定定位，不被表格的滚动容器裁掉
export function RowMenu({ items }) {
  const [at, setAt] = useState(null);
  const btnRef = useRef(null);
  const menuRef = useRef(null);
  useEffect(() => {
    if (!at) return undefined;
    const close = (e) => {
      if (btnRef.current?.contains(e.target) || menuRef.current?.contains(e.target)) return;
      setAt(null);
    };
    const away = () => setAt(null);
    document.addEventListener('mousedown', close);
    window.addEventListener('resize', away);
    document.addEventListener('scroll', away, true);
    return () => {
      document.removeEventListener('mousedown', close);
      window.removeEventListener('resize', away);
      document.removeEventListener('scroll', away, true);
    };
  }, [at]);
  const toggle = () => {
    if (at) { setAt(null); return; }
    const r = btnRef.current.getBoundingClientRect();
    setAt({ right: Math.max(8, window.innerWidth - r.right), top: r.bottom + 4 });
  };
  return (
    <>
      <button ref={btnRef} type="button" className="btn ghost sm icon" title="更多操作" onClick={toggle}><Icon name="more" size={14} /></button>
      {at ? createPortal(
        <div ref={menuRef} className="evp-rowmenu-m" style={{ right: at.right, top: at.top }}>
          <Menu items={items} onPick={() => setAt(null)} />
        </div>,
        document.body,
      ) : null}
    </>
  );
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  }
}

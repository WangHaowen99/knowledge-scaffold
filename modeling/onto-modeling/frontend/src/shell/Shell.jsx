// 外壳：全局页 ShellG（顶栏 + 可选二级导航）、项目页 ShellP（左栏 + 子导航）
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp, useProject } from '../app/context.js';
import { navigate, pagePath } from '../app/router.js';
import { toast } from '../app/toast.jsx';
import { errorText } from '../api/errors.js';
import { Button, Drawer, Icon, Menu, OverlayHostCtx, cx, useOutside } from '../ui/index.jsx';
import { CommandPalette } from './CommandPalette.jsx';
import { applyNotification } from './notifications.js';
import { GlobalTopbar, MaintenanceBanner, ProjectTopbar } from './Topbar.jsx';

function useOverlayHost() {
  const [host, setHost] = useState(null);
  const ref = useCallback((el) => setHost(el), []);
  return [host, ref];
}

const NTF_STYLE = {
  autobuild_finished: ['bot', 'var(--i50)', 'var(--indigo)'],
  autobuild_progress: ['rocket', 'var(--b50)', 'var(--blue)'],
  autobuild_suspension: ['pause', 'var(--a50)', 'var(--amber2)'],
  autobuild_stopped: ['alert', 'var(--r50)', 'var(--red)'],
  needs_input: ['hand', 'var(--a50)', 'var(--amber2)'],
  step_done: ['check', 'var(--b50)', 'var(--blue)'],
  session_error: ['alert', 'var(--r50)', 'var(--red)'],
  evidence_conflict: ['alert', 'var(--r50)', 'var(--red)'],
  publish_done: ['upload', 'var(--t50)', 'var(--teal)'],
  nomination_reviewed: ['book', 'var(--b50)', 'var(--blue)'],
  request_pending: ['inbox', 'var(--a50)', 'var(--amber2)'],
  request_reviewed: ['db', 'var(--b50)', 'var(--blue)'],
  upload_rejected: ['ban', 'var(--r50)', 'var(--red)'],
};

// 通知抽屉（A-13 / A-14，用户流 EU-01 实时追加）
function NotificationDrawer({ open, onClose, items, onReadAll }) {
  return (
    <Drawer
      open={open}
      onClose={onClose}
      icon="bell"
      title="通知"
      w={420}
      foot={<Button size="sm" kind="ghost" onClick={onReadAll}>全部标为已读</Button>}
    >
      {items.length ? items.map((n) => {
        const [i, bg, c] = NTF_STYLE[n.kind] || ['info', 'var(--b50)', 'var(--blue)'];
        return (
          <div className="en-ntf" key={n.id}>
            <span className="ni" style={{ background: bg, color: c }}><Icon name={i} size={18} /></span>
            <div className="grow">
              <div className="b">{n.title}</div>
              <div className="sm muted mt4">{n.body}</div>
              {n.action ? (
                <div className="mt8">
                  <Button size="xs" kind="soft" onClick={() => { onClose(); navigate(pagePath(n.action.page, n.project_id), n.action.params); }}>{n.action.label}</Button>
                </div>
              ) : null}
            </div>
          </div>
        );
      }) : <div className="muted sm">暂时没有通知</div>}
    </Drawer>
  );
}

// 全局页：用户流在这里开（契约 1.11：全局页开用户流）
export function ShellG({ active, side, children, flush, bodyStyle, onUserEvent }) {
  const { api, services, setMaintenance } = useApp();
  const [host, hostRef] = useOverlayHost();
  const [ntfOpen, setNtfOpen] = useState(false);
  const [ntf, setNtf] = useState({ items: [], unread: 0 });
  const handler = useRef(onUserEvent);
  handler.current = onUserEvent;

  useEffect(() => {
    let alive = true;
    api.notifications({ limit: 20 }).then((r) => alive && setNtf({ items: r.items || [], unread: r.unread_count || 0 })).catch(() => {});
    const stop = services.openStream({
      kind: 'user',
      url: (q) => api.userEventsUrl(q),
      onEvent: (env) => {
        if (env.type === 'notification') setNtf((s) => applyNotification(s, env.data));
        if (env.type === 'maintenance.updated') setMaintenance(env.data);
        handler.current?.(env);
      },
    });
    return () => { alive = false; stop(); };
  }, [api, services, setMaintenance]);

  const readAll = async () => {
    try {
      const r = await api.markNotificationsRead({ all: true });
      setNtf((s) => ({ items: s.items.map((n) => ({ ...n, read: true })), unread: r.unread_count }));
    } catch (e) {
      toast(errorText(e), 'alert');
    }
  };

  return (
    <OverlayHostCtx.Provider value={host}>
      <div className="app">
        <GlobalTopbar active={active} unread={ntf.unread} onBell={() => setNtfOpen(true)} />
        <MaintenanceBanner />
        <div className="app-main">
          {side}
          <div className={cx('pg-body', flush && 'flush')} style={bodyStyle}>{children}</div>
        </div>
        <NotificationDrawer open={ntfOpen} onClose={() => setNtfOpen(false)} items={ntf.items} onReadAll={readAll} />
        <div ref={hostRef} style={{ display: 'contents' }} />
      </div>
      <CommandPalette />
    </OverlayHostCtx.Provider>
  );
}

// ---------- 项目页 ----------
const KERNEL_RAIL = [['wb-l2', '工作台', 'layout'], ['studio', 'Studio', 'graph'], ['resources', '资料', 'folder'], ['publish', '发布', 'upload']];
// 子页面归到哪个导航项（同设计稿 RAIL_OF）
export const RAIL_OF = {
  inbox: 'wb-l2', 'wb-materials': 'wb-l2', 'wb-blueprint': 'wb-l2', 'l4-run': 'wb-l2', incremental: 'wb-l2', flowchart: 'wb-l2', 'chat-states': 'wb-l2',
  'studio-table': 'studio', 'studio-er': 'studio', reports: 'studio', sources: 'resources', snapshots: 'resources', schema: 'resources', versions: 'publish',
};
const MAX_PLUGIN_RAIL = 5;

function ProjectRail({ active }) {
  const { pid, extensions, inboxCount } = useProject();
  const [moreAt, setMoreAt] = useState(null);
  const moreRef = useRef(null);
  useOutside(moreRef, () => setMoreAt(null), !!moreAt);
  const act = RAIL_OF[active] || active;
  const nav = extensions?.nav || [];
  const kernel = KERNEL_RAIL.map(([id, title, icon]) => {
    const n = nav.find((x) => x.id === id);
    return { id, title: n?.title || title, icon: n?.icon || icon };
  });
  // 插件页面：只列声明的页面 ∩ 前端内置页面，按声明顺序，超过 5 个进「更多」（详设 5.9.7）
  const plugins = nav.filter((x) => x.kind === 'plugin').sort((a, b) => a.order - b.order);
  const shown = plugins.slice(0, MAX_PLUGIN_RAIL);
  const more = plugins.slice(MAX_PLUGIN_RAIL);
  const item = (x, badge) => (
    <a key={x.id} className={act === x.id ? 'on' : ''} href={`#${pagePath(x.id, pid)}`}>
      <Icon name={x.icon} size={18} />
      <span>{x.title}</span>
      {/* 按数字换键：数变了重新挂载，播一次跳动 */}
      {badge ? <span key={badge} className="bdg">{badge > 99 ? '99+' : badge}</span> : null}
    </a>
  );
  return (
    <nav className="prail">
      {kernel.map((x) => item(x, x.id === 'wb-l2' ? inboxCount : 0))}
      {shown.length ? (
        <>
          <div className="pr-pl">插件</div>
          {shown.map((x) => item(x, 0))}
          {more.length ? (
            <a className={more.some((m) => m.id === act) ? 'on' : ''} onClick={(e) => { const r = e.currentTarget.getBoundingClientRect(); setMoreAt({ left: r.right + 6, top: r.top }); }}>
              <Icon name="more" size={18} /><span>更多</span>
            </a>
          ) : null}
        </>
      ) : null}
      <span className="gap" />
      <hr />
      <a className={act === 'proj-settings' ? 'on' : ''} href={`#${pagePath('proj-settings', pid)}`}>
        <Icon name="gear" size={18} /><span>设置</span>
      </a>
      {moreAt ? (
        <div ref={moreRef}>
          <Menu className="pr-more" style={moreAt} items={more.map((m) => ({ label: m.title, icon: m.icon, onClick: () => navigate(pagePath(m.id, pid)) }))} onPick={() => setMoreAt(null)} />
        </div>
      ) : null}
    </nav>
  );
}

function SubNav({ active }) {
  const { pid, extensions, inboxCount } = useProject();
  const grp = RAIL_OF[active] || active;
  const plugMark = (title) => (
    <span className="sn-pl" data-pop={`由「${title}」插件提供`}><Icon name="puzzle" size={12} /></span>
  );
  let items = null;
  if (grp === 'wb-l2') items = [['wb-l2', '对话'], ['inbox', <>待处理{inboxCount ? <span className="sn-n">{inboxCount}</span> : null}</>]];
  if (grp === 'studio') {
    // 包选用了 ER 视图（PL-12 view_choices）时，Studio 的视图切换多一项（R23）
    const er = (extensions?.view_choices || []).some((v) => v.id === 'er_view');
    items = [['studio', '图谱'], ...(er ? [['studio-er', 'ER 视图']] : []), ['studio-table', '表格与条目'], ['reports', '报告']];
  }
  if (grp === 'publish') items = [['publish', '发布'], ['versions', '版本历史']];
  if (grp === 'resources') {
    items = [['resources', '资料区'], ...(extensions?.subpages || []).filter((s) => s.parent === 'resources').map((s) => [s.id, <>{s.title}{plugMark(s.pack_label || '系统逆向建模')}</>])];
  }
  if (!items) return null;
  const cur = items.some(([k]) => k === active) ? active : grp;
  return (
    <div className="subnav">
      {items.map(([k, t]) => (
        <a key={k} className={k === cur ? 'on' : ''} href={`#${pagePath(k, pid)}`}>{t}</a>
      ))}
    </div>
  );
}

export function ShellP({ active, nosub, flush, bodyStyle, children, overlay }) {
  const { pid, extensions, inboxCount } = useProject();
  const [host, hostRef] = useOverlayHost();
  const body = <div className={cx('pg-body', flush && 'flush')} style={bodyStyle}>{children}</div>;
  const grp = RAIL_OF[active] || active;
  const hasSub = !nosub && ['wb-l2', 'studio', 'publish', 'resources'].includes(grp);
  return (
    <OverlayHostCtx.Provider value={host}>
      <div className="app">
        <ProjectTopbar />
        <MaintenanceBanner />
        <div className="app-main">
          <ProjectRail active={active} />
          {hasSub ? <div className="pcol"><SubNav active={active} />{body}</div> : body}
        </div>
        {overlay}
        <div ref={hostRef} style={{ display: 'contents' }} />
      </div>
      <CommandPalette projectId={pid} inboxCount={inboxCount} extensions={extensions} />
    </OverlayHostCtx.Provider>
  );
}

export function PageHead({ title, desc, actions, eyebrow }) {
  return (
    <div className="page-h">
      <div>
        {eyebrow ? <div className="eyebrow">{eyebrow}</div> : null}
        <h1>{title}</h1>
        {desc ? <p>{desc}</p> : null}
      </div>
      {actions ? <div className="acts">{actions}</div> : null}
    </div>
  );
}

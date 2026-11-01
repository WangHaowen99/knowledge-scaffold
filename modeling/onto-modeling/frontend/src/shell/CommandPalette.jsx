// ⌘K 命令面板：跳转、全文搜索（F-07）、全部命令（S-18，选中经 S-20 发到对话）
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp } from '../app/context.js';
import { navigate, pagePath } from '../app/router.js';
import { toast } from '../app/toast.jsx';
import { errorText } from '../api/errors.js';
import { Icon, Kbd } from '../ui/index.jsx';

const listeners = new Set();
export function openPalette() { listeners.forEach((fn) => fn()); }

export const PAGE_TITLES = {
  'wb-l2': '工作台', inbox: '待处理', studio: 'Studio 图谱', 'studio-table': 'Studio 表格', reports: 'Studio 报告',
  resources: '资料区', sources: '业务系统数据源', snapshots: '采集与快照', schema: '证据浏览器', publish: '发布', versions: '版本历史',
  incremental: '增量更新', 'wb-materials': '资料分析', 'wb-blueprint': '切片规划', flowchart: '流程图', 'plugin-detail': '插件详情', 'plugin-upload': '上传与审核插件', sys: '系统设置',
  'ops-obs': '可观测', 'ops-ctl': '运维管控与健康', 'ops-users': '用户、审计与回收站', 'l4-run': '自主构建', 'proj-settings': '项目设置', scenarios: '场景走查',
  knowledge: '知识沉淀', scripts: '代码生成', plugins: '插件中心', me: '个人设置', ops: '运维中心', projects: '我的项目',
  create: '新建项目', import: '导入项目', 'chat-states': '对话卡片与状态',
};

function staticItems({ projectId, inboxCount, hasPlugin, hasSubpage, role }) {
  const canOps = role === 'admin' || role === 'ops';
  if (!projectId) {
    return [
      ['跳转', '我的项目', 'grid', 'projects'], ['跳转', '新建项目…', 'plus', 'create'], ['跳转', '导入项目…', 'download', 'import'],
      ['插件', '安装插件 / 能力包…', 'puzzle', 'plugins'], ['设置', '我的模型配置', 'key', 'me'],
      ...(canOps ? [['运维', '查看运行状态', 'activity', 'ops']] : []),
    ];
  }
  return [
    ['跳转', '打开 Studio 图谱', 'graph', 'studio'], ['跳转', `待处理（${inboxCount ?? 0}）`, 'inbox', 'inbox'],
    ...(hasSubpage('schema') ? [['跳转', '证据浏览器', 'db', 'schema']] : []), ['跳转', 'Studio 报告', 'filetext', 'reports'],
    ['建模', '发起增量更新…', 'refresh', 'incremental'], ['建模', '调整业务切片…', 'layers', 'wb-blueprint', { dialog: 'blueprint' }],
    ['建模', '开始自主构建（L4，一路建完）…', 'rocket', 'l4-run'], ['建模', '自主构建设置', 'gear', 'proj-settings'],
    ...(hasPlugin('scenarios') ? [['建模', '补齐过期场景', 'flask', 'scenarios']] : []),
    ['资料', '上传资料…', 'upload', 'resources'],
    ...(hasSubpage('sources') ? [['资料', '连接业务系统…', 'plug', 'sources'], ['资料', '采集一次快照', 'scan', 'snapshots']] : []),
    ['发布', '发布检查与发布…', 'upload', 'publish'], ['发布', '标记一个版本…', 'tag', 'versions'],
    ['插件', '安装插件 / 能力包…', 'puzzle', 'plugins'], ['插件', '本项目已启用的能力', 'layers', 'proj-settings'],
    ['设置', '我的模型配置', 'key', 'me'], ['设置', '对外 MCP 与 Token', 'link', 'proj-settings'],
    ...(canOps ? [['运维', '查看运行状态', 'activity', 'ops']] : []),
  ];
}

export function CommandPalette({ projectId, inboxCount, extensions, conversationId }) {
  const { api, user } = useApp();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [sel, setSel] = useState(0);
  const [remote, setRemote] = useState({ hits: [], commands: [] });
  const inputRef = useRef(null);
  const catalog = useRef(null);

  useEffect(() => {
    const fn = () => { setOpen(true); setQ(''); setSel(0); setTimeout(() => inputRef.current?.focus(), 20); };
    listeners.add(fn);
    const key = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); fn(); }
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('keydown', key);
    return () => { listeners.delete(fn); document.removeEventListener('keydown', key); };
  }, []);

  useEffect(() => {
    if (!open || !projectId) return undefined;
    let alive = true;
    const t = setTimeout(async () => {
      try {
        if (!catalog.current) catalog.current = await api.commandCatalog(projectId);
        const cmds = q ? catalog.current.groups.flatMap((g) => g.items).filter((c) => c.available && (c.label.includes(q) || c.description.includes(q))) : [];
        const res = q ? await api.search(projectId, q) : null;
        if (!alive) return;
        const hits = [];
        (res?.term_summary || []).forEach((t2) => {
          const b = t2.by_group;
          hits.push({ label: `「${t2.term}」· 知识包 ${b.entries} 处、资料 ${b.materials} 处、报告 ${b.reports} 处`, icon: 'search', page: 'studio' });
        });
        (res?.groups || []).forEach((g) => g.items.forEach((h) => hits.push({ label: `${h.title} · ${h.snippet}`, icon: 'link', page: h.page })));
        setRemote({ hits: hits.slice(0, 6), commands: cmds.slice(0, 6) });
      } catch {
        if (alive) setRemote({ hits: [], commands: [] });
      }
    }, 180);
    return () => { alive = false; clearTimeout(t); };
  }, [q, open, projectId, api]);

  const hasPlugin = useCallback((id) => (extensions?.nav || []).some((n) => n.id === id), [extensions]);
  const hasSubpage = useCallback((id) => (extensions?.subpages || []).some((n) => n.id === id), [extensions]);

  const list = useMemo(() => {
    const items = [];
    remote.hits.forEach((h) => items.push({ group: '内容', label: h.label, icon: h.icon, page: h.page }));
    remote.commands.forEach((c) => items.push({ group: '命令', label: c.label, icon: 'cmd', command: c, desc: c.description }));
    staticItems({ projectId, inboxCount, hasPlugin, hasSubpage, role: user?.role })
      .filter(([g, t]) => !q || t.includes(q) || g.includes(q))
      .forEach(([g, t, i, page, params]) => items.push({ group: g, label: t, icon: i, page, params }));
    return items;
  }, [remote, projectId, inboxCount, hasPlugin, hasSubpage, user, q]);

  const pick = async (it) => {
    setOpen(false);
    if (it.command) {
      try {
        await api.sendCommand(projectId, { command: it.command.command, text: it.command.label, origin: { page: 'palette', label: '命令面板' }, conversation_id: conversationId || undefined });
        navigate(pagePath('wb-l2', projectId));
      } catch (e) {
        toast(errorText(e), 'alert');
      }
      return;
    }
    navigate(pagePath(it.page, projectId), it.params);
  };

  if (!open) return <div id="palette" />;
  let g = '';
  return (
    <div id="palette" className="show" onMouseDown={(e) => { if (e.target === e.currentTarget) setOpen(false); }}>
      <div className="box">
        <div className="q">
          <Icon name="search" size={18} />
          <input
            ref={inputRef}
            placeholder="搜索页面、产物或命令…"
            value={q}
            onChange={(e) => { setQ(e.target.value); setSel(0); }}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(list.length - 1, s + 1)); }
              if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(0, s - 1)); }
              if (e.key === 'Enter' && list[sel]) { e.preventDefault(); pick(list[sel]); }
            }}
          />
          <Kbd>esc</Kbd>
        </div>
        <div className="list">
          {list.length ? list.map((it, i) => {
            const head = it.group !== g ? (g = it.group, <div className="gt">{it.group}</div>) : null;
            return (
              <div key={`${it.group}-${it.label}`}>
                {head}
                <div className={`it ${i === sel ? 'on' : ''}`} onMouseEnter={() => setSel(i)} onClick={() => pick(it)}>
                  <Icon name={it.icon} size={14} />{it.label}
                  <span className="d">{it.desc || PAGE_TITLES[it.page] || ''}</span>
                </div>
              </div>
            );
          }) : <div className="gt">没有匹配</div>}
        </div>
      </div>
    </div>
  );
}

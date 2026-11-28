// ⌘K 命令面板：跳转、全文搜索（F-07）、全部命令（S-18，组名、条目名和输入框「/」菜单一样；每次打开重取，选中经 S-20 发条目的 target）
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useApp } from '../app/context.js';
import { navigate, pagePath } from '../app/router.js';
import { toast } from '../app/toast.jsx';
import { errorText } from '../api/errors.js';
import { Icon, Kbd } from '../ui/index.jsx';
import { contentHits, paletteList, staticItems } from './paletteModel.js';
import { catalogCommand } from '../state/flow.js';
import { filterCatalog } from '../state/commandMenu.js';

const listeners = new Set();
export function openPalette() { listeners.forEach((fn) => fn()); }

export const PAGE_TITLES = {
  'wb-l2': '工作台', inbox: '待处理', studio: 'Studio 图谱', 'studio-table': 'Studio 表格', 'studio-er': 'ER 视图', reports: 'Studio 报告',
  resources: '资料区', publish: '发布', versions: '版本历史',
  incremental: '增量更新', 'wb-materials': '资料分析', 'wb-blueprint': '建模蓝图', flowchart: '流程图', 'plugin-detail': '插件详情', 'plugin-upload': '上传与审核插件', sys: '系统设置',
  'ops-obs': '可观测', 'ops-ctl': '运维管控与健康', 'ops-users': '用户、审计与回收站', 'l4-run': '自主构建', 'proj-settings': '项目设置', scenarios: '场景模拟',
  scripts: '代码生成', plugins: '插件中心', me: '个人设置', ops: '运维中心', projects: '我的项目',
  create: '新建项目', import: '导入项目',
};

export function CommandPalette({ projectId, inboxCount, extensions, conversationId }) {
  const { api, user } = useApp();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [sel, setSel] = useState(0);
  const [remote, setRemote] = useState({ hits: [] });
  const inputRef = useRef(null);
  const [catalog, setCatalog] = useState(null);

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
    if (!open || !projectId) { setCatalog(null); return undefined; }
    let alive = true;
    api.commandCatalog(projectId).then((c) => { if (alive) setCatalog(c); }).catch(() => {});
    return () => { alive = false; };
  }, [open, projectId, api]);

  useEffect(() => {
    if (!open || !projectId) return undefined;
    let alive = true;
    const t = setTimeout(async () => {
      try {
        const res = q ? await api.search(projectId, q) : null;
        if (alive) setRemote({ hits: contentHits(res) });
      } catch {
        if (alive) setRemote({ hits: [] });
      }
    }, 180);
    return () => { alive = false; clearTimeout(t); };
  }, [q, open, projectId, api]);

  const hasPlugin = useCallback((id) => (extensions?.nav || []).some((n) => n.id === id), [extensions]);

  const list = useMemo(() => paletteList({
    q, hits: remote.hits, commands: q ? filterCatalog(catalog, q) : [], statics: staticItems({ projectId, inboxCount, hasPlugin, user }),
  }), [remote, catalog, projectId, inboxCount, hasPlugin, user, q]);

  const pick = async (it) => {
    setOpen(false);
    if (it.command) {
      try {
        const body = { ...catalogCommand(it.command), origin: { page: 'palette', label: '命令面板' }, conversation_id: conversationId || undefined };
        await (body.node_id
          ? api.startNode(projectId, body.node_id, { conversation_id: body.conversation_id, origin: body.origin })
          : api.sendCommand(projectId, body));
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
                  <span className="d">{PAGE_TITLES[it.page] || ''}</span>
                </div>
              </div>
            );
          }) : <div className="gt">没有匹配</div>}
        </div>
      </div>
    </div>
  );
}

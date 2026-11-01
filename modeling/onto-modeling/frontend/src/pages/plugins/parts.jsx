// 插件中心 3 页共用（设计稿 pages-60-plugins.js 的 plSideNav、PL_CAT）：左栏（浏览分类、本项目、管理员）、「启用到」哪个项目。
import { useEffect, useMemo, useState } from 'react';
import { useApp } from '../../app/context.js';
import { SNav } from '../../shell/SideNav.jsx';
import { can } from '../../state/roles.js';
import { Chip } from '../../ui/index.jsx';
import '../../styles/plugins.css';

// 分类：名称、图标、说明（设计稿 PL_CAT）；node、exporter 契约里有、设计稿没画，有包时才在左栏出现
export const PACK_CATEGORIES = {
  capability: ['能力包', 'cube', '决定怎么建模'],
  industry: ['行业包', 'book', '行业知识资产，只有数据'],
  system: ['系统知识包', 'server', '商用系统的表字段字典'],
  provider: ['证据源提供方', 'db', '连接器网关里运行，管理员安装'],
  node: ['节点包', 'branch', '往流程里加节点'],
  exporter: ['导出器', 'download', '把知识包导出成其他格式'],
};
// 卡片图标底色（设计稿 .pl-ico.cap / .node / .ind / .sys / .prov / .exp）
export const PACK_ICON_CLASS = { capability: 'cap', node: 'node', industry: 'ind', system: 'sys', provider: 'prov', exporter: 'exp' };

const PROJECT_KEY = 'onto.plugins.project';

/**
 * 「启用到」哪个项目：地址参数 ?project= 优先，其次上次选的，最后取最近的一个未归档项目。
 * 返回 { projectId, setProjectId, projects }；projects 为 P-01 的卡片（未归档）。
 */
export function usePluginProject(route) {
  const { api } = useApp();
  const [projects, setProjects] = useState(null);
  const [chosen, setChosen] = useState(() => {
    try { return route?.query?.project || window.localStorage.getItem(PROJECT_KEY) || ''; } catch { return route?.query?.project || ''; }
  });
  useEffect(() => {
    let alive = true;
    api.projects({ limit: 200 }).then((r) => alive && setProjects((r.items || []).filter((p) => !p.archived))).catch(() => alive && setProjects([]));
    return () => { alive = false; };
  }, [api]);
  const projectId = useMemo(() => {
    if (!projects) return chosen || null;
    if (chosen && projects.some((p) => p.id === chosen)) return chosen;
    return projects[0]?.id || null;
  }, [projects, chosen]);
  const setProjectId = (id) => {
    setChosen(id);
    try { window.localStorage.setItem(PROJECT_KEY, id); } catch { /* 存储不可用时只在本页生效 */ }
  };
  return { projectId, setProjectId, projects: projects || [] };
}

/**
 * 左栏。active 是分类键（all、capability…、inst、upd）或 'up'（上传与审核）。
 * counts 为 PL-01 的 counts；没传时自己按 projectId 取一次。
 * onCat 没传时点分类跳到插件中心（?cat=）。
 */
export function PluginsNav({ active, counts: given, onCat, projectId }) {
  const { api, user } = useApp();
  const [fetched, setFetched] = useState(null);
  useEffect(() => {
    if (given) return undefined;
    let alive = true;
    api.packs(projectId ? { project_id: projectId } : undefined).then((r) => alive && setFetched(r.counts || null)).catch(() => {});
    return () => { alive = false; };
  }, [api, given, projectId]);
  const counts = given || fetched || {};
  const n = (k) => <span className="xs faint num">{counts[k] ?? ''}</span>;
  const item = (key, title, icon, extra) => (onCat ? { key, title, icon, extra } : { key, title, icon, extra, go: 'plugins', query: key === 'all' ? undefined : { cat: key } });
  const cats = Object.entries(PACK_CATEGORIES)
    .filter(([k]) => !['node', 'exporter'].includes(k) || counts[k] > 0)
    .map(([k, [t, i]]) => item(k, t, i, n(k)));
  return (
    <SNav
      active={active}
      onPick={onCat}
      groups={[
        { title: '浏览', items: [item('all', '全部', 'grid', n('all')), ...cats] },
        {
          title: '本项目',
          items: [
            item('inst', '已启用', 'check', n('enabled')),
            item('upd', '可更新', 'refresh', counts.updatable ? <Chip tone="amber" sm>{counts.updatable}</Chip> : null),
          ],
        },
        { title: '管理员', items: [{ key: 'up', title: '上传与审核', icon: 'upload', go: 'plugin-upload', hidden: !can(user, 'admin') }] },
      ]}
      bottom={(
        <div className="pl-snote"><b>谁能做什么</b><br />管理员把包安装到平台；你把包启用到自己的项目。启用后保存一个版本，从下一条消息开始生效。</div>
      )}
    />
  );
}

// 插件中心 3 页共用（设计稿 pages-60-plugins.js 的 plSideNav、PL_CAT）：左栏（浏览分类、本项目、管理员）、「启用到」哪个项目。
import { useEffect, useMemo, useState } from 'react';
import { useApp } from '../../app/context.js';
import { SNav } from '../../shell/SideNav.jsx';
import { can } from '../../state/roles.js';
import { Chip } from '../../ui/index.jsx';
import { OPTIONAL_CATEGORIES, PACK_CATEGORIES } from './catalogModel.js';
import '../../styles/plugins.css';

// 分类与图标底色的定义在 catalogModel.js（纯逻辑，单测用），这里照旧导出
export { PACK_CATEGORIES, PACK_ICON_CLASS } from './catalogModel.js';

const PROJECT_KEY = 'onto.plugins.project';

/**
 * 「启用到」哪个项目：地址参数 ?project= 优先，其次上次选的，最后取最近的一个未归档项目。
 * 返回 { projectId, setProjectId, projects, ready }；projects 为 P-01 的卡片（未归档），ready 表示项目列表已取到。
 * enabled=false 时不取项目列表（左栏已经拿到项目时用）。
 */
export function usePluginProject(route, { enabled = true } = {}) {
  const { api } = useApp();
  const [projects, setProjects] = useState(null);
  const [chosen, setChosen] = useState(() => {
    try { return route?.query?.project || window.localStorage.getItem(PROJECT_KEY) || ''; } catch { return route?.query?.project || ''; }
  });
  // 从项目里带着 ?project= 进来（项目设置「添加能力」、⌘K、助手推荐）：「启用到」就是这个项目，并记下来，
  // 之后从详情页回来、从顶栏进来都还是它（F2 第 4 条：原来按上次选的或最近的项目，可能装到别的项目）
  const wanted = route?.query?.project;
  useEffect(() => {
    if (!wanted) return;
    setChosen(wanted);
    try { window.localStorage.setItem(PROJECT_KEY, wanted); } catch { /* 存储不可用时只在本页生效 */ }
  }, [wanted]);
  useEffect(() => {
    if (!enabled) return undefined;
    let alive = true;
    api.projects({ limit: 200 }).then((r) => alive && setProjects((r.items || []).filter((p) => !p.archived))).catch(() => alive && setProjects([]));
    return () => { alive = false; };
  }, [api, enabled]);
  const projectId = useMemo(() => {
    if (!projects) return chosen || null;
    if (chosen && projects.some((p) => p.id === chosen)) return chosen;
    return projects[0]?.id || null;
  }, [projects, chosen]);
  const setProjectId = (id) => {
    setChosen(id);
    try { window.localStorage.setItem(PROJECT_KEY, id); } catch { /* 存储不可用时只在本页生效 */ }
  };
  return { projectId, setProjectId, projects: projects || [], ready: !enabled || projects !== null };
}

/**
 * 左栏。active 是分类键（all、capability…、inst、upd）或 'up'（上传与审核）。
 * counts 为 PL-01 的 counts；没传时自己取一次：projectId 没传时按「启用到」上次选的项目算（「已启用」「可更新」和插件中心一致）。
 * onCat 没传时点分类跳到插件中心（?cat=）。
 */
export function PluginsNav({ active, counts: given, onCat, projectId }) {
  const { api, user } = useApp();
  const [fetched, setFetched] = useState(null);
  const fallback = usePluginProject(undefined, { enabled: !given && projectId === undefined });
  const pid = projectId !== undefined ? projectId : fallback.projectId;
  const waiting = projectId === undefined && !fallback.ready;
  useEffect(() => {
    if (given || waiting) return undefined;
    let alive = true;
    api.packs(pid ? { project_id: pid } : undefined).then((r) => alive && setFetched(r.counts || null)).catch(() => {});
    return () => { alive = false; };
  }, [api, given, pid, waiting]);
  const counts = given || fetched || {};
  const n = (k) => <span className="xs faint num">{counts[k] ?? ''}</span>;
  const item = (key, title, icon, extra) => (onCat ? { key, title, icon, extra } : { key, title, icon, extra, go: 'plugins', query: key === 'all' ? undefined : { cat: key } });
  const cats = Object.entries(PACK_CATEGORIES)
    .filter(([k]) => !OPTIONAL_CATEGORIES.includes(k) || counts[k] > 0)
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
        can(user, 'admin')
          ? { title: '管理员', items: [{ key: 'up', title: '上传与审核', icon: 'upload', go: 'plugin-upload' }] }
          : { title: '我的', items: [{ key: 'up', title: '我的上传', icon: 'upload', go: 'plugin-upload' }] },
      ]}
    />
  );
}

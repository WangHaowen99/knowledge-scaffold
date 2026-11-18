// ⌘K 命令面板的数据：跳转项、全文搜索结果、排序（纯逻辑，单测用）
import { can } from '../state/roles.js';

export function staticItems({ projectId, inboxCount, hasPlugin, user }) {
  const canOps = can(user, 'ops.read');
  if (!projectId) {
    return [
      ['跳转', '我的项目', 'grid', 'projects'], ['跳转', '新建项目…', 'plus', 'create'], ['跳转', '导入项目…', 'download', 'import'],
      ['插件', '安装插件 / 能力包…', 'puzzle', 'plugins'], ['设置', '我的模型配置', 'key', 'me', { section: 'model' }],
      ...(can(user, 'admin') ? [['设置', '系统设置', 'gear', 'sys']] : []),
      ...(canOps ? [['运维', '查看运行状态', 'activity', 'ops']] : []),
    ];
  }
  return [
    ['跳转', '打开 Studio 图谱', 'graph', 'studio'], ['跳转', `待处理（${inboxCount ?? 0}）`, 'inbox', 'inbox'],
    ['跳转', 'Studio 报告', 'filetext', 'reports'],
    ['建模', '发起增量更新…', 'refresh', 'incremental'], ['建模', '建模蓝图…', 'layers', 'wb-blueprint', { dialog: 'blueprint' }],
    // 开始自主构建走项目设置的开始入口（AB-03 预览 → 确认 → AB-04，契约 5.0）
    ['建模', '开始自主构建…', 'rocket', 'proj-settings', { section: 'autobuild', dialog: 'start' }], ['建模', '自主构建设置', 'gear', 'proj-settings', { section: 'autobuild' }],
    ...(hasPlugin('scenarios') ? [['建模', '补齐过期场景', 'flask', 'scenarios']] : []),
    ['资料', '上传资料…', 'upload', 'resources'],
    ['发布', '发布检查与发布…', 'upload', 'publish'], ['发布', '标记一个版本…', 'tag', 'versions'],
    // 插件中心是全局页：带上当前项目，「启用到」就是这个项目（F2 第 4 条）
    ['插件', '安装插件 / 能力包…', 'puzzle', 'plugins', { project: projectId }], ['插件', '本项目已启用的能力', 'layers', 'proj-settings', { section: 'plugins' }],
    ['设置', '我的模型配置', 'key', 'me', { section: 'model' }], ['设置', '对外 MCP 与 Token', 'link', 'proj-settings', { section: 'mcp' }],
    ...(canOps ? [['运维', '查看运行状态', 'activity', 'ops']] : []),
  ];
}

/** 全文搜索（F-07）的结果；一处都没命中的词不列，否则回车会跳到空的 Studio 搜索 */
export function contentHits(res) {
  const hits = [];
  (res?.term_summary || []).forEach((t) => {
    const b = t.by_group || {};
    if (!((b.entries || 0) + (b.materials || 0) + (b.reports || 0))) return;
    hits.push({ label: `「${t.term}」· 知识包 ${b.entries || 0} 处、资料 ${b.materials || 0} 处、报告 ${b.reports || 0} 处`, icon: 'search', page: 'studio' });
  });
  (res?.groups || []).forEach((g) => g.items.forEach((h) => hits.push({ label: `${h.title} · ${h.snippet}`, icon: 'link', page: h.page })));
  return hits.slice(0, 6);
}

/**
 * 列表：对得上输入的跳转项在前（回车直接去），然后全文搜索、全部命令。
 * commands 是筛过的 S-18 组（commandMenu.filterCatalog）：组名、条目名和输入框「/」菜单一样，最多列 COMMAND_MAX 条
 */
export const COMMAND_MAX = 6;
export function paletteList({ q, hits, commands, statics }) {
  const items = [];
  statics
    .filter(([g, t]) => !q || t.includes(q) || g.includes(q))
    .forEach(([g, t, i, page, params]) => items.push({ group: g, label: t, icon: i, page, params }));
  hits.forEach((h) => items.push({ group: '内容', label: h.label, icon: h.icon, page: h.page }));
  (commands || []).flatMap((g) => g.items.map((c) => ({ group: g.label, label: c.label, icon: 'cmd', command: c })))
    .slice(0, COMMAND_MAX)
    .forEach((c) => items.push(c));
  return items;
}

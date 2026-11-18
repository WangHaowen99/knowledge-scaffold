// ⌘K 命令面板（F2 第 4 条：插件安装入口）：输入能对上跳转项时它排在全文搜索前；项目里「安装插件」带上当前项目；没有命中的全文搜索不列
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import { filterCatalog } from '../src/state/commandMenu.js';
import { catalogCommand } from '../src/state/flow.js';
import { COMMAND_MAX, contentHits, paletteList, staticItems } from '../src/shell/paletteModel.js';

const user = { username: 'tester', role: 'user' };
const ctx = { inboxCount: 0, hasPlugin: () => false, hasSubpage: () => false, user };

test('项目里「安装插件 / 能力包…」去插件中心时带上当前项目；项目外不带', () => {
  const inProject = staticItems({ ...ctx, projectId: 'task-a' }).find(([, t]) => t.startsWith('安装插件'));
  assert.equal(inProject[3], 'plugins');
  assert.deepEqual(inProject[4], { project: 'task-a' });
  const outside = staticItems({ ...ctx, projectId: undefined }).find(([, t]) => t.startsWith('安装插件'));
  assert.equal(outside[4], undefined);
});

test('输入「安装」：跳转项排在全文搜索前，回车（第一项）就是安装插件', () => {
  const hits = contentHits({ term_summary: [{ term: '安装', by_group: { entries: 2, materials: 1, reports: 0 } }], groups: [] });
  const list = paletteList({ q: '安装', hits, commands: [], statics: staticItems({ ...ctx, projectId: 'task-a' }) });
  assert.equal(list[0].label, '安装插件 / 能力包…');
  assert.deepEqual(list[0].params, { project: 'task-a' });
  assert.equal(list[1].group, '内容');
});

test('对不上跳转项时全文搜索在前、命令随后；没输入时只有跳转项', () => {
  const hits = contentHits({ term_summary: [{ term: '门店', by_group: { entries: 3, materials: 0, reports: 0 } }], groups: [] });
  const commands = [{ id: 'core.validate', label: '校验', items: [{ id: 'core.validate#0', label: '门店盘点', target: '/x' }] }];
  const list = paletteList({ q: '门店', hits, commands, statics: staticItems({ ...ctx, projectId: 'task-a' }) });
  assert.deepEqual(list.map((x) => x.group), ['内容', '校验']);
  const idle = paletteList({ q: '', hits: [], commands: [], statics: staticItems({ ...ctx, projectId: 'task-a' }) });
  assert.ok(idle.length > 5 && idle.every((x) => x.page));
});

test('全文搜索一处都没命中的词不列（原来「「安装」· 知识包 0 处、资料 0 处、报告 0 处」排第一，回车去了 Studio）', () => {
  assert.deepEqual(contentHits({ term_summary: [{ term: '安装', by_group: { entries: 0, materials: 0, reports: 0 } }], groups: [] }), []);
  const hits = contentHits({ term_summary: [], groups: [{ items: [{ title: '门店', snippet: '对象', page: 'studio-table' }] }] });
  assert.deepEqual(hits, [{ label: '门店 · 对象', icon: 'link', page: 'studio-table' }]);
});

test('⌘K 的命令和输入框「/」菜单同一份 S-18：组名是节点名、条目名一样、筛法一样，没有灰字说明；选中发条目的 target', () => {
  const catalog = F.commandCatalog();
  const groups = filterCatalog(catalog, '模拟');
  const list = paletteList({ q: '模拟', hits: [], commands: groups, statics: [] });
  assert.deepEqual(list.map((x) => [x.group, x.label]), groups.flatMap((g) => g.items.map((c) => [g.label, c.label])));
  assert.deepEqual(list.map((x) => [x.group, x.label]), [['场景模拟', '场景模拟'], ['场景模拟', '交互模拟']]);
  assert.ok(list.every((x) => x.command && !('desc' in x)));
  assert.deepEqual(catalogCommand(list[1].command), { command: '/ontology-trace', text: '交互', display_label: '交互模拟' });
  // 命令最多列 COMMAND_MAX 条，按组的顺序取
  const many = paletteList({ q: '', hits: [], commands: filterCatalog(catalog, ''), statics: [] });
  assert.equal(many.length, COMMAND_MAX);
  assert.deepEqual(many.slice(0, 2).map((x) => x.label), ['资料分析', '追加资料分析']);
});

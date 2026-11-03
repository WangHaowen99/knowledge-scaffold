// ⌘K 命令面板（F2 第 4 条：插件安装入口）：输入能对上跳转项时它排在全文搜索前；项目里「安装插件」带上当前项目；没有命中的全文搜索不列
import assert from 'node:assert/strict';
import test from 'node:test';
import { contentHits, paletteList, staticItems } from '../src/shell/paletteModel.js';

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
  const commands = [{ label: '门店盘点', description: '', command: '/x' }];
  const list = paletteList({ q: '门店', hits, commands, statics: staticItems({ ...ctx, projectId: 'task-a' }) });
  assert.deepEqual(list.map((x) => x.group), ['内容', '命令']);
  const idle = paletteList({ q: '', hits: [], commands: [], statics: staticItems({ ...ctx, projectId: 'task-a' }) });
  assert.ok(idle.length > 5 && idle.every((x) => x.page));
});

test('全文搜索一处都没命中的词不列（原来「「安装」· 知识包 0 处、资料 0 处、报告 0 处」排第一，回车去了 Studio）', () => {
  assert.deepEqual(contentHits({ term_summary: [{ term: '安装', by_group: { entries: 0, materials: 0, reports: 0 } }], groups: [] }), []);
  const hits = contentHits({ term_summary: [], groups: [{ items: [{ title: '门店', snippet: '对象', page: 'studio-table' }] }] });
  assert.deepEqual(hits, [{ label: '门店 · 对象', icon: 'link', page: 'studio-table' }]);
});

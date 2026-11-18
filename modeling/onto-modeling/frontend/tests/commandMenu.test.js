// 输入框「/」全部命令菜单（S-18）：斜杠后面的字按名字筛、组就是导航栏节点、↑↓ 选、回车执行、Esc 关、没有匹配时按原文发
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import {
  filterCatalog, flatItems, menuKey, menuPick, menuQuery, moveSelection,
} from '../src/state/commandMenu.js';
import { catalogCommand } from '../src/state/flow.js';

const catalog = F.commandCatalog();

test('只有斜杠开头才是查询，斜杠后面的字（去掉首尾空格）是筛选词', () => {
  assert.equal(menuQuery('/'), '');
  assert.equal(menuQuery('/ 模拟 '), '模拟');
  assert.equal(menuQuery('帮我看看 /x'), null);
  assert.equal(menuQuery(''), null);
  assert.equal(menuQuery(undefined), null);
});

test('不筛时分组、条目和 S-18 一样，组名是节点名；按名字筛不分大小写，没有条目的组不留', () => {
  const all = filterCatalog(catalog, '');
  assert.deepEqual(all.map((g) => g.label), catalog.groups.map((g) => g.label));
  assert.deepEqual(flatItems(all).map((c) => c.label), catalog.groups.flatMap((g) => g.items.map((c) => c.label)));
  // 挂在节点下面的可选节点按钮跟着节点那组
  assert.ok(all.find((g) => g.label === '资料分析').items.some((c) => c.label === '绘制流程图'));
  assert.ok(all.find((g) => g.label === '提取操作').items.some((c) => c.label === '提取规则与指标'));

  const hit = filterCatalog(catalog, '流程图');
  assert.deepEqual(hit.map((g) => g.label), ['资料分析']);
  assert.deepEqual(flatItems(hit).map((c) => c.label), ['绘制流程图', '全量刷新流程图', '校验流程图']);
  const mixed = filterCatalog({ groups: [{ id: 'g', label: 'G', items: [{ id: 'a', label: 'Mock 数据' }] }] }, 'mock');
  assert.equal(flatItems(mixed).length, 1);
  assert.deepEqual(filterCatalog(catalog, '没有这个'), []);
  assert.deepEqual(filterCatalog(null, ''), []);
  // 后端标了不可用的不列
  assert.deepEqual(filterCatalog({ groups: [{ id: 'g', label: 'G', items: [{ id: 'a', label: 'A', available: false }] }] }, ''), []);
});

test('↑↓ 停在两头；回车取选中的那条，没有匹配时为空', () => {
  assert.equal(moveSelection(0, -1, 3), 0);
  assert.equal(moveSelection(1, 1, 3), 2);
  assert.equal(moveSelection(2, 1, 3), 2);
  assert.equal(moveSelection(4, 1, 0), 0);
  const groups = filterCatalog(catalog, '模拟');
  assert.equal(menuPick(groups, 1).label, '交互模拟');
  assert.equal(menuPick([], 0), null);
});

test('按键：Esc 只关菜单；斜杠查询时 ↑↓ 移动、回车执行选中的那条；输入法组字中、Shift+回车、菜单为空时不归菜单', () => {
  const st = { open: true, filtering: true, count: 3, sel: 1 };
  assert.deepEqual(menuKey({ key: 'Escape' }, st), { type: 'close' });
  assert.deepEqual(menuKey({ key: 'ArrowDown' }, st), { type: 'move', sel: 2 });
  assert.deepEqual(menuKey({ key: 'ArrowUp' }, { ...st, sel: 0 }), { type: 'move', sel: 0 });
  assert.deepEqual(menuKey({ key: 'Enter' }, st), { type: 'run', index: 1 });
  assert.equal(menuKey({ key: 'Enter', composing: true }, st), null);
  assert.equal(menuKey({ key: 'Enter', shift: true }, st), null);
  assert.equal(menuKey({ key: 'a' }, st), null);
  // 没有匹配：菜单不显示，回车按原文发送，Esc 交给工作台
  assert.equal(menuKey({ key: 'Enter' }, { ...st, count: 0, sel: 0 }), null);
  assert.equal(menuKey({ key: 'Escape' }, { ...st, count: 0, sel: 0 }), null);
  // 从按钮打开、框里是别的字：只有 Esc 归菜单，回车照常发框里的字
  assert.deepEqual(menuKey({ key: 'Escape' }, { ...st, filtering: false }), { type: 'close' });
  assert.equal(menuKey({ key: 'Enter' }, { ...st, filtering: false }), null);
  assert.equal(menuKey({ key: 'ArrowDown' }, { ...st, filtering: false }), null);
  assert.equal(menuKey({ key: 'Escape' }, { ...st, open: false }), null);
});

test('执行条目发它的完整命令文本（target）带上按钮名，和导航栏、建议条同一个名字', () => {
  const item = flatItems(filterCatalog(catalog, '追加'))[0];
  assert.deepEqual(catalogCommand(item), { command: '/ontology-research', text: '追加', display_label: '追加资料分析' });
  const scene = flatItems(filterCatalog(catalog, '场景'))[0];
  assert.deepEqual(catalogCommand(scene), { command: '/ontology-trace', text: '全部', display_label: '场景模拟' });
});

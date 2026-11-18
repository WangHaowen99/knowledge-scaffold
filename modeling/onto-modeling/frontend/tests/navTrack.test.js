// 导航栏（节点模型重构 2b）：上导航栏的节点、条件节点的位置、节点下拉里的按钮、可选节点行与「这一轮不处理」
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import {
  navMenu, nodeButtons, normalizeFlow, optionalStart, trackNodes,
} from '../src/state/flow.js';

const flow = (o) => normalizeFlow(F.flow(o));
const byId = (f, id) => f.nodes.find((n) => n.id === id);
const pages = F.extensions().pages;

test('导航栏只放必需节点；条件成立的增量更新出现在它挂的节点（提取操作）后面，处理完就没有', () => {
  assert.deepEqual(trackNodes(flow()).map((n) => n.title), ['资料分析', '提取对象', '提取操作', '增量更新', '校验', '场景模拟', '发布']);
  assert.deepEqual(trackNodes(flow({ update: false })).map((n) => n.title), ['资料分析', '提取对象', '提取操作', '校验', '场景模拟', '发布']);
  // 后端顺序里条件节点排在后面时，照样挂到 under 后面
  const f = flow();
  const update = byId(f, 'core.update');
  const moved = { ...f, nodes: [...f.nodes.filter((n) => n !== update), update] };
  assert.deepEqual(trackNodes(moved).map((n) => n.id).slice(2, 5), ['core.operation', 'core.update', 'core.validate']);
  // 已停用的包的节点不上
  const off = { ...f, nodes: f.nodes.map((n) => (n.id === 'trace.trace' ? { ...n, flags: { ...n.flags, disabled: true } } : n)) };
  assert.ok(!trackNodes(off).some((n) => n.id === 'trace.trace'));
});

test('节点自己的按钮：主命令在前、变体在后，核验那句当普通消息；过期时叫节点名的刷新顶替主命令，原因带在按钮上', () => {
  const f = flow();
  assert.deepEqual(nodeButtons(byId(f, 'core.research')).map((b) => [b.label, b.kind, b.primary]), [
    ['资料分析', 'command', true], ['追加资料分析', 'command', false], ['核验资料分析报告', 'message', false],
  ]);
  const trace = nodeButtons(byId(f, 'trace.trace'));
  assert.deepEqual(trace.map((b) => b.label), ['场景模拟', '交互模拟', '生成示例数据'], '主命令同名的那条不再重复列');
  assert.equal(trace[0].reason, '对象、操作有变化');
  assert.equal(trace[0].target, '/ontology-trace 全部');
  // 跳过、恢复、旧的「去确认」不在按钮里
  const research = { ...byId(f, 'core.research'), actions: [{ kind: 'confirm', label: '去确认', target: 'wb-blueprint' }, ...byId(f, 'core.research').actions] };
  assert.ok(!nodeButtons(research).some((b) => b.label === '去确认'));
  assert.ok(!nodeButtons(byId(f, 'core.metric')).some((b) => ['跳过', '恢复'].includes(b.label)));
});

test('节点下拉：分隔线下面是挂在它下面的可选节点，没跳过给「跳过」、已跳过给「恢复」；面板节点只有状态，点名字去声明它的插件页', () => {
  const f = flow();
  const op = navMenu(f, byId(f, 'core.operation'), pages);
  assert.deepEqual(op.buttons.map((b) => b.label), ['提取操作']);
  assert.deepEqual(op.optional.map((o) => [o.title, o.display_state, o.op]), [['提取规则与指标', 'pending', 'skip'], ['求解适配', 'ready', null]]);
  assert.deepEqual(op.optional[0].start, { kind: 'command', label: '提取规则与指标', target: '/ontology-extract-rule-metric' });
  assert.deepEqual(op.optional[1].start, { kind: 'page', page: 'scripts', params: { node: 'codegen.solver' } });
  assert.equal(op.endRound, false);
  assert.equal(op.direct, null);

  const trace = navMenu(f, byId(f, 'trace.trace'), pages);
  assert.deepEqual(trace.optional.map((o) => [o.title, o.op]), [['生成脚本', 'restore']]);
  // 没有插件页声明面板节点时只显示状态，点不了
  assert.equal(navMenu(f, byId(f, 'core.operation'), []).optional[1].start, null);
  // 已停用的可选节点不列
  const off = { ...f, nodes: f.nodes.map((n) => (n.id === 'core.metric' ? { ...n, flags: { ...n.flags, disabled: true } } : n)) };
  assert.deepEqual(navMenu(off, byId(off, 'core.operation'), pages).optional.map((o) => o.id), ['codegen.solver']);
});

test('条件节点的下拉：自己的按钮加「这一轮不处理」；发布只有一个跳页面的按钮，点节点名直接去，不做下拉', () => {
  const f = flow();
  const update = navMenu(f, byId(f, 'core.update'), pages);
  assert.deepEqual(update.buttons.map((b) => b.label), ['增量更新']);
  assert.equal(update.endRound, true);
  assert.deepEqual(update.optional, []);

  const publish = navMenu(f, byId(f, 'core.publish'), pages);
  assert.deepEqual(publish.direct, { label: '发布', target: 'publish', kind: 'page', primary: true, reason: '' });
  assert.equal(publish.empty, false);
  // 什么都没有的节点不做下拉
  const bare = navMenu(f, { id: 'x.bare', title: '空', need: 'required', actions: [] }, pages);
  assert.equal(bare.empty, true);
});

test('可选节点点名字开始：有斜杠命令的发主命令（过期发刷新那条），脚本、人工节点调 FL-05', () => {
  assert.deepEqual(optionalStart({ id: 'p.script', title: '跑脚本', kind: 'script', actions: [] }), { kind: 'node', id: 'p.script' });
  const stale = {
    id: 'diagram.flowchart', title: '绘制流程图', state: 'stale',
    actions: [{ kind: 'refresh', label: '绘制流程图', target: '/ontology-flowchart 只查改动' }, { kind: 'start', label: '绘制流程图', target: '/ontology-flowchart' }],
  };
  assert.deepEqual(optionalStart(stale), { kind: 'command', label: '绘制流程图', target: '/ontology-flowchart 只查改动' });
});

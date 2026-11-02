// 流程视图：P1（B3a）与 P2 两种 /flow 形状归一、主线顺序、抽屉分组、节点动作、命令归属
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import {
  commandName, commandNode, mergeFlow, nodeActionKind, normalizeFlow, overlayRunning, sectionGroups, trackNodes,
} from '../src/state/flow.js';

test('P1 形状：主线按 after 连线排出 6 个必经步骤，分支与按需节点不进进度条', () => {
  const flow = normalizeFlow(F.flowP1());
  assert.deepEqual(trackNodes(flow).map((n) => n.title), ['资料分析', '对象提取', '操作提取', '质量检查', '示例数据', '场景验证']);
  assert.equal(flow.slices.length, 0);
  assert.deepEqual(flow.slice_stages, []);
});

test('已停用的包的节点不进进度条，只在抽屉分组里', () => {
  const raw = F.flowP1();
  raw.nodes.push({ id: 'naming.check', title: '命名检查', placement: 'step', section: 'required', state: 'skipped',
    display_state: 'disabled', flags: { needs_review: false, disabled: true, backfill: false } });
  raw.sections.find((x) => x.id === 'required').nodes.push('naming.check');
  const flow = normalizeFlow(raw);
  assert.ok(!trackNodes(flow).some((n) => n.id === 'naming.check'));
  assert.ok(sectionGroups(flow).find((g) => g.id === 'required').nodes.some((n) => n.id === 'naming.check'));
});

test('同一锚点后面挂了两个主线节点：按后端给的节点顺序排', () => {
  const raw = F.flowP1();
  const at = raw.nodes.findIndex((n) => n.id === 'core.validate') + 1;
  raw.nodes.splice(at, 0, { id: 'naming.check', title: '命名检查', placement: 'step', section: 'required', state: 'not_ready' });
  raw.edges = [...(raw.edges || []), { from: 'core.validate', to: 'naming.check' }];
  const titles = trackNodes(normalizeFlow(raw)).map((n) => n.title);
  assert.deepEqual(titles.slice(3, 6), ['质量检查', '命名检查', '示例数据']);
});

test('P1 形状：没有 section 的节点按顶层 sections 归组，空组不出', () => {
  const raw = F.flowP1();
  raw.nodes = raw.nodes.map(({ section, ...n }) => n);
  const flow = normalizeFlow(raw);
  const groups = sectionGroups(flow);
  assert.deepEqual(groups.map((g) => g.id), ['required', 'optional', 'on_demand']);
  assert.equal(groups[0].title, '必经步骤');
  assert.equal(flow.nodes.find((n) => n.id === 'core.update').section, 'on_demand');
});

test('缺 display_state 时按 state 补；待确认显示为 pending', () => {
  const flow = normalizeFlow({ nodes: [{ id: 'a', title: 'A', state: 'pending_confirm' }, { id: 'b', title: 'B' }] });
  assert.equal(flow.nodes[0].display_state, 'pending');
  assert.equal(flow.nodes[1].state, 'not_ready');
  assert.equal(flow.nodes[1].display_state, 'not_ready');
  assert.deepEqual(flow.edges, []);
  assert.deepEqual(flow.commands, {});
  assert.deepEqual(sectionGroups(flow).map((g) => g.id), ['required']);
});

test('P2 形状原样保留切片与节点分组', () => {
  const flow = normalizeFlow(F.flow({ trace: true }));
  assert.equal(flow.slices.length, 5);
  assert.equal(flow.slice_stages.at(-1).id, 'scenario');
  assert.equal(trackNodes(flow)[0].id, 'core.research');
  assert.ok(sectionGroups(flow).some((g) => g.id === 'pre_publish'));
});

test('节点动作：页面 id 跳页，命令文本发到对话，无 target 的跳过、恢复、开始调流程接口', () => {
  assert.equal(nodeActionKind({ kind: 'refresh', label: '刷新', target: 'scenarios' }), 'navigate');
  assert.equal(nodeActionKind({ kind: 'start', label: '提取对象', target: '/ontology-extract-object' }), 'command');
  assert.equal(nodeActionKind({ kind: 'start', label: '核验报告', target: '运行 python3 …核验资料分析报告' }), 'command');
  assert.equal(nodeActionKind({ kind: 'skip', label: '跳过' }), 'skip');
  assert.equal(nodeActionKind({ kind: 'restore', label: '恢复' }), 'restore');
  assert.equal(nodeActionKind({ kind: 'start', label: '开始' }), 'start');
  assert.equal(nodeActionKind(null), null);
});

test('命令归属：去斜杠与 ontology- 前缀，按 commands 找节点', () => {
  assert.equal(commandName('/ontology-trace 全部'), 'trace');
  assert.equal(commandName('/ontology-extract-rule-metric'), 'extract-rule-metric');
  assert.equal(commandName('/version'), 'version');
  assert.equal(commandName('帮我看看'), null);
  const flow = normalizeFlow(F.flowP1());
  assert.equal(commandNode(flow, '/ontology-codegen-scan'), 'core.codegen');
  assert.equal(commandNode(flow, '/ontology-overview'), null);
  assert.equal(commandNode(null, '/ontology-trace'), null);
});

test('运行中的命令先把所属节点显示为进行中；已在跑或找不到节点时不变', () => {
  const flow = normalizeFlow(F.flowP1());
  const v = overlayRunning(flow, 'core.validate');
  assert.equal(v.nodes.find((n) => n.id === 'core.validate').display_state, 'running');
  assert.equal(flow.nodes.find((n) => n.id === 'core.validate').display_state, 'ready');
  assert.equal(overlayRunning(v, 'core.validate'), v);
  assert.equal(overlayRunning(flow, 'nope'), flow);
  assert.equal(overlayRunning(flow, null), flow);
});

test('流程增量只带 state 时显示态跟着变', () => {
  const flow = normalizeFlow(F.flowP1());
  const m = mergeFlow(flow, { nodes: [{ id: 'core.validate', state: 'running' }] });
  assert.equal(m.nodes.find((n) => n.id === 'core.validate').display_state, 'running');
  assert.equal(mergeFlow(null, {}), null);
});

// 流程视图：P1（B3a）与 P2 两种 /flow 形状归一、主线顺序、节点分组、命令归属
import assert from 'node:assert/strict';
import test from 'node:test';
import * as F from '../src/api/mock/fixtures.js';
import {
  commandFromTarget, commandName, commandNode, flowNeedsReload, mergeFlow, multiSlice, normalizeFlow, suggestionCommand, overlayRunning, sliceName,
  trackClass, trackNodes, unitTitle,
} from '../src/state/flow.js';

test('P1 形状：主线按 after 连线排出 6 个必经步骤，分支与按需节点不进进度条', () => {
  const flow = normalizeFlow(F.flowP1());
  assert.deepEqual(trackNodes(flow).map((n) => n.title), ['资料分析', '对象提取', '操作提取', '质量检查', '示例数据', '场景验证']);
  assert.equal(flow.slices.length, 0);
  assert.deepEqual(flow.slice_stages, []);
});

test('已停用的包的节点不进进度条，节点和分组照样保留', () => {
  const raw = F.flowP1();
  raw.nodes.push({ id: 'naming.check', title: '命名检查', placement: 'step', section: 'required', state: 'skipped',
    display_state: 'disabled', flags: { needs_review: false, disabled: true, backfill: false } });
  raw.sections.find((x) => x.id === 'required').nodes.push('naming.check');
  const flow = normalizeFlow(raw);
  assert.ok(!trackNodes(flow).some((n) => n.id === 'naming.check'));
  assert.ok(flow.sections.find((s) => s.id === 'required').nodes.includes('naming.check'));
});

test('同一锚点后面挂了两个主线节点：按后端给的节点顺序排', () => {
  const raw = F.flowP1();
  const at = raw.nodes.findIndex((n) => n.id === 'core.validate') + 1;
  raw.nodes.splice(at, 0, { id: 'naming.check', title: '命名检查', placement: 'step', section: 'required', state: 'not_ready' });
  raw.edges = [...(raw.edges || []), { from: 'core.validate', to: 'naming.check' }];
  const titles = trackNodes(normalizeFlow(raw)).map((n) => n.title);
  assert.deepEqual(titles.slice(3, 6), ['质量检查', '命名检查', '示例数据']);
});

test('P1 形状：没有 section 的节点按顶层 sections 归组', () => {
  const raw = F.flowP1();
  raw.nodes = raw.nodes.map(({ section, ...n }) => n);
  const flow = normalizeFlow(raw);
  assert.equal(flow.nodes.find((n) => n.id === 'core.research').section, 'required');
  assert.equal(flow.nodes.find((n) => n.id === 'core.update').section, 'on_demand');
});

test('缺 display_state 时按 state 补；待确认显示为 pending', () => {
  const flow = normalizeFlow({ nodes: [{ id: 'a', title: 'A', state: 'pending_confirm' }, { id: 'b', title: 'B' }] });
  assert.equal(flow.nodes[0].display_state, 'pending');
  assert.equal(flow.nodes[1].state, 'not_ready');
  assert.equal(flow.nodes[1].display_state, 'not_ready');
  assert.deepEqual(flow.edges, []);
  assert.deepEqual(flow.commands, {});
  // 没有顶层 sections 时按节点的 section 补一份
  assert.deepEqual(flow.sections.find((s) => s.id === 'required').nodes, ['a', 'b']);
  assert.equal(flow.sections.find((s) => s.id === 'required').title, '必经步骤');
});

test('P2 形状原样保留分段与节点分组', () => {
  const flow = normalizeFlow(F.flow({ trace: true }));
  assert.deepEqual(flow.slices.map((s) => s.id), ['all']);
  assert.equal(flow.slice_stages.at(-1).id, 'scenario');
  assert.equal(trackNodes(flow)[0].id, 'core.research');
  assert.ok(flow.sections.some((s) => s.id === 'pre_publish'));
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

test('F8 起不分段：「全部」不显示名字、要有叫法时叫「建模」；老运行的分段照原名，两段以上才算分段', () => {
  assert.equal(sliceName('all', '全部'), '');
  assert.equal(sliceName('s2', '安全库存测算'), '安全库存测算');
  assert.equal(sliceName(undefined, undefined), '');
  assert.equal(unitTitle('all', '全部'), '建模');
  assert.equal(unitTitle('s2', '安全库存测算'), '安全库存测算');
  assert.equal(multiSlice(['all']), false);
  assert.equal(multiSlice(['s1']), false);
  assert.equal(multiSlice(['s1', 's2']), true);
  assert.equal(multiSlice(null), false);
  // 现在一个节点一个单元：不是段，不叫「分段」，不说「这一段」
  assert.equal(sliceName('core.object', '提取对象'), '');
  assert.equal(unitTitle('core.object', '提取对象'), '提取对象');
  assert.equal(multiSlice(['core.object', 'core.operation', 'core.validate']), false);
  assert.equal(multiSlice(['s1', 's2', 'core.validate']), true);
});

test('节点动作的命令文本拆成 S-20 的 command / args / text；不以斜杠开头的当普通消息', () => {
  assert.deepEqual(commandFromTarget('/ontology-research phase=blueprint'), { command: '/ontology-research', args: { phase: 'blueprint' }, text: '' });
  assert.deepEqual(commandFromTarget('/ontology-research 仅基于本地文档 '), { command: '/ontology-research', text: '仅基于本地文档' });
  assert.deepEqual(commandFromTarget('/ontology-extract-object'), { command: '/ontology-extract-object', text: '' });
  assert.deepEqual(commandFromTarget('运行 python3 .claude/scripts/research_checker.py 核验'), { text: '运行 python3 .claude/scripts/research_checker.py 核验' });
});

test('建议条 send 动作：text 为空串时只发 command + args；整段命令文本拆成 args；普通消息不走 S-20', () => {
  assert.deepEqual(suggestionCommand({ type: 'send', text: '', command: '/ontology-extract-rule-metric', args: { slice: 's2' } }), { command: '/ontology-extract-rule-metric', args: { slice: 's2' }, text: '' });
  assert.deepEqual(suggestionCommand({ type: 'send', text: '/ontology-research phase=blueprint', command: '/ontology-research' }), { command: '/ontology-research', args: { phase: 'blueprint' }, text: '' });
  assert.deepEqual(suggestionCommand({ type: 'send', text: '提取这一段的规则', command: '/ontology-extract-rule-metric' }), { command: '/ontology-extract-rule-metric', text: '提取这一段的规则' });
  assert.equal(suggestionCommand({ type: 'send', text: '继续刚才的任务' }), null);
});

test('flow.updated 带来新的 flow_hash（节点集合变了，如启用图示后多出流程图）时要重取整份流程；增量合不进新节点', () => {
  const flow = normalizeFlow({ flow_hash: 'h1', nodes: [{ id: 'core.research', state: 'done' }, { id: 'core.model', state: 'ready' }] });
  assert.equal(flowNeedsReload(flow, { flow_hash: 'h1', nodes: [{ id: 'core.model', state: 'running' }] }), false);
  assert.equal(flowNeedsReload(flow, { nodes: [{ id: 'core.model', state: 'running' }] }), false);
  const added = { flow_hash: 'h2', nodes: [{ id: 'diagram.flowchart', state: 'ready' }] };
  assert.equal(flowNeedsReload(flow, added), true);
  assert.equal(flowNeedsReload(null, added), false, '流程还没取到时不另外重取');
  const merged = mergeFlow(flow, added);
  assert.deepEqual(merged.nodes.map((n) => n.id), ['core.research', 'core.model']);
  assert.equal(merged.flow_hash, 'h2');
  assert.equal(flowNeedsReload(merged, added), false, '同一批事件只重取一次');
});

test('顶栏轨迹：需复核、自主完成、通过都算完成，可补做淡显（导入知识包后建模已有、资料分析越过）', () => {
  assert.equal(trackClass('confirmed'), 'ok');
  assert.equal(trackClass('needs_review'), 'ok');
  assert.equal(trackClass('auto'), 'ok');
  assert.equal(trackClass('backfill'), 'skip');
  assert.equal(trackClass('stale'), 'stale'); // 过期：做过、要重跑，和没做过的空圈分开
  assert.equal(trackClass('ready'), '');
  assert.equal(trackClass(undefined), '');
});

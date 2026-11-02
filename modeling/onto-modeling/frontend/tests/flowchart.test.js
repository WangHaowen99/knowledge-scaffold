import assert from 'node:assert/strict';
import test from 'node:test';
import { bindFileTransport, request } from '../src/api/files.js';
import { isCompressedDrawio, modelToDrawio } from '@packs/diagram/ui/flowchart/drawioConvert.js';
import { createFlowchartEditorSession, reduceFlowchartEditorSession } from '@packs/diagram/ui/flowchart/flowchartEditorState.js';
import {
  checkFlowchart, checkSummary, edgeGeometry, edgeTypeFor, fitViewport, nodeSize, normalizeModel, summarize, textToList,
} from '@packs/diagram/ui/flowchart/flowchartModel.js';
import { autoLayout } from '@packs/diagram/ui/flowchart/layout.js';
import { initialSelection, lockBarText } from '@packs/diagram/ui/flowchart/flowchartDialog.js';
import { latestFlowchartArtifact } from '../src/app/packUi.js';

const n = (id, type, label, x, y, extra = {}) => ({ id, type, label, x, y, ...extra });
const e = (id, type, from, to, label = '') => ({ id, type, from, to, label, condition: '' });

// 设计稿 FC_DEV 的版式（合成场景的文字）
function overage() {
  return normalizeModel({
    process: { name: '超量处理流程' },
    nodes: [
      n('n1', 'start', '受理申请', 150, 60), n('n2', 'step', '读取申请量', 150, 170), n('n3', 'data', '补货单（草稿）', 400, 170),
      n('n4', 'step', '取标准上限', 150, 300, { pending: true }), n('n5', 'data', '陈列量标准', 400, 300), n('n6', 'decision', '严于标准？', 150, 450),
      n('n7', 'step', '按门店执行', 400, 450), n('n8', 'step', '生成审批提示', 150, 590), n('n9', 'step', '挂到补货台账', 400, 590), n('n10', 'end', '输出超量清单', 650, 520),
    ],
    edges: [
      e('e1', 'seq', 'n1', 'n2'), e('e2', 'data', 'n3', 'n2', '读取'), e('e3', 'seq', 'n2', 'n4'), e('e4', 'data', 'n5', 'n4', '读取'),
      e('e5', 'seq', 'n4', 'n6'), e('e6', 'cond', 'n6', 'n7', '是'), e('e7', 'cond', 'n6', 'n8', '否'), e('e8', 'seq', 'n8', 'n9'),
      e('e9', 'seq', 'n7', 'n10'), e('e10', 'seq', 'n9', 'n10'),
    ],
  });
}

test('规模：数据对象不算节点', () => {
  assert.deepEqual(summarize(overage()), { nodes: 8, decisions: 1, text: '8 个节点、1 个判断' });
  assert.equal(summarize(null).text, '0 个节点、0 个判断');
});

test('节点尺寸按类型取设计稿的值，文字长了才加宽', () => {
  assert.deepEqual(nodeSize({ type: 'step', label: '读取申请量' }), { w: 132, h: 42 });
  assert.deepEqual(nodeSize({ type: 'decision', label: '严于标准？' }), { w: 144, h: 72 });
  assert.deepEqual(nodeSize({ type: 'end', label: '输出超量清单' }), { w: 104, h: 38 });
  assert.deepEqual(nodeSize({ type: 'data', label: '补货单（草稿）' }), { w: 116, h: 36 });
  assert.ok(nodeSize({ type: 'step', label: '按商品品类逐项取标准上限再和申请量比较' }).w > 132);
});

test('连线端点落在边的中点，同一边进来两条线时上下错开 8', () => {
  const m = overage();
  const g = edgeGeometry(m.nodes, m.edges);
  const pt = (id) => { const x = g.get(id); return [x.x1, x.y1, x.x2, x.y2]; };
  // 与设计稿 FC_DEV 的 E 表逐条一致
  assert.deepEqual(pt('e1'), [150, 79, 150, 149]);
  assert.deepEqual(pt('e2'), [342, 170, 216, 170]);
  assert.deepEqual(pt('e5'), [150, 321, 150, 414]);
  assert.deepEqual(pt('e6'), [222, 450, 334, 450]);
  assert.deepEqual(pt('e7'), [150, 486, 150, 569]);
  assert.deepEqual(pt('e9'), [466, 450, 598, 512]);
  assert.deepEqual(pt('e10'), [466, 590, 598, 528]);
  assert.equal(g.get('e6').mx, 278);
});

test('适配视野与设计稿取景一致；图很小时给图例让位', () => {
  const m = overage();
  const vp = fitViewport(m.nodes, 870, 621.4);
  assert.ok(Math.abs(vp.zoom - 621.4 / 740) < 1e-6);
  assert.ok(Math.abs(vp.y - 66 * vp.zoom) < 0.01);
  assert.ok(Math.abs(vp.x - 99) < 1.5);
  // 画布很矮：流程单位的留白不够图例，改按屏幕像素，上方至少留 78
  const small = fitViewport(m.nodes, 870, 420);
  const top = m.nodes.reduce((a, x) => Math.min(a, x.y - nodeSize(x).h / 2), Infinity);
  assert.ok(small.y + top * small.zoom >= 78);
});

test('规范化：补齐缺省字段，丢掉指向不存在节点的连线', () => {
  const m = normalizeModel({ nodes: [{ id: 'a', type: 'weird', label: 'x' }, { type: 'end' }], edges: [{ from: 'a', to: 'zz' }, { from: 'a', to: 'n1' }] });
  assert.equal(m.storage, 'standard');
  assert.deepEqual(m.process, { name: '', description: '', roles: [], inputs: [], exceptions: [], handoffs: [] });
  assert.equal(m.nodes[0].type, 'step');
  assert.equal(m.nodes[1].id, 'n1');
  assert.equal(m.edges.length, 1);
  assert.equal(m.edges[0].type, 'seq');
  assert.deepEqual(textToList('补货专员\n\n 区域督导 \n'), ['补货专员', '区域督导']);
  assert.equal(edgeTypeFor({ type: 'decision' }, { type: 'step' }), 'cond');
  assert.equal(edgeTypeFor({ type: 'data' }, { type: 'step' }), 'data');
  assert.equal(edgeTypeFor({ type: 'step' }, null), 'seq');
});

test('确定性检查：待定口径是提醒，孤立节点与少出口的判断是错误', () => {
  const ok = checkFlowchart(overage());
  assert.equal(ok.ok, true);
  assert.equal(ok.errors.length, 0);
  assert.deepEqual(ok.warnings.map((w) => [w.code, w.field]), [['W-FC-PENDING', 'nodes/n4']]);
  assert.equal(checkSummary(ok), '流程图校验通过：0 错误，1 提醒（「取标准上限」口径待定）');

  const m = overage();
  m.nodes.push(n('n11', 'step', '新建步骤', 150, 700));
  m.edges = m.edges.filter((x) => x.id !== 'e7');
  const bad = checkFlowchart(m);
  assert.equal(bad.ok, false);
  assert.deepEqual(bad.errors.map((x) => x.code).sort(), ['E-FC-BRANCH', 'E-FC-ISOLATED']);
  assert.match(checkSummary(bad), /^流程图校验没通过：2 错误/);
});

test('编辑会话：改动、撤销、重做与脏标记', () => {
  let s = createFlowchartEditorSession(overage(), { savedRevision: 'r1', selection: { type: 'node', id: 'n4' } });
  assert.equal(s.dirty, false);
  assert.deepEqual(s.selection, { type: 'node', id: 'n4' });
  s = reduceFlowchartEditorSession(s, { type: 'add-node', node: { type: 'decision', label: '新建判断' }, position: { x: 150, y: 710 } });
  assert.equal(s.model.nodes.length, 11);
  assert.equal(s.dirty, true);
  assert.deepEqual(s.selection, { type: 'node', id: 'n11' });
  s = reduceFlowchartEditorSession(s, { type: 'undo' });
  assert.equal(s.dirty, false);
  assert.equal(s.selection, null);
  s = reduceFlowchartEditorSession(s, { type: 'redo' });
  assert.equal(s.model.nodes.length, 11);
  // 删节点连带删它的连线
  s = reduceFlowchartEditorSession(s, { type: 'delete-node', id: 'n6' });
  assert.equal(s.model.edges.some((x) => x.from === 'n6' || x.to === 'n6'), false);
});

test('编辑会话：同一字段连续输入只记一步撤销；保存中又改了仍算未保存', () => {
  let s = createFlowchartEditorSession(overage(), { savedRevision: 'r1' });
  ['取', '取标', '取标准'].forEach((label) => { s = reduceFlowchartEditorSession(s, { type: 'update-node', id: 'n4', patch: { label }, coalesce: 'label' }); });
  assert.equal(s.past.length, 1);
  s = reduceFlowchartEditorSession(s, { type: 'update-process', patch: { roles: ['补货专员'] }, coalesce: 'roles' });
  assert.equal(s.past.length, 2);
  s = reduceFlowchartEditorSession(s, { type: 'mark-saving' });
  assert.equal(s.saveStatus, 'saving');
  s = reduceFlowchartEditorSession(s, { type: 'move-node', id: 'n7', position: { x: 410, y: 460 } });
  s = reduceFlowchartEditorSession(s, { type: 'mark-saved', savedRevision: 'r2' });
  assert.equal(s.savedRevision, 'r2');
  assert.equal(s.dirty, true);
  assert.equal(s.saveStatus, 'dirty');
  // 有未保存修改时不被远端版本覆盖，除非明确要求
  const kept = reduceFlowchartEditorSession(s, { type: 'replace-model', model: overage(), savedRevision: 'r3' });
  assert.equal(kept, s);
  const forced = reduceFlowchartEditorSession(s, { type: 'replace-model', model: overage(), savedRevision: 'r3', force: true });
  assert.equal(forced.dirty, false);
  assert.equal(forced.savedRevision, 'r3');
});

test('编辑会话：连线不重复、不自连；拖到空白处新建相连节点', () => {
  let s = createFlowchartEditorSession(overage());
  const same = reduceFlowchartEditorSession(s, { type: 'connect', from: 'n1', to: 'n2' });
  assert.equal(same, s);
  assert.equal(reduceFlowchartEditorSession(s, { type: 'connect', from: 'n1', to: 'n1' }), s);
  s = reduceFlowchartEditorSession(s, { type: 'spawn-connected', from: 'n7', position: { x: 620.4, y: 380.6 } });
  const added = s.model.nodes.at(-1);
  assert.deepEqual([added.x, added.y, added.type], [620, 381, 'step']);
  assert.deepEqual(s.model.edges.at(-1), { id: 'e11', type: 'seq', from: 'n7', to: added.id, label: '', condition: '' });
});

test('drawio：模型转明文 XML，待定节点虚线；能认出压缩存储', () => {
  const xml = modelToDrawio(overage());
  assert.match(xml, /<diagram name="超量处理流程">/);
  assert.match(xml, /id="n4" value="取标准上限" style="[^"]*dashed=1;"/);
  assert.match(xml, /<mxGeometry x="84" y="279" width="132" height="42"/);
  assert.match(xml, /source="n6" target="n7"/);
  assert.equal(isCompressedDrawio(xml), false);
  assert.equal(isCompressedDrawio('<mxfile><diagram name="a">7VnbbuIwEP0a</diagram></mxfile>'), true);
  assert.match(modelToDrawio(normalizeModel({ nodes: [n('a', 'step', 'A<&>"', 0, 0)] })), /value="A&lt;&amp;&gt;&quot;"/);
});

test('自动布局：主流程自上而下，数据对象挂在它连着的步骤右边', () => {
  const pos = autoLayout(overage());
  assert.ok(pos.n2.y > pos.n1.y && pos.n4.y > pos.n2.y && pos.n6.y > pos.n4.y);
  assert.equal(pos.n3.y, pos.n2.y);
  assert.ok(pos.n3.x > pos.n2.x + 66);
  assert.equal(pos.n5.y, pos.n4.y);
});

test('弹窗：打开时选中待定节点；锁定条两态的说法', () => {
  assert.deepEqual(initialSelection(overage()), { type: 'node', id: 'n4' });
  assert.equal(initialSelection({ nodes: [n('a', 'step', 'A', 0, 0)] }), null);
  const lock = (state) => ({ state, slice: { id: 's5', name: '超量与审批提示' }, entries: [], artifacts: [], paths: [] });
  assert.deepEqual(lockBarText(lock('assistant_running')), {
    title: '助手正在改「超量与审批提示」这一片，这张图在写入范围里，确认生效或撤回后才能编辑',
    desc: '片外的条目照常编辑；也可以回到对话里打断它',
  });
  assert.deepEqual(lockBarText(lock('pending_confirm')), {
    title: '「超量与审批提示」这一片的修改还没确认，这张图在写入范围里，确认生效或撤回后才能编辑',
    desc: '助手已经停下来等你确认；片外的条目照常编辑',
  });
  assert.equal(lockBarText({ state: 'none' }), null);
  assert.equal(lockBarText({ ...lock('pending_confirm'), slice: null }, { text: '兜底说法' }).title, '兜底说法');
});

test('flowchart 页不带 key：取对话里最近一张流程图卡', () => {
  const card = (id, key) => ({ kind: 'card', card: { card_id: id, card_type: 'flowchart', artifact: { type: 'diagram.flowchart', key, title: key } } });
  const state = {
    order: ['a', 'b', 'c', 'd'],
    items: { a: card('a', 'replenish-main-flow'), b: { kind: 'assistant' }, c: card('c', 'overage-flow'), d: { kind: 'card', card: { card_type: 'step_output' } } },
  };
  assert.equal(latestFlowchartArtifact(state).key, 'overage-flow');
  assert.equal(latestFlowchartArtifact({ order: [], items: {} }), null);
});

test('文件接口：绑定后 v1 路径走应用客户端，其余路径走应用传输层', async () => {
  const calls = [];
  bindFileTransport({
    client: { request: async (method, path, opts) => { calls.push(['client', method, path, opts.body]); return { ok: 1 }; } },
    fetch: async (url, init) => { calls.push(['fetch', init.method, url]); return new Response('{"legacy":1}', { status: 200 }); },
    tokens: { access: 't1' },
  });
  try {
    assert.deepEqual(await request('/api/v1/projects/p1/files/content', { method: 'PUT', body: JSON.stringify({ path: 'a', content: 'x' }) }), { ok: 1 });
    assert.deepEqual(await request('/api/projects/p1/studio/schema'), { legacy: 1 });
    assert.deepEqual(calls, [
      ['client', 'PUT', '/projects/p1/files/content', { path: 'a', content: 'x' }],
      ['fetch', 'GET', '/api/projects/p1/studio/schema'],
    ]);
  } finally {
    bindFileTransport(null);
  }
});

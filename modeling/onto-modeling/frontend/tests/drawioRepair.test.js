// develop tests/drawioRepair.test.js（!613）移植到图示包：节点类型由样式和 onto_type 定，没有「类型无效」这一类问题；
// 带链接的外层对象本分支认得，照常打开；预览是 FlowchartSvg 的模型。
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DOMParser, XMLSerializer } from '@xmldom/xmldom';
import {
  applyDrawioRepair, canRepairDrawio, drawioSourceCheck, inspectDrawioRepair, keepRepairDrafts, repairIssueKey, repairIssueLabel,
  repairPreviewGraph, repairSelection, validateRepairedDrawio,
} from '@packs/diagram/ui/flowchart/drawioRepair.js';

globalThis.DOMParser = DOMParser;
globalThis.XMLSerializer = XMLSerializer;

function diagram(cells) {
  return `<mxfile><diagram name="合成流程"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>${cells}</root></mxGraphModel></diagram></mxfile>`;
}

function node(id, label, x = 30, parent = '1') {
  return `<mxCell id="${id}" value="${label}" vertex="1" parent="${parent}"><mxGeometry x="${x}" y="30" width="100" height="50" as="geometry"/></mxCell>`;
}

function edge(id, source, target, value = '') {
  return `<mxCell id="${id}" value="${value}" edge="1" parent="1" source="${source}" target="${target}"><mxGeometry relative="1" as="geometry"/></mxCell>`;
}

const cellsOf = (xml) => Array.from(new DOMParser().parseFromString(xml, 'text/xml').getElementsByTagName('mxCell'));
const xOf = (cell) => Number(cell.getElementsByTagName('mxGeometry')[0].getAttribute('x'));

test('重复 ID 的重叠节点可分别识别，修复后错开显示', () => {
  const xml = diagram(node('n1', '节点甲') + node('n1', '节点乙'));
  const graph = repairPreviewGraph(xml);
  assert.equal(graph.nodes.length, 2);
  assert.notEqual(graph.nodes[0].id, graph.nodes[1].id);
  assert.ok(graph.nodes[1].x - graph.nodes[0].x >= 250);
  const issues = inspectDrawioRepair(xml);
  assert.equal(issues.length, 1);
  const [issue] = issues;
  assert.equal(issue.type, 'duplicate');
  assert.equal(issue.overlap, true);
  assert.match(issue.title, /节点乙/);
  assert.equal(repairIssueLabel(issue), '节点「节点甲」与「节点乙」共用编号（当前修复后者）');
  assert.throws(() => applyDrawioRepair(xml, issue, { mode: 'rename', id: 'n1' }), /未使用的节点 ID/);
  const repaired = applyDrawioRepair(xml, issue, { mode: 'rename', id: 'n2' });
  assert.deepEqual(inspectDrawioRepair(repaired), []);
  const nodes = cellsOf(repaired).filter((cell) => cell.getAttribute('vertex') === '1');
  assert.deepEqual(nodes.map((cell) => cell.getAttribute('id')), ['n1', 'n2']);
  assert.ok(xOf(nodes[1]) - xOf(nodes[0]) >= 250);
  assert.equal(validateRepairedDrawio(repaired).nodes.length, 2);
});

test('三个节点完全重叠时各自错开，不互相压住', () => {
  let xml = diagram(node('n1', '节点甲') + node('n1', '节点乙') + node('n1', '节点丙'));
  const spots = (graph) => new Set(graph.nodes.map((item) => item.x)).size;
  assert.equal(spots(repairPreviewGraph(xml)), 3);
  xml = applyDrawioRepair(xml, inspectDrawioRepair(xml)[0], { mode: 'rename', id: 'n2' });
  assert.equal(spots(repairPreviewGraph(xml)), 3);
  const [left] = inspectDrawioRepair(xml);
  assert.equal(left.suggestion, 'n1-2');
  xml = applyDrawioRepair(xml, left, { mode: 'rename', id: left.suggestion });
  assert.equal(spots(validateRepairedDrawio(xml)), 3);
});

test('悬空边可补建节点或改接现有节点', () => {
  const xml = diagram(node('n1', '节点甲') + node('n2', '节点乙', 220) + edge('e1', 'n1', 'missing'));
  const [issue] = inspectDrawioRepair(xml);
  assert.equal(issue.type, 'dangling');
  assert.equal(issue.endpoint, 'target');
  assert.deepEqual(issue.options.map((option) => option.value), ['n1', 'n2']);
  const graph = repairPreviewGraph(xml);
  assert.equal(graph.nodes.length, 3);
  assert.equal(graph.edges.length, 1);
  assert.ok(graph.nodes.some((item) => item.missing && item.label === '缺失节点：missing'));
  assert.throws(() => applyDrawioRepair(xml, issue), /请选择补建节点或连接到现有节点/);
  const linked = applyDrawioRepair(xml, issue, { mode: 'existing', nodeId: 'n2' });
  assert.deepEqual(inspectDrawioRepair(linked), []);
  assert.equal(validateRepairedDrawio(linked).edges[0].to, 'n2');
  assert.throws(() => applyDrawioRepair(linked, issue, { mode: 'existing', nodeId: 'n2' }), /待修复单元格已变化/);
  assert.throws(() => applyDrawioRepair(xml, issue, { mode: 'create', label: ' ' }), /请输入新节点名称/);
  const created = validateRepairedDrawio(applyDrawioRepair(xml, issue, { mode: 'create', label: '节点 <丙>' }));
  assert.deepEqual(created.nodes.map((item) => [item.id, item.label, item.type]).at(-1), ['missing', '节点 <丙>', 'step']);
});

test('只有一个现有节点时不默认生成自环，可补建缺失节点', () => {
  const xml = diagram(node('n1', '起点') + edge('e1', 'n1', 'missing-node'));
  const [issue] = inspectDrawioRepair(xml);
  assert.equal(issue.options.length, 1);
  assert.match(issue.options[0].label, /会形成自环/);
  assert.throws(() => applyDrawioRepair(xml, issue, { mode: 'existing' }), /请选择现有节点/);
  const repaired = applyDrawioRepair(xml, issue, { mode: 'create', label: '终点' });
  const parsed = validateRepairedDrawio(repaired);
  assert.equal(parsed.nodes.length, 2);
  assert.equal(parsed.edges[0].from, 'n1');
  assert.equal(parsed.edges[0].to, 'missing-node');
  assert.equal(parsed.nodes[1].label, '终点');
  assert.equal(repairIssueLabel(issue), '从「起点」出发的连线，终点未连接');
});

test('外层对象照常打开；面板无法修复的 ID 异常回退高级编辑器', () => {
  const wrapped = diagram('<UserObject id="n1" label="合成节点" link="https://example.com">'
    + '<mxCell vertex="1" parent="1"><mxGeometry x="30" y="30" width="100" height="50" as="geometry"/></mxCell></UserObject>');
  assert.equal(inspectDrawioRepair(wrapped).length, 0);
  assert.equal(canRepairDrawio(wrapped), false);
  assert.equal(drawioSourceCheck(wrapped), null);

  const base = node('n1', '节点甲') + node('n2', '节点乙', 220);
  const link = (id) => edge(id, 'n1', 'n2');
  const mixed = diagram(base + node('n1', '重复节点', 400) + link('e1') + link('e1'));
  assert.equal(inspectDrawioRepair(mixed).length, 1);
  assert.equal(canRepairDrawio(mixed), false);
  assert.equal(canRepairDrawio(diagram(base + link('n1'))), false);
  assert.equal(canRepairDrawio(diagram(base + link('e1')
    + '<mxCell value="文字" style="text;html=1;" vertex="1" parent="1" '
    + 'data-flowchart-annotation-edge="e1"><mxGeometry x="30" y="30" width="40" height="20" as="geometry"/></mxCell>')), false);
  assert.equal(canRepairDrawio(diagram(base + node('n1', '重复节点', 400) + link('e1'))), true);
});

test('打开前的检查：能修、修不了、没问题', () => {
  const base = node('n1', '节点甲') + node('n2', '节点乙', 220);
  const duplicate = drawioSourceCheck(diagram(base + node('n1', '节点丙', 400)));
  assert.deepEqual(duplicate, { kind: 'repair', message: '节点 ID 重复：n1' });
  assert.deepEqual(drawioSourceCheck(diagram(base + edge('e1', 'n1', 'n2') + edge('e1', 'n2', 'n1'))),
    { kind: 'blocked', message: '单元格 ID 重复：e1' });
  assert.equal(drawioSourceCheck(diagram(base + edge('e1', 'n1', 'n2'))), null);
  assert.equal(drawioSourceCheck('<mxfile><diagram name="压缩">7VhNc5swEP01</diagram></mxfile>'), null);
  assert.equal(drawioSourceCheck(''), null);
});

test('包在用户对象里的单元格改在外层编号、里层端点上', () => {
  const wrap = (id, label, inner) => `<object id="${id}" label="${label}" onto_type="step">${inner}</object>`;
  const box = (x) => `<mxCell vertex="1" parent="1"><mxGeometry x="${x}" y="30" width="100" height="50" as="geometry"/></mxCell>`;
  let xml = diagram(wrap('n1', '节点甲', box(30)) + wrap('n1', '节点乙', box(220))
    + '<object id="e1" label="" onto_type="seq"><mxCell edge="1" parent="1" source="n1" target="missing"><mxGeometry relative="1" as="geometry"/></mxCell></object>');
  assert.equal(drawioSourceCheck(xml).kind, 'repair');
  xml = applyDrawioRepair(xml, inspectDrawioRepair(xml)[0], { mode: 'rename', id: 'n2' });
  xml = applyDrawioRepair(xml, inspectDrawioRepair(xml)[0], { mode: 'existing', nodeId: 'n2' });
  const parsed = validateRepairedDrawio(xml);
  assert.deepEqual(parsed.nodes.map((item) => item.id), ['n1', 'n2']);
  assert.deepEqual(parsed.edges.map((item) => [item.from, item.to]), [['n1', 'n2']]);
});

test('连在泳道上的连线、泳道里的节点不算问题；连线上的文字不当节点', () => {
  const lane = '<mxCell id="lane" value="合成泳道" style="swimlane;html=1;" vertex="1" parent="1"><mxGeometry x="200" y="100" width="300" height="200" as="geometry"/></mxCell>';
  const label = '<mxCell id="t1" value="通过" style="edgeLabel;html=1;" vertex="1" connectable="0" parent="e1"><mxGeometry relative="1" as="geometry"/></mxCell>';
  const xml = diagram(lane + node('n1', '节点甲') + node('n2', '节点乙', 40, 'lane') + edge('e1', 'n1', 'n2') + label + edge('e2', 'n1', 'lane'));
  assert.equal(drawioSourceCheck(xml), null);
  const graph = repairPreviewGraph(xml);
  assert.deepEqual(graph.nodes.map((item) => [item.id, item.x, item.y]), [['n1', 80, 55], ['n2', 290, 155]]);
  assert.equal(graph.edges.length, 1);
  const broken = xml.replace('source="n1" target="n2"', 'source="n1" target=""');
  const [issue] = inspectDrawioRepair(broken);
  assert.equal(repairIssueLabel(issue), '连线「通过」终点未连接');
  const removed = applyDrawioRepair(broken, issue, { mode: 'delete' });
  assert.equal(cellsOf(removed).some((cell) => ['e1', 't1'].includes(cell.getAttribute('id'))), false);
});

test('可删除悬空连线及其文字标注，或经确认删除完全重叠的重复节点', () => {
  const loose = diagram(node('n1', '节点甲') + edge('e1', 'n1', 'missing')
    + '<mxCell id="note" value="说明" style="text;html=1;" vertex="1" parent="1" '
    + 'data-flowchart-annotation-edge="e1"><mxGeometry x="50" y="50" width="40" height="20" as="geometry"/></mxCell>');
  assert.equal(canRepairDrawio(loose), true);
  const removedEdge = applyDrawioRepair(loose, inspectDrawioRepair(loose)[0], { mode: 'delete' });
  assert.deepEqual(inspectDrawioRepair(removedEdge), []);
  assert.equal(cellsOf(removedEdge).some((cell) => cell.getAttribute('id') === 'e1' || cell.getAttribute('id') === 'note'), false);
  assert.equal(validateRepairedDrawio(removedEdge).edges.length, 0);

  const duplicate = diagram(node('n1', '节点甲') + node('n1', '节点乙'));
  const removedNode = applyDrawioRepair(duplicate, inspectDrawioRepair(duplicate)[0], { mode: 'delete' });
  assert.equal(validateRepairedDrawio(removedNode).nodes.length, 1);
  const separate = diagram(node('n1', '节点甲') + node('n1', '节点乙', 220));
  const separateIssue = inspectDrawioRepair(separate)[0];
  assert.equal(validateRepairedDrawio(applyDrawioRepair(separate, separateIssue, { mode: 'delete' })).nodes.length, 1);
  assert.deepEqual(validateRepairedDrawio(applyDrawioRepair(separate, separateIssue,
    { mode: 'rename', id: 'n2' })).nodes.map((item) => item.id), ['n1', 'n2']);
});

test('还有问题没修完时不能保存', () => {
  const xml = diagram(node('n1', '节点甲') + edge('e1', 'n1', 'missing') + edge('e2', '', 'n1'));
  assert.equal(inspectDrawioRepair(xml).length, 2);
  assert.throws(() => validateRepairedDrawio(xml), /仍有 2 项结构问题需要修复/);
  assert.equal(repairPreviewGraph(xml).nodes.filter((item) => item.missing).length, 2);
});

test('无连线文字时用已连接节点描述问题，不把节点名当作连线名', () => {
  const xml = diagram(node('start', '合成开始') + edge('e1', 'start', 'missing'));
  const [issue] = inspectDrawioRepair(xml);
  assert.equal(repairIssueLabel(issue), '从「合成开始」出发的连线，终点未连接');
});

test('选择问题可定位预览中的真实单元格，删除后保留其他项的输入', () => {
  const xml = diagram(node('n1', '节点甲') + node('n1', '节点乙') + node('n2', '节点丙', 220) + edge('e1', 'n1', 'missing'));
  const issues = inspectDrawioRepair(xml);
  const duplicate = issues.find((issue) => issue.type === 'duplicate');
  const dangling = issues.find((issue) => issue.type === 'dangling');
  const graph = repairPreviewGraph(xml);
  const picked = repairSelection(graph, duplicate);
  assert.equal(picked.type, 'node');
  assert.equal(graph.nodes.find((item) => item.id === picked.id).label, '节点乙');
  assert.deepEqual(repairSelection(graph, dangling), { type: 'edge', id: graph.edges[0].id });
  assert.equal(repairSelection(graph, undefined), null);
  const drafts = { [repairIssueKey(dangling)]: { mode: 'create', label: '合成终点' } };
  const repaired = applyDrawioRepair(xml, duplicate, { mode: 'delete' });
  const nextIssues = inspectDrawioRepair(repaired);
  const kept = keepRepairDrafts(xml, issues, nextIssues, drafts, duplicate, { mode: 'delete' });
  assert.deepEqual(kept[repairIssueKey(nextIssues.find((issue) => issue.type === 'dangling'))],
    { mode: 'create', label: '合成终点' });
});

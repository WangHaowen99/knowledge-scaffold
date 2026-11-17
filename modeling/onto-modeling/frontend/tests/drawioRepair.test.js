import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DOMParser, XMLSerializer } from '@xmldom/xmldom';
import { applyDrawioRepair, canRepairDrawio, inspectDrawioRepair, keepRepairDrafts, repairIssueKey, repairIssueLabel, repairPreviewGraph, repairPreviewXml, validateRepairedDrawio } from '../src/components/flowchart/drawioRepair.js';

globalThis.DOMParser = DOMParser;
globalThis.XMLSerializer = XMLSerializer;

function diagram(cells) {
  return `<mxfile><diagram name="合成流程"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>${cells}</root></mxGraphModel></diagram></mxfile>`;
}

function node(id, label, x = 30, kind = 'action') {
  return `<mxCell id="${id}" value="${label}" vertex="1" parent="1" data-flowchart-node-kind="${kind}"><mxGeometry x="${x}" y="30" width="100" height="50" as="geometry"/></mxCell>`;
}

test('重复 ID 的重叠节点可分别识别，修复后错开显示', () => {
  const xml = diagram(node('n1', '节点甲') + node('n1', '节点乙'));
  const preview = new DOMParser().parseFromString(repairPreviewXml(xml), 'text/xml');
  const previewNodes = Array.from(preview.getElementsByTagName('mxCell'))
    .filter((cell) => cell.getAttribute('vertex') === '1');
  assert.equal(previewNodes.length, 2);
  assert.notEqual(previewNodes[0].getAttribute('id'), previewNodes[1].getAttribute('id'));
  assert.notEqual(previewNodes[0].getElementsByTagName('mxGeometry')[0].getAttribute('x'),
    previewNodes[1].getElementsByTagName('mxGeometry')[0].getAttribute('x'));
  assert.equal(inspectDrawioRepair(xml).length, 1);
  const graph = repairPreviewGraph(xml)[0];
  assert.equal(graph.nodes.length, 2);
  assert.ok(graph.nodes.every((item) => item.type === 'flow' && item.data.disabled));
  assert.ok(graph.nodes[1].position.x - graph.nodes[0].position.x >= 250);
  const [issue] = inspectDrawioRepair(xml);
  assert.equal(issue.type, 'duplicate');
  assert.equal(issue.overlap, true);
  assert.match(issue.title, /节点乙/);
  assert.equal(repairIssueLabel(issue), '节点「节点甲」与「节点乙」共用编号（当前修复后者）');
  const repaired = applyDrawioRepair(xml, issue, 'n2');
  assert.deepEqual(inspectDrawioRepair(repaired), []);
  const nodes = Array.from(new DOMParser().parseFromString(repaired, 'text/xml').getElementsByTagName('mxCell'))
    .filter((cell) => cell.getAttribute('vertex') === '1');
  assert.deepEqual(nodes.map((cell) => cell.getAttribute('id')), ['n1', 'n2']);
  assert.ok(Number(nodes[1].getElementsByTagName('mxGeometry')[0].getAttribute('x'))
    - Number(nodes[0].getElementsByTagName('mxGeometry')[0].getAttribute('x')) >= 250);
  assert.equal(validateRepairedDrawio(repaired).diagrams[0].nodes.length, 2);
});

test('悬空边和非法节点类型可逐项修复', () => {
  let xml = diagram(node('n1', '节点甲', 30, 'invalid-test-kind')
    + node('n2', '节点乙', 220)
    + '<mxCell id="e1" edge="1" parent="1" source="n1" target="missing"><mxGeometry relative="1" as="geometry"/></mxCell>');
  const kind = inspectDrawioRepair(xml).find((issue) => issue.type === 'kind');
  xml = applyDrawioRepair(xml, kind, 'decision');
  const edge = inspectDrawioRepair(xml).find((issue) => issue.type === 'dangling');
  const preview = new DOMParser().parseFromString(repairPreviewXml(xml), 'text/xml');
  assert.equal(Array.from(preview.getElementsByTagName('mxCell'))
    .filter((cell) => cell.getAttribute('vertex') === '1').length, 3);
  assert.match(new XMLSerializer().serializeToString(preview), /缺失节点：missing/);
  const graph = repairPreviewGraph(xml)[0];
  assert.equal(graph.nodes.length, 3);
  assert.equal(graph.edges.length, 1);
  assert.ok(graph.nodes.some((item) => item.className === 'flowchart-repair-missing-node'));
  assert.throws(() => applyDrawioRepair(xml, edge), /请选择补建节点或连接到现有节点/);
  xml = applyDrawioRepair(xml, edge, { mode: 'existing', nodeId: 'n2' });
  assert.deepEqual(inspectDrawioRepair(xml), []);
  const parsed = validateRepairedDrawio(xml).diagrams[0];
  assert.equal(parsed.nodes[0].kind, 'decision');
  assert.equal(parsed.edges[0].to, 'n2');
});

test('只有一个现有节点时不默认生成自环，可补建缺失节点', () => {
  const xml = diagram(node('n1', '起点')
    + '<mxCell id="e1" edge="1" parent="1" source="n1" target="missing-node"><mxGeometry relative="1" as="geometry"/></mxCell>');
  const [issue] = inspectDrawioRepair(xml);
  assert.equal(issue.options.length, 1);
  assert.match(issue.options[0].label, /会形成自环/);
  assert.throws(() => applyDrawioRepair(xml, issue, { mode: 'existing' }), /请选择现有节点/);
  const repaired = applyDrawioRepair(xml, issue, { mode: 'create', label: '终点' });
  const parsed = validateRepairedDrawio(repaired).diagrams[0];
  assert.equal(parsed.nodes.length, 2);
  assert.equal(parsed.edges[0].from, 'n1');
  assert.equal(parsed.edges[0].to, 'missing-node');
  assert.equal(parsed.nodes[1].label, '终点');
  assert.equal(repairIssueLabel(issue), '从「起点」出发的连线，终点未连接');
});

test('外层对象与面板无法修复的 ID 异常回退高级编辑器', () => {
  const wrapped = diagram('<UserObject id="n1" label="合成节点" link="https://example.com">'
    + '<mxCell vertex="1" parent="1"><mxGeometry x="30" y="30" width="100" height="50" as="geometry"/></mxCell></UserObject>');
  assert.equal(inspectDrawioRepair(wrapped).length, 0);
  assert.equal(canRepairDrawio(wrapped), false);

  const edge = (id, source = 'n1', target = 'n2') =>
    `<mxCell id="${id}" edge="1" parent="1" source="${source}" target="${target}">`
      + '<mxGeometry relative="1" as="geometry"/></mxCell>';
  const base = node('n1', '节点甲') + node('n2', '节点乙', 220);
  const mixed = diagram(base + node('n1', '重复节点', 400) + edge('e1') + edge('e1'));
  assert.equal(inspectDrawioRepair(mixed).length, 1);
  assert.equal(canRepairDrawio(mixed), false);
  assert.equal(canRepairDrawio(diagram(base + edge('n1'))), false);
  assert.equal(canRepairDrawio(diagram(base + edge('e1')
    + '<mxCell value="文字" style="text;html=1;" vertex="1" parent="1" '
    + 'data-flowchart-annotation-edge="e1"><mxGeometry x="30" y="30" width="40" height="20" as="geometry"/></mxCell>')), false);
  assert.equal(canRepairDrawio(diagram(base + node('n1', '重复节点', 400) + edge('e1'))), true);
});

test('可删除悬空连线及其文字标注，或经确认删除完全重叠的重复节点', () => {
  const loose = diagram(node('n1', '节点甲')
    + '<mxCell id="e1" edge="1" parent="1" source="n1" target="missing"><mxGeometry relative="1" as="geometry"/></mxCell>'
    + '<mxCell id="note" value="说明" style="text;html=1;" vertex="1" parent="1" '
    + 'data-flowchart-annotation-edge="e1"><mxGeometry x="50" y="50" width="40" height="20" as="geometry"/></mxCell>');
  assert.equal(canRepairDrawio(loose), true);
  const removedEdge = applyDrawioRepair(loose, inspectDrawioRepair(loose)[0], { mode: 'delete' });
  assert.deepEqual(inspectDrawioRepair(removedEdge), []);
  const edgeCells = Array.from(new DOMParser().parseFromString(removedEdge, 'text/xml').getElementsByTagName('mxCell'));
  assert.equal(edgeCells.some((cell) => cell.getAttribute('id') === 'e1' || cell.getAttribute('id') === 'note'), false);
  assert.equal(validateRepairedDrawio(removedEdge).diagrams[0].edges.length, 0);

  const duplicate = diagram(node('n1', '节点甲') + node('n1', '节点乙'));
  const removedNode = applyDrawioRepair(duplicate, inspectDrawioRepair(duplicate)[0], { mode: 'delete' });
  assert.equal(validateRepairedDrawio(removedNode).diagrams[0].nodes.length, 1);
  const separate = diagram(node('n1', '节点甲') + node('n1', '节点乙', 220));
  const separateIssue = inspectDrawioRepair(separate)[0];
  assert.equal(validateRepairedDrawio(applyDrawioRepair(separate, separateIssue, { mode: 'delete' })).diagrams[0].nodes.length, 1);
  assert.deepEqual(validateRepairedDrawio(applyDrawioRepair(separate, separateIssue,
    { mode: 'rename', id: 'n2' })).diagrams[0].nodes.map((item) => item.id), ['n1', 'n2']);
});

test('无连线文字时用已连接节点描述问题，不把节点名当作连线名', () => {
  const xml = diagram(node('start', '合成开始')
    + '<mxCell id="e1" edge="1" parent="1" source="start" target="missing">'
    + '<mxGeometry relative="1" as="geometry"/></mxCell>');
  const [issue] = inspectDrawioRepair(xml);
  assert.equal(repairIssueLabel(issue), '从「合成开始」出发的连线，终点未连接');
});

test('选择问题可定位预览中的真实单元格，删除后保留其他项的输入', () => {
  const xml = diagram(node('n1', '节点甲') + node('n1', '节点乙')
    + node('n2', '节点丙', 220, 'invalid-test-kind')
    + '<mxCell id="e1" edge="1" parent="1" source="n1" target="missing">'
    + '<mxGeometry relative="1" as="geometry"/></mxCell>');
  const issues = inspectDrawioRepair(xml);
  const duplicate = issues.find((issue) => issue.type === 'duplicate');
  const kind = issues.find((issue) => issue.type === 'kind');
  const dangling = issues.find((issue) => issue.type === 'dangling');
  const graph = repairPreviewGraph(xml)[0];
  assert.ok(graph.nodes.some((item) => item.repairCellIndex === duplicate.cellIndex));
  assert.ok(graph.nodes.some((item) => item.repairCellIndex === kind.cellIndex));
  assert.ok(graph.edges.some((item) => item.repairCellIndex === dangling.cellIndex));
  const drafts = {
    [repairIssueKey(kind)]: 'decision',
    [repairIssueKey(dangling)]: { mode: 'create', label: '合成终点' },
  };
  const repaired = applyDrawioRepair(xml, duplicate, { mode: 'delete' });
  const kept = keepRepairDrafts(xml, issues, inspectDrawioRepair(repaired), drafts, duplicate, { mode: 'delete' });
  const nextIssues = inspectDrawioRepair(repaired);
  assert.equal(kept[repairIssueKey(nextIssues.find((issue) => issue.type === 'kind'))], 'decision');
  assert.deepEqual(kept[repairIssueKey(nextIssues.find((issue) => issue.type === 'dangling'))],
    { mode: 'create', label: '合成终点' });
});

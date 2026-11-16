import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DOMParser, XMLSerializer } from '@xmldom/xmldom';
import { applyDrawioRepair, inspectDrawioRepair, repairPreviewGraph, repairPreviewXml, validateRepairedDrawio } from '../src/components/flowchart/drawioRepair.js';

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
});

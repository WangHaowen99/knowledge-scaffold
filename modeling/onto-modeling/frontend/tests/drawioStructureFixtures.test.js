import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { test } from 'node:test';
import { DOMParser, XMLSerializer } from '@xmldom/xmldom';
import { drawioStructureError } from '@packs/diagram/ui/flowchart/drawioConvert.js';
import { canRepairDrawio, inspectDrawioRepair } from '@packs/diagram/ui/flowchart/drawioRepair.js';

globalThis.DOMParser = DOMParser;
globalThis.XMLSerializer = XMLSerializer;

const fixtureRoot = new URL('../../fixtures/drawio-structure/', import.meta.url);
const fixtureCases = new URL('cases.json', fixtureRoot);
const fallbackCases = [
  { name: 'valid-wrapped', frontend_error: null, repair_issues: [], repairable: false },
  { name: 'duplicate-id', frontend_error: '节点 ID 重复', repair_issues: ['duplicate'], repairable: true },
  { name: 'dangling-edge', frontend_error: '指向不存在的节点', repair_issues: ['dangling'], repairable: true },
  { name: 'duplicate-edge-id', frontend_error: '单元格 ID 重复', repair_issues: [], repairable: false },
  { name: 'missing-annotation-id', frontend_error: '文字标注 ID 缺失或重复', repair_issues: [], repairable: false },
];
const cases = existsSync(fixtureCases)
  ? JSON.parse(readFileSync(fixtureCases, 'utf8')) : fallbackCases;

// The CI test runner copies test modules without the repository's fixtures directory.
function syntheticDiagram(caseName) {
  const work = caseName === 'valid-wrapped'
    ? '<UserObject id="work" label="Work" link="https://example.com"><mxCell vertex="1" style="rounded=1" parent="1"><mxGeometry x="20" y="130" width="100" height="60" as="geometry"/></mxCell></UserObject>'
    : '<mxCell id="work" value="Work" vertex="1" style="rounded=1" parent="1"><mxGeometry x="20" y="130" width="100" height="60" as="geometry"/></mxCell>';
  const extra = {
    'duplicate-id': '<mxCell id="work" value="Work copy" vertex="1" style="rounded=1" parent="1"><mxGeometry x="220" y="130" width="100" height="60" as="geometry"/></mxCell>',
    'dangling-edge': '<mxCell id="e3" edge="1" source="work" target="missing" parent="1"><mxGeometry relative="1" as="geometry"/></mxCell>',
    'duplicate-edge-id': '<mxCell id="e1" edge="1" source="work" target="end" parent="1"><mxGeometry relative="1" as="geometry"/></mxCell>',
    'missing-annotation-id': '<mxCell value="Label" style="text;html=1;" vertex="1" parent="1" data-flowchart-annotation-edge="e1"><mxGeometry x="90" y="90" width="50" height="20" as="geometry"/></mxCell>',
  }[caseName] || '';
  return `<mxfile><diagram name="synthetic"><mxGraphModel><root>
    <mxCell id="0"/><mxCell id="1" parent="0"/>
    <mxCell id="start" value="Start" vertex="1" style="shape=mxgraph.flowchart.terminator" parent="1"><mxGeometry x="20" y="20" width="100" height="60" as="geometry"/></mxCell>
    ${work}
    <mxCell id="end" value="End" vertex="1" style="shape=mxgraph.flowchart.terminator" parent="1"><mxGeometry x="20" y="240" width="100" height="60" as="geometry"/></mxCell>
    <mxCell id="e1" edge="1" source="start" target="work" parent="1"><mxGeometry relative="1" as="geometry"/></mxCell>
    <mxCell id="e2" edge="1" source="work" target="end" parent="1"><mxGeometry relative="1" as="geometry"/></mxCell>
    ${extra}
    </root></mxGraphModel></diagram></mxfile>`;
}

for (const item of cases) {
  test(`共享图样例：${item.name}`, () => {
    const fixture = new URL(`${item.name}.drawio`, fixtureRoot);
    const xml = existsSync(fixture) ? readFileSync(fixture, 'utf8') : syntheticDiagram(item.name);
    const message = drawioStructureError(xml);
    if (item.frontend_error) assert.ok(message.includes(item.frontend_error), message);
    else assert.equal(message, '');
    assert.deepEqual(inspectDrawioRepair(xml).map((issue) => issue.type), item.repair_issues);
    assert.equal(canRepairDrawio(xml), item.repairable);
  });
}

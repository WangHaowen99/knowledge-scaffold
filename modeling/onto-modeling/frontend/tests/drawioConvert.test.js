// develop tests/drawioConvert.test.js（!547）按原断言移植到本分支对应的函数：
// develop 的 serializeDocumentToDrawio → 图示包的 modelToDrawio（流程说明、过程元数据、节点详情、连线文字与条件写成内核属性）；
// parseDrawioToDocument → 图示包的 drawioToModel（develop !607 的文字标注用例）；
// replace-document / update-diagram → 编辑会话的 replace-model / update-process。
// 原用例里「原文件的连线样式、节点尺寸保留」在本分支由服务端把模型合进原文（AR-10），见后端 test_pack_review_fixes。
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DOMParser } from '@xmldom/xmldom';
import { drawioToModel, modelToDrawio } from '@packs/diagram/ui/flowchart/drawioConvert.js';
import { createFlowchartEditorSession, reduceFlowchartEditorSession } from '@packs/diagram/ui/flowchart/flowchartEditorState.js';
import { normalizeModel } from '@packs/diagram/ui/flowchart/flowchartModel.js';

globalThis.DOMParser = DOMParser;

// 本分支写出的单元格包在 object 里：id、文字在 object 上，样式、几何在里面的 mxCell
function cellsOf(xml) {
  const root = new DOMParser().parseFromString(xml, 'text/xml').getElementsByTagName('root')[0];
  return Array.from(root.childNodes).filter((el) => el.nodeType === 1).map((el) => {
    const bare = el.tagName === 'mxCell';
    return { id: el.getAttribute('id'), text: el.getAttribute(bare ? 'value' : 'label'), cell: bare ? el : el.getElementsByTagName('mxCell')[0] };
  });
}

test('独立文字标注归入对应分支连线，保存后保持原图结构', () => {
  const xml = `<mxfile><diagram name="示例"><mxGraphModel><root>
    <mxCell id="0"/><mxCell id="1" parent="0"/>
    <mxCell id="decision" value="是否通过" style="rhombus;" vertex="1" parent="1"><mxGeometry x="300" y="200" width="100" height="80" as="geometry"/></mxCell>
    <mxCell id="left" value="处理甲" vertex="1" parent="1"><mxGeometry x="100" y="370" width="100" height="60" as="geometry"/></mxCell>
    <mxCell id="right" value="处理乙" vertex="1" parent="1"><mxGeometry x="500" y="370" width="100" height="60" as="geometry"/></mxCell>
    <mxCell id="yes" edge="1" parent="1" source="decision" target="left"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="150" y="300"/></Array></mxGeometry></mxCell>
    <mxCell id="no" edge="1" parent="1" source="decision" target="right"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="550" y="300"/></Array></mxGeometry></mxCell>
    <mxCell id="yes-text" value="通过" style="text;html=1;strokeColor=none;fillColor=none;" vertex="1" parent="1"><mxGeometry x="150" y="300" width="50" height="24" as="geometry"/></mxCell>
    <mxCell id="no-text" value="未通过" style="text;html=1;strokeColor=none;fillColor=none;" vertex="1" parent="1"><mxGeometry x="550" y="300" width="50" height="24" as="geometry"/></mxCell>
  </root></mxGraphModel></diagram></mxfile>`;
  const model = drawioToModel(xml);
  assert.equal(model.nodes.length, 3);
  assert.equal(model.edges.length, 2);
  assert.deepEqual(model.edges.map((edge) => edge.label), ['通过', '未通过']);
  assert.deepEqual(model.edges.map((edge) => edge.type), ['cond', 'cond']);
  const saved = modelToDrawio(model);
  const reloaded = drawioToModel(saved);
  assert.equal(reloaded.nodes.length, 3);
  assert.deepEqual(reloaded.edges.map((edge) => edge.label), ['通过', '未通过']);
  const cells = cellsOf(saved);
  assert.equal(cells.filter((c) => c.cell.getAttribute('vertex') === '1').length, 5);
  assert.equal(cells.filter((c) => c.cell.getAttribute('edge') === '1').length, 2);
  assert.equal(cells.find((c) => c.id === 'yes').cell.getElementsByTagName('mxPoint').length, 1);
  model.edges[0].label = '已通过';
  const renamed = modelToDrawio(model);
  const renamedCells = cellsOf(renamed);
  assert.equal(renamedCells.find((c) => c.id === 'yes-text').text, '已通过');
  assert.equal(renamedCells.find((c) => c.id === 'yes').text, '');
  assert.equal(drawioToModel(renamed).edges[0].label, '已通过');
});

// develop 把归不到连线的文字交给 drawio 编辑器；本分支内核写回只改认得的单元格，它当普通批注原样保留，不会丢
test('无法确定所属连线的文字当普通批注保留，保存时不丢', () => {
  const xml = `<mxfile><diagram><mxGraphModel><root>
    <mxCell id="0"/><mxCell id="1" parent="0"/>
    <mxCell id="note" value="说明" style="text;html=1;" vertex="1" parent="1">
      <mxGeometry x="20" y="20" width="80" height="30" as="geometry"/>
    </mxCell>
  </root></mxGraphModel></diagram></mxfile>`;
  const model = drawioToModel(xml);
  assert.equal(model.nodes.length, 0);
  const saved = modelToDrawio(model);
  assert.match(saved, /<object id="note" label="说明">\s*<mxCell style="text;html=1;" vertex="1" parent="1">/);
  assert.match(saved, /<mxGeometry x="20" y="20" width="80" height="30" as="geometry" \/>/);
});

test('保存时分别写入流程说明、连线文字与业务条件', () => {
  const model = normalizeModel({
    process: { name: '示例流程', description: '说明 A & B\n第二行', roles: ['角色甲'], inputs: ['输入甲'] },
    nodes: [
      { id: 'a', type: 'start', label: '开始', description: '触发说明', x: 70, y: 56 },
      { id: 'b', type: 'end', label: '完成', x: 70, y: 186 },
    ],
    edges: [
      { id: 'ab', from: 'a', to: 'b', type: 'seq', label: '下一步', condition: '满足条件时继续' },
      { id: 'ba', from: 'b', to: 'a', type: 'exc', label: '', condition: '需要重新处理' },
    ],
  });
  const xml = modelToDrawio(model);
  assert.match(xml, /process_description="说明 A &amp; B&#10;第二行"/);
  assert.match(xml, /process_roles="\[&quot;角色甲&quot;\]" process_inputs="\[&quot;输入甲&quot;\]"/);
  assert.match(xml, /description="触发说明"/);
  assert.match(xml, /label="下一步"[^>]*condition="满足条件时继续"/);
  assert.match(xml, /condition="需要重新处理"/);
});

test('空白流程图可以序列化', () => {
  const xml = modelToDrawio(normalizeModel({ process: { name: '空白流程' }, nodes: [], edges: [] }));
  assert.match(xml, /<diagram name="空白流程">/);
  assert.match(xml, /<mxGraphModel /);
});

test('保存过程中父组件旧文档刷新不会覆盖编辑器内容', () => {
  const original = normalizeModel({ process: { name: '示例流程' }, nodes: [], edges: [] });
  const initial = createFlowchartEditorSession(original, { savedRevision: 1 });
  const changed = reduceFlowchartEditorSession(initial, { type: 'update-process', patch: { description: '新说明' } });
  const saving = reduceFlowchartEditorSession(changed, { type: 'mark-saving' });
  const oldRefresh = reduceFlowchartEditorSession(saving, { type: 'replace-model', model: original, savedRevision: 2 });
  assert.equal(oldRefresh, saving);
  const saved = reduceFlowchartEditorSession(saving, { type: 'mark-saved', savedRevision: 2 });
  assert.equal(saved.model.process.description, '新说明');
  assert.equal(saved.dirty, false);
  assert.equal(reduceFlowchartEditorSession(saved, { type: 'replace-model', model: original, savedRevision: 1 }), saved);
});

test('同一修订的父组件旧文档不能覆盖已保存编辑内容', () => {
  const original = normalizeModel({ process: { name: '示例流程' }, nodes: [], edges: [] });
  const changed = reduceFlowchartEditorSession(createFlowchartEditorSession(original, { savedRevision: 1 }), {
    type: 'update-process', patch: { description: '新说明' },
  });
  const saved = reduceFlowchartEditorSession(
    reduceFlowchartEditorSession(changed, { type: 'mark-saving' }),
    { type: 'mark-saved', savedRevision: 2 },
  );
  assert.equal(reduceFlowchartEditorSession(saved, { type: 'replace-model', model: original, savedRevision: 2 }), saved);
});

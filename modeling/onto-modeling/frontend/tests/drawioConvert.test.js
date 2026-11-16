import assert from 'node:assert/strict';
import { test } from 'node:test';
import { DOMParser } from '@xmldom/xmldom';
import {
  parseDrawioToDocument,
  serializeDocumentToDrawio,
} from '../src/components/flowchart/drawioConvert.js';
import {
  createFlowchartEditorSession,
  reduceFlowchartEditorSession,
} from '../src/components/flowchart/flowchartEditorState.js';

globalThis.DOMParser = DOMParser;

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
  const diagram = parseDrawioToDocument(xml).diagrams[0];
  assert.equal(diagram.nodes.length, 3);
  assert.equal(diagram.edges.length, 2);
  assert.deepEqual(diagram.edges.map((edge) => edge.label), ['通过', '未通过']);
  assert.deepEqual(diagram.edges.map((edge) => edge.kind), ['condition', 'condition']);
  const saved = serializeDocumentToDrawio({ diagrams: [diagram] });
  const reloaded = parseDrawioToDocument(saved).diagrams[0];
  assert.equal(reloaded.nodes.length, 3);
  assert.deepEqual(reloaded.edges.map((edge) => edge.label), ['通过', '未通过']);
  const cells = Array.from(new DOMParser().parseFromString(saved, 'text/xml').getElementsByTagName('mxCell'));
  assert.equal(cells.filter((cell) => cell.getAttribute('vertex') === '1').length, 5);
  assert.equal(cells.filter((cell) => cell.getAttribute('edge') === '1').length, 2);
  assert.equal(cells.find((cell) => cell.getAttribute('id') === 'yes').getElementsByTagName('mxPoint').length, 1);
  diagram.edges[0].label = '已通过';
  const renamed = serializeDocumentToDrawio({ diagrams: [diagram] });
  const renamedCells = Array.from(new DOMParser().parseFromString(renamed, 'text/xml').getElementsByTagName('mxCell'));
  assert.equal(renamedCells.find((cell) => cell.getAttribute('id') === 'yes-text').getAttribute('value'), '已通过');
  assert.equal(renamedCells.find((cell) => cell.getAttribute('id') === 'yes').getAttribute('value'), '');
  assert.equal(parseDrawioToDocument(renamed).diagrams[0].edges[0].label, '已通过');
});

test('无法确定所属连线的文字交给 drawio 编辑器，避免保存时丢失', () => {
  const xml = `<mxfile><diagram><mxGraphModel><root>
    <mxCell id="0"/><mxCell id="1" parent="0"/>
    <mxCell id="note" value="说明" style="text;html=1;" vertex="1" parent="1">
      <mxGeometry x="20" y="20" width="80" height="30" as="geometry"/>
    </mxCell>
  </root></mxGraphModel></diagram></mxfile>`;
  assert.throws(() => parseDrawioToDocument(xml), /unsupported text annotation/);
});

test('保存时分别写入流程说明、连线文字与业务条件', () => {
    const document = {
      diagrams: [{
        id: 'diagram-1', title: '示例流程', description: '说明 A & B\n第二行',
        processMeta: { actors: ['角色甲'], inputs: ['输入甲'] },
        nodes: [
          { id: 'a', kind: 'start', label: '开始', detail: '触发说明',
            meta: { width: 120, height: 72, style: 'shape=mxgraph.flowchart.terminator;' } },
          { id: 'b', kind: 'end', label: '完成' },
        ],
        edges: [{
          id: 'ab', from: 'a', to: 'b', kind: 'sequence',
          label: '下一步', conditionText: '满足条件时继续',
          meta: { style: 'edgeStyle=orthogonalEdgeStyle;dashed=1;' },
        }, {
          id: 'ba', from: 'b', to: 'a', kind: 'exception',
          label: '', conditionText: '需要重新处理',
        }],
        layout: { direction: 'TB', positions: { a: { x: 10, y: 20 }, b: { x: 10, y: 150 } } },
      }],
    };
    const xml = serializeDocumentToDrawio(document);
    assert.match(xml, /data-flowchart-description="说明 A &amp; B&#10;第二行"/);
    assert.match(xml, /data-flowchart-process-meta="\{&quot;actors&quot;:\[&quot;角色甲&quot;\],&quot;inputs&quot;:\[&quot;输入甲&quot;\]\}"/);
    assert.match(xml, /data-flowchart-detail="触发说明"/);
    assert.match(xml, /value="下一步"[^>]*data-flowchart-label="下一步"/);
    assert.match(xml, /data-flowchart-condition="满足条件时继续"/);
    assert.match(xml, /style="edgeStyle=orthogonalEdgeStyle;dashed=1;"/);
});

test('空白流程图可以序列化', () => {
    const xml = serializeDocumentToDrawio({ diagrams: [{
      id: 'diagram-1', title: '空白流程', nodes: [], edges: [],
    }] });
    assert.match(xml, /<diagram name="空白流程">/);
    assert.match(xml, /<mxGraphModel /);
});

test('保存过程中父组件旧文档刷新不会覆盖编辑器内容', () => {
  const original = { diagrams: [{ id: 'diagram-1', title: '示例流程', nodes: [], edges: [] }] };
  const initial = createFlowchartEditorSession(original, { savedRevision: 1 });
  const changed = reduceFlowchartEditorSession(initial, {
    type: 'update-diagram', diagramId: 'diagram-1', patch: { description: '新说明' },
  });
  const saving = reduceFlowchartEditorSession(changed, { type: 'mark-saving' });
  const oldRefresh = reduceFlowchartEditorSession(saving, {
    type: 'replace-document', document: original, savedRevision: 2,
  });
  assert.equal(oldRefresh, saving);
  const saved = reduceFlowchartEditorSession(saving, { type: 'mark-saved', savedRevision: 2 });
  assert.equal(saved.document.diagrams[0].description, '新说明');
  assert.equal(saved.dirty, false);
  assert.equal(reduceFlowchartEditorSession(saved, {
    type: 'replace-document', document: original, savedRevision: 1,
  }), saved);
});

test('同一修订的父组件旧文档不能覆盖已保存编辑内容', () => {
  const original = { diagrams: [{ id: 'diagram-1', title: '示例流程', nodes: [], edges: [] }] };
  const changed = reduceFlowchartEditorSession(createFlowchartEditorSession(original, { savedRevision: 1 }), {
    type: 'update-diagram', diagramId: 'diagram-1', patch: { description: '新说明' },
  });
  const saved = reduceFlowchartEditorSession(
    reduceFlowchartEditorSession(changed, { type: 'mark-saving' }),
    { type: 'mark-saved', savedRevision: 2 },
  );
  assert.equal(reduceFlowchartEditorSession(saved, {
    type: 'replace-document', document: original, savedRevision: 2,
  }), saved);
});

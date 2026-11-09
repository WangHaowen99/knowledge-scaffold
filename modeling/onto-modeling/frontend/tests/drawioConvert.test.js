import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  serializeDocumentToDrawio,
} from '../src/components/flowchart/drawioConvert.js';
import {
  createFlowchartEditorSession,
  reduceFlowchartEditorSession,
} from '../src/components/flowchart/flowchartEditorState.js';

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

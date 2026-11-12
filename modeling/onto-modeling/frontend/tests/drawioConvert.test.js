// develop tests/drawioConvert.test.js（!547）按原断言移植到本分支对应的函数：
// develop 的 serializeDocumentToDrawio → 图示包的 modelToDrawio（流程说明、过程元数据、节点详情、连线文字与条件写成内核属性）；
// replace-document / update-diagram → 编辑会话的 replace-model / update-process。
// 原用例里「原文件的连线样式、节点尺寸保留」在本分支由服务端把模型合进原文（AR-10），见后端 test_pack_review_fixes。
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { modelToDrawio } from '@packs/diagram/ui/flowchart/drawioConvert.js';
import { createFlowchartEditorSession, reduceFlowchartEditorSession } from '@packs/diagram/ui/flowchart/flowchartEditorState.js';
import { normalizeModel } from '@packs/diagram/ui/flowchart/flowchartModel.js';

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

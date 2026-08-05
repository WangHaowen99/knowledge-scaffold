import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  DIAGRAM_DOCUMENT_PATH,
  diagramIdAt,
  diagramIndexFromPath,
  hasDiagramDocument,
  isProcessDiagramPath,
  parseDiagramDocument,
  resolveDiagramTarget,
} from '../frontend/src/mermaidDiagramTarget.js';

function sampleDocument() {
  return {
    diagrams: [
      { id: 'intake', title: '受理流程', nodes: [], edges: [] },
      { id: 'approval', title: '审批流程', nodes: [], edges: [] },
    ],
  };
}

const sampleFiles = ['index.md', DIAGRAM_DOCUMENT_PATH, 'diagrams/受理流程.mmd', 'diagrams/审批流程.mmd'];

test('process_N.mmd 映射为 0 基图序号', () => {
  assert.equal(diagramIndexFromPath('diagrams/process_1.mmd'), 0);
  assert.equal(diagramIndexFromPath('diagrams/process_12.mmd'), 11);
});

test('同名 Mermaid 文件识别为流程图路径', () => {
  assert.equal(isProcessDiagramPath('diagrams/审批流程.mmd'), true);
  assert.equal(isProcessDiagramPath('diagrams/process_3.mmd'), true);
});

test('非流程图路径不参与图形化编辑', () => {
  for (const path of [
    'index.md',
    'diagrams/index.md',
    DIAGRAM_DOCUMENT_PATH,
    'diagrams/process_1.mmd.bak',
    'other/process_1.mmd',
    'diagrams/process_.mmd',
    'diagrams/process_0.mmd',
    '',
    null,
    undefined,
  ]) {
    assert.equal(diagramIndexFromPath(path as string), null, `应降级为原文视图：${String(path)}`);
  }
});

test('缺少 processes.json 时判定为无源文档', () => {
  assert.equal(hasDiagramDocument(sampleFiles), true);
  assert.equal(hasDiagramDocument(['index.md', 'diagrams/process_1.mmd']), false);
  assert.equal(hasDiagramDocument(undefined as unknown as string[]), false);
});

test('源文档解析失败时返回 null 而不是抛错', () => {
  assert.deepEqual(parseDiagramDocument(JSON.stringify(sampleDocument())), sampleDocument());
  assert.equal(parseDiagramDocument('{ not json'), null);
  assert.equal(parseDiagramDocument('null'), null);
  assert.equal(parseDiagramDocument('[]'), null);
  assert.equal(parseDiagramDocument(JSON.stringify({ title: '缺少流程列表' })), null);
});

test('按序号定位到被点击的那张图', () => {
  const document = sampleDocument();
  assert.equal(diagramIdAt(document, 0), 'intake');
  assert.equal(diagramIdAt(document, 1), 'approval');
  assert.equal(diagramIdAt(document, 5), '');
  assert.equal(diagramIdAt(null, 0), '');
});

test('resolveDiagramTarget 打通路径到可编辑目标', () => {
  const target = resolveDiagramTarget({
    path: 'diagrams/审批流程.mmd',
    files: sampleFiles,
    documentContent: JSON.stringify(sampleDocument()),
  });
  assert.ok(target);
  assert.equal(target.index, 1);
  assert.equal(target.activeDiagramId, 'approval');
  assert.equal(target.document.diagrams.length, 2);
});

test('历史 process_N.mmd 仍能定位对应流程图', () => {
  const target = resolveDiagramTarget({
    path: 'diagrams/process_2.mmd',
    files: [...sampleFiles, 'diagrams/process_2.mmd'],
    documentContent: JSON.stringify(sampleDocument()),
  });
  assert.equal(target?.activeDiagramId, 'approval');
});

test('.mmd 与源文档不同步时降级为原文视图', () => {
  // 文件树里有未关联名称的 .mmd，源文档中没有对应流程
  const target = resolveDiagramTarget({
    path: 'diagrams/未知流程.mmd',
    files: [...sampleFiles, 'diagrams/未知流程.mmd'],
    documentContent: JSON.stringify(sampleDocument()),
  });
  assert.equal(target, null);
});

test('源文档缺失或损坏时降级为原文视图', () => {
  assert.equal(resolveDiagramTarget({
    path: 'diagrams/受理流程.mmd',
    files: ['index.md', 'diagrams/受理流程.mmd'],
    documentContent: null,
  }), null);
  assert.equal(resolveDiagramTarget({
    path: 'diagrams/受理流程.mmd',
    files: sampleFiles,
    documentContent: '{ broken',
  }), null);
});

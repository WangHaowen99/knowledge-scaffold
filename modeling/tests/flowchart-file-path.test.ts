import assert from 'node:assert/strict';
import test from 'node:test';

import {
  diagramFileEntries,
  diagramFilePath,
  obsoleteDiagramFilePaths,
} from '../src/flowchart/file-path.js';

test('流程名称直接形成同名 Mermaid 文件路径', () => {
  assert.equal(diagramFilePath('授信审批'), 'diagrams/授信审批.mmd');
  assert.equal(diagramFilePath(' 月度复核 '), 'diagrams/月度复核.mmd');
});

test('流程文件名拒绝跨平台非法字符和同名覆盖', () => {
  assert.equal(diagramFilePath('申请/审批'), 'diagrams/申请_审批.mmd');
  assert.equal(diagramFilePath('客户（企业）审批'), 'diagrams/客户_企业_审批.mmd');
  assert.throws(() => diagramFilePath('CON'), /不能用于流程图文件名/u);
  assert.throws(() => diagramFileEntries([
    { id: 'one', title: '授信审批' },
    { id: 'two', title: '授信审批' },
  ]), /流程名称重复/u);
});

test('流程改名时只清理旧派生文件并兼容历史 process_N 文件', () => {
  const currentFiles = {
    'diagrams/index.md': '# 流程图目录',
    'diagrams/processes.json': JSON.stringify({
      diagrams: [{ id: 'approval', title: '旧授信审批', nodes: [], edges: [] }],
    }),
    'diagrams/旧授信审批.mmd': 'flowchart TD',
    'diagrams/process_1.mmd': 'legacy',
    'diagrams/人工补充.mmd': 'manual',
  };
  const nextDocument = {
    diagrams: [{ id: 'approval', title: '新授信审批', nodes: [], edges: [] }],
  };

  assert.deepEqual(obsoleteDiagramFilePaths(currentFiles, nextDocument), [
    'diagrams/process_1.mmd',
    'diagrams/旧授信审批.mmd',
  ]);
});

test('保存安全文件名时清理升级前的旧括号文件名', () => {
  const currentFiles = {
    'diagrams/processes.json': JSON.stringify({
      diagrams: [{ id: 'approval', title: '申请（企业）', nodes: [], edges: [] }],
    }),
    'diagrams/申请（企业）.mmd': 'legacy',
  };
  const nextDocument = { diagrams: [{ id: 'approval', title: '申请（企业）', nodes: [], edges: [] }] };
  assert.deepEqual(obsoleteDiagramFilePaths(currentFiles, nextDocument), ['diagrams/申请（企业）.mmd']);
});

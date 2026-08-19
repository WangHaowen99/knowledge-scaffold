import assert from 'node:assert/strict';
import test from 'node:test';
import { createProject } from '../src/projects/repo.js';
import {
  answerMaterialInventoryQuestion,
  buildMaterialInventory,
  hasMaterialInventoryIntent,
} from '../src/workflow/material-inventory.js';
import { packageChatPlan } from '../src/workflow/package-chat.js';
import { buildTools } from '../src/workflow/tools.js';

const documents = [
  {
    id: 'doc-guide', projectId: 'project-demo', filename: 'guide.md', kind: 'markdown' as const,
    parseStatus: 'parsed' as const, textContent: '合成资料正文', createdAt: '2026-01-01T00:00:00.000Z',
  },
  {
    id: 'doc-data', projectId: 'project-demo', filename: 'dataset.xlsx', kind: 'excel' as const,
    parseStatus: 'parsed' as const,
    tables: [{ name: 'Data', fields: [], rowCount: 2 }], createdAt: '2026-01-01T00:00:01.000Z',
  },
];

test('资料清单保留正文和表格文件，并按资料数统计', async () => {
  const materials = buildMaterialInventory(documents);
  assert.equal(materials.length, 2);
  assert.equal(materials[1].filename, 'dataset.xlsx');
  assert.equal(materials[1].tableCount, 1);

  const list = buildTools({
    tables: [],
    documents: [{ id: 'doc-guide', filename: 'guide.md', textContent: '合成资料正文' }],
    materials,
    captured: {},
  }).find((tool) => tool.name === 'list_documents') as any;
  const result = await list.execute('inventory-test', {});
  assert.equal(result.details.count, 2);
  assert.match(result.content[0].text, /guide\.md/);
  assert.match(result.content[0].text, /dataset\.xlsx/);
});

test('资料数量和成功清单问题直接使用全量资料清单回答', () => {
  const materials = buildMaterialInventory(documents);
  assert.equal(hasMaterialInventoryIntent('上述两个文件，是否已上传成功'), true);
  assert.equal(hasMaterialInventoryIntent('上传成功的文档，有哪些？'), true);
  const reply = answerMaterialInventoryQuestion('上述两个文件，是否已上传成功', materials);
  assert.match(reply ?? '', /共接收到 2 份资料/);
  assert.match(reply ?? '', /guide\.md/);
  assert.match(reply ?? '', /dataset\.xlsx/);
});

test('普通建模问题不触发资料清单快捷回答', () => {
  const materials = buildMaterialInventory(documents);
  assert.equal(answerMaterialInventoryQuestion('请根据资料补充一个规则', materials), undefined);
  assert.equal(answerMaterialInventoryQuestion('如何上传文档才能成功', materials), undefined);
  assert.equal(answerMaterialInventoryQuestion('请根据上传文件成功生成规则', materials), undefined);
});

test('主聊天的资料清单问题不依赖模型推断', async () => {
  const project = createProject({ owner: 'operator_a', name: 'project_alpha' });
  const result = await packageChatPlan(project, [], '上传成功的文档，有哪些？', {
    materials: buildMaterialInventory(documents),
  });
  assert.match(result.reply ?? '', /共接收到 2 份资料/);
  assert.match(result.reply ?? '', /dataset\.xlsx/);
});

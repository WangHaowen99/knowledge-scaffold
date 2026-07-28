import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const dataDir = mkdtempSync(join(tmpdir(), 'modeling-competency-'));
process.env.DATA_DIR = dataDir;

const legacyDb = new DatabaseSync(join(dataDir, 'modeling.db'));
legacyDb.exec(`CREATE TABLE competency_questions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  question TEXT NOT NULL,
  selected INTEGER NOT NULL DEFAULT 0,
  covered_operation TEXT,
  created_at TEXT NOT NULL
)`);
legacyDb.close();

test('领域问题：自定义问题新增后选中，重新生成候选时仍保留', async () => {
  const { createProject } = await import('../src/projects/repo.js');
  const {
    addCustomCompetency,
    listCompetency,
    replaceCompetency,
  } = await import('../src/workflow/m2-repo.js');
  const project = createProject({ owner: 'alice', name: '领域问题项目' });

  const { getDb } = await import('../src/db/index.js');
  const columns = getDb().prepare('PRAGMA table_info(competency_questions)').all() as Array<{ name: string }>;
  assert.ok(columns.some((column) => column.name === 'source'));

  replaceCompetency(project.id, ['原候选问题']);
  const custom = addCustomCompetency(project.id, '  用户自定义问题  ');

  assert.equal(custom.question, '用户自定义问题');
  assert.equal(custom.selected, true);
  assert.equal(custom.source, 'custom');

  replaceCompetency(project.id, ['新候选问题']);
  assert.deepEqual(
    listCompetency(project.id).map(({ question, selected, source }) => ({ question, selected, source })),
    [
      { question: '用户自定义问题', selected: true, source: 'custom' },
      { question: '新候选问题', selected: false, source: 'generated' },
    ],
  );
});

test('领域问题：重复添加已有问题只选中原记录', async () => {
  const { createProject } = await import('../src/projects/repo.js');
  const {
    addCustomCompetency,
    listCompetency,
    replaceCompetency,
  } = await import('../src/workflow/m2-repo.js');
  const project = createProject({ owner: 'alice', name: '领域问题去重项目' });

  replaceCompetency(project.id, ['如何查询订单状态？']);
  const added = addCustomCompetency(project.id, '如何查询订单状态？');

  assert.equal(added.selected, true);
  assert.equal(listCompetency(project.id).length, 1);
});

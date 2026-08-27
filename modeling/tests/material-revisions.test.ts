import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';

import { ensureDocumentRevisionColumns } from '../src/db/index.js';
import { DocumentRevisionRepo, materialSemanticHash } from '../src/documents/repo.js';
import {
  analyzeMaterialImpact,
  applyMaterialInvalidation,
  listMaterialImpacts,
  resolveMaterialImpactScope,
  storeMaterialImpact,
} from '../src/documents/material-impact.js';
import type { PackageSpec } from '../src/types.js';

const schema = readFileSync(new URL('../src/db/schema.sql', import.meta.url), 'utf8');

function setup() {
  const db = new DatabaseSync(':memory:');
  db.exec('PRAGMA foreign_keys = ON');
  db.exec(schema);
  const now = new Date().toISOString();
  db.prepare(`INSERT INTO projects
    (id, owner, name, domain, background, stage, created_at, updated_at)
    VALUES ('project-material', 'owner', '材料修订', '', '', 'ingest', ?, ?)`)
    .run(now, now);
  return { db, repo: new DocumentRevisionRepo(db) };
}

test('旧 documents 表在完整 schema 建索引前补齐资料修订列', () => {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    kind TEXT NOT NULL,
    parse_status TEXT NOT NULL DEFAULT 'pending',
    parse_error TEXT,
    text_content TEXT,
    tables_json TEXT,
    created_at TEXT NOT NULL
  );
  INSERT INTO documents
    (id, project_id, filename, kind, parse_status, created_at)
  VALUES ('legacy-doc', 'legacy-project', 'legacy.md', 'markdown', 'parsed', '2026-07-24T00:00:00.000Z');`);

  ensureDocumentRevisionColumns(db);
  assert.doesNotThrow(() => db.exec(schema));
  const row = db.prepare(`SELECT source_key, revision_no, lifecycle_status, raw_content
    FROM documents WHERE id = 'legacy-doc'`).get() as any;
  assert.deepEqual({ ...row }, {
    source_key: 'legacy-doc', revision_no: 1, lifecycle_status: 'active', raw_content: null,
  });
});

test('新 schema 兼容尚未使用资料修订字段的旧写入方', () => {
  const { db, repo } = setup();
  db.prepare(`INSERT INTO documents
    (id, project_id, filename, kind, parse_status, text_content, created_at)
    VALUES ('legacy-insert', 'project-material', 'legacy.md', 'markdown', 'parsed', '正文', ?)`)
    .run(new Date().toISOString());

  const document = repo.get('legacy-insert');
  assert.equal(document?.sourceKey, 'legacy-insert');
  assert.equal(document?.revisionNo, 1);
  assert.equal(document?.purpose, 'auto');
  assert.equal(document?.readingMode, 'on_demand');
  assert.equal(document?.directiveRevision, 0);
});

test('资料用途与阅读方式随修订继承，且数据 Schema 只接受表格', () => {
  const { repo } = setup();
  const first = repo.insert({
    projectId: 'project-material', filename: 'schema.xlsx', kind: 'excel',
    purpose: 'schema', readingMode: 'full', parseStatus: 'parsed',
    tables: [{ name: '设备', fields: [], rowCount: 20 }],
  });
  assert.equal(first.document.purpose, 'schema');
  assert.equal(first.document.readingMode, 'full');

  const replacement = repo.insert({
    projectId: 'project-material', filename: 'schema.xlsx', kind: 'excel',
    parseStatus: 'parsed', tables: [{ name: '设备', fields: [], rowCount: 21 }],
  });
  assert.equal(replacement.document.purpose, 'schema');
  assert.equal(replacement.document.readingMode, 'full');

  const guide = repo.insert({
    projectId: 'project-material', filename: 'guide.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '业务说明',
  }).document;
  assert.throws(() => repo.updateDirectives(guide.id, { purpose: 'schema', readingMode: 'on_demand' }), /Excel 或 CSV/);
  assert.throws(() => repo.updateDirectives(guide.id, { purpose: 'term', readingMode: 'on_demand' }), /术语表/);
  const updated = repo.updateDirectives(guide.id, {
    purpose: 'goal', readingMode: 'full', expectedDirectiveRevision: 0,
  });
  assert.equal(updated.purpose, 'goal');
  assert.equal(updated.readingMode, 'full');
  assert.equal(updated.directiveRevision, 1);
  assert.throws(() => repo.updateDirectives(guide.id, {
    purpose: 'evidence', readingMode: 'on_demand', expectedDirectiveRevision: 0,
  }), /其他位置更新/);
});

test('同名资料形成不可变修订且仅返回活动版本', () => {
  const { db, repo } = setup();
  const first = repo.insert({
    projectId: 'project-material', filename: 'process.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '# 流程\n第一步', rawContent: Buffer.from('# 流程\n第一步'),
  });
  const second = repo.insert({
    projectId: 'project-material', filename: 'process.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '# 流程\n第二步', rawContent: Buffer.from('# 流程\n第二步'),
  });

  assert.equal(second.change.kind, 'replaced');
  assert.equal(second.change.semanticChanged, true);
  assert.equal(second.document.sourceKey, first.document.sourceKey);
  assert.equal(second.document.revisionNo, 2);
  assert.equal(second.document.supersedesId, first.document.id);
  assert.equal(repo.rawContent(second.document.id)?.toString('utf8'), '# 流程\n第二步');
  assert.deepEqual(repo.list('project-material').map((item) => item.id), [second.document.id]);
  const history = repo.history('project-material', first.document.sourceKey);
  assert.deepEqual(history.map((item) => [item.revisionNo, item.lifecycleStatus]), [
    [2, 'active'], [1, 'superseded'],
  ]);
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM documents').get().count, 2);
});

test('纯格式变化保留修订但不传播语义失效', () => {
  const { repo } = setup();
  repo.insert({
    projectId: 'project-material', filename: 'policy.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '规则 A\n阈值 10', rawContent: Buffer.from('规则 A\n阈值 10'),
  });
  const next = repo.insert({
    projectId: 'project-material', filename: 'policy.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '  规则 A  \r\n\r\n 阈值   10  ',
    rawContent: Buffer.from('  规则 A  \r\n\r\n 阈值   10  '),
  });

  assert.equal(next.change.kind, 'format_only');
  assert.equal(next.change.semanticChanged, false);
  assert.notEqual(next.document.contentHash, next.change.previous?.contentHash);
  assert.equal(next.document.semanticHash, next.change.previous?.semanticHash);
});

test('后台解析完成时才计算资料修订的最终语义变化', () => {
  const { repo } = setup();
  const first = repo.insert({
    projectId: 'project-material', filename: 'policy.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '规则 A\n阈值 10', rawContent: Buffer.from('规则 A\n阈值 10'),
  });
  const pending = repo.insert({
    projectId: 'project-material', filename: 'policy.md', kind: 'markdown',
    parseStatus: 'pending', rawContent: Buffer.from('  规则 A  \r\n\r\n 阈值   10  '),
  });

  assert.equal(pending.document.parseStatus, 'pending');
  assert.deepEqual(repo.pending().map((item) => item.id), [pending.document.id]);
  const completed = repo.completeParse(pending.document.id, {
    parseStatus: 'parsed', textContent: '  规则 A  \r\n\r\n 阈值   10  ',
  });

  assert.equal(completed.document.parseStatus, 'parsed');
  assert.deepEqual(repo.pending(), []);
  assert.equal(completed.change.kind, 'format_only');
  assert.equal(completed.change.semanticChanged, false);
  assert.equal(completed.document.semanticHash, first.document.semanticHash);
  assert.notEqual(completed.document.contentHash, first.document.contentHash);
});

test('删除资料只停用活动修订并保留完整历史', () => {
  const { db, repo } = setup();
  const inserted = repo.insert({
    projectId: 'project-material', filename: 'facts.txt', kind: 'text',
    parseStatus: 'parsed', textContent: '事实',
  });
  const change = repo.retire(inserted.document.id);

  assert.equal(change.kind, 'deleted');
  assert.equal(change.semanticChanged, true);
  assert.deepEqual(repo.list('project-material'), []);
  assert.equal(repo.history('project-material')[0]?.lifecycleStatus, 'deleted');
  assert.equal(db.prepare('SELECT COUNT(*) AS count FROM documents').get().count, 1);
});

test('表格语义哈希忽略对象键顺序但保留业务数据变化', () => {
  const left = materialSemanticHash({
    kind: 'excel', parseStatus: 'parsed',
    tables: [{ name: 'records', rowCount: 1, fields: [{ name: 'value', inferredType: 'number', sampleValues: ['10'], distinctRatio: 1 }], rows: [['10']] }],
  });
  const same = materialSemanticHash({
    kind: 'excel', parseStatus: 'parsed',
    tables: [{ rows: [['10']], fields: [{ distinctRatio: 1, sampleValues: ['10'], inferredType: 'number', name: 'value' }], rowCount: 1, name: 'records' }],
  });
  const changed = materialSemanticHash({
    kind: 'excel', parseStatus: 'parsed',
    tables: [{ name: 'records', rowCount: 1, fields: [{ name: 'value', inferredType: 'number', sampleValues: ['11'], distinctRatio: 1 }], rows: [['11']] }],
  });

  assert.equal(left, same);
  assert.notEqual(left, changed);
});

function packageWithMaterialEvidence(documentId: string): PackageSpec {
  return {
    id: 'generic-package', name: '通用包', description: '通用业务', version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [{
      key: 'Request', title: '请求', description: '业务请求', fields: [],
      sourceRefs: [{ kind: 'document', documentId, filename: 'policy.md' }],
      relations: [{ relation: 'uses', target: 'Policy', evidence: [`material-revision:${documentId}`] }],
    }, {
      key: 'Policy', title: '策略', description: '业务策略', fields: [],
      sourceRefs: [{ kind: 'document', documentId, filename: 'policy.md' }],
    }],
    operations: [{
      key: 'Handle', title: '处理', description: '处理请求', inputs: [], outputs: [], flow: [],
      relatedObjects: ['Request', 'Policy'], evidence: [`material-revision:${documentId}`],
    }],
    rules: [{ key: 'Eligibility', title: '适用条件', content: '按策略判断', evidence: [`material-revision:${documentId}`] }],
    scripts: [{
      key: 'calculate', title: '计算', description: '确定性计算', boundOperation: 'Handle',
      runtime: 'python', entrypoint: 'run.py', timeoutSeconds: 30, parameters: [], outputs: [],
      boundaries: [], evidence: ['operation:Handle'], sourceCode: 'print({})',
    }],
  };
}

test('影响分析只命中引用旧资料修订的知识资源', () => {
  const { repo } = setup();
  const first = repo.insert({
    projectId: 'project-material', filename: 'policy.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '旧策略',
  });
  const next = repo.insert({
    projectId: 'project-material', filename: 'policy.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '新策略',
  });
  const impact = analyzeMaterialImpact(next.change, packageWithMaterialEvidence(first.document.id));

  assert.equal(impact.earliestMilestone, 'knowledge_confirmed');
  // script:calculate 不是直接引用这份资料，而是绑定在受影响的 operation:Handle 上——
  // 操作变了脚本同样要复核，所以影响面按引用关系传播到它
  assert.deepEqual(impact.affectedResources.map((item) => `${item.kind}:${item.key}`).sort(), [
    'object:Policy', 'object:Request', 'operation:Handle', 'relation:Request.uses',
    'rule:Eligibility', 'script:calculate',
  ]);
  assert.deepEqual(impact.staleWorkflowSteps, [
    'scenario', 'data_source', 'evaluate', 'diagnose', 'optimize', 'release',
  ]);
});

test('格式变化不传播失效，新增资料在语义核对完成前不作废任何内容', () => {
  const { db, repo } = setup();
  const first = repo.insert({
    projectId: 'project-material', filename: 'policy.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '策略', rawContent: Buffer.from('策略'),
  });
  const format = repo.insert({
    projectId: 'project-material', filename: 'policy.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '  策略  ', rawContent: Buffer.from('  策略  '),
  });
  const added = repo.insert({
    projectId: 'project-material', filename: 'notes.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '补充说明',
  });

  assert.equal(analyzeMaterialImpact(format.change, packageWithMaterialEvidence(first.document.id)).earliestMilestone, undefined);
  const addedImpact = analyzeMaterialImpact(added.change, packageWithMaterialEvidence(first.document.id));
  assert.equal(addedImpact.scopeResolved, false);
  assert.equal(addedImpact.earliestMilestone, undefined);
  assert.deepEqual(addedImpact.affectedResources, []);
  assert.deepEqual(applyMaterialInvalidation(db, 'project-material', addedImpact), { staleTaskIds: [] });

  const stored = storeMaterialImpact(db, 'project-material', added.change, addedImpact);
  const resolved = resolveMaterialImpactScope(
    db,
    'project-material',
    stored.id,
    packageWithMaterialEvidence(first.document.id),
    [{ kind: 'object', key: 'Policy' }],
  );
  assert.equal(resolved.scopeResolved, true);
  assert.deepEqual(resolved.directResources, [{ kind: 'object', key: 'Policy' }]);
  assert.deepEqual(resolved.affectedResources.map((item) => `${item.kind}:${item.key}`).sort(), [
    'object:Policy', 'operation:Handle', 'script:calculate',
  ]);
  assert.equal(listMaterialImpacts(db, 'project-material')[0]?.scopeResolved, true);
});

test('资料变更只生成定向复核任务，不再推倒已确认阶段', () => {
  const { db, repo } = setup();
  const now = new Date().toISOString();
  db.prepare(`INSERT INTO ai_construction_graphs
    (project_id, revision, status, source, created_at, updated_at)
    VALUES ('project-material', 8, 'completed', 'new', ?, ?)`).run(now, now);
  const milestones = [
    ['materials_ready', 1], ['goal_confirmed', 2], ['process_confirmed', 3],
    ['knowledge_confirmed', 4], ['deterministic_confirmed', 5], ['construction_completed', 6],
  ] as const;
  for (const [id, ordinal] of milestones) {
    db.prepare(`INSERT INTO ai_construction_milestones
      (project_id, milestone_id, ordinal, title, status, confirmed_revision_oid, updated_at)
      VALUES ('project-material', ?, ?, ?, 'confirmed', 'revision-old', ?)`)
      .run(id, ordinal, id, now);
  }
  const tasks = [
    ['task-material', 'material_analysis', 'materials_ready', 1],
    ['task-knowledge', 'knowledge_extraction', 'knowledge_confirmed', 2],
    ['task-validation', 'package_validation', 'knowledge_confirmed', 3],
    ['task-script', 'script_decision', 'deterministic_confirmed', 4],
    ['task-complete', 'completion_review', 'construction_completed', 5],
  ] as const;
  for (const [id, kind, milestone, ordinal] of tasks) {
    db.prepare(`INSERT INTO ai_construction_tasks
      (id, project_id, task_kind, milestone_id, ordinal, title, goal, status, required,
        output_revision_oid, created_at, updated_at)
      VALUES (?, 'project-material', ?, ?, ?, ?, ?, 'completed', 1, 'revision-old', ?, ?)`)
      .run(id, kind, milestone, ordinal, id, id, now, now);
  }
  const first = repo.insert({
    projectId: 'project-material', filename: 'policy.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '旧策略',
  });
  const next = repo.insert({
    projectId: 'project-material', filename: 'policy.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '新策略',
  });
  const analysis = analyzeMaterialImpact(next.change, packageWithMaterialEvidence(first.document.id));
  const stored = storeMaterialImpact(db, 'project-material', next.change, analysis);
  const invalidation = applyMaterialInvalidation(db, 'project-material', analysis);

  assert.equal(stored.changeKind, 'replaced');
  assert.equal(listMaterialImpacts(db, 'project-material')[0]?.id, stored.id);
  assert.equal(invalidation.graphRevision, 9);

  const status = (milestoneId: string) => (db.prepare(`SELECT status FROM ai_construction_milestones
    WHERE project_id = 'project-material' AND milestone_id = ?`).get(milestoneId) as any).status;

  // 已确认的中间阶段一律不动：它们确认的是当时那部分内容，而现在我们精确知道
  // 哪些内容受影响，没必要把整段推倒让人重走一遍。
  assert.equal(status('process_confirmed'), 'confirmed');
  assert.equal(status('knowledge_confirmed'), 'confirmed');
  assert.equal(status('deterministic_confirmed'), 'confirmed');
  // 只撤销「建模完成确认」——它断言的是「整体已一致」，资料一变这个断言就不成立
  assert.equal(status('construction_completed'), 'active');

  // 任务也不再被作废，用户不用重做没被影响的部分
  for (const taskId of ['task-knowledge', 'task-validation', 'task-script']) {
    assert.equal((db.prepare('SELECT status FROM ai_construction_tasks WHERE id = ?')
      .get(taskId) as any).status, 'completed', `${taskId} 不该被作废`);
  }
  assert.deepEqual(invalidation.staleTaskIds, []);

  // 取而代之的是一条列清影响面的定向复核任务
  const review = db.prepare(`SELECT id, status, reason FROM ai_construction_tasks
    WHERE project_id = 'project-material' AND task_kind = 'material_change_review'`).all() as any[];
  assert.equal(review.length, 1);
  assert.equal(review[0].id, invalidation.reviewTaskId);
  assert.equal(review[0].status, 'pending');
  assert.match(review[0].reason, /资料变更后需要复核/);
  // 影响面要具体到资源，而不是一句「有内容受影响」
  assert.match(review[0].reason, /业务操作|规则|业务对象/);

  // 同一份资料再变一次，只刷新那条任务，不攒出一串
  const third = repo.insert({
    projectId: 'project-material', filename: 'policy.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '第三版策略',
  });
  applyMaterialInvalidation(db, 'project-material',
    analyzeMaterialImpact(third.change, packageWithMaterialEvidence(first.document.id)));
  assert.equal((db.prepare(`SELECT COUNT(*) AS count FROM ai_construction_tasks
    WHERE project_id = 'project-material' AND task_kind = 'material_change_review'`)
    .get() as any).count, 1);
});

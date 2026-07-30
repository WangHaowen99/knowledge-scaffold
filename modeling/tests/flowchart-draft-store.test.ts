import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { DatabaseSync } from 'node:sqlite';

import {
  FlowchartDraftError,
  FlowchartDraftStore,
} from '../src/flowchart/draft-store.js';

const schema = readFileSync(new URL('../src/db/schema.sql', import.meta.url), 'utf8');

function createDb(): DatabaseSync {
  const db = new DatabaseSync(':memory:');
  db.exec('PRAGMA foreign_keys = ON');
  db.exec(schema);
  db.prepare(`INSERT INTO projects
    (id, owner, name, domain, background, stage, created_at, updated_at)
    VALUES (?, ?, ?, '', '', 'project_setup', ?, ?)`)
    .run('pj_flowchart', 'owner', '流程项目', '2026-07-24T00:00:00.000Z', '2026-07-24T00:00:00.000Z');
  db.prepare(`INSERT INTO authoring_runs
    (id, project_id, intent, status, created_at, updated_at)
    VALUES (?, ?, 'process_review', 'waiting', ?, ?)`)
    .run('run_flowchart', 'pj_flowchart', '2026-07-24T00:00:00.000Z', '2026-07-24T00:00:00.000Z');
  db.prepare(`INSERT INTO authoring_interactions
    (id, project_id, run_id, type, payload_json, created_at)
    VALUES (?, ?, ?, 'flowchart_review', '{"purpose":"process_review"}', ?)`)
    .run('interaction_flowchart', 'pj_flowchart', 'run_flowchart', '2026-07-24T00:00:00.000Z');
  return db;
}

function sampleDocument(label = '审核资料'): unknown {
  return {
    diagrams: [{
      id: 'approval', title: '审批流程',
      nodes: [{ id: 'review', label, kind: 'action' }],
      edges: [],
    }],
  };
}

test('创建草稿时保存不可变的首个修订', () => {
  const store = new FlowchartDraftStore(createDb());

  const created = store.create({
    projectId: 'pj_flowchart',
    interactionId: 'interaction_flowchart',
    document: sampleDocument(),
    actorUsername: 'owner',
  });

  assert.equal(created.revision, 1);
  assert.equal(created.document.diagrams[0].nodes[0].label, '审核资料');
  assert.deepEqual(
    new Set(created.warnings.map((issue) => issue.code)),
    new Set(['missing_start', 'missing_end']),
  );
  assert.equal(created.submittedRevision, undefined);
  assert.deepEqual(store.get('pj_flowchart', 'interaction_flowchart'), created);
  assert.deepEqual(store.getRevision('pj_flowchart', 'interaction_flowchart', 1), created.revisionRecord);
});

test('结构错误不会进入草稿修订', () => {
  const store = new FlowchartDraftStore(createDb());

  assert.throws(
    () => store.create({
      projectId: 'pj_flowchart', interactionId: 'interaction_flowchart', actorUsername: 'owner',
      document: {
        diagrams: [{
          id: 'broken', title: '错误流程',
          nodes: [{ id: 'node', label: '步骤', kind: 'action' }],
          edges: [{ id: 'edge', from: 'node', to: 'missing', kind: 'sequence' }],
        }],
      },
    }),
    (error: unknown) => (
      error instanceof FlowchartDraftError
      && error.code === 'invalid_document'
      && error.issues[0]?.code === 'missing_edge_endpoint'
    ),
  );
  assert.equal(store.get('pj_flowchart', 'interaction_flowchart'), null);
});

test('保存整份文档时检查 expectedRevision 并保留历史修订', () => {
  const store = new FlowchartDraftStore(createDb());
  store.create({
    projectId: 'pj_flowchart', interactionId: 'interaction_flowchart',
    document: sampleDocument(), actorUsername: 'owner',
  });

  const saved = store.save({
    projectId: 'pj_flowchart',
    interactionId: 'interaction_flowchart',
    expectedRevision: 1,
    document: sampleDocument('复核资料'),
    actorUsername: 'editor',
  });

  assert.equal(saved.revision, 2);
  assert.equal(saved.document.diagrams[0].nodes[0].label, '复核资料');
  assert.equal(
    store.getRevision('pj_flowchart', 'interaction_flowchart', 1)?.document.diagrams[0].nodes[0].label,
    '审核资料',
  );
  assert.throws(
    () => store.save({
      projectId: 'pj_flowchart', interactionId: 'interaction_flowchart',
      expectedRevision: 1, document: sampleDocument('过期覆盖'), actorUsername: 'editor',
    }),
    (error: unknown) => error instanceof FlowchartDraftError && error.code === 'conflict',
  );
  assert.equal(store.getRevision('pj_flowchart', 'interaction_flowchart', 3), null);
});

test('提交标记只接受当前已保存修订且重复提交幂等', () => {
  const store = new FlowchartDraftStore(createDb());
  store.create({
    projectId: 'pj_flowchart', interactionId: 'interaction_flowchart',
    document: sampleDocument(), actorUsername: 'owner',
  });

  const submitted = store.markSubmitted({
    projectId: 'pj_flowchart', interactionId: 'interaction_flowchart',
    expectedRevision: 1, actorUsername: 'owner',
  });
  const repeated = store.markSubmitted({
    projectId: 'pj_flowchart', interactionId: 'interaction_flowchart',
    expectedRevision: 1, actorUsername: 'owner',
  });

  assert.equal(submitted.submittedRevision, 1);
  assert.equal(repeated.submittedRevision, 1);
  assert.deepEqual(store.listSubmissions('pj_flowchart', 'interaction_flowchart').map((item) => item.revision), [1]);

  store.save({
    projectId: 'pj_flowchart', interactionId: 'interaction_flowchart', expectedRevision: 1,
    document: sampleDocument('二次修改'), actorUsername: 'owner',
  });
  assert.throws(
    () => store.markSubmitted({
      projectId: 'pj_flowchart', interactionId: 'interaction_flowchart',
      expectedRevision: 1, actorUsername: 'owner',
    }),
    (error: unknown) => error instanceof FlowchartDraftError && error.code === 'conflict',
  );
});

test('非 pending 的流程图确认交互不能创建、保存或提交草稿', () => {
  const createDbInstance = createDb();
  createDbInstance.prepare(`UPDATE authoring_interactions SET status = 'answered'
    WHERE id = 'interaction_flowchart'`).run();
  const createStore = new FlowchartDraftStore(createDbInstance);
  assert.throws(
    () => createStore.create({
      projectId: 'pj_flowchart', interactionId: 'interaction_flowchart',
      document: sampleDocument(), actorUsername: 'owner',
    }),
    (error: unknown) => error instanceof FlowchartDraftError && error.code === 'conflict',
  );

  const db = createDb();
  const store = new FlowchartDraftStore(db);
  store.create({
    projectId: 'pj_flowchart', interactionId: 'interaction_flowchart',
    document: sampleDocument(), actorUsername: 'owner',
  });
  db.prepare(`UPDATE authoring_interactions SET status = 'cancelled'
    WHERE id = 'interaction_flowchart'`).run();

  assert.throws(
    () => store.save({
      projectId: 'pj_flowchart', interactionId: 'interaction_flowchart', expectedRevision: 1,
      document: sampleDocument('已结束后修改'), actorUsername: 'owner',
    }),
    (error: unknown) => error instanceof FlowchartDraftError && error.code === 'conflict',
  );
  assert.throws(
    () => store.markSubmitted({
      projectId: 'pj_flowchart', interactionId: 'interaction_flowchart',
      expectedRevision: 1, actorUsername: 'owner',
    }),
    (error: unknown) => error instanceof FlowchartDraftError && error.code === 'conflict',
  );
});

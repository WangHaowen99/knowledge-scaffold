import assert from 'node:assert/strict';
import test from 'node:test';

import { assertEvaluationDatabaseReady } from '../src/evaluation/eval-db.js';
import type { ProjectDataSource } from '../src/evaluation/data-source-repo.js';

function source(provider: ProjectDataSource['provider']): ProjectDataSource {
  return {
    projectId: 'project-1',
    mode: 'connection',
    provider,
    workspaceId: 'mw-shadow-project-1',
    ontologyId: 'demo',
    config: {},
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-01T00:00:00.000Z',
  };
}

test('NL2SQL evaluation stops when the uploaded workbook cannot be provisioned', () => {
  assert.throws(
    () => assertEvaluationDatabaseReady(
      { ok: false, detail: "评测库落库失败：Access denied for user 'modeling_eval'" },
      source('nl2sql_http'),
    ),
    /评测库准备失败.*Access denied/,
  );
});

test('Neo4j evaluation does not depend on the optional MySQL mirror', () => {
  assert.doesNotThrow(() => assertEvaluationDatabaseReady(
    { ok: false, detail: '未配置评测库地址' },
    source('neo4j'),
  ));
});

import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';

import { buildProjectAuditFiles } from '../src/authoring/audit-export.js';

const schema = readFileSync(new URL('../src/db/schema.sql', import.meta.url), 'utf8');

function dbWithProject() {
  const db = new DatabaseSync(':memory:');
  db.exec('PRAGMA foreign_keys = ON');
  db.exec(schema);
  const now = '2026-07-23T10:00:00.000Z';
  db.prepare(`INSERT INTO projects
    (id, owner, name, domain, background, created_at, updated_at)
    VALUES ('pj_audit', 'alice', '审计项目', '通用业务', '验证审计导出', ?, ?)`)
    .run(now, now);
  db.prepare(`INSERT INTO documents
    (id, project_id, filename, kind, parse_status, text_content, created_at)
    VALUES ('doc_a', 'pj_audit', '材料.md', 'markdown', 'parsed', '材料正文', ?)`)
    .run(now);
  db.prepare(`INSERT INTO project_data_sources
    (project_id, mode, provider, workspace_id, ontology_id, public_config_json, created_at, updated_at)
    VALUES ('pj_audit', 'connection', 'nl2sql_http', 'workspace-a', 'ontology-a', ?, ?, ?)`)
    .run(JSON.stringify({
      url: 'http://provider:5600',
      token: 'must-not-export',
      nested: {
        apiKey: 'hidden',
        authorization: 'Bearer must-not-export',
        cookie: 'session=must-not-export',
        private_key: 'must-not-export',
      },
    }), now, now);
  db.prepare(`INSERT INTO eval_rounds
    (id, project_id, package_version, status, progress_done, progress_total, pass_count,
      phase, log, artifact_revision_oid, created_at, finished_at)
    VALUES ('round_a', 'pj_audit', '1.0.0-rc.1', 'done', 1, 1, 1,
      'done', '评测完成', 'revision-a', ?, ?)`)
    .run(now, now);
  db.prepare(`INSERT INTO eval_results
    (id, round_id, testcase_id, question, ground_truth, final_answer, verdict, reason, created_at)
    VALUES ('result_a', 'round_a', 'case_a', '真实问题', '参考答案', '评测答案', 'pass', '一致', ?)`)
    .run(now);
  db.prepare(`INSERT INTO eval_result_traces
    (result_id, run_id, session_id, tool_calls_json)
    VALUES ('result_a', 'run-a', 'session-a', '[{"tool":"search"}]')`).run();
  return db;
}

test('建模审计包包含项目全链路记录、当前知识包快照和完整性清单', () => {
  const db = dbWithProject();
  const files = buildProjectAuditFiles(db, 'pj_audit', {
    exportedAt: '2026-07-23T11:00:00.000Z',
    packageRevision: 'revision-a',
    packageFiles: {
      'manifest.yaml': 'id: generic-package\n',
      'objects/customer.md': '# 客户\n',
    },
  });

  assert.deepEqual(Object.keys(files).sort(), [
    'audit/integrity.json',
    'audit/manifest.json',
    'audit/records/documents.json',
    'audit/records/eval_result_traces.json',
    'audit/records/eval_results.json',
    'audit/records/eval_rounds.json',
    'audit/records/project.json',
    'audit/records/project_data_sources.json',
    'knowledge-package/manifest.yaml',
    'knowledge-package/objects/customer.md',
  ]);
  const manifest = JSON.parse(files['audit/manifest.json']);
  assert.equal(manifest.project.id, 'pj_audit');
  assert.equal(manifest.packageRevision, 'revision-a');
  assert.deepEqual(manifest.recordCounts, {
    documents: 1,
    eval_result_traces: 1,
    eval_results: 1,
    eval_rounds: 1,
    project_data_sources: 1,
  });
  const integrity = JSON.parse(files['audit/integrity.json']);
  assert.equal(integrity.algorithm, 'sha256');
  assert.equal(integrity.files['knowledge-package/manifest.yaml'], createHash('sha256')
    .update('id: generic-package\n').digest('hex'));
});

test('建模审计包递归脱敏凭据但保留公开连接和评测证据', () => {
  const files = buildProjectAuditFiles(dbWithProject(), 'pj_audit', {
    exportedAt: '2026-07-23T11:00:00.000Z',
  });
  const source = files['audit/records/project_data_sources.json'];
  assert.doesNotMatch(source, /must-not-export|hidden/);
  assert.match(source, /http:\/\/provider:5600/);
  assert.match(source, /\[REDACTED\]/);
  assert.match(files['audit/records/eval_results.json'], /评测答案/);
  assert.match(files['audit/records/eval_result_traces.json'], /search/);
});

test('不存在的项目不能导出建模审计包', () => {
  assert.throws(() => buildProjectAuditFiles(dbWithProject(), 'pj_missing'), /项目不存在/);
});

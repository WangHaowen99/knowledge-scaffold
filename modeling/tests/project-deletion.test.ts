import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-project-deletion-'));

test('project deletion: removes local project data and temporary uploads', async () => {
  const [{ getDb, nowIso }, projects, deletion, uploads] = await Promise.all([
    import('../src/db/index.js'),
    import('../src/projects/repo.js'),
    import('../src/projects/delete-service.js'),
    import('../src/graph-import/upload-store.js'),
  ]);
  const db = getDb();
  const project = projects.createProject({ owner: 'alice', name: 'Delete me' });
  const now = nowIso();

  db.prepare(`INSERT INTO documents (id, project_id, filename, kind, created_at)
    VALUES ('doc-delete', ?, 'source.md', 'markdown', ?)`).run(project.id, now);
  db.prepare(`INSERT INTO wf_nodes (id, project_id, type, ordinal, title, created_at, updated_at)
    VALUES ('node-delete', ?, 'analyze', 0, 'Analyze', ?, ?)`).run(project.id, now, now);
  db.prepare(`INSERT INTO interview_batches
    (id, project_id, node_id, ordinal, questions_json, answered, created_at)
    VALUES ('batch-delete', ?, 'node-delete', 0, '[]', 1, ?)`).run(project.id, now);
  db.prepare(`INSERT INTO interview_answers
    (id, batch_id, question_id, answer_json, created_at)
    VALUES ('answer-delete', 'batch-delete', 'q0', '{}', ?)`).run(now);
  db.prepare(`INSERT INTO competency_questions
    (id, project_id, question, selected, source, created_at)
    VALUES ('cq-delete', ?, 'Question', 1, 'custom', ?)`).run(project.id, now);
  db.prepare(`INSERT INTO package_versions
    (id, project_id, version, spec_json, coverage_json, created_at)
    VALUES ('package-delete', ?, '1.0.0', '{}', '{}', ?)`).run(project.id, now);
  db.prepare(`INSERT INTO project_data_sources
    (project_id, mode, provider, workspace_id, ontology_id, public_config_json, created_at, updated_at)
    VALUES (?, 'none', NULL, 'workspace-delete', NULL, '{}', ?, ?)`).run(project.id, now, now);
  const upload = uploads.saveGraphImportUpload({
    projectId: project.id,
    owner: project.owner,
    filename: 'source.xlsx',
    buffer: Buffer.from('temporary upload'),
  });
  db.prepare(`INSERT INTO graph_import_runs
    (id, project_id, filename, file_hash, package_version, mapping_json, status, created_at)
    VALUES ('graph-run-delete', ?, 'source.xlsx', 'hash', '1.0.0', '{}', 'done', ?)`).run(project.id, now);
  db.prepare(`INSERT INTO evaluation_database_import_runs
    (id, project_id, filename, database_name, status, created_at, finished_at)
    VALUES ('eval-db-import-delete', ?, 'evaluation.xlsx', 'mw_test', 'done', ?, ?)`)
    .run(project.id, now, now);
  db.prepare(`INSERT INTO testcases
    (id, project_id, question, ground_truth, source, created_at)
    VALUES ('tc-delete', ?, 'Question', 'Answer', 'manual', ?)`).run(project.id, now);
  db.prepare(`INSERT INTO eval_rounds
    (id, project_id, package_version, status, created_at)
    VALUES ('round-delete', ?, '1.0.0', 'done', ?)`).run(project.id, now);
  db.prepare(`INSERT INTO eval_results
    (id, round_id, testcase_id, question, ground_truth, created_at)
    VALUES ('result-delete', 'round-delete', 'tc-delete', 'Question', 'Answer', ?)`).run(now);
  db.prepare(`INSERT INTO opt_suggestions
    (id, project_id, round_id, based_version, title, detail, kind, status, created_at)
    VALUES ('suggestion-delete', ?, 'round-delete', '1.0.0', 'Title', 'Detail',
      'clarify_description', 'pending', ?)`).run(project.id, now);
  db.prepare(`INSERT INTO package_checks
    (project_id, version, findings_json, created_at) VALUES (?, '1.0.0', '[]', ?)`).run(project.id, now);
  db.prepare(`INSERT INTO package_overrides
    (project_id, path, content, updated_at) VALUES (?, 'manifest.yaml', 'content', ?)`).run(project.id, now);
  db.prepare(`INSERT INTO settings (key, value, updated_at)
    VALUES ('project-deletion-global', 'keep', ?)`).run(now);

  assert.equal(existsSync(upload.storedPath), true);
  assert.equal(deletion.deleteProjectLocalData(project.id, project.owner), true);
  assert.equal(existsSync(upload.storedPath), false);
  assert.equal(projects.getProject(project.id), null);

  const projectTables = [
    'documents', 'wf_nodes', 'interview_batches', 'competency_questions', 'package_versions',
    'project_data_sources', 'graph_import_uploads', 'graph_import_runs',
    'evaluation_database_import_runs', 'testcases', 'eval_rounds', 'opt_suggestions',
    'package_checks', 'package_overrides',
  ];
  for (const table of projectTables) {
    const row = db.prepare(`SELECT COUNT(*) AS count FROM ${table} WHERE project_id = ?`).get(project.id) as { count: number };
    assert.equal(row.count, 0, `${table} should be deleted`);
  }
  assert.equal((db.prepare('SELECT COUNT(*) AS count FROM interview_answers').get() as any).count, 0);
  assert.equal((db.prepare('SELECT COUNT(*) AS count FROM eval_results').get() as any).count, 0);
  assert.equal((db.prepare("SELECT value FROM settings WHERE key = 'project-deletion-global'").get() as any).value, 'keep');
});

test('project deletion: owner condition prevents deleting another user project', async () => {
  const [projects, deletion] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/projects/delete-service.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: 'Protected' });

  assert.equal(deletion.deleteProjectLocalData(project.id, 'bob'), false);
  assert.equal(projects.getProject(project.id)?.owner, 'alice');
});

test('project deletion: operation registry reports active work by project prefix', async () => {
  const { InFlightOperations } = await import('../src/in-flight-operations.js');
  const operations = new InFlightOperations();
  let finish = () => {};
  const running = operations.run('pj-active:generate', 'generate', () => new Promise<void>((resolve) => { finish = resolve; }));

  assert.equal(operations.currentByPrefix('pj-active:'), 'generate');
  assert.equal(operations.currentByPrefix('pj-other:'), null);

  finish();
  await running;
  assert.equal(operations.currentByPrefix('pj-active:'), null);
});

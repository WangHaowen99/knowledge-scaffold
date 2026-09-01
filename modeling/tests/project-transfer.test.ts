import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';

import { strFromU8, strToU8, unzipSync, zipSync } from 'fflate';

import { IsomorphicGitArtifactRepository } from '../src/artifacts/isomorphic-git-repository.js';
import {
  buildProjectTransferArchive,
  importProjectTransferArchive,
  projectTransferFilename,
  ProjectTransferError,
} from '../src/projects/project-transfer.js';

const SOURCE_PROJECT = 'pj_source_demo';
const SOURCE_OWNER = 'operator_a';
const TARGET_OWNER = 'operator_b';

function createDatabase(path: string): DatabaseSync {
  const db = new DatabaseSync(path);
  db.exec(`PRAGMA foreign_keys = ON;
    CREATE TABLE projects (
      id TEXT PRIMARY KEY,
      owner TEXT NOT NULL,
      kind TEXT NOT NULL,
      runtime TEXT NOT NULL,
      name TEXT NOT NULL,
      stage TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE project_members (
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      username TEXT NOT NULL,
      role TEXT NOT NULL,
      added_by TEXT NOT NULL,
      PRIMARY KEY (project_id, username)
    );
    CREATE TABLE documents (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      source_key TEXT NOT NULL,
      filename TEXT NOT NULL,
      raw_content BLOB,
      created_at TEXT NOT NULL
    );
    CREATE TABLE document_notes (
      id TEXT PRIMARY KEY,
      document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      actor_username TEXT NOT NULL,
      payload_json TEXT NOT NULL
    );
    CREATE TABLE artifact_revisions (
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      oid TEXT NOT NULL,
      sequence INTEGER NOT NULL,
      PRIMARY KEY (project_id, oid)
    );
    CREATE TABLE graph_import_uploads (
      id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      owner TEXT NOT NULL,
      filename TEXT NOT NULL,
      stored_path TEXT NOT NULL,
      file_hash TEXT NOT NULL,
      size_bytes INTEGER NOT NULL,
      expires_at TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE project_data_sources (
      project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
      public_config_json TEXT NOT NULL
    );
    CREATE TABLE settings (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );`);
  return db;
}

async function createSourceFixture(root: string): Promise<{ db: DatabaseSync; graphContent: Buffer }> {
  const dataDir = join(root, 'source-data');
  await mkdir(dataDir, { recursive: true });
  const db = createDatabase(join(dataDir, 'source.db'));
  const createdAt = '2026-01-02T03:04:05.000Z';
  db.prepare(`INSERT INTO projects
    (id, owner, kind, runtime, name, stage, created_at, updated_at)
    VALUES (?, ?, 'modeling', 'agent', 'project_alpha', 'review', ?, ?)`)
    .run(SOURCE_PROJECT, SOURCE_OWNER, createdAt, createdAt);
  db.prepare(`INSERT INTO project_members (project_id, username, role, added_by)
    VALUES (?, 'operator_c', 'viewer', ?)`).run(SOURCE_PROJECT, SOURCE_OWNER);
  db.prepare(`INSERT INTO documents (id, project_id, source_key, filename, raw_content, created_at)
    VALUES ('doc_source', ?, 'doc_source', 'synthetic.txt', ?, ?)`)
    .run(SOURCE_PROJECT, Buffer.from([0, 1, 2, 255]), createdAt);
  db.prepare(`INSERT INTO document_notes (id, document_id, actor_username, payload_json)
    VALUES ('note_source', 'doc_source', ?, ?)`)
    .run(SOURCE_OWNER, JSON.stringify({ projectId: SOURCE_PROJECT, documentId: 'doc_source' }));
  db.prepare(`INSERT INTO project_data_sources (project_id, public_config_json)
    VALUES (?, '{"endpoint":"<redacted>"}')`).run(SOURCE_PROJECT);
  db.prepare(`INSERT INTO settings (key, value, updated_at) VALUES (?, '1', ?)`)
    .run(`semantic_binding_ignored:${SOURCE_PROJECT}:revision_a`, createdAt);
  db.prepare(`INSERT INTO settings (key, value, updated_at) VALUES ('consumer_api_url', '<redacted>', ?)`)
    .run(createdAt);

  const artifacts = new IsomorphicGitArtifactRepository({ rootDir: join(dataDir, 'artifacts') });
  const initial = await artifacts.initialize(SOURCE_PROJECT);
  const current = await artifacts.commit(SOURCE_PROJECT, initial.oid, [
    { kind: 'write', path: 'package.json', content: '{"name":"synthetic"}\n' },
  ], { message: '保存合成知识包' });
  db.prepare(`INSERT INTO artifact_revisions (project_id, oid, sequence) VALUES (?, ?, 1)`)
    .run(SOURCE_PROJECT, initial.oid);
  db.prepare(`INSERT INTO artifact_revisions (project_id, oid, sequence) VALUES (?, ?, 2)`)
    .run(SOURCE_PROJECT, current.oid);

  const graphContent = Buffer.from('synthetic graph upload');
  const graphPath = join(dataDir, 'graph-source.xlsx');
  await writeFile(graphPath, graphContent);
  db.prepare(`INSERT INTO graph_import_uploads
    (id, project_id, owner, filename, stored_path, file_hash, size_bytes, expires_at, created_at)
    VALUES ('giu_source', ?, ?, 'synthetic.xlsx', ?, ?, ?, ?, ?)`)
    .run(
      SOURCE_PROJECT,
      SOURCE_OWNER,
      graphPath,
      createHash('sha256').update(graphContent).digest('hex'),
      graphContent.byteLength,
      '2026-01-02T04:04:05.000Z',
      createdAt,
    );
  return { db, graphContent };
}

test('项目迁移包完整往返、跨账号导入并支持重复导入', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'project-transfer-test-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const source = await createSourceFixture(root);
  t.after(() => source.db.close());
  const archive = await buildProjectTransferArchive({
    db: source.db,
    dataDir: join(root, 'source-data'),
    projectId: SOURCE_PROJECT,
    owner: SOURCE_OWNER,
  });

  const archiveEntries = unzipSync(archive);
  const databaseText = strFromU8(archiveEntries['database.json']);
  assert.doesNotMatch(databaseText, new RegExp(SOURCE_PROJECT));
  assert.doesNotMatch(databaseText, new RegExp(SOURCE_OWNER));
  assert.doesNotMatch(databaseText, /consumer_api_url/);
  assert.doesNotMatch(databaseText, /project_data_sources/);
  assert.doesNotMatch(databaseText, /project_members/);

  const targetDataDir = join(root, 'target-data');
  await mkdir(targetDataDir, { recursive: true });
  const target = createDatabase(join(targetDataDir, 'target.db'));
  t.after(() => target.close());

  const first = await importProjectTransferArchive({
    db: target,
    dataDir: targetDataDir,
    archive,
    owner: TARGET_OWNER,
  });
  const second = await importProjectTransferArchive({
    db: target,
    dataDir: targetDataDir,
    archive,
    owner: TARGET_OWNER,
  });

  assert.notEqual(first.projectId, SOURCE_PROJECT);
  assert.notEqual(second.projectId, first.projectId);
  assert.deepEqual(
    target.prepare('SELECT owner, name FROM projects ORDER BY rowid').all()
      .map((row) => ({ ...row })),
    [
      { owner: TARGET_OWNER, name: 'project_alpha' },
      { owner: TARGET_OWNER, name: 'project_alpha' },
    ],
  );
  assert.equal(target.prepare('SELECT COUNT(*) AS count FROM project_members').get()!.count, 0);
  assert.equal(target.prepare('SELECT COUNT(*) AS count FROM project_data_sources').get()!.count, 0);

  const documents = target.prepare(`SELECT id, project_id, source_key, raw_content
    FROM documents ORDER BY rowid`).all() as Array<Record<string, unknown>>;
  assert.equal(documents.length, 2);
  assert.notEqual(documents[0].id, 'doc_source');
  assert.equal(documents[0].project_id, first.projectId);
  assert.equal(documents[0].source_key, documents[0].id);
  assert.deepEqual(Buffer.from(documents[0].raw_content as Uint8Array), Buffer.from([0, 1, 2, 255]));

  const notes = target.prepare(`SELECT document_id, actor_username, payload_json
    FROM document_notes ORDER BY rowid`).all() as Array<Record<string, unknown>>;
  assert.equal(notes[0].actor_username, TARGET_OWNER);
  assert.equal(JSON.parse(String(notes[0].payload_json)).projectId, first.projectId);
  assert.equal(JSON.parse(String(notes[0].payload_json)).documentId, documents[0].id);

  const uploads = target.prepare(`SELECT project_id, owner, stored_path, file_hash
    FROM graph_import_uploads ORDER BY rowid`).all() as Array<Record<string, unknown>>;
  assert.equal(uploads.length, 2);
  assert.equal(uploads[0].project_id, first.projectId);
  assert.equal(uploads[0].owner, TARGET_OWNER);
  assert.deepEqual(await readFile(String(uploads[0].stored_path)), source.graphContent);
  assert.equal(
    createHash('sha256').update(await readFile(String(uploads[0].stored_path))).digest('hex'),
    uploads[0].file_hash,
  );
  assert.equal(
    await readFile(join(targetDataDir, 'artifacts', first.projectId, 'package.json'), 'utf8'),
    '{"name":"synthetic"}\n',
  );
  const importedArtifacts = new IsomorphicGitArtifactRepository({ rootDir: join(targetDataDir, 'artifacts') });
  assert.equal((await importedArtifacts.history(first.projectId)).length, 2);
  const initialRevision = (await importedArtifacts.history(first.projectId))[1];
  assert.equal((await importedArtifacts.snapshot(first.projectId, initialRevision.oid)).files.length, 0);
  assert.equal(
    target.prepare(`SELECT COUNT(*) AS count FROM settings
      WHERE key LIKE 'semantic_binding_ignored:%'`).get()!.count,
    2,
  );
});

test('迁移包内容损坏时拒绝导入', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'project-transfer-corrupt-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const source = await createSourceFixture(root);
  t.after(() => source.db.close());
  const archive = await buildProjectTransferArchive({
    db: source.db,
    dataDir: join(root, 'source-data'),
    projectId: SOURCE_PROJECT,
    owner: SOURCE_OWNER,
  });
  const entries = unzipSync(archive);
  entries['database.json'] = strToU8('{}');
  const corrupted = zipSync(entries);

  const targetDataDir = join(root, 'target-data');
  await mkdir(targetDataDir, { recursive: true });
  const target = createDatabase(join(targetDataDir, 'target.db'));
  t.after(() => target.close());
  await assert.rejects(
    importProjectTransferArchive({ db: target, dataDir: targetDataDir, archive: corrupted, owner: TARGET_OWNER }),
    (error: unknown) => error instanceof ProjectTransferError && /校验失败/.test(error.message),
  );
  assert.equal(target.prepare('SELECT COUNT(*) AS count FROM projects').get()!.count, 0);
});

test('数据库恢复失败时回滚项目并清理已恢复文件', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'project-transfer-rollback-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const source = await createSourceFixture(root);
  t.after(() => source.db.close());
  const archive = await buildProjectTransferArchive({
    db: source.db,
    dataDir: join(root, 'source-data'),
    projectId: SOURCE_PROJECT,
    owner: SOURCE_OWNER,
    agentRuntime: {
      format: 'modeling-agent-project-transfer',
      version: 1,
      sourceProjectId: 'pj_source',
      tables: {},
      checkpoints: [],
    },
  });

  const targetDataDir = join(root, 'target-data');
  await mkdir(targetDataDir, { recursive: true });
  const target = createDatabase(join(targetDataDir, 'target.db'));
  t.after(() => target.close());
  target.exec(`CREATE TRIGGER reject_documents BEFORE INSERT ON documents
    BEGIN SELECT RAISE(ABORT, 'synthetic rejection'); END;`);

  let importedAgentProject = '';
  let removedAgentProject = '';
  await assert.rejects(
    importProjectTransferArchive({
      db: target,
      dataDir: targetDataDir,
      archive,
      owner: TARGET_OWNER,
      importAgentRuntime: async (projectId, payload) => {
        importedAgentProject = projectId;
        assert.equal(payload.format, 'modeling-agent-project-transfer');
      },
      removeAgentRuntime: async (projectId) => { removedAgentProject = projectId; },
    }),
    /synthetic rejection/,
  );
  assert.match(importedAgentProject, /^pj_/);
  assert.equal(removedAgentProject, importedAgentProject);
  assert.equal(target.prepare('SELECT COUNT(*) AS count FROM projects').get()!.count, 0);
  await assert.rejects(readFile(join(targetDataDir, 'artifacts', 'missing', 'package.json')));
  const artifactRoot = join(targetDataDir, 'artifacts');
  await assert.rejects(readFile(artifactRoot));
});

test('迁移包文件名过滤路径字符', () => {
  assert.equal(projectTransferFilename(' project / alpha '), 'project-alpha.project-transfer.zip');
});

test('当前完整数据库结构可以导出并导入空项目', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'project-transfer-schema-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const schema = await readFile(new URL('../src/db/schema.sql', import.meta.url), 'utf8');
  const sourceDir = join(root, 'source-data');
  const targetDir = join(root, 'target-data');
  await mkdir(sourceDir, { recursive: true });
  await mkdir(targetDir, { recursive: true });
  const source = new DatabaseSync(join(sourceDir, 'source.db'));
  const target = new DatabaseSync(join(targetDir, 'target.db'));
  t.after(() => source.close());
  t.after(() => target.close());
  source.exec(`PRAGMA foreign_keys = ON; ${schema}`);
  target.exec(`PRAGMA foreign_keys = ON; ${schema}`);
  const createdAt = '2026-01-02T03:04:05.000Z';
  source.prepare(`INSERT INTO projects
    (id, owner, kind, runtime, name, domain, background, target_users, glossary_prefs,
     sensitivity_level, workflow_profile_json, stage, created_at, updated_at)
    VALUES (?, ?, 'modeling', 'agent', 'project_alpha', '', '', NULL, NULL,
      'standard', '{}', 'project_setup', ?, ?)`)
    .run(SOURCE_PROJECT, SOURCE_OWNER, createdAt, createdAt);
  source.prepare(`INSERT INTO documents
    (id, project_id, source_key, revision_no, filename, kind, purpose, reading_mode,
     directive_revision, parse_status, raw_content, content_hash, semantic_hash,
     lifecycle_status, created_at)
    VALUES ('doc_source', ?, 'doc_source', 1, 'synthetic.txt', 'txt', 'auto', 'on_demand',
      0, 'parsed', ?, 'hash_a', 'hash_b', 'active', ?)`)
    .run(SOURCE_PROJECT, Buffer.from('synthetic material'), createdAt);
  source.prepare(`INSERT INTO agent_candidate_workspaces
    (project_id, run_id, owner, base_revision_oid, package_data_source_type, candidate_id,
     lease_expires_at, credential_hash, credential_expires_at, created_at, updated_at)
    VALUES (?, 'run_source', ?, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'neo4j', NULL,
      '2099-01-01T00:00:00.000Z', 'abcdef', '2099-01-01T00:00:00.000Z', ?, ?)`)
    .run(SOURCE_PROJECT, SOURCE_OWNER, createdAt, createdAt);

  const archive = await buildProjectTransferArchive({
    db: source,
    dataDir: sourceDir,
    projectId: SOURCE_PROJECT,
    owner: SOURCE_OWNER,
    agentRuntime: {
      format: 'modeling-agent-project-transfer',
      version: 1,
      sourceProjectId: 'pj_source',
      tables: {},
      checkpoints: [],
    },
  });
  const imported = await importProjectTransferArchive({
    db: target,
    dataDir: targetDir,
    archive,
    owner: TARGET_OWNER,
    importAgentRuntime: async () => ({ run_source: 'run_target' }),
  });
  assert.equal(target.prepare('SELECT owner FROM projects WHERE id = ?').get(imported.projectId)!.owner, TARGET_OWNER);
  assert.equal(target.prepare('SELECT COUNT(*) AS count FROM documents WHERE project_id = ?')
    .get(imported.projectId)!.count, 1);
  assert.deepEqual({ ...target.prepare(`SELECT run_id, owner, lease_expires_at, credential_hash,
    credential_expires_at FROM agent_candidate_workspaces WHERE project_id = ?`).get(imported.projectId)! }, {
    run_id: 'run_target',
    owner: TARGET_OWNER,
    lease_expires_at: '1970-01-01T00:00:00.000Z',
    credential_hash: null,
    credential_expires_at: null,
  });
});

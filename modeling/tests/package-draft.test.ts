import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import type { DataDictTable, PackageSpec } from '../src/types.js';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-package-draft-'));

function packageSpec(version: string): PackageSpec {
  return {
    id: 'draft-demo',
    name: '草稿演示',
    description: '草稿后端测试知识包',
    version,
    language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects: [{
      key: 'Order',
      title: '订单',
      description: '订单对象',
      sourceTable: 'Orders',
      primaryKey: ['id'],
      fields: [{ key: 'id', cnName: '订单ID', type: 'string', required: true, sourceColumn: 'order_id' }],
    }],
    operations: [{
      key: 'query_order',
      title: '查询订单',
      description: '按订单ID查询订单',
      inputs: [{ param: 'id', cnName: '订单ID', type: 'string', required: true }],
      outputs: [{ field: 'order', cnName: '订单', type: 'object' }],
      flow: ['根据 id 查询 object:Order'],
      relatedObjects: ['Order'],
      usesRules: ['active_order'],
      usesMetrics: ['order_count'],
      competencyQuestion: '如何查询有效订单？',
    }],
    rules: [{
      key: 'active_order',
      title: '有效订单',
      description: '判断订单是否有效',
      appliesToObjects: ['Order'],
      content: '订单状态为有效时属于有效订单。',
      conditions: [{ condition: '状态为有效', conclusion: '有效订单' }],
      exceptions: ['已删除订单除外'],
    }],
    metrics: [{
      key: 'order_count',
      title: '订单数',
      description: '有效订单数量',
      usesObjects: ['Order'],
      caliber: '统计有效订单记录数。',
      boundaries: ['仅统计未删除订单。'],
      nullPolicy: [{ field: 'id', handling: '忽略空主键记录' }],
      dimensions: [{ key: 'status', note: '按订单状态拆分' }],
    }],
  };
}

function dataTables(): DataDictTable[] {
  return [{
    name: 'Orders',
    fields: [{
      name: 'order_id',
      inferredType: 'string',
      sampleValues: ['O-1'],
      distinctRatio: 1,
    }],
    rowCount: 1,
  }];
}

test('package draft parser: round-trips every editable detail format without changing rendered output', async () => {
  const [{ applyPackageDraftFiles }, { renderPackage }] = await Promise.all([
    import('../src/workflow/package-draft-parser.js'),
    import('../src/workflow/package-render.js'),
  ]);
  const spec = packageSpec('1.0.0');
  const rendered = renderPackage(spec);
  const editablePaths = [
    'objects/Order.md',
    'operations/query_order.md',
    'rules/active_order.md',
    'metrics/order_count.md',
  ];
  const parsed = applyPackageDraftFiles(spec, editablePaths.map((path) => ({
    path,
    content: rendered[path],
  })));
  assert.deepEqual(renderPackage(parsed), rendered);
  assert.equal(parsed.objects[0].fields[0].sourceColumn, 'order_id');
  assert.deepEqual(parsed.objects[0].primaryKey, ['id']);
});

test('package draft: creates one active draft from the latest package version and restores it', async () => {
  const [projects, versions, drafts, dbModule] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/m2-repo.js'),
    import('../src/workflow/package-draft.js'),
    import('../src/db/index.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: 'Draft create' });

  assert.throws(
    () => drafts.createOrGetPackageDraft(project.id, 'alice'),
    (error: unknown) => error instanceof drafts.PackageDraftError
      && error.code === 'PACKAGE_DRAFT_BASE_VERSION_REQUIRED',
  );

  versions.insertPackageVersion(project.id, '1.0.0', packageSpec('1.0.0'), { covered: [], uncovered: [] });
  const created = drafts.createOrGetPackageDraft(project.id, 'alice');
  assert.equal(created.created, true);
  assert.equal(created.draft.baseVersion, '1.0.0');
  assert.equal(created.draft.revision, 0);
  assert.equal(created.draft.changedFileCount, 0);
  assert.equal(created.draft.stale, false);

  const restored = drafts.createOrGetPackageDraft(project.id, 'alice');
  assert.equal(restored.created, false);
  assert.equal(restored.draft.id, created.draft.id);
  assert.throws(
    () => drafts.publishPackageDraft({
      projectId: project.id,
      expectedRevision: 0,
      tables: dataTables(),
      selectedQuestions: [],
    }),
    (error: unknown) => error instanceof drafts.PackageDraftError
      && error.code === 'PACKAGE_DRAFT_NO_CHANGES',
  );
  assert.equal(
    (dbModule.getDb().prepare("SELECT COUNT(*) AS count FROM package_drafts WHERE project_id = ? AND status = 'active'")
      .get(project.id) as { count: number }).count,
    1,
  );
});

test('legacy override migration: publishes editable overrides as one formal version and is idempotent', async () => {
  const [projects, versions, drafts, migrations, documents, renderer, dbModule] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/m2-repo.js'),
    import('../src/workflow/package-draft.js'),
    import('../src/workflow/package-override-migration.js'),
    import('../src/documents/repo.js'),
    import('../src/workflow/package-render.js'),
    import('../src/db/index.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: 'Legacy override publish' });
  versions.insertPackageVersion(project.id, '1.0.0', packageSpec('1.0.0'), { covered: [], uncovered: [] });
  documents.insertDocument({
    projectId: project.id,
    filename: 'orders.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: dataTables(),
  });
  const rendered = renderer.renderPackage(packageSpec('1.0.0'));
  const changed = rendered['operations/query_order.md']
    .replaceAll('按订单ID查询订单', '按订单编号查询最新订单');
  dbModule.getDb().prepare(`INSERT INTO package_overrides
    (project_id, path, content, updated_at) VALUES (?,?,?,?)`)
    .run(project.id, 'operations/query_order.md', changed, '2026-07-29T10:00:00.000Z');

  const summary = migrations.migrateLegacyPackageOverrides();
  assert.equal(summary.projectCount, 1);
  assert.equal(summary.publishedCount, 1);
  assert.equal(summary.projects[0].version, '1.1.0');
  assert.equal(drafts.getActivePackageDraft(project.id), null);
  assert.equal(versions.latestPackageVersion(project.id)?.spec.operations[0].description, '按订单编号查询最新订单');
  assert.equal((dbModule.getDb().prepare(
    'SELECT COUNT(*) AS count FROM package_overrides WHERE project_id = ?',
  ).get(project.id) as { count: number }).count, 0);
  const archive = dbModule.getDb().prepare(`SELECT disposition, base_version, target_version
    FROM package_override_migration_archive WHERE project_id = ?`).get(project.id) as any;
  assert.deepEqual({ ...archive }, { disposition: 'published', base_version: '1.0.0', target_version: '1.1.0' });
  assert.equal(migrations.migrateLegacyPackageOverrides().projectCount, 0);
});

test('legacy override migration: keeps derived-file changes in a review draft without runtime overlay', async () => {
  const [projects, versions, drafts, migrations, renderer, dbModule] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/m2-repo.js'),
    import('../src/workflow/package-draft.js'),
    import('../src/workflow/package-override-migration.js'),
    import('../src/workflow/package-render.js'),
    import('../src/db/index.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: 'Legacy derived override' });
  versions.insertPackageVersion(project.id, '1.0.0', packageSpec('1.0.0'), { covered: [], uncovered: [] });
  const rendered = renderer.renderPackage(packageSpec('1.0.0'));
  const changedManifest = rendered['manifest.yaml'].replace('name: 草稿演示', 'name: 旧版人工名称');
  dbModule.getDb().prepare(`INSERT INTO package_overrides
    (project_id, path, content, updated_at) VALUES (?,?,?,?)`)
    .run(project.id, 'manifest.yaml', changedManifest, '2026-07-29T11:00:00.000Z');

  const summary = migrations.migrateLegacyPackageOverrides();
  assert.equal(summary.pendingReviewCount, 1);
  assert.deepEqual(summary.projects[0].unsupportedPaths, ['manifest.yaml']);
  const draft = drafts.getActivePackageDraft(project.id)!;
  assert.equal(draft.createdBy, 'legacy-override-migration');
  assert.equal(draft.changedFileCount, 1);
  assert.equal(drafts.getPackageDraftFile(project.id, 'manifest.yaml').content, changedManifest);
  assert.equal(versions.listPackageVersions(project.id).length, 1);
  assert.equal((dbModule.getDb().prepare(
    'SELECT COUNT(*) AS count FROM package_overrides WHERE project_id = ?',
  ).get(project.id) as { count: number }).count, 0);
  assert.match((dbModule.getDb().prepare(
    'SELECT detail FROM package_override_migration_archive WHERE project_id = ?',
  ).get(project.id) as { detail: string }).detail, /派生文件/);

  const reverted = drafts.revertPackageDraftFile({
    projectId: project.id,
    path: 'manifest.yaml',
    expectedRevision: draft.revision,
  });
  assert.equal(reverted.draft.changedFileCount, 0);
  drafts.discardPackageDraft({ projectId: project.id, expectedRevision: reverted.draft.revision });
});

test('legacy override migration: archives orphaned overrides without creating a draft', async () => {
  const [projects, migrations, drafts, dbModule] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/package-override-migration.js'),
    import('../src/workflow/package-draft.js'),
    import('../src/db/index.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: 'Legacy orphan override' });
  dbModule.getDb().prepare(`INSERT INTO package_overrides
    (project_id, path, content, updated_at) VALUES (?,?,?,?)`)
    .run(project.id, 'objects/Missing.md', 'orphaned content', '2026-07-29T12:00:00.000Z');

  const summary = migrations.migrateLegacyPackageOverrides();
  assert.equal(summary.archivedCount, 1);
  assert.equal(drafts.getActivePackageDraft(project.id), null);
  const archive = dbModule.getDb().prepare(`SELECT content, disposition
    FROM package_override_migration_archive WHERE project_id = ?`).get(project.id) as any;
  assert.deepEqual({ ...archive }, { content: 'orphaned content', disposition: 'orphaned-no-package-version' });
  assert.equal((dbModule.getDb().prepare(
    'SELECT COUNT(*) AS count FROM package_overrides WHERE project_id = ?',
  ).get(project.id) as { count: number }).count, 0);
});

test('package draft: saves files with optimistic revision checks and ignores idempotent saves', async () => {
  const [projects, versions, drafts] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/m2-repo.js'),
    import('../src/workflow/package-draft.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: 'Draft revision' });
  versions.insertPackageVersion(project.id, '1.0.0', packageSpec('1.0.0'), { covered: [], uncovered: [] });
  drafts.createOrGetPackageDraft(project.id, 'alice');

  const initial = drafts.getPackageDraftFile(project.id, 'operations/query_order.md');
  assert.equal(initial.changed, false);
  assert.equal(initial.draftRevision, 0);

  const changedContent = initial.content.replace('按订单ID查询订单', '按订单编号查询最新订单');
  const saved = drafts.savePackageDraftFile({
    projectId: project.id,
    path: initial.path,
    content: changedContent,
    expectedRevision: 0,
    expectedContentHash: initial.contentHash,
  });
  assert.equal(saved.draft.revision, 1);
  assert.equal(saved.draft.changedFileCount, 1);
  assert.equal(saved.file.changed, true);
  assert.equal(saved.file.content, changedContent);
  assert.notEqual(saved.file.contentHash, saved.file.baseContentHash);

  const idempotent = drafts.savePackageDraftFile({
    projectId: project.id,
    path: initial.path,
    content: changedContent,
    expectedRevision: 1,
  });
  assert.equal(idempotent.draft.revision, 1);

  assert.throws(
    () => drafts.savePackageDraftFile({
      projectId: project.id,
      path: initial.path,
      content: `${changedContent}\n过期页面覆盖`,
      expectedRevision: 0,
      expectedContentHash: initial.contentHash,
    }),
    (error: unknown) => error instanceof drafts.PackageDraftError
      && error.code === 'PACKAGE_DRAFT_REVISION_CONFLICT'
      && error.details.currentRevision === 1
      && error.details.conflictScope === 'file'
      && error.details.currentContentHash === saved.file.contentHash,
  );

  assert.throws(
    () => drafts.getPackageDraftFile(project.id, 'operations/missing.md'),
    (error: unknown) => error instanceof drafts.PackageDraftError
      && error.code === 'PACKAGE_DRAFT_FILE_NOT_FOUND',
  );
});

test('package draft: stale pages cannot write into a newly created draft with a reused revision', async () => {
  const [projects, versions, drafts] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/m2-repo.js'),
    import('../src/workflow/package-draft.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: 'Draft identity' });
  versions.insertPackageVersion(project.id, '1.0.0', packageSpec('1.0.0'), { covered: [], uncovered: [] });
  const first = drafts.createOrGetPackageDraft(project.id, 'alice').draft;
  drafts.discardPackageDraft({
    projectId: project.id,
    expectedRevision: first.revision,
    expectedDraftId: first.id,
  });

  const second = drafts.createOrGetPackageDraft(project.id, 'alice').draft;
  assert.equal(second.revision, first.revision);
  assert.notEqual(second.id, first.id);
  const file = drafts.getPackageDraftFile(project.id, 'operations/query_order.md');
  assert.throws(
    () => drafts.savePackageDraftFile({
      projectId: project.id,
      path: file.path,
      content: `${file.content}\n旧页面修改`,
      expectedRevision: first.revision,
      expectedContentHash: file.contentHash,
      expectedDraftId: first.id,
    }),
    (error: unknown) => error instanceof drafts.PackageDraftError
      && error.code === 'PACKAGE_DRAFT_REVISION_CONFLICT'
      && error.details.conflictScope === 'draft'
      && error.details.expectedDraftId === first.id
      && error.details.currentDraftId === second.id,
  );
});

test('package draft: tracks multiple files and removes entries restored to their base content', async () => {
  const [projects, versions, drafts] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/m2-repo.js'),
    import('../src/workflow/package-draft.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: 'Draft files' });
  versions.insertPackageVersion(project.id, '1.0.0', packageSpec('1.0.0'), { covered: [], uncovered: [] });
  drafts.createOrGetPackageDraft(project.id, 'alice');

  const operation = drafts.getPackageDraftFile(project.id, 'operations/query_order.md');
  const object = drafts.getPackageDraftFile(project.id, 'objects/Order.md');
  const first = drafts.savePackageDraftFile({
    projectId: project.id,
    path: operation.path,
    content: `${operation.content}\n人工补充`,
    expectedRevision: 0,
    expectedContentHash: operation.contentHash,
  });
  assert.equal(first.draft.revision, 1);
  const second = drafts.savePackageDraftFile({
    projectId: project.id,
    path: object.path,
    content: `${object.content}\n人工补充`,
    expectedRevision: 0,
    expectedContentHash: object.contentHash,
  });
  assert.equal(second.draft.revision, 2);
  assert.deepEqual(second.draft.changedFiles.map((file) => file.path), [
    'objects/Order.md',
    'operations/query_order.md',
  ]);

  const reverted = drafts.revertPackageDraftFile({
    projectId: project.id,
    path: operation.path,
    expectedRevision: second.draft.revision,
  });
  assert.equal(reverted.draft.revision, 3);
  assert.equal(reverted.file.changed, false);
  assert.deepEqual(reverted.draft.changedFiles.map((file) => file.path), ['objects/Order.md']);

  const restoredBySave = drafts.savePackageDraftFile({
    projectId: project.id,
    path: object.path,
    content: object.baseContent,
    expectedRevision: reverted.draft.revision,
  });
  assert.equal(restoredBySave.draft.revision, 4);
  assert.equal(restoredBySave.draft.changedFileCount, 0);
  assert.equal(restoredBySave.file.changed, false);
});

test('package draft: marks an outdated base and allows a new draft after explicit discard', async () => {
  const [projects, versions, drafts, dbModule] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/m2-repo.js'),
    import('../src/workflow/package-draft.js'),
    import('../src/db/index.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: 'Draft discard' });
  versions.insertPackageVersion(project.id, '1.0.0', packageSpec('1.0.0'), { covered: [], uncovered: [] });
  const original = drafts.createOrGetPackageDraft(project.id, 'alice').draft;

  const nextVersionId = versions.insertPackageVersion(
    project.id,
    '1.1.0',
    packageSpec('1.1.0'),
    { covered: [], uncovered: [] },
  );
  dbModule.getDb().prepare("UPDATE package_versions SET created_at = '9999-01-01T00:00:00.000Z' WHERE id = ?")
    .run(nextVersionId);
  assert.equal(drafts.getActivePackageDraft(project.id)?.stale, true);

  assert.throws(
    () => drafts.discardPackageDraft({ projectId: project.id, expectedRevision: 1 }),
    (error: unknown) => error instanceof drafts.PackageDraftError
      && error.code === 'PACKAGE_DRAFT_REVISION_CONFLICT',
  );
  const discarded = drafts.discardPackageDraft({ projectId: project.id, expectedRevision: 0 });
  assert.equal(discarded.status, 'discarded');
  assert.equal(discarded.revision, 1);
  assert.equal(drafts.getActivePackageDraft(project.id), null);

  const replacement = drafts.createOrGetPackageDraft(project.id, 'alice');
  assert.equal(replacement.created, true);
  assert.notEqual(replacement.draft.id, original.id);
  assert.equal(replacement.draft.baseVersion, '1.1.0');
  assert.equal(replacement.draft.stale, false);
});

test('package draft: publishes multiple editable detail files as one validated package version', async () => {
  const [projects, versions, drafts, dbModule] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/m2-repo.js'),
    import('../src/workflow/package-draft.js'),
    import('../src/db/index.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: 'Draft publish' });
  versions.insertPackageVersion(project.id, '1.0.0', packageSpec('1.0.0'), { covered: [], uncovered: [] });
  drafts.createOrGetPackageDraft(project.id, 'alice');

  const edits = [
    {
      path: 'objects/Order.md',
      edit: (content: string) => content.replace('title: 订单', 'title: 客户订单'),
    },
    {
      path: 'operations/query_order.md',
      edit: (content: string) => content.replaceAll('按订单ID查询订单', '按订单ID查询最新有效订单'),
    },
    {
      path: 'rules/active_order.md',
      edit: (content: string) => content.replace('订单状态为有效时属于有效订单。', '订单状态为有效且未删除时属于有效订单。'),
    },
    {
      path: 'metrics/order_count.md',
      edit: (content: string) => content.replace('统计有效订单记录数。', '统计有效且未删除的订单记录数。'),
    },
  ];
  let revision = 0;
  for (const edit of edits) {
    const file = drafts.getPackageDraftFile(project.id, edit.path);
    const saved = drafts.savePackageDraftFile({
      projectId: project.id,
      path: edit.path,
      content: edit.edit(file.content),
      expectedRevision: revision,
    });
    revision = saved.draft.revision;
  }

  const published = drafts.publishPackageDraft({
    projectId: project.id,
    expectedRevision: revision,
    tables: dataTables(),
    selectedQuestions: ['如何查询有效订单？'],
    summary: '同步修订对象、操作、规则和指标',
  });
  assert.equal(published.version, '1.1.0');
  assert.equal(published.draft.status, 'published');
  assert.equal(published.draft.revision, revision + 1);
  assert.deepEqual(published.changedFiles, edits.map((edit) => edit.path).sort());
  assert.deepEqual(published.coverage, {
    covered: [{ question: '如何查询有效订单？', operation: 'query_order' }],
    uncovered: [],
  });
  assert.equal(versions.listPackageVersions(project.id).length, 2);
  assert.equal(drafts.getActivePackageDraft(project.id), null);

  const stored = versions.getPackageVersion(project.id, published.version)!;
  assert.equal(stored.spec.objects[0].title, '客户订单');
  assert.equal(stored.spec.objects[0].fields[0].sourceColumn, 'order_id');
  assert.equal(stored.spec.operations[0].description, '按订单ID查询最新有效订单');
  assert.equal(stored.spec.rules?.[0].content, '订单状态为有效且未删除时属于有效订单。');
  assert.equal(stored.spec.metrics?.[0].caliber, '统计有效且未删除的订单记录数。');
  const draftRow = dbModule.getDb().prepare('SELECT status, revision FROM package_drafts WHERE id = ?')
    .get(published.draft.id) as { status: string; revision: number };
  assert.equal(draftRow.status, 'published');
  assert.equal(draftRow.revision, revision + 1);
});

test('package draft: rejects derived files and keeps the draft active without creating a version', async () => {
  const [projects, versions, drafts] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/m2-repo.js'),
    import('../src/workflow/package-draft.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: 'Draft derived file' });
  versions.insertPackageVersion(project.id, '1.0.0', packageSpec('1.0.0'), { covered: [], uncovered: [] });
  drafts.createOrGetPackageDraft(project.id, 'alice');
  const manifest = drafts.getPackageDraftFile(project.id, 'manifest.yaml');
  drafts.savePackageDraftFile({
    projectId: project.id,
    path: manifest.path,
    content: manifest.content.replace('name: 草稿演示', 'name: 人工改名'),
    expectedRevision: 0,
  });

  assert.throws(
    () => drafts.publishPackageDraft({
      projectId: project.id,
      expectedRevision: 1,
      tables: dataTables(),
      selectedQuestions: [],
    }),
    (error: unknown) => error instanceof drafts.PackageDraftError
      && error.code === 'PACKAGE_DRAFT_FILE_NOT_PUBLISHABLE'
      && error.details.path === 'manifest.yaml',
  );
  assert.equal(versions.listPackageVersions(project.id).length, 1);
  assert.equal(drafts.getActivePackageDraft(project.id)?.revision, 1);
});

test('package draft: rejects revision conflicts and a stale base before publishing', async () => {
  const [projects, versions, drafts, dbModule] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/m2-repo.js'),
    import('../src/workflow/package-draft.js'),
    import('../src/db/index.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: 'Draft stale publish' });
  versions.insertPackageVersion(project.id, '1.0.0', packageSpec('1.0.0'), { covered: [], uncovered: [] });
  drafts.createOrGetPackageDraft(project.id, 'alice');
  const operation = drafts.getPackageDraftFile(project.id, 'operations/query_order.md');
  drafts.savePackageDraftFile({
    projectId: project.id,
    path: operation.path,
    content: operation.content.replaceAll('按订单ID查询订单', '按订单号查询订单'),
    expectedRevision: 0,
  });

  assert.throws(
    () => drafts.publishPackageDraft({
      projectId: project.id,
      expectedRevision: 0,
      tables: dataTables(),
      selectedQuestions: [],
    }),
    (error: unknown) => error instanceof drafts.PackageDraftError
      && error.code === 'PACKAGE_DRAFT_REVISION_CONFLICT',
  );

  const nextVersionId = versions.insertPackageVersion(
    project.id,
    '1.1.0',
    packageSpec('1.1.0'),
    { covered: [], uncovered: [] },
  );
  dbModule.getDb().prepare("UPDATE package_versions SET created_at = '9999-01-01T00:00:00.000Z' WHERE id = ?")
    .run(nextVersionId);
  assert.throws(
    () => drafts.publishPackageDraft({
      projectId: project.id,
      expectedRevision: 1,
      tables: dataTables(),
      selectedQuestions: [],
    }),
    (error: unknown) => error instanceof drafts.PackageDraftError
      && error.code === 'PACKAGE_DRAFT_BASE_VERSION_STALE'
      && error.details.latestVersion === '1.1.0',
  );
  assert.equal(versions.listPackageVersions(project.id).length, 2);
  assert.equal(drafts.getActivePackageDraft(project.id)?.revision, 1);
});

test('package draft: rolls back a failed package validation and exposes one uniform consumption gate', async () => {
  const [projects, versions, drafts] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/m2-repo.js'),
    import('../src/workflow/package-draft.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: 'Draft validation rollback' });
  versions.insertPackageVersion(project.id, '1.0.0', packageSpec('1.0.0'), { covered: [], uncovered: [] });
  drafts.createOrGetPackageDraft(project.id, 'alice');
  const operation = drafts.getPackageDraftFile(project.id, 'operations/query_order.md');
  drafts.savePackageDraftFile({
    projectId: project.id,
    path: operation.path,
    content: operation.content.replace('    - Order', '    - MissingObject'),
    expectedRevision: 0,
  });

  assert.throws(
    () => drafts.assertNoActivePackageDraft(project.id, 'evaluate'),
    (error: unknown) => error instanceof drafts.PackageDraftError
      && error.code === 'PACKAGE_DRAFT_REQUIRES_PUBLISH'
      && error.details.operation === 'evaluate'
      && error.details.changedFileCount === 1,
  );
  assert.throws(
    () => drafts.publishPackageDraft({
      projectId: project.id,
      expectedRevision: 1,
      tables: dataTables(),
      selectedQuestions: [],
    }),
    (error: unknown) => error instanceof drafts.PackageDraftError
      && error.code === 'PACKAGE_DRAFT_VALIDATION_FAILED'
      && Array.isArray(error.details.findings),
  );
  assert.equal(versions.listPackageVersions(project.id).length, 1);
  assert.equal(drafts.getActivePackageDraft(project.id)?.revision, 1);
});

test('legacy override migration: isolates a failed project so startup migration can continue', async () => {
  const [projects, versions, drafts, migrations, renderer, dbModule] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/m2-repo.js'),
    import('../src/workflow/package-draft.js'),
    import('../src/workflow/package-override-migration.js'),
    import('../src/workflow/package-render.js'),
    import('../src/db/index.js'),
  ]);
  const project = projects.createProject({ owner: 'alice', name: 'Legacy migration isolation' });
  versions.insertPackageVersion(project.id, '1.0.0', packageSpec('1.0.0'), { covered: [], uncovered: [] });
  drafts.createOrGetPackageDraft(project.id, 'alice');
  const nextVersionId = versions.insertPackageVersion(
    project.id,
    '1.1.0',
    packageSpec('1.1.0'),
    { covered: [], uncovered: [] },
  );
  dbModule.getDb().prepare("UPDATE package_versions SET created_at = '9999-01-01T00:00:00.000Z' WHERE id = ?")
    .run(nextVersionId);
  const changed = renderer.renderPackage(packageSpec('1.1.0'))['operations/query_order.md']
    .replaceAll('按订单ID查询订单', '按旧版人工口径查询订单');
  dbModule.getDb().prepare(`INSERT INTO package_overrides
    (project_id, path, content, updated_at) VALUES (?,?,?,?)`)
    .run(project.id, 'operations/query_order.md', changed, '2026-07-29T13:00:00.000Z');

  const summary = migrations.migrateLegacyPackageOverrides();
  assert.equal(summary.projectCount, 1);
  assert.equal(summary.failedCount, 1);
  assert.equal(summary.projects[0].projectId, project.id);
  assert.match(summary.projects[0].error ?? '', /草稿基线已过时/);
  assert.equal(drafts.getActivePackageDraft(project.id)?.baseVersion, '1.0.0');
  assert.equal((dbModule.getDb().prepare(
    'SELECT COUNT(*) AS count FROM package_overrides WHERE project_id = ?',
  ).get(project.id) as { count: number }).count, 1);
  assert.equal((dbModule.getDb().prepare(
    'SELECT COUNT(*) AS count FROM package_override_migration_archive WHERE project_id = ?',
  ).get(project.id) as { count: number }).count, 0);
});

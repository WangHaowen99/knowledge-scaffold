import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'package-check-history-'));

const { createProject } = await import('../src/projects/repo.js');
const { getDb } = await import('../src/db/index.js');
const {
  getCheck,
  getSemanticCheckCache,
  runStableSemanticCheck,
  saveCheck,
  semanticCheckInputHash,
} = await import('../src/workflow/package-check.js');
const { getPackageWarningReview } = await import('../src/workflow/warning-acceptance.js');
const { deferPackageErrors, getPackageErrorDeferral } = await import('../src/workflow/package-error-deferral.js');
const { ArtifactRevisionRepo } = await import('../src/artifacts/revision-repo.js');

function semanticSpec(overrides: Record<string, unknown> = {}) {
  return {
    id: 'semantic-cache-agent',
    name: '语义缓存测试',
    description: '测试重复检查结果是否稳定',
    version: '1.0.0',
    language: 'zh-CN',
    defaultProviderAlias: 'default',
    objects: [{
      key: 'Product', title: '产品', description: '产品业务定义',
      fields: [{ key: 'name', name: '名称', type: 'string', required: true }],
      relations: [], sourceRefs: [], sourceTables: [],
    }],
    operations: [], rules: [], metrics: [], scripts: [], status: 'draft',
    ...overrides,
  } as any;
}

test('语义检查哈希忽略版本号和发布状态但响应业务内容变化', () => {
  const project = createProject({ owner: 'alice', name: '语义哈希' });
  const baseline = semanticCheckInputHash(project, semanticSpec());
  const metadataOnly = semanticCheckInputHash(project, semanticSpec({
    version: '1.0.1', status: 'review',
  }));
  const changed = semanticCheckInputHash(project, semanticSpec({
    objects: [{
      key: 'Product', title: '产品', description: '产品业务定义已修改',
      fields: [{ key: 'name', name: '名称', type: 'string', required: true }],
      relations: [], sourceRefs: [], sourceTables: [],
    }],
  }));

  assert.equal(metadataOnly, baseline);
  assert.notEqual(changed, baseline);
});

test('语义检查哈希覆盖超长脚本被模型传输摘要省略的中段', () => {
  const project = createProject({ owner: 'alice', name: '超长脚本语义哈希' });
  const source = `${'head\n'.repeat(3_000)}${'middle-a\n'.repeat(1_500)}${'tail\n'.repeat(2_000)}`;
  const changed = source.replace('middle-a\n', 'middle-b\n');
  const withScript = (sourceCode: string) => semanticSpec({ scripts: [{
    key: 'calculate_product', title: '产品计算', description: '计算产品结果',
    runtime: 'python', entrypoint: 'calculate_product.py', timeoutSeconds: 30,
    parameters: [], outputs: [], boundaries: [], sourceCode,
  }] });

  assert.notEqual(
    semanticCheckInputHash(project, withScript(source)),
    semanticCheckInputHash(project, withScript(changed)),
  );
});

test('同一业务内容的重复检查复用首次语义结果', async () => {
  const project = createProject({ owner: 'alice', name: '语义结果复用' });
  const spec = semanticSpec();
  const existing = [{
    id: 'stable-semantic-warning', source: 'semantic' as const, severity: '警告' as const,
    category: '业务边界', target: 'Product', message: '需要确认产品适用范围',
  }];
  let runs = 0;
  const first = await runStableSemanticCheck(project, spec, {
    existingFindings: existing,
    runner: async () => {
      runs += 1;
      return [{
        id: 'drifted-error', source: 'semantic', severity: '错误', category: '新错误',
        target: 'Product', message: '同一内容被重新判定为错误',
      }];
    },
  });
  const second = await runStableSemanticCheck(project, semanticSpec({
    version: '1.0.1', status: 'review',
  }), {
    runner: async () => {
      runs += 1;
      return [];
    },
  });

  assert.equal(runs, 0);
  assert.equal(first.reused, true);
  assert.equal(second.reused, true);
  assert.deepEqual(second.findings.map((finding) => finding.id), ['stable-semantic-warning']);
  assert.deepEqual(
    getSemanticCheckCache(project.id, semanticCheckInputHash(project, spec))
      ?.map((finding) => finding.id),
    ['stable-semantic-warning'],
  );
});

test('强制语义复查会显式运行模型并更新缓存', async () => {
  const project = createProject({ owner: 'alice', name: '强制语义复查' });
  const spec = semanticSpec();
  let runs = 0;
  const result = await runStableSemanticCheck(project, spec, {
    force: true,
    existingFindings: [],
    runner: async () => {
      runs += 1;
      return [{
        id: 'forced-semantic-hint', source: 'semantic', severity: '提示', category: '复查',
        target: 'Product', message: '强制复查的新结果',
      }];
    },
  });

  assert.equal(runs, 1);
  assert.equal(result.reused, false);
  assert.deepEqual(
    getSemanticCheckCache(project.id, result.semanticHash)?.map((finding) => finding.id),
    ['forced-semantic-hint'],
  );
});

test('迟到的旧修订校验不会覆盖已保存的新修订校验', () => {
  const project = createProject({ owner: 'alice', name: '校验修订历史' });
  saveCheck(project.id, '2.0.0', [{
    id: 'finding-b', source: 'rule', severity: '警告', category: '新修订',
    target: 'operations/b.md', message: 'B 修订的校验结果',
  }], 'revision-b');

  // A 的慢校验在 B 完成后才返回。
  saveCheck(project.id, '1.0.0', [{
    id: 'finding-a', source: 'rule', severity: '错误', category: '旧修订',
    target: 'operations/a.md', message: 'A 修订的迟到结果',
  }], 'revision-a');

  const current = getCheck(project.id, 'revision-b');
  assert.equal(current?.version, '2.0.0');
  assert.equal(current?.revision, 'revision-b');
  assert.deepEqual(current?.findings.map((finding) => finding.id), ['finding-b']);
});

test('兼容调用按 artifact 序号读取最新修订而不是最后完成的校验', () => {
  const project = createProject({ owner: 'alice', name: '最近校验兼容读取' });
  const revisionA = 'a'.repeat(40);
  const revisionB = 'b'.repeat(40);
  const revisions = new ArtifactRevisionRepo();
  revisions.record({
    projectId: project.id, oid: revisionA, cause: 'generate', summary: 'A 修订',
    actorKind: 'agent', actorName: '建模助手',
  });
  revisions.record({
    projectId: project.id, oid: revisionB, parentOid: revisionA, cause: 'edit', summary: 'B 修订',
    actorKind: 'user', actorName: 'alice',
  });

  saveCheck(project.id, '2.0.0', [{
    id: 'finding-b-sequenced', source: 'rule', severity: '警告', category: '新修订',
    target: 'operations/b.md', message: 'B 修订的校验结果',
  }], revisionB);
  saveCheck(project.id, '1.0.0', [{
    id: 'finding-a-sequenced', source: 'rule', severity: '错误', category: '旧修订',
    target: 'operations/a.md', message: 'A 修订的迟到结果',
  }], revisionA);

  assert.equal(getCheck(project.id)?.revision, revisionB);
});

test('精确读取兼容只存在于旧 package_checks 表的记录', () => {
  const project = createProject({ owner: 'alice', name: '旧校验表兼容' });
  getDb().prepare(`INSERT INTO package_checks
    (project_id, version, findings_json, artifact_revision_oid, created_at)
    VALUES (?,?,?,?,?)`).run(
    project.id,
    '0.9.0',
    JSON.stringify([{ id: 'legacy-finding', source: 'rule', severity: '提示',
      category: '旧记录', target: 'package/index.md', message: '旧表中的校验结果' }]),
    'legacy-revision',
    '2026-07-24T00:00:00.000Z',
  );

  const restored = getCheck(project.id, 'legacy-revision');
  assert.equal(restored?.version, '0.9.0');
  assert.equal(restored?.findings[0].id, 'legacy-finding');
});

test('警告复核始终读取明确修订的校验结果', () => {
  const project = createProject({ owner: 'alice', name: '警告修订历史' });
  saveCheck(project.id, '2.0.0', [{
    id: 'warning-b', source: 'semantic', severity: '警告', category: '业务边界',
    target: 'operations/b.md', message: 'B 修订待确认的业务边界',
  }], 'revision-b');
  saveCheck(project.id, '1.0.0', [{
    id: 'warning-a', source: 'semantic', severity: '警告', category: '旧业务边界',
    target: 'operations/a.md', message: 'A 修订迟到的业务边界',
  }], 'revision-a');

  const review = getPackageWarningReview(project.id, 'revision-b');
  assert.equal(review.warningTotal, 1);
  assert.equal(review.items[0].finding.id, 'warning-b');
});

test('旧修订校验迟到不使新修订的错误暂缓失效', () => {
  const project = createProject({ owner: 'alice', name: '错误暂缓修订历史' });
  saveCheck(project.id, '2.0.0', [{
    id: 'error-b', source: 'semantic', severity: '错误', category: '业务口径',
    target: 'operations/b.md', message: 'B 修订尚待后续评测验证',
  }], 'revision-b');
  deferPackageErrors({
    projectId: project.id,
    revision: 'revision-b',
    reason: '先验证完整业务链路，再依据真实评测统一修复',
    actor: 'alice',
    actorRole: 'project_owner',
  });

  saveCheck(project.id, '1.0.0', [{
    id: 'error-a', source: 'semantic', severity: '错误', category: '旧业务口径',
    target: 'operations/a.md', message: 'A 修订迟到的错误',
  }], 'revision-a');

  assert.equal(getPackageErrorDeferral(project.id, 'revision-b')?.valid, true);
});

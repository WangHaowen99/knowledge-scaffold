import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'package-error-deferral-'));

const { createProject } = await import('../src/projects/repo.js');
const { saveCheck } = await import('../src/workflow/package-check.js');
const {
  deferPackageErrors,
  getPackageErrorDeferral,
} = await import('../src/workflow/package-error-deferral.js');

const blockingFinding = {
  id: 'error-1', source: 'semantic' as const, severity: '错误' as const,
  category: '口径冲突', target: 'operation:CalculateScore', message: '指标口径尚未统一',
};

test('阻断错误暂缓只绑定当前知识包修订并保留审计信息', () => {
  const project = createProject({ owner: 'alice', name: '暂缓错误进入验证' });
  saveCheck(project.id, '0.1.0', [blockingFinding], 'revision-a');

  const deferred = deferPackageErrors({
    projectId: project.id,
    revision: 'revision-a',
    reason: '先验证完整业务链路，再依据真实评测统一修复',
    actor: 'alice',
    actorRole: 'project_owner',
  });

  assert.equal(deferred.errorCount, 1);
  assert.equal(deferred.valid, true);
  assert.equal(deferred.reason, '先验证完整业务链路，再依据真实评测统一修复');
  assert.equal(getPackageErrorDeferral(project.id, 'revision-a')?.actor, 'alice');
  assert.equal(getPackageErrorDeferral(project.id, 'revision-b'), null);

  saveCheck(project.id, '0.1.0', [{
    ...blockingFinding, id: 'error-2', category: '证据不足', message: '当前结论缺少可追溯证据',
  }], 'revision-a');
  assert.equal(getPackageErrorDeferral(project.id, 'revision-a')?.valid, false);
});

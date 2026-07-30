import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'optimization-candidate-'));

const [
  { createProject },
  m4repo,
  candidateModule,
  { renderPackage },
  { getPackageArtifactStore },
  diagnosisRepo,
] = await Promise.all([
  import('../src/projects/repo.js'),
  import('../src/workflow/m4-repo.js'),
  import('../src/evaluation/optimization-candidate.js'),
  import('../src/workflow/package-render.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/evaluation/failure-diagnosis-repo.js'),
]);

const baseSpec = {
  id: 'candidate-demo', name: '候选测试', description: '原始说明', version: '1.0.0',
  language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [],
};
const coverage = { covered: [], uncovered: [] };

test('优化候选成功响应只暴露候选标识、状态、数量和基线修订', () => {
  assert.deepEqual(candidateModule.optimizationCandidateApplyResponse(
    { id: 'candidate-1', status: 'pending' }, 2, 'revision-base',
  ), {
    candidateId: 'candidate-1', status: 'pending', selected: 2, basedRevisionOid: 'revision-base',
  });
});

test('优化输出只创建 ArtifactChange 候选，未接受前不推进建议或正式修订', async () => {
  const project = createProject({ owner: 'alice', name: '候选关联' });
  const [suggestion] = m4repo.insertSuggestions(
    project.id, 'round-1', '1.0.0', 'revision-base', [{
      title: '补充说明', detail: '补充独立证据', kind: 'clarify_description',
      diagnosisIds: ['dg-1'], evidence: ['资料原文'],
    }],
  );
  const current = {
    revision: { oid: 'revision-base', parentOid: null, message: '', author: { name: 'a', email: 'a@b' }, createdAt: '' },
    spec: baseSpec,
    coverage,
    files: renderPackage(baseSpec),
  };
  let createInput: any;
  let rejected = false;
  const candidate = await candidateModule.createOptimizationCandidate({
    store: {
      createCandidateRevision: async (_projectId, input) => {
        createInput = input;
        return { id: 'candidate-1', status: 'pending' };
      },
      rejectCandidateRevision: async () => { rejected = true; },
    },
    projectId: project.id,
    current,
    spec: { ...baseSpec, description: '修订后的说明' },
    coverage,
    suggestionIds: [suggestion.id],
    summary: '优化候选',
    linkCandidate: (candidateId) => m4repo.markCandidateRevision(project.id, [suggestion.id], candidateId),
  });

  assert.equal(candidate.id, 'candidate-1');
  assert.equal(createInput.expectedRevision, 'revision-base');
  assert.ok(createInput.groups[0].changes.every((change: any) => change.kind === 'write'));
  assert.equal(rejected, false);
  const linked = m4repo.getSuggestions([suggestion.id])[0];
  assert.equal(linked.status, 'candidate');
  assert.equal(linked.candidateRevisionId, 'candidate-1');
  assert.equal(linked.appliedRevision, undefined);
});

test('候选接受、退回、拒绝和过期会同步建议状态', () => {
  const project = createProject({ owner: 'alice', name: '候选决策' });
  const suggestions = m4repo.insertSuggestions(
    project.id, 'round-2', '1.0.0', 'revision-base', [
      { title: '建议一', detail: '详情一', kind: 'modify_operation' },
      { title: '建议二', detail: '详情二', kind: 'modify_object' },
      { title: '建议三', detail: '详情三', kind: 'modify_rule' },
    ],
  );

  m4repo.markCandidateRevision(project.id, [suggestions[0].id], 'candidate-accepted');
  assert.equal(m4repo.markCandidateRevisionApplied(
    project.id, 'candidate-accepted', '1.0.0', 'revision-accepted',
  ), 1);
  const accepted = m4repo.getSuggestions([suggestions[0].id])[0];
  assert.equal(accepted.status, 'applied');
  assert.equal(accepted.appliedVersion, '1.0.0');
  assert.equal(accepted.appliedRevision, 'revision-accepted');

  m4repo.markCandidateRevision(project.id, [suggestions[1].id], 'candidate-returned');
  assert.equal(m4repo.returnCandidateRevision(project.id, 'candidate-returned', '请补充边界条件'), 1);
  const returned = m4repo.getSuggestions([suggestions[1].id])[0];
  assert.equal(returned.status, 'pending');
  assert.equal(returned.candidateRevisionId, 'candidate-returned');
  assert.equal(returned.candidateFeedback, '请补充边界条件');

  m4repo.markCandidateRevision(project.id, [suggestions[2].id], 'candidate-expired');
  assert.equal(m4repo.rejectCandidateRevision(project.id, 'candidate-expired'), 1);
  assert.equal(m4repo.getSuggestions([suggestions[2].id])[0].status, 'rejected');
});

test('优化候选不得旁路提升语义版本', () => {
  const current = {
    revision: { oid: 'revision-base', parentOid: null, message: '', author: { name: 'a', email: 'a@b' }, createdAt: '' },
    spec: baseSpec,
    coverage,
    files: renderPackage(baseSpec),
  };
  assert.throws(() => candidateModule.buildOptimizationArtifactChanges(
    current, { ...baseSpec, version: '1.0.1' }, coverage,
  ), /不能旁路提升语义版本/);
});

test('优化候选删除过期受管文件但保留导入的非受管 extras', () => {
  const currentSpec = {
    ...baseSpec,
    rules: [{
      key: 'legacy_rule', title: '旧规则', description: '已经过期的受管规则',
      content: '订单状态为完成时可以结算。',
      conditions: [{ condition: '订单状态为完成', conclusion: '订单可结算' }],
    }],
  };
  const current = {
    revision: { oid: 'revision-base', parentOid: null, message: '', author: { name: 'a', email: 'a@b' }, createdAt: '' },
    spec: currentSpec,
    coverage,
    files: {
      ...renderPackage(currentSpec),
      'extras/imported-note.md': '导入资料，不由渲染器管理',
    },
  };

  const changes = candidateModule.buildOptimizationArtifactChanges(
    current, { ...baseSpec, description: '删除旧规则后的说明' }, coverage,
  );

  assert.ok(changes.some((change: any) => change.kind === 'delete'
    && change.path === 'package/rules/legacy_rule.md'));
  assert.ok(changes.some((change: any) => change.kind === 'delete'
    && change.path === 'package/rules/index.md'));
  assert.equal(changes.some((change: any) => change.path === 'package/extras/imported-note.md'), false);
});

test('重跑同轮诊断会关闭旧优化候选和建议但保留其他来源候选', async () => {
  const project = createProject({ owner: 'alice', name: '重跑诊断关闭候选' });
  const store = getPackageArtifactStore();
  const initial = await store.ensureProject(project.id);
  const generated = await store.savePackageVersion(project.id, {
    expectedRevision: initial.oid,
    spec: baseSpec,
    coverage,
    action: { cause: 'generate', summary: '生成基线', actorKind: 'agent', actorName: '助手' },
  });
  const roundId = 'round-rerun';
  const [oldDiagnosis] = diagnosisRepo.replaceFailureDiagnoses(
    project.id, roundId, '1.0.0', generated.revision.oid, [{
      category: 'knowledge_gap', title: '旧诊断', summary: '旧诊断摘要', evidence: ['旧证据'],
      confidence: 'high', blocking: false, repairScope: 'package', packageOptimizable: true,
      resultIds: ['result-1'], relatedQuestions: ['问题'], suggestedAction: '补充说明',
    }],
  );
  const confirmed = diagnosisRepo.resolveFailureDiagnosis({
    projectId: project.id,
    diagnosisId: oldDiagnosis.id,
    action: 'confirm_rule',
    note: '确认规则适用于同类业务。',
    actor: 'alice',
  });
  const [suggestion] = m4repo.insertSuggestions(
    project.id, roundId, '1.0.0', generated.revision.oid, [{
      title: '旧建议', detail: '根据旧诊断生成', kind: 'clarify_description',
      diagnosisIds: [confirmed.id],
    }],
  );
  const current = await store.readPackageFiles(project.id, generated.revision.oid);
  const optimizationCandidate = await candidateModule.createOptimizationCandidate({
    store,
    projectId: project.id,
    current,
    spec: { ...baseSpec, description: '旧诊断生成的修改' },
    coverage,
    suggestionIds: [suggestion.id],
    summary: '旧优化候选',
    linkCandidate: (candidateId) => m4repo.markCandidateRevision(project.id, [suggestion.id], candidateId),
  });
  const otherCandidate = await store.createCandidateRevision(project.id, {
    expectedRevision: generated.revision.oid,
    groups: [{
      id: 'controlled-repair', summary: '其他来源候选',
      changes: [{ kind: 'write', path: 'package/index.md', content: `${current.files['index.md']}\n其他来源\n` }],
    }],
    action: {
      cause: 'controlled_repair', summary: '其他来源候选', actorKind: 'agent', actorName: '助手',
    },
  });

  diagnosisRepo.replaceFailureDiagnoses(
    project.id, roundId, '1.0.0', generated.revision.oid, [{
      category: 'knowledge_gap', title: '新诊断', summary: '重跑后的诊断', evidence: ['新证据'],
      confidence: 'high', blocking: false, repairScope: 'package', packageOptimizable: true,
      resultIds: ['result-1'], relatedQuestions: ['问题'], suggestedAction: '重新生成建议',
    }],
  );
  const retired = await candidateModule.retireOptimizationCandidatesForDiagnosisRerun({
    store,
    projectId: project.id,
    roundId,
  });

  assert.deepEqual(retired.candidateIds, [optimizationCandidate.id]);
  assert.equal((await store.getCandidateRevision(project.id, optimizationCandidate.id)).status, 'rejected');
  assert.equal(m4repo.getSuggestions([suggestion.id])[0].status, 'rejected');
  assert.equal((await store.getCandidateRevision(project.id, otherCandidate.id)).status, 'pending');
});

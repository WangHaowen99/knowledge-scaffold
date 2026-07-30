import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'optimization-candidate-'));

const [{ createProject }, m4repo, candidateModule, { renderPackage }] = await Promise.all([
  import('../src/projects/repo.js'),
  import('../src/workflow/m4-repo.js'),
  import('../src/evaluation/optimization-candidate.js'),
  import('../src/workflow/package-render.js'),
]);

const baseSpec = {
  id: 'candidate-demo', name: '候选测试', description: '原始说明', version: '1.0.0',
  language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [],
};
const coverage = { covered: [], uncovered: [] };

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
  assert.equal(returned.candidateRevisionId, undefined);
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

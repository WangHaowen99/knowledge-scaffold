import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-project-profile-'));

const { createProject, getProject, updateProject } = await import('../src/projects/repo.js');

test('新项目保留流程选择', () => {
  const project = createProject({
    owner: 'alice',
    name: '对公营销',
    targetUsers: '客户经理',
    sensitivityLevel: 'standard',
    workflowProfile: {
      enabledSteps: ['optimize'],
      requiredSteps: ['scenario', 'data_source'],
      releaseRequested: true,
      externalResearch: true,
    },
  });

  assert.deepEqual(getProject(project.id)?.workflowProfile, {
    enabledSteps: ['optimize'],
    requiredSteps: ['scenario', 'data_source'],
    releaseRequested: true,
    externalResearch: true,
  });
});

test('敏感项目关闭联网调研并忽略未知步骤', () => {
  const project = createProject({
    owner: 'alice',
    name: '敏感项目',
    sensitivityLevel: 'restricted',
    workflowProfile: {
      enabledSteps: ['diagnose', 'unknown' as any],
      requiredSteps: [],
      releaseRequested: false,
      externalResearch: true,
    },
  });

  assert.equal(project.sensitivityLevel, 'restricted');
  assert.deepEqual(project.workflowProfile, {
    enabledSteps: ['diagnose'],
    requiredSteps: [],
    releaseRequested: false,
    externalResearch: false,
  });
});

test('项目信息可修改，敏感项目会关闭联网调研', () => {
  const project = createProject({
    owner: 'alice',
    name: '原项目',
    domain: '金融',
    background: '原目标',
    targetUsers: '原用户',
    workflowProfile: { externalResearch: true },
  });

  const updated = updateProject(project.id, {
    name: ' 对公营销 ',
    background: '服务客户经理',
    targetUsers: '',
    glossaryPrefs: '统一称为企业客户',
    sensitivityLevel: 'restricted',
    workflowProfile: { enabledSteps: ['optimize'] },
  });

  assert.equal(updated?.name, '对公营销');
  assert.equal(updated?.domain, '金融');
  assert.equal(updated?.targetUsers, undefined);
  assert.equal(updated?.glossaryPrefs, '统一称为企业客户');
  assert.deepEqual(updated?.workflowProfile, {
    enabledSteps: ['optimize'],
    requiredSteps: ['scenario', 'data_source'],
    releaseRequested: true,
    externalResearch: false,
  });
  assert.throws(() => updateProject(project.id, { name: '  ' }), /项目名必填/);
});

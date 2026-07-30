import assert from 'node:assert/strict';
import test from 'node:test';
import {
  isReleaseCandidateVersion,
  nextValidationCandidateVersion,
  promoteReleaseCandidate,
  recommendNextStableVersion,
} from '../src/workflow/package-version-lifecycle.js';

test('新建草稿完成 AI 辅助构建后从 1.0.0-rc.1 开始验证', () => {
  assert.equal(nextValidationCandidateVersion({
    currentVersion: '0.1.0', currentStatus: 'draft',
  }), '1.0.0-rc.1');
});

test('同一稳定目标的优化修订递增 RC 序号', () => {
  assert.equal(nextValidationCandidateVersion({
    currentVersion: '1.0.0-rc.1', currentStatus: 'review',
  }), '1.0.0-rc.2');
  assert.equal(nextValidationCandidateVersion({
    currentVersion: '2.4.0-rc.19', currentStatus: 'review',
  }), '2.4.0-rc.20');
  assert.equal(nextValidationCandidateVersion({
    currentVersion: '1.4.0', currentStatus: 'review',
  }), '1.4.1-rc.1');
});

test('已发布版本根据兼容性影响创建下一稳定目标的 RC', () => {
  assert.equal(nextValidationCandidateVersion({
    currentVersion: '1.4.2', currentStatus: 'released', changeLevel: 'patch',
  }), '1.4.3-rc.1');
  assert.equal(nextValidationCandidateVersion({
    currentVersion: '1.4.2', currentStatus: 'released', changeLevel: 'minor',
  }), '1.5.0-rc.1');
  assert.equal(nextValidationCandidateVersion({
    currentVersion: '1.4.2', currentStatus: 'released', changeLevel: 'major',
  }), '2.0.0-rc.1');
});

test('正式发布只能把 RC 提升为对应稳定版本', () => {
  assert.equal(isReleaseCandidateVersion('1.0.0-rc.4'), true);
  assert.equal(isReleaseCandidateVersion('1.0.0'), false);
  assert.equal(promoteReleaseCandidate('1.0.0-rc.4'), '1.0.0');
  assert.throws(() => promoteReleaseCandidate('1.0.0'), /发布候选/);
  assert.throws(() => promoteReleaseCandidate('1.0.0-beta.1'), /发布候选/);
});

test('助手的版本建议只表达语义影响而不直接跳过 RC', () => {
  assert.equal(recommendNextStableVersion('1.2.3', 'compatible_fix'), '1.2.4');
  assert.equal(recommendNextStableVersion('1.2.3', 'compatible_feature'), '1.3.0');
  assert.equal(recommendNextStableVersion('1.2.3', 'breaking_change'), '2.0.0');
});

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-evaluation-progress-'));

test('评测轮次持久化阶段和当前用例开始时间', async () => {
  const [{ createProject }, repo] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/workflow/m3-repo.js'),
  ]);
  const project = createProject({ owner: 'alice', name: '评测进度', domain: 'demo', background: '' });
  const round = repo.createRound(project.id, '1.0.0', 3);

  assert.equal(round.phase, 'workspace');
  assert.ok(round.phaseStartedAt);
  assert.equal(round.currentItemStartedAt, undefined);

  repo.roundPhase(round.id, 'database');
  assert.equal(repo.getRound(round.id)?.phase, 'database');

  repo.roundPhase(round.id, 'cases');
  repo.roundCaseStarted(round.id);
  const running = repo.getRound(round.id);
  assert.equal(running?.phase, 'cases');
  assert.ok(running?.currentItemStartedAt);

  repo.finishRound(round.id, 'done');
  const finished = repo.getRound(round.id);
  assert.equal(finished?.phase, 'done');
  assert.ok(finished?.finishedAt);
});

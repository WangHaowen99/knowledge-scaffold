import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'evaluation-restart-recovery-'));

const m3repo = await import('../src/workflow/m3-repo.js');
const { createProject } = await import('../src/projects/repo.js');

test('服务重启后遗留的评测轮次显式失败并保留进度日志', () => {
  const project = createProject({ owner: 'alice', name: '评测重启恢复' });
  const round = m3repo.createRound(project.id, '1.0.0', 3, 'revision-1');
  m3repo.roundProgress(round.id, 1, 1);
  m3repo.roundLog(round.id, '第 1 条已完成');

  assert.equal(m3repo.failInterruptedEvaluationRounds('服务重启，本轮评测已中断'), 1);
  const recovered = m3repo.getRound(round.id)!;
  assert.equal(recovered.status, 'failed');
  assert.equal(recovered.progressDone, 1);
  assert.match(recovered.log, /第 1 条已完成/);
  assert.match(recovered.log, /服务重启/);
  assert.equal(m3repo.failInterruptedEvaluationRounds('再次扫描'), 0);
});

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildMaterialAnalysisProgressBlocks,
  type MaterialAnalysisProgress,
} from '../src/ai-construction/material-analysis-progress.js';

test('资料分析进展给出阶段、百分比、已用时和剩余时长', () => {
  const progress: MaterialAnalysisProgress = {
    phase: 'artifact_batch_2', title: '分析第 2/3 批材料',
    summary: '业务规则.docx、产品表', percent: 60,
  };
  const blocks = buildMaterialAnalysisProgressBlocks(progress, 70_000);

  assert.equal(blocks[0].type, 'status');
  assert.equal(blocks[0].title, '分析第 2/3 批材料');
  assert.equal((blocks[0] as any).progress, 60);
  assert.match((blocks[1] as any).text, /已用时约 1 分 10 秒/);
  assert.match((blocks[1] as any).text, /剩余约/);
});

test('资料分析完成时剩余时长归零', () => {
  const blocks = buildMaterialAnalysisProgressBlocks({
    phase: 'complete', title: '资料分析完成', percent: 100,
  }, 120_000);
  assert.deepEqual((blocks[0] as any).eta, {
    minMinutes: 0, maxMinutes: 0, confidence: 'high',
  });
  assert.match((blocks[1] as any).text, /分析阶段已完成/);
});

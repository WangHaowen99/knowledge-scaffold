import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildJudgeSystemPrompt,
  buildJudgeUserPrompt,
  extractJudgeScopeHighlights,
} from '../src/evaluation/judge.js';

test('判分提示明确组合编号不是批次编号，并接受百分比相对容差', () => {
  const prompt = buildJudgeSystemPrompt();
  assert.match(prompt, /组合.*用例标识.*不能.*第 N 批/);
  assert.match(prompt, /绝对误差.*或.*相对误差.*任一/);
  assert.match(prompt, /98\.56%.*98\.50%/);
});

test('判分前提取全月汇总，避免被批次明细误导', () => {
  const answer = [
    '| 第8批 | 生产线A120.71% | 生产线B72.46% | 生产线C144.72% |',
    '| **全月** | 生产线A96.99% | 生产线B83.13% | 生产线C102.95% |',
    '**总结：** 全月生产线C过载，生产线A和生产线B不过载。',
  ].join('\n');
  assert.deepEqual(extractJudgeScopeHighlights(answer), [
    '| **全月** | 生产线A96.99% | 生产线B83.13% | 生产线C102.95% |',
    '**总结：** 全月生产线C过载，生产线A和生产线B不过载。',
  ]);
  const prompt = buildJudgeUserPrompt('问题', '组合 8 的三个装置负荷', answer);
  assert.match(prompt, /同层级汇总索引/);
  assert.ok(prompt.indexOf('全月**') < prompt.indexOf('智能体完整回答'));
});

test('没有汇总行时不制造虚假范围提示', () => {
  assert.deepEqual(extractJudgeScopeHighlights('只回答：可以办理。'), []);
  assert.doesNotMatch(buildJudgeUserPrompt('问题', '答案', '只回答：可以办理。'), /同层级汇总索引/);
});

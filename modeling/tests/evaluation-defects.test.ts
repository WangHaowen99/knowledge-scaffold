import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  canPlanEvaluationRepair,
  selectableEvaluationDefects,
  submittableEvaluationDefectIds,
  toggleEvaluationDefect,
} from '../frontend/src/evaluationDefects.js';

const defect = (overrides: Record<string, unknown> = {}) => ({
  id: 'dg-1', repairScope: 'package', resolutionStatus: 'pending', ...overrides,
});

test('只有归因到知识包且未关闭的失败原因可以回流建模', () => {
  assert.equal(canPlanEvaluationRepair(defect()), true);
  assert.equal(canPlanEvaluationRepair(defect({ repairScope: 'data' })), false);
  assert.equal(canPlanEvaluationRepair(defect({ repairScope: 'testcase' })), false);
  assert.equal(canPlanEvaluationRepair(defect({ blocking: true })), false);
  assert.equal(canPlanEvaluationRepair(defect({ resolutionStatus: 'resolved' })), false);
  assert.equal(canPlanEvaluationRepair(defect({ resolutionStatus: 'irrelevant' })), false);
  // 专家已确认或暂缓的知识包缺陷仍然可以回流
  assert.equal(canPlanEvaluationRepair(defect({ resolutionStatus: 'confirmed' })), true);
  assert.equal(canPlanEvaluationRepair(defect({ resolutionStatus: 'deferred' })), true);
  assert.equal(canPlanEvaluationRepair(undefined), false);
});

test('可选缺陷按范围过滤', () => {
  const diagnoses = [
    defect({ id: 'a' }),
    defect({ id: 'b', repairScope: 'runtime' }),
    defect({ id: 'c' }),
  ];
  assert.deepEqual(selectableEvaluationDefects(diagnoses).map((item) => item.id), ['a', 'c']);
});

test('勾选可反复切换', () => {
  const once = toggleEvaluationDefect(new Set(), 'a');
  assert.deepEqual([...once], ['a']);
  assert.deepEqual([...toggleEvaluationDefect(once, 'a')], []);
  assert.deepEqual([...toggleEvaluationDefect(once, 'b')].sort(), ['a', 'b']);
});

test('提交前丢弃已失效或不可回流的选中项', () => {
  const diagnoses = [defect({ id: 'a' }), defect({ id: 'b', repairScope: 'data' })];
  // 'c' 来自上一轮诊断，已经不在当前列表里
  const selected = new Set(['a', 'b', 'c']);
  assert.deepEqual(submittableEvaluationDefectIds(diagnoses, selected), ['a']);
});

test('未勾选时没有可提交项', () => {
  assert.deepEqual(submittableEvaluationDefectIds([defect()], new Set()), []);
});

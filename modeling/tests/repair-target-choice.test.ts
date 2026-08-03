import assert from 'node:assert/strict';
import test from 'node:test';
import {
  repairTargetCandidatesFrom, withRepairTarget,
} from '../frontend/src/repairTargetChoice.js';

const unresolved = (candidates: unknown) => ({
  data: { code: 'REPAIR_TARGET_UNRESOLVED', candidates },
});

test('从 409 里取出候选目标', () => {
  assert.deepEqual(
    repairTargetCandidatesFrom(unresolved([
      { targetKind: 'object', targetKey: 'ipo_detail_info' },
      { targetKind: 'object', targetKey: 'major_project_info' },
    ])),
    [
      { targetKind: 'object', targetKey: 'ipo_detail_info', value: 'object:ipo_detail_info' },
      { targetKind: 'object', targetKey: 'major_project_info', value: 'object:major_project_info' },
    ],
  );
});

test('别的错误码不冒出选择框', () => {
  assert.deepEqual(repairTargetCandidatesFrom({ data: { code: 'REVISION_CONFLICT' } }), []);
  assert.deepEqual(repairTargetCandidatesFrom(new Error('boom')), []);
  assert.deepEqual(repairTargetCandidatesFrom(undefined), []);
});

test('候选缺字段时跳过，不生成点了没用的选项', () => {
  assert.deepEqual(
    repairTargetCandidatesFrom(unresolved([{ targetKind: 'object' }, { targetKey: 'x' }, null])),
    [],
  );
  assert.deepEqual(repairTargetCandidatesFrom(unresolved('not-an-array')), []);
});

test('选定目标并进回答，不动原有答案', () => {
  const response = { answers: [{ questionId: 'expert_decision', text: '两者应分开编号' }] };
  assert.deepEqual(withRepairTarget(response, 'object:ipo_detail_info'), {
    answers: [
      { questionId: 'expert_decision', text: '两者应分开编号' },
      { questionId: 'repair_target', selected: ['object:ipo_detail_info'] },
    ],
  });
});

test('改选之后是覆盖而不是叠加两条 repair_target', () => {
  const once = withRepairTarget({ answers: [] }, 'object:a');
  const twice = withRepairTarget(once, 'object:b');
  assert.deepEqual(twice.answers, [{ questionId: 'repair_target', selected: ['object:b'] }]);
});

test('没选目标时原样返回，不塞空答案', () => {
  const response = { answers: [{ questionId: 'expert_decision', text: 'x' }] };
  assert.equal(withRepairTarget(response, ''), response);
});

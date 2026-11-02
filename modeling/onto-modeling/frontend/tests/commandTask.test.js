// 页面命令任务：合并进度、取本页进行中的任务、识别刚结束的任务、进度条第二行
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  finishedTasks, isTaskActive, mergeTask, pageTask, sendHint, taskDetail, taskPercent,
} from '../src/state/commandTask.js';

const t = (id, page, state, extra = {}) => ({ task_id: id, origin: { page, label: page }, state, percent: null, step_label: null, started_at: '2026-11-01T10:00:00.000Z', ...extra });

test('按 task_id 合并：瞬时百分比和状态变化都按最新的覆盖', () => {
  let list = mergeTask([], t('a', 'scenarios', 'running', { percent: 10 }));
  list = mergeTask(list, { task_id: 'a', percent: 39, step_label: '已推演 9 / 23' });
  assert.equal(list.length, 1);
  assert.equal(list[0].percent, 39);
  assert.equal(list[0].state, 'running');
  assert.equal(mergeTask(list, { percent: 1 }), list);
});

test('本页进行中的任务取最近发起的一条，别的页和已结束的不算', () => {
  const list = [
    t('a', 'scenarios', 'done'),
    t('b', 'scenarios', 'running', { started_at: '2026-11-01T10:01:00.000Z' }),
    t('c', 'scenarios', 'queued', { started_at: '2026-11-01T10:02:00.000Z' }),
    t('d', 'scripts', 'running', { started_at: '2026-11-01T10:03:00.000Z' }),
  ];
  assert.equal(pageTask(list, 'scenarios').task_id, 'c');
  assert.equal(pageTask(list, 'knowledge'), null);
  assert.equal(isTaskActive(t('x', 'p', 'waiting_user')), true);
  assert.equal(isTaskActive(t('x', 'p', 'interrupted')), false);
  assert.equal(isTaskActive(null), false);
});

test('从进行中变成结束的任务才算刚结束', () => {
  const prev = [t('a', 'p', 'running'), t('b', 'p', 'done'), t('c', 'p', 'running')];
  const next = [t('a', 'p', 'done'), t('b', 'p', 'done'), t('c', 'p', 'running'), t('d', 'p', 'failed')];
  assert.deepEqual(finishedTasks(prev, next).map((x) => x.task_id), ['a']);
});

test('进度条第二行：运行中写步骤，排队与等回答写状态，再接页面的补充说明', () => {
  assert.equal(taskDetail(t('a', 'p', 'running', { step_label: '已推演 9 / 23' }), '结果回到下面的列表'), '已推演 9 / 23 · 结果回到下面的列表');
  assert.equal(taskDetail(t('a', 'p', 'queued'), ''), '排队中');
  assert.equal(taskDetail(t('a', 'p', 'waiting_user', { step_label: '第 2 步' })), '等你在对话里回答');
  assert.equal(taskDetail(null, '说明'), '说明');
  assert.equal(taskPercent(t('a', 'p', 'running', { percent: 38.6 })), 39);
  assert.equal(taskPercent(t('a', 'p', 'running', { percent: 130 })), 100);
  assert.equal(taskPercent(t('a', 'p', 'running')), null);
  assert.equal(sendHint('批量推演 23 个场景'), '在对话里执行：批量推演 23 个场景');
});

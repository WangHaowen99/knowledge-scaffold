// F2 第 8 条：对话里直接切换陪伴建模 / 自主构建
import assert from 'node:assert/strict';
import test from 'node:test';
import { modeChipLabel, planModeSwitch, researchDone } from '../src/pages/workbench/buildModeModel.js';

test('不在运行中：切到陪伴建模只改默认；已经是陪伴建模不用做', () => {
  assert.equal(planModeSwitch({ target: 'L2', mode: 'L4', runState: null }), 'set');
  assert.equal(planModeSwitch({ target: 'L2', mode: 'L2', runState: null }), 'noop');
  assert.equal(planModeSwitch({ target: 'L2', mode: 'L4', runState: 'completed' }), 'set');
});

test('切到自主构建：F8 起蓝图不用确认，资料分析做完就走开始前预览，没做完只改默认', () => {
  assert.equal(planModeSwitch({ target: 'L4', mode: 'L2', runState: null, researchDone: true }), 'set_then_build');
  assert.equal(planModeSwitch({ target: 'L4', mode: 'L2', runState: null, researchDone: false }), 'set_wait_research');
  // 已经是自主构建但没在跑：照样走开始前预览（对话里开始一次运行）
  assert.equal(planModeSwitch({ target: 'L4', mode: 'L4', runState: 'stopped', researchDone: true }), 'set_then_build');
});

test('资料分析做完了没有：已确认、已过期、跳过算做完；没有这个节点的流程不挡', () => {
  const flow = (state) => ({ nodes: [{ id: 'core.research', state }] });
  ['confirmed', 'stale', 'skipped'].forEach((s) => assert.equal(researchDone(flow(s)), true, s));
  ['running', 'ready', 'not_ready', 'pending_confirm'].forEach((s) => assert.equal(researchDone(flow(s)), false, s));
  assert.equal(researchDone({ nodes: [] }), true);
  assert.equal(researchDone(null), true);
});

test('运行中切回陪伴建模走「转为陪伴」；正在停下的等它停；已在转的不重复', () => {
  for (const s of ['running', 'queued', 'pausing', 'paused', 'interrupted']) {
    assert.equal(planModeSwitch({ target: 'L2', mode: 'L4', runState: s }), 'convert', s);
  }
  assert.equal(planModeSwitch({ target: 'L2', mode: 'L4', runState: 'stopping' }), 'busy');
  assert.equal(planModeSwitch({ target: 'L2', mode: 'L4', runState: 'converting' }), 'noop');
  assert.equal(planModeSwitch({ target: 'L4', mode: 'L4', runState: 'running' }), 'noop');
});

test('模式胶囊：运行没结束时说运行状态，结束后说项目的构建方式', () => {
  assert.equal(modeChipLabel({ mode: 'L4', runState: 'running' }), '自主构建中');
  assert.equal(modeChipLabel({ mode: 'L4', runState: 'paused' }), '自主构建 · 已暂停');
  assert.equal(modeChipLabel({ mode: 'L2', runState: 'converting' }), '转为陪伴中');
  assert.equal(modeChipLabel({ mode: 'L2', runState: 'converted' }), '陪伴建模');
  assert.equal(modeChipLabel({ mode: 'L4', runState: null }), '自主构建');
});

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  ASSISTANT_PANE_MIN_PX,
  ASSISTANT_PANE_RATIO,
  assistantPaneBounds,
  assistantPaneRatio,
  assistantPaneWidth,
} from '../frontend/src/paneLayout.js';
import { aiConstructionTaskEstimate, aiConstructionTaskDuration } from '../src/ai-construction/task-graph.js';
import { confirmUploadDuringAnalysis } from '../frontend/src/flowchartNavigationGuard.js';

// ---- 三栏布局按比例 ----

test('助手栏宽度随容器按比例伸缩，而不是停在固定值', () => {
  const narrow = assistantPaneWidth(1280);
  const wide = assistantPaneWidth(2560);
  assert.ok(wide > narrow, `宽屏应更宽：${narrow} → ${wide}`);
  assert.equal(narrow, Math.round(1280 * ASSISTANT_PANE_RATIO.default));
  assert.equal(wide, Math.round(2560 * ASSISTANT_PANE_RATIO.default));
});

test('窄容器下以可读性下限为准', () => {
  const bounds = assistantPaneBounds(900);
  assert.equal(bounds.min, ASSISTANT_PANE_MIN_PX);
  assert.ok(assistantPaneWidth(900) >= ASSISTANT_PANE_MIN_PX);
});

test('拖拽结果被夹在比例上下限内', () => {
  const width = 2000;
  const bounds = assistantPaneBounds(width);
  assert.equal(assistantPaneWidth(width, 0.9), bounds.max);
  assert.equal(assistantPaneWidth(width, 0.01), bounds.min);
});

test('像素宽度换算回比例后缩放窗口仍保持观感', () => {
  const ratio = assistantPaneRatio(2000, 600);
  assert.ok(Math.abs(ratio - 0.3) < 0.001, `实际 ${ratio}`);
  // 同一比例在更大的容器上得到更大的像素宽度
  assert.ok(assistantPaneWidth(2500, ratio) > assistantPaneWidth(2000, ratio));
});

test('容器尺寸未知时退回可读性下限而不是 NaN', () => {
  assert.equal(assistantPaneWidth(0), ASSISTANT_PANE_MIN_PX);
  assert.equal(assistantPaneWidth(Number.NaN), ASSISTANT_PANE_MIN_PX);
});

// ---- 预计时长动态化 ----

const task = (overrides: Record<string, unknown> = {}) => ({
  id: 't', kind: 'material_analysis', milestoneId: 'materials_ready', ordinal: 1,
  title: 't', goal: 'g', status: 'completed', required: true, dependsOn: [],
  createdAt: '2026-08-01T10:00:00.000Z', updatedAt: '2026-08-01T10:06:00.000Z',
  ...overrides,
}) as any;

test('没有历史样本时使用静态兜底值', () => {
  assert.deepEqual(
    aiConstructionTaskEstimate('material_analysis', []),
    aiConstructionTaskDuration('material_analysis'),
  );
});

test('有多次实测时按实测区间估算，不再用静态值', () => {
  const history = [
    task({ id: 'a', updatedAt: '2026-08-01T10:20:00.000Z' }),  // 20 分钟
    task({ id: 'b', updatedAt: '2026-08-01T10:30:00.000Z' }),  // 30 分钟
  ];
  const [min, max] = aiConstructionTaskEstimate('material_analysis', history);
  // 静态值是 [2, 8]，实测远超它，说明估算确实跟着实际走
  assert.ok(min >= 15, `下限应贴近实测：${min}`);
  assert.ok(max >= 30, `上限应贴近实测：${max}`);
});

test('只有单次样本时与静态区间取并集，保留不确定度', () => {
  const [min, max] = aiConstructionTaskEstimate('material_analysis', [task()]); // 6 分钟
  const [staticMin, staticMax] = aiConstructionTaskDuration('material_analysis');
  assert.ok(min <= staticMin, `单样本下限不应比静态值更乐观：${min} vs ${staticMin}`);
  assert.ok(max >= staticMax, `单样本上限不应比静态值更乐观：${max} vs ${staticMax}`);
});

test('未完成或时间戳异常的任务不参与估算', () => {
  const noisy = [
    task({ id: 'running', status: 'running' }),
    task({ id: 'reversed', updatedAt: '2026-08-01T09:00:00.000Z' }),
    task({ id: 'bad', createdAt: 'not-a-date' }),
  ];
  assert.deepEqual(
    aiConstructionTaskEstimate('material_analysis', noisy),
    aiConstructionTaskDuration('material_analysis'),
  );
});

test('只统计同种任务的耗时', () => {
  const history = [task({ id: 'other', kind: 'knowledge_extraction', updatedAt: '2026-08-01T12:00:00.000Z' })];
  assert.deepEqual(
    aiConstructionTaskEstimate('material_analysis', history),
    aiConstructionTaskDuration('material_analysis'),
  );
});

// ---- 分析期间上传资料 ----

test('分析进行中上传资料必须先确认，因为整轮分析会作废', () => {
  let asked = '';
  const confirm = (message: string) => { asked = message; return false; };
  assert.equal(confirmUploadDuringAnalysis(true, confirm), false);
  assert.match(asked, /本轮分析作废/);
  assert.equal(confirmUploadDuringAnalysis(true, () => true), true);
});

test('未在分析时上传不打扰用户', () => {
  let asked = false;
  assert.equal(confirmUploadDuringAnalysis(false, () => { asked = true; return true; }), true);
  assert.equal(asked, false);
});

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
  // 静态值是 [2, 8]，实测 20/30 分钟远超它，说明估算确实跟着实际走。
  // 两个样本中位数 25，按不足三样本的宽带 [0.5, 2] 得 [13, 50]。
  assert.ok(min >= 12, `下限应贴近实测：${min}`);
  assert.ok(max >= 30, `上限应贴近实测：${max}`);
});

test('只有单次样本时放宽置信带，不给过窄的假精确', () => {
  const [min, max] = aiConstructionTaskEstimate('material_analysis', [task()]); // 6 分钟
  // 以实测 6 分钟为中心按 [0.5, 2] 放宽 → [3, 12]，而不是被静态表拉成两头都极端
  assert.deepEqual([min, max], [3, 12]);
  assert.ok(max / min <= 10, '区间不能宽到没有参考价值');
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

// ---- 跨项目样本：不然每种 kind 一个项目只跑一次，需要估算时永远没样本 ----

test('本项目没跑过的任务，用跨项目实测样本而不是静态表', () => {
  const fallback = aiConstructionTaskDuration('knowledge_extraction');
  const estimate = aiConstructionTaskEstimate('knowledge_extraction', [], {
    knowledge_extraction: [60, 70, 80],
  });
  assert.notDeepEqual(estimate, fallback, '有跨项目样本时不应再退回静态表');
  assert.deepEqual(estimate, [49, 105]);
});

test('本项目样本与跨项目样本合并统计', () => {
  const estimate = aiConstructionTaskEstimate(
    'material_analysis',
    [task({ createdAt: '2026-08-01T10:00:00.000Z', updatedAt: '2026-08-01T10:20:00.000Z' })],
    { material_analysis: [10] },
  );
  // 本项目 20 分钟、跨项目 10 分钟 → 中位数 15，两样本宽带 [0.5, 2]
  assert.deepEqual(estimate, [8, 30]);
});

test('跨项目样本里的脏数据被忽略', () => {
  const fallback = aiConstructionTaskDuration('material_analysis');
  assert.deepEqual(
    aiConstructionTaskEstimate('material_analysis', [], { material_analysis: [0, -5, Number.NaN] }),
    fallback,
  );
});

// ---- 忙碌态以服务端运行记录为准 ----

test('取最早开始的活动运行，用户最关心已经等了多久', async () => {
  const { activeRunOf, activeRunLabel } = await import('../frontend/src/activeRun.js');
  const run = activeRunOf({ activeRuns: [
    { id: 'b', status: 'running', intent: '生成知识结构', createdAt: '2026-08-01T10:05:00.000Z' },
    { id: 'a', status: 'queued', intent: '确认建模目标', createdAt: '2026-08-01T10:01:00.000Z' },
    { id: 'c', status: 'completed', intent: '早就结束了', createdAt: '2026-08-01T09:00:00.000Z' },
  ] });
  assert.equal(run?.id, 'a');
  assert.equal(activeRunLabel(run), '正在确认建模目标');
});

test('没有活动运行时不显示忙碌态', async () => {
  const { activeRunOf } = await import('../frontend/src/activeRun.js');
  assert.equal(activeRunOf({ activeRuns: [{ status: 'completed' }] }), null);
  assert.equal(activeRunOf({ activeRuns: [] }), null);
  assert.equal(activeRunOf(undefined), null);
});

test('intent 缺失时有兜底文案，不显示空白', async () => {
  const { activeRunLabel } = await import('../frontend/src/activeRun.js');
  assert.equal(activeRunLabel({ intent: '  ' }), '正在处理');
  assert.equal(activeRunLabel(null), '正在处理');
});

// ---- 离群样本不能把估算顶成离谱数字 ----

test('等待用户的任务跨了几天，不能当成工作量样本', () => {
  const fallback = aiConstructionTaskDuration('modeling_brief');
  // 5319 分钟 = 3.7 天，实测出现过：项目开着过了个周末
  assert.deepEqual(
    aiConstructionTaskEstimate('modeling_brief', [], { modeling_brief: [5319] }),
    fallback,
    '超过四小时的样本一律当作空等，退回静态表',
  );
});

test('用中位数而不是极值——一个离群点不该撑爆区间', () => {
  // 四个正常样本 + 一个偏大的：极值法会给出 [16, 216]，中位数法稳在 20 附近
  const estimate = aiConstructionTaskEstimate('knowledge_extraction', [], {
    knowledge_extraction: [18, 20, 22, 20, 180],
  });
  assert.ok(estimate[1] <= 45, `上限被离群点撑爆了：${estimate[1]}`);
  assert.ok(estimate[0] >= 10 && estimate[0] <= 20, `下限异常：${estimate[0]}`);
});

test('样本不足时放宽置信带，而不是与静态表取并集', () => {
  // 并集会把两个区间的极端各取一头，结果比任何一方都宽——实测出现过 2–23 分钟
  const [min, max] = aiConstructionTaskEstimate('material_analysis', [], { material_analysis: [5] });
  assert.deepEqual([min, max], [3, 10], '单样本以实测为中心放宽，而不是被静态表拉扯');
});

test('估算区间不会宽到没有参考价值', () => {
  const [min, max] = aiConstructionTaskEstimate('knowledge_extraction', [], {
    knowledge_extraction: [1, 2, 40, 60, 200],
  });
  assert.ok(max / Math.max(1, min) <= 10, `区间跨度 ${min}–${max}，宽到无法据此做决定`);
});

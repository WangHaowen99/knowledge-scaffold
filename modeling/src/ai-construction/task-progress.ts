/**
 * 长任务的实时进度上报。
 *
 * 原先只有资料分析在报，而它是最短的一个（2~8 分钟）；最长的三个——知识结构生成
 * （15~45 分钟）、逐项修复（5~30 分钟）、脚本方案生成（10~40 分钟）——全程只有一个
 * 转圈和一个秒数在动。用户没法判断它是在干活还是卡住了。
 *
 * 关键不是报「第几批」，而是**报已经产出了什么**：用户真正想知道的是「它在干活吗、
 * 干出什么了」。所以 produced 是主要内容，批次序号只是进度条的依据。
 *
 * 消息用固定 key 就地更新（progressOnly），不会在时间线里堆出几十条。
 * 深层代码只要有 projectId 就能调，不必把上报通道一路当参数传下去——
 * 与 cancellation.ts 的取舍一致。
 */
import { AiConstructionMessageStore, notifyAiConstructionMessageWritten } from './messages.js';
import { AiConstructionTaskGraph, type AiConstructionTaskKind } from './task-graph.js';
import { getAuthoringState } from '../authoring/state.js';
import { getDb } from '../db/index.js';

export interface LongTaskProgressInput {
  /** 认哪种正在执行的任务。同一时刻这些 kind 里只会有一个在跑 */
  taskKinds: AiConstructionTaskKind[];
  /** 一句话说明此刻在做什么，如「正在生成第 2/5 批业务对象」 */
  title: string;
  /** 已经产出了什么，如「已产出 12 个对象、7 条规则」。空着就只显示批次 */
  produced?: string;
  /** 已完成 / 总数，用于进度条和收窄剩余时长 */
  done: number;
  total: number;
  /** 本轮任务已经跑了多久，用于按实测速度推算剩余 */
  elapsedMs: number;
}

/**
 * 按已完成批次的实测速度推算剩余时长。
 * 比静态表准得多：静态表给的是「28–90 分钟」这种宽到没法据此做决定的区间。
 */
export function remainingMinutesFromBatches(
  done: number,
  total: number,
  elapsedMs: number,
): { minMinutes: number; maxMinutes: number } | null {
  if (done <= 0 || total <= done || elapsedMs <= 0) return null;
  const perBatchMs = elapsedMs / done;
  const remainingMs = perBatchMs * (total - done);
  const minutes = remainingMs / 60_000;
  // 留出余量：后面的批次未必和前面一样快
  return {
    minMinutes: Math.max(1, Math.round(minutes * 0.8)),
    maxMinutes: Math.max(1, Math.round(minutes * 1.4)),
  };
}

/** 上报失败不能影响任务本身——进度只是展示 */
export function reportLongTaskProgress(projectId: string, input: LongTaskProgressInput): void {
  try {
    const db = getDb();
    const graph = new AiConstructionTaskGraph(db).snapshot(projectId);
    const task = graph?.tasks.find(
      (item) => input.taskKinds.includes(item.kind) && item.status === 'running',
    );
    if (!task) return;

    const percent = input.total > 0
      ? Math.max(0, Math.min(99, Math.round((input.done / input.total) * 100)))
      : 0;
    const eta = remainingMinutesFromBatches(input.done, input.total, input.elapsedMs);
    const messages = new AiConstructionMessageStore(
      db,
      notifyAiConstructionMessageWritten((command) => getAuthoringState().apply(command)),
    );
    messages.upsert(projectId, {
      key: `progress.task.${task.id}`,
      role: 'assistant',
      taskId: task.id,
      progressOnly: true,
      blocks: [
        {
          type: 'status',
          title: input.title,
          summary: input.produced ?? `已完成 ${input.done}/${input.total}`,
          milestoneId: task.milestoneId,
          taskId: task.id,
          progress: percent,
          eta: eta
            ? { minMinutes: eta.minMinutes, maxMinutes: eta.maxMinutes, confidence: 'medium' }
            : { minMinutes: null, maxMinutes: null, confidence: 'low' },
        },
      ],
    });
  } catch {
    /* 进度展示失败不阻断实际任务 */
  }
}

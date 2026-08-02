/**
 * 长任务的取消检查点。
 *
 * 取消只能做到检查点粒度：单次模型调用一旦发出就停不下来，所以「取消」的真实含义是
 * 「当前这一批跑完就停」，而不是立即中断。界面上必须如实这么说，不能做成假的秒停。
 *
 * 检查点放在天然的批次边界上——资料分析的每批材料之间、知识生成的每批对象/知识
 * 候选之间、受控修复的每条问题之间。这些位置的共同点是：此刻的中间结果是完整的，
 * 停下来不会留下半条记录。
 *
 * 取消状态存在 inflight_operations 表里（登记表本来就跟着长任务的生命周期走），
 * 深层代码只要有 projectId 就能查，不必把信号一路当参数传下去。
 */
import { InFlightOperations, OperationCancelledError } from './in-flight-operations.js';

/** 可取消的长任务及其登记键。必须与启动作业时用的键完全一致 */
export const CANCELLABLE_OPERATIONS = {
  analyze: { key: (projectId: string) => `${projectId}:analyze`, label: '资料分析' },
  knowledgeGenerate: {
    key: (projectId: string) => `${projectId}:knowledge-generate`, label: '知识结构生成',
  },
  knowledgeRepair: {
    key: (projectId: string) => `${projectId}:knowledge-repair`, label: '知识包逐项修复',
  },
  scriptGeneration: {
    key: (projectId: string) => `${projectId}:script-generation`, label: '脚本方案生成',
  },
} as const;

export type CancellableOperation = keyof typeof CANCELLABLE_OPERATIONS;

/** 在检查点调用：已请求取消就抛 OperationCancelledError，由作业收尾统一处理 */
export function throwIfCancelled(projectId: string, operation: CancellableOperation): void {
  const spec = CANCELLABLE_OPERATIONS[operation];
  new InFlightOperations().throwIfCancelled(spec.key(projectId), spec.label);
}

export function isCancelled(projectId: string, operation: CancellableOperation): boolean {
  const spec = CANCELLABLE_OPERATIONS[operation];
  return new InFlightOperations().isCancelRequested(spec.key(projectId));
}

/** @returns false 表示当前没有在跑的该类任务，调用方据此如实提示 */
export function requestCancel(projectId: string, operation: CancellableOperation): boolean {
  const spec = CANCELLABLE_OPERATIONS[operation];
  return new InFlightOperations().requestCancel(spec.key(projectId));
}

export { OperationCancelledError };

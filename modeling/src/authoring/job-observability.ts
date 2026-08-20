import type { JsonObject } from './types.js';
import type { PiAuthoringAdapter } from './pi-adapter.js';

export const JOB_HEARTBEAT_INTERVAL_MS = 15_000;
export const JOB_HEARTBEAT_STALE_MS = 90_000;

export interface ObservableJobState {
  phase?: string;
  currentStep?: string;
  progress?: number | null;
  eta?: JsonObject;
  retryCount?: number;
  queuePosition?: number;
  errorCode?: string;
  recoveryAction?: string;
}

export function jobEtaFromProgress(progress: number, elapsedSeconds: number): JsonObject {
  const bounded = Math.max(0, Math.min(99, Number(progress) || 0));
  if (bounded < 1 || elapsedSeconds <= 0) {
    return { minMinutes: null, maxMinutes: null, confidence: 'low' };
  }
  const remainingSeconds = (elapsedSeconds / bounded) * (100 - bounded);
  const confidence = bounded >= 70 ? 'high' : bounded >= 30 ? 'medium' : 'low';
  return {
    minMinutes: Math.max(1, Math.ceil((remainingSeconds * 0.75) / 60)),
    maxMinutes: Math.max(1, Math.ceil((remainingSeconds * 1.35) / 60)),
    confidence,
  };
}

export function startJobHeartbeat(
  adapter: PiAuthoringAdapter,
  projectId: string,
  runId: string,
  state: ObservableJobState,
  intervalMs = JOB_HEARTBEAT_INTERVAL_MS,
): () => void {
  const timer = setInterval(() => {
    // 只刷新时间戳，不重复写入启动时的 phase/progress 快照，避免把实时进度回退到 0%。
    // 阶段变化由调用方显式上报；同时由适配器保护 waiting 交互不被心跳误恢复成 running。
    try { adapter.heartbeat(projectId, runId, {}); } catch { /* 任务收尾自行处理 */ }
  }, intervalMs);
  timer.unref?.();
  return () => clearInterval(timer);
}

export function jobHeartbeatStale(lastHeartbeatAt: string | undefined, now = Date.now()): boolean {
  const at = Date.parse(String(lastHeartbeatAt ?? ''));
  return !Number.isFinite(at) || now - at > JOB_HEARTBEAT_STALE_MS;
}

/**
 * 「还在跑」和「已经挂了」在界面上长得一模一样——都是一个转圈加一个往上走的秒数。
 *
 * 有了按批次的进度上报之后就能区分：超过一段时间没有新进展，就如实说出来并提示
 * 可以取消重试，而不是让用户对着转圈干等，也不是替他判定成失败（任务可能只是
 * 卡在一次很慢的模型调用上，等下去仍会出结果）。
 */

/** 超过这个时长没有新进展就提示。取最长单批耗时的量级，避免正常慢批被误报 */
export const STALL_THRESHOLD_MS = 8 * 60 * 1000;

/** 找该任务最近一条进度消息的时间 */
export function lastProgressAt({ messages = [], taskId } = {}) {
  if (!taskId) return null;
  let latest = null;
  for (const message of messages) {
    if (message?.taskId !== taskId) continue;
    const at = Date.parse(message?.createdAt ?? '');
    if (!Number.isFinite(at)) continue;
    if (latest === null || at > latest) latest = at;
  }
  return latest;
}

/**
 * @returns 停滞了多少毫秒；没有停滞或判断不了时返回 null
 */
export function stalledForMs({
  messages = [], taskId, startedAt, now = Date.now(), thresholdMs = STALL_THRESHOLD_MS,
} = {}) {
  const lastProgress = lastProgressAt({ messages, taskId });
  const started = Date.parse(startedAt ?? '');
  // 取两者较晚的。只看最近一条进度消息会误报：一个任务可能几十分钟前就有过消息
  // （比如开轮说明），而当前这次操作一秒前才启动、还来不及写进度——那不是停滞。
  // 界面上同时出现「正在处理 · 1 秒」和「已经 43 分钟没有新进展」就是这么来的。
  const candidates = [lastProgress, started].filter((value) => Number.isFinite(value));
  if (!candidates.length) return null;
  const since = Math.max(...candidates);
  const idle = now - since;
  return idle >= thresholdMs ? idle : null;
}

export function stallHint(idleMs) {
  const minutes = Math.floor(idleMs / 60_000);
  return `已经 ${minutes} 分钟没有新进展。任务可能仍在处理一次较慢的调用；`
    + `如果不想再等，可以取消后重试，已完成的部分会保留。`;
}

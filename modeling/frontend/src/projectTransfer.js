const DEFAULT_POLL_INTERVAL_MS = 600;
const DEFAULT_TIMEOUT_MS = 60 * 60_000;

export async function waitForProjectTransferJob(
  getJob,
  jobId,
  {
    onProgress,
    pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  } = {},
) {
  const startedAt = Date.now();
  while (Date.now() - startedAt <= timeoutMs) {
    const response = await getJob(jobId);
    const job = response?.job ?? response;
    if (!job || typeof job !== 'object') throw new Error('项目迁移任务状态不正确');
    onProgress?.(job.progress ?? { percent: 0, message: '' });
    if (job.status === 'completed') return job;
    if (job.status === 'failed') throw new Error(job.error || '项目迁移失败');
    await wait(pollIntervalMs);
  }
  throw new Error('项目迁移等待超时，任务可能仍在后台执行');
}

export function responseDownloadFilename(response, fallback) {
  const disposition = response.headers.get('content-disposition') || '';
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const plain = disposition.match(/filename="?([^";]+)"?/i)?.[1];
  if (encoded) {
    try { return decodeURIComponent(encoded); } catch { /* 使用普通文件名 */ }
  }
  return plain || fallback;
}

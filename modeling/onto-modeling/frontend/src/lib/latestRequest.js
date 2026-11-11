// 只认最后一次请求：迟到的响应不覆盖新结果（知识包版本对比的切换来源、选文件、轮询、上传取消）
export function createLatestRequest() {
  let current = 0;

  return {
    invalidate() { current += 1; },
    async run(load, { success, failure, settled, stale }) {
      const request = ++current;
      try {
        const value = await load();
        if (request === current) success(value);
        else if (stale) stale(value);
      } catch (error) {
        if (request === current) failure(error);
      } finally {
        if (request === current) settled();
      }
    },
  };
}

export function createAbortableRequest() {
  let controller = null;
  return {
    abort() { controller?.abort(); controller = null; },
    begin() { controller?.abort(); controller = new AbortController(); return controller.signal; },
    finish(signal) { if (controller?.signal === signal) controller = null; },
  };
}

export function createSerialPoller({ delay = 700, poll, onValue, onError, isDone }) {
  let active = true;
  let timer = null;

  const run = async () => {
    if (!active) return;
    try {
      const value = await poll();
      if (!active) return;
      onValue(value);
      if (isDone(value)) return;
    } catch (error) {
      if (!active) return;
      if (onError(error) === true) return;
    }
    if (active) timer = setTimeout(run, delay);
  };

  timer = setTimeout(run, delay);
  return () => {
    active = false;
    if (timer !== null) clearTimeout(timer);
  };
}

export function createGenerationGuard() {
  let generation = 0;
  return {
    begin() { generation += 1; return generation; },
    invalidate() { generation += 1; },
    isCurrent(value) { return value === generation; },
  };
}

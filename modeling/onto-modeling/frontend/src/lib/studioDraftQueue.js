export function enqueueStudioDraftSave(queueRef, save) {
  const queued = queueRef.current.then(save, save);
  queueRef.current = queued;
  return queued;
}

// 草稿自动暂存（develop !608）：按顺序一个个存，慢的旧请求盖不掉新内容；排到时才看有没有没存的改动、读最新内容，
// 没改动就跳过；存失败把改动标记放回去，留给下一次（或离开前的那次）重试。返回 {ok, saved, result?, error?}，不抛错
export function enqueueLatestDraftSave(queueRef, dirtyRef, save) {
  return enqueueStudioDraftSave(queueRef, async () => {
    if (!dirtyRef.current) return { ok: true, saved: false };
    dirtyRef.current = false;
    try {
      return { ok: true, saved: true, result: await save() };
    } catch (error) {
      dirtyRef.current = true;
      return { ok: false, saved: false, error };
    }
  });
}

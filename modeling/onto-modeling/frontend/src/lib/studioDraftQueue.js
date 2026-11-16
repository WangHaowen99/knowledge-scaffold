export function enqueueStudioDraftSave(queueRef, save) {
  const queued = queueRef.current.then(save, save);
  queueRef.current = queued;
  return queued;
}

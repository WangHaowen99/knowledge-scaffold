export function synchronizePackageCheck({
  projectId,
  eventTarget,
  loadCheck,
  runCheck,
  onSnapshot,
  onChecked,
  onError,
  onPackageUpdated,
  onRecheckStart,
  onRecheckFinish,
}) {
  const attemptedRevisions = new Set();
  let stopped = false;
  let requested = false;
  let running = null;

  const drain = async () => {
    while (requested && !stopped) {
      requested = false;
      try {
        const snapshot = await loadCheck(projectId);
        if (stopped) return;
        onSnapshot?.(snapshot);
        const revision = snapshot?.currentRevision;
        if (!snapshot?.stale || !revision || attemptedRevisions.has(revision)) continue;

        attemptedRevisions.add(revision);
        onRecheckStart?.(revision);
        try {
          const checked = await runCheck(projectId);
          if (stopped) return;
          onChecked?.(checked);
          eventTarget.dispatchEvent(new Event('mw-package-check-updated'));
        } catch (error) {
          attemptedRevisions.delete(revision);
          if (!stopped) onError?.(error);
        } finally {
          if (!stopped) onRecheckFinish?.(revision);
        }
      } catch (error) {
        if (!stopped) onError?.(error);
      }
    }
  };

  const refresh = () => {
    if (stopped) return Promise.resolve();
    requested = true;
    if (!running) {
      running = drain().finally(() => { running = null; });
    }
    return running;
  };
  const onPackageUpdate = () => {
    onPackageUpdated?.();
    void refresh();
  };
  const onCheckUpdate = () => { void refresh(); };
  eventTarget.addEventListener('mw-package-updated', onPackageUpdate);
  eventTarget.addEventListener('mw-package-check-updated', onCheckUpdate);
  void refresh();

  return {
    refresh,
    async whenIdle() {
      while (running) await running;
    },
    stop() {
      stopped = true;
      eventTarget.removeEventListener('mw-package-updated', onPackageUpdate);
      eventTarget.removeEventListener('mw-package-check-updated', onCheckUpdate);
    },
  };
}

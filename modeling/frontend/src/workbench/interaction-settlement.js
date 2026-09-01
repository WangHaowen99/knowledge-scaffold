export async function refreshWorkbenchAfterConfirmation({
  refreshSnapshot,
  eventTarget = globalThis.window,
} = {}) {
  let refreshError = null;
  try {
    await refreshSnapshot?.();
  } catch (error) {
    refreshError = error;
  }
  eventTarget?.dispatchEvent?.(new Event('mw-package-updated'));
  return { refreshError };
}

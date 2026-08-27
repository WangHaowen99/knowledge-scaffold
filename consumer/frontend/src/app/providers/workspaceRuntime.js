export const clearWorkspaceRuntime = ({ streamManager, chat, resetSolver }) => {
  streamManager.clearAllCaches();
  chat.resetSessionsState();
  resetSolver?.();
};

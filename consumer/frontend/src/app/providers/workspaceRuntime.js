export const clearWorkspaceRuntime = ({ streamManager, chat }) => {
  streamManager.clearAllCaches();
  chat.resetSessionsState();
};

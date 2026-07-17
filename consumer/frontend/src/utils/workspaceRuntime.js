export const clearWorkspaceRuntime = ({ streamManager, chat }) => {
  streamManager.stopAllStreams();
  streamManager.clearAllCaches();
  chat.resetSessionsState();
};

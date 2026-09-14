// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
export const clearWorkspaceRuntime = ({ streamManager, chat, resetSolver }) => {
  streamManager.clearAllCaches();
  chat.resetSessionsState();
  resetSolver?.();
};

export const FLOWCHART_UNSAVED_MESSAGE = '流程图还有未保存修改，离开后这些修改会丢失。确定离开吗？';
export const WORKSPACE_UNSAVED_MESSAGE = '当前工作区还有未保存修改，离开后这些修改会丢失。确定离开吗？';

export function confirmUnsavedNavigation(
  dirty,
  confirm = globalThis.confirm?.bind(globalThis),
  message = WORKSPACE_UNSAVED_MESSAGE,
) {
  if (!dirty) return true;
  return typeof confirm === 'function' ? Boolean(confirm(message)) : false;
}

export function protectUnsavedBeforeUnload(event, dirty) {
  if (!dirty) return false;
  event.preventDefault();
  event.returnValue = '';
  return true;
}

export function confirmFlowchartNavigation(dirty, confirm = globalThis.confirm?.bind(globalThis)) {
  return confirmUnsavedNavigation(dirty, confirm, FLOWCHART_UNSAVED_MESSAGE);
}

export function protectFlowchartBeforeUnload(event, dirty) {
  return protectUnsavedBeforeUnload(event, dirty);
}

// 后端在分析结束时会比对资料指纹，期间新增资料会让整轮分析作废（MaterialAnalysisStaleError）。
// 这个代价对用户不可见——分析动辄几分钟——所以上传前必须说清楚。
export const UPLOAD_DURING_ANALYSIS_MESSAGE = '资料分析正在进行中。现在上传新资料会让本轮分析作废，'
  + '需要重新分析全部资料。确定现在上传吗？';

export function confirmUploadDuringAnalysis(
  analysisRunning,
  confirm = globalThis.confirm?.bind(globalThis),
) {
  if (!analysisRunning) return true;
  return typeof confirm === 'function' ? Boolean(confirm(UPLOAD_DURING_ANALYSIS_MESSAGE)) : false;
}

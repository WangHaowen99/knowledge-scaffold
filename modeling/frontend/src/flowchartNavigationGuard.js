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

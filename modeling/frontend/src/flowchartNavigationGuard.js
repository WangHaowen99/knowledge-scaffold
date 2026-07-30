export const FLOWCHART_UNSAVED_MESSAGE = '流程图还有未保存修改，离开后这些修改会丢失。确定离开吗？';

export function confirmFlowchartNavigation(dirty, confirm = globalThis.confirm?.bind(globalThis)) {
  if (!dirty) return true;
  return typeof confirm === 'function' ? Boolean(confirm(FLOWCHART_UNSAVED_MESSAGE)) : false;
}

export function protectFlowchartBeforeUnload(event, dirty) {
  if (!dirty) return false;
  event.preventDefault();
  event.returnValue = '';
  return true;
}

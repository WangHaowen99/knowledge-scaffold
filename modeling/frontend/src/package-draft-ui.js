const EDITABLE_PACKAGE_FILE = /^(objects|operations|rules|metrics)\/([^/]+)\.md$/;

export function isPackageDraftEditablePath(path) {
  const match = typeof path === 'string' ? path.match(EDITABLE_PACKAGE_FILE) : null;
  return !!match && match[2] !== 'index';
}

export function latestChangedPackageDraftPath(draft) {
  const changedFiles = Array.isArray(draft?.changedFiles) ? draft.changedFiles : [];
  return changedFiles.reduce((latest, current) => {
    if (!isPackageDraftEditablePath(current?.path)) return latest;
    if (!latest) return current;
    const currentAt = Date.parse(current.updatedAt || '') || 0;
    const latestAt = Date.parse(latest.updatedAt || '') || 0;
    return currentAt >= latestAt ? current : latest;
  }, null)?.path ?? null;
}

const OPERATION_LABELS = {
  generate: '重新生成知识包',
  'package-chat': '知识包修缮对话',
  'package-chat-apply': '应用知识包修缮计划',
  'package-check': '知识包体检',
  'package-export': '导出知识包',
  'data-source-update': '保存数据源配置',
  'graph-import-upload': '上传图数据',
  'graph-import-validate': '校验图数据',
  'graph-import-execute': '导入图数据',
  evaluate: '运行知识包评测',
  'optimize-propose': '生成优化建议',
  'optimize-apply': '应用优化建议',
};

export function packageDraftOperationLabel(operation) {
  return OPERATION_LABELS[operation] ?? '当前操作';
}

export function isPackageDraftRequiredError(error) {
  return error?.data?.code === 'PACKAGE_DRAFT_REQUIRES_PUBLISH';
}

export function isPackageDraftRevisionConflict(error) {
  return error?.data?.code === 'PACKAGE_DRAFT_REVISION_CONFLICT';
}

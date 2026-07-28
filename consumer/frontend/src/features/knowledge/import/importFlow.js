export const IMPORT_PHASE = Object.freeze({
  IDLE: 'idle',
  CONFIRMING: 'confirming',
  UPLOADING: 'uploading',
  CONFLICT: 'conflict',
  SUCCESS: 'success',
  ERROR: 'error',
  CANCELLED: 'cancelled',
});

export const IMPORT_ACTION = Object.freeze({
  OVERWRITE_MATCHING: 'overwrite_matching',
  OVERWRITE_ACTIVE: 'overwrite_active',
  CREATE_VERSION: 'create_version',
});

export const ONTOLOGY_VERSION_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const IMPORT_WARNING_PREVIEW_LIMIT = 10;

export const IMPORT_SAFETY_COPY = Object.freeze({
  title: '确认导入知识包',
  body: '导入包可能包含脚本、映射和业务定义。系统会校验路径与格式，上传者仍需确认来源可信、内容正确且不存在安全风险。',
  acknowledgement: '我已核对文件来源，并愿意对导入内容负责。',
});

export const IMPORT_STOP_WAITING_COPY = Object.freeze({
  action: '停止等待',
  body: '停止等待只会结束当前页面对响应的等待。服务端可能仍在解析或写入，导入结果也可能已经生效；页面稍后会自动刷新核对。',
});

export function createImportFlowState() {
  return {
    phase: IMPORT_PHASE.IDLE,
    file: null,
    acknowledged: false,
    conflict: null,
    newVersion: '',
    request: null,
    result: null,
    error: '',
    stoppedWaiting: false,
    reconciliationPending: false,
  };
}

export function validateImportFile(file) {
  if (!file) return '未选择知识包文件';
  if (!String(file.name || '').toLowerCase().endsWith('.zip')) return '仅支持 .zip 知识包';
  if (!Number(file.size || 0)) return '所选 ZIP 文件为空';
  return '';
}

export function normalizeImportConflict(payload = {}) {
  const conflictType = String(payload.conflict_type || '');
  return {
    type: conflictType,
    ontologyId: String(payload.ontology_id || ''),
    ontologyName: String(payload.ontology_name || payload.ontology_id || ''),
    version: String(payload.version || ''),
    activeVersion: String(payload.active_version || ''),
    versions: Array.isArray(payload.versions) ? payload.versions.map(String) : [],
    message: String(payload.error || payload.detail || ''),
    overwriteAction: conflictType === 'version_exists'
      ? IMPORT_ACTION.OVERWRITE_MATCHING
      : IMPORT_ACTION.OVERWRITE_ACTIVE,
  };
}

export function validateImportDecision(state, action) {
  if (!state?.conflict) return '当前没有待处理的版本冲突';
  if (action === IMPORT_ACTION.CREATE_VERSION) {
    const version = String(state.newVersion || '').trim();
    if (!version) return '请输入新增版本号';
    if (!ONTOLOGY_VERSION_PATTERN.test(version)) {
      return '版本号仅支持字母、数字、点、下划线和短横线，且需以字母或数字开头';
    }
    if (state.conflict.versions.includes(version)) return `版本 ${version} 已存在，请换一个版本号`;
    return '';
  }
  if (action !== state.conflict.overwriteAction) return '该冲突不支持所选覆盖方式';
  return '';
}

export function importRequestFields(request = {}) {
  const action = String(request.action || '');
  if (!action) return {};
  if (action === IMPORT_ACTION.CREATE_VERSION) {
    return { action, new_version: String(request.newVersion || '').trim() };
  }
  return { action, overwrite: 'true' };
}

export function reduceImportFlow(state, event) {
  const current = state || createImportFlowState();
  switch (event?.type) {
    case 'SELECT_FILE': {
      const error = validateImportFile(event.file);
      if (error) {
        return { ...createImportFlowState(), phase: IMPORT_PHASE.ERROR, file: event.file || null, error };
      }
      return {
        ...createImportFlowState(),
        phase: IMPORT_PHASE.CONFIRMING,
        file: event.file,
      };
    }
    case 'SET_ACKNOWLEDGED':
      return current.phase === IMPORT_PHASE.CONFIRMING
        ? { ...current, acknowledged: Boolean(event.value), error: '' }
        : current;
    case 'START_UPLOAD':
      if (current.phase !== IMPORT_PHASE.CONFIRMING || !current.acknowledged) {
        return { ...current, error: '请先确认导入安全边界' };
      }
      return {
        ...current,
        phase: IMPORT_PHASE.UPLOADING,
        request: { action: '', newVersion: '' },
        error: '',
        result: null,
      };
    case 'CONFLICT':
      return {
        ...current,
        phase: IMPORT_PHASE.CONFLICT,
        conflict: normalizeImportConflict(event.payload),
        newVersion: '',
        error: '',
      };
    case 'SET_NEW_VERSION':
      return current.phase === IMPORT_PHASE.CONFLICT
        ? { ...current, newVersion: String(event.value || ''), error: '' }
        : current;
    case 'RESOLVE_CONFLICT': {
      const error = validateImportDecision(current, event.action);
      if (error) return { ...current, error };
      const request = {
        action: event.action,
        newVersion: event.action === IMPORT_ACTION.CREATE_VERSION
          ? String(current.newVersion || '').trim()
          : '',
      };
      return { ...current, phase: IMPORT_PHASE.UPLOADING, request, error: '' };
    }
    case 'FAILURE':
      return {
        ...current,
        phase: IMPORT_PHASE.ERROR,
        error: String(event.error || '知识包导入失败'),
      };
    case 'RETRY':
      return current.file && current.request
        ? { ...current, phase: IMPORT_PHASE.UPLOADING, error: '' }
        : current;
    case 'REOPEN_CONFLICT':
      return current.conflict
        ? { ...current, phase: IMPORT_PHASE.CONFLICT, error: '' }
        : current;
    case 'SUCCESS': {
      const result = event.payload || {};
      return {
        ...current,
        phase: IMPORT_PHASE.SUCCESS,
        result: {
          ...result,
          preview: buildImportPreview(result),
        },
        conflict: null,
        file: null,
        error: '',
      };
    }
    case 'CLEAR_PREVIEW':
      return current.result
        ? { ...current, result: { ...current.result, preview: null } }
        : current;
    case 'CANCEL': {
      const stoppedWaiting = current.phase === IMPORT_PHASE.UPLOADING;
      return {
        ...createImportFlowState(),
        phase: IMPORT_PHASE.CANCELLED,
        stoppedWaiting,
        reconciliationPending: stoppedWaiting,
      };
    }
    case 'RECONCILED':
      return current.phase === IMPORT_PHASE.CANCELLED && current.reconciliationPending
        ? { ...current, reconciliationPending: false }
        : current;
    case 'RESET':
      return createImportFlowState();
    default:
      return current;
  }
}

export function importFlowNotice(state) {
  switch (state?.phase) {
    case IMPORT_PHASE.CONFIRMING:
      return `待确认导入：${state.file?.name || ''}`;
    case IMPORT_PHASE.UPLOADING:
      return `正在上传并解析：${state.file?.name || ''}`;
    case IMPORT_PHASE.CONFLICT:
      return `检测到版本冲突：${state.conflict?.ontologyName || state.conflict?.ontologyId || ''}`;
    case IMPORT_PHASE.SUCCESS: {
      const result = state.result || {};
      const activeVersion = result.active_version || result.version || '';
      const warningMessages = importWarningMessages(importResultWarningDetails(result));
      const warningPreview = warningMessages.slice(0, IMPORT_WARNING_PREVIEW_LIMIT);
      const hiddenWarningCount = warningMessages.length - warningPreview.length;
      const warningNotice = warningMessages.length
        ? `；警告 ${warningMessages.length} 条：${warningPreview.join('；')}${hiddenWarningCount ? `；其余 ${hiddenWarningCount} 条未展示` : ''}`
        : '';
      return `导入成功：${result.ontology_name || result.ontology_id || '知识包'}（对象 ${result.objects_count ?? '-'} · 操作 ${result.operations_count ?? '-'}${activeVersion ? ` · 激活版本 ${activeVersion}` : ''}）${warningNotice}`;
    }
    case IMPORT_PHASE.ERROR:
      return `导入失败：${state.error || '请求异常，请稍后重试'}`;
    case IMPORT_PHASE.CANCELLED:
      if (!state.stoppedWaiting) return '已关闭知识包导入';
      return state.reconciliationPending
        ? '已停止等待导入结果；服务端处理可能仍在执行或已经生效，正在延迟刷新核对。'
        : '已执行一次延迟刷新核对；服务端仍可能继续处理，请稍后再次刷新并核对知识包列表和激活版本。';
    default:
      return '';
  }
}

export function importFlowNoticeTone(state) {
  switch (state?.phase) {
    case IMPORT_PHASE.SUCCESS:
      return 'success';
    case IMPORT_PHASE.ERROR:
      return 'error';
    case IMPORT_PHASE.CONFIRMING:
    case IMPORT_PHASE.UPLOADING:
    case IMPORT_PHASE.CONFLICT:
    case IMPORT_PHASE.CANCELLED:
      return 'info';
    default:
      return undefined;
  }
}

function buildImportPreview(result) {
  const preview = result.preview;
  const warnings = importResultWarningDetails(result);
  const hasWarnings = Array.isArray(warnings)
    ? warnings.length > 0
    : Object.keys(warnings).length > 0;
  if (!hasWarnings) return preview || null;
  if (preview && typeof preview === 'object' && !Array.isArray(preview)) {
    return { ...preview, warnings };
  }
  return {
    ...(preview ? { preview } : {}),
    warnings,
  };
}

function importResultWarningDetails(result = {}) {
  const warnings = importWarningDetails(result.warnings);
  const normalizationWarnings = importWarningMessages(result.normalization_warnings);
  if (!normalizationWarnings.length) return warnings;

  if (Array.isArray(warnings)) {
    return {
      ...(warnings.length ? { general: warnings } : {}),
      normalization_warnings: normalizationWarnings,
    };
  }

  return {
    ...warnings,
    normalization_warnings: [
      ...importWarningMessages(warnings.normalization_warnings),
      ...normalizationWarnings,
    ],
  };
}

function importWarningMessages(warnings) {
  const values = Array.isArray(warnings)
    ? warnings
    : warnings && typeof warnings === 'object'
      ? Object.values(warnings).flatMap((value) => (Array.isArray(value) ? value : [value]))
      : warnings
        ? [warnings]
        : [];
  return values.map((value) => String(value || '').trim()).filter(Boolean);
}

function importWarningDetails(warnings) {
  if (Array.isArray(warnings)) return importWarningMessages(warnings);
  if (!warnings || typeof warnings !== 'object') {
    return importWarningMessages(warnings);
  }
  return Object.fromEntries(
    Object.entries(warnings)
      .map(([category, values]) => [category, importWarningMessages(values)])
      .filter(([, values]) => values.length > 0),
  );
}

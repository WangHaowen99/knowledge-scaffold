import assert from 'node:assert/strict';
import test from 'node:test';

import {
  IMPORT_ACTION,
  IMPORT_PHASE,
  IMPORT_SAFETY_COPY,
  IMPORT_STOP_WAITING_COPY,
  createImportFlowState,
  importFlowNotice,
  importFlowNoticeTone,
  importRequestFields,
  normalizeImportConflict,
  reduceImportFlow,
  validateImportDecision,
  validateImportFile,
} from './importFlow.js';

const zip = { name: 'demo.zip', size: 128 };

test('safety confirmation states uploader responsibility boundaries', () => {
  assert.match(IMPORT_SAFETY_COPY.body, /来源可信/);
  assert.match(IMPORT_SAFETY_COPY.body, /内容正确/);
  assert.match(IMPORT_SAFETY_COPY.body, /安全风险/);
  assert.match(IMPORT_SAFETY_COPY.acknowledgement, /负责/);
});

test('import file validation rejects missing, non-zip and empty files', () => {
  assert.equal(validateImportFile(null), '未选择知识包文件');
  assert.match(validateImportFile({ name: 'demo.tar', size: 1 }), /\.zip/);
  assert.match(validateImportFile({ name: 'demo.zip', size: 0 }), /为空/);
  assert.equal(validateImportFile(zip), '');
});

test('safety acknowledgement gates the first upload', () => {
  const selected = reduceImportFlow(createImportFlowState(), { type: 'SELECT_FILE', file: zip });
  assert.equal(selected.phase, IMPORT_PHASE.CONFIRMING);
  assert.match(reduceImportFlow(selected, { type: 'START_UPLOAD' }).error, /确认/);

  const acknowledged = reduceImportFlow(selected, { type: 'SET_ACKNOWLEDGED', value: true });
  const uploading = reduceImportFlow(acknowledged, { type: 'START_UPLOAD' });
  assert.equal(uploading.phase, IMPORT_PHASE.UPLOADING);
  assert.deepEqual(uploading.request, { action: '', newVersion: '' });
});

test('conflict type determines the only valid overwrite action', () => {
  const matching = normalizeImportConflict({
    conflict_type: 'version_exists',
    ontology_id: 'demo',
    version: '1.0.0',
  });
  const active = normalizeImportConflict({
    conflict_type: 'missing_version_existing',
    ontology_id: 'demo',
    active_version: '2.0.0',
  });
  assert.equal(matching.overwriteAction, IMPORT_ACTION.OVERWRITE_MATCHING);
  assert.equal(active.overwriteAction, IMPORT_ACTION.OVERWRITE_ACTIVE);
});

test('create version validates syntax and rejects an existing version', () => {
  const state = {
    ...createImportFlowState(),
    phase: IMPORT_PHASE.CONFLICT,
    conflict: normalizeImportConflict({ conflict_type: 'version_exists', versions: ['1.0.0'] }),
    newVersion: '',
  };
  assert.match(validateImportDecision(state, IMPORT_ACTION.CREATE_VERSION), /请输入/);
  assert.match(validateImportDecision({ ...state, newVersion: '.bad' }, IMPORT_ACTION.CREATE_VERSION), /仅支持/);
  assert.match(validateImportDecision({ ...state, newVersion: '1.0.0' }, IMPORT_ACTION.CREATE_VERSION), /已存在/);
  assert.equal(validateImportDecision({ ...state, newVersion: '1.0.1-rc.1' }, IMPORT_ACTION.CREATE_VERSION), '');
});

test('failed conflict request keeps retry and return-to-decision context', () => {
  let state = reduceImportFlow(createImportFlowState(), { type: 'SELECT_FILE', file: zip });
  state = reduceImportFlow(state, { type: 'SET_ACKNOWLEDGED', value: true });
  state = reduceImportFlow(state, { type: 'START_UPLOAD' });
  state = reduceImportFlow(state, {
    type: 'CONFLICT',
    payload: { conflict_type: 'version_exists', versions: ['1.0.0'] },
  });
  state = reduceImportFlow(state, { type: 'SET_NEW_VERSION', value: '1.0.1' });
  state = reduceImportFlow(state, { type: 'RESOLVE_CONFLICT', action: IMPORT_ACTION.CREATE_VERSION });
  state = reduceImportFlow(state, { type: 'FAILURE', error: '网络异常' });
  assert.equal(state.phase, IMPORT_PHASE.ERROR);
  assert.deepEqual(state.request, { action: IMPORT_ACTION.CREATE_VERSION, newVersion: '1.0.1' });
  assert.equal(reduceImportFlow(state, { type: 'RETRY' }).phase, IMPORT_PHASE.UPLOADING);
  assert.equal(reduceImportFlow(state, { type: 'REOPEN_CONFLICT' }).phase, IMPORT_PHASE.CONFLICT);
});

test('name conflict requires package rename and cannot retry the same upload', () => {
  const file = { name: 'package-beta.zip', size: 10 };
  let state = reduceImportFlow(createImportFlowState(), { type: 'SELECT_FILE', file });
  state = reduceImportFlow(state, { type: 'SET_ACKNOWLEDGED', value: true });
  state = reduceImportFlow(state, { type: 'START_UPLOAD' });
  state = { ...state, conflict: normalizeImportConflict({ conflict_type: 'version_exists' }) };
  state = reduceImportFlow(state, {
    type: 'FAILURE',
    error: '知识包名称已存在，请修改名称后重新导入。',
    errorCode: 'ontology_name_conflict',
  });

  assert.equal(state.phase, IMPORT_PHASE.ERROR);
  assert.equal(state.errorCode, 'ontology_name_conflict');
  assert.equal(state.request, null);
  assert.equal(state.conflict, null);
  assert.equal(reduceImportFlow(state, { type: 'RETRY' }).phase, IMPORT_PHASE.ERROR);
  assert.match(importFlowNotice(state), /修改名称后重新导入/);
});

test('request fields match backend import contract', () => {
  assert.deepEqual(importRequestFields({}), {});
  assert.deepEqual(importRequestFields({ action: IMPORT_ACTION.OVERWRITE_MATCHING }), {
    action: 'overwrite_matching',
    overwrite: 'true',
  });
  assert.deepEqual(importRequestFields({ action: IMPORT_ACTION.CREATE_VERSION, newVersion: '2.0.0' }), {
    action: 'create_version',
    new_version: '2.0.0',
  });
});

test('cancel clears retained file, conflict and retry context', () => {
  const cancelled = reduceImportFlow({
    ...createImportFlowState(),
    phase: IMPORT_PHASE.ERROR,
    file: zip,
    conflict: normalizeImportConflict({ conflict_type: 'version_exists' }),
    request: { action: IMPORT_ACTION.OVERWRITE_MATCHING, newVersion: '' },
  }, { type: 'CANCEL' });
  assert.equal(cancelled.phase, IMPORT_PHASE.CANCELLED);
  assert.equal(cancelled.file, null);
  assert.equal(cancelled.conflict, null);
  assert.equal(cancelled.request, null);
  assert.equal(cancelled.stoppedWaiting, false);
  assert.equal(cancelled.reconciliationPending, false);
});

test('stopping an in-flight import keeps server-side semantics explicit until reconciliation', () => {
  const stopped = reduceImportFlow({
    ...createImportFlowState(),
    phase: IMPORT_PHASE.UPLOADING,
    file: zip,
  }, { type: 'CANCEL' });

  assert.equal(stopped.phase, IMPORT_PHASE.CANCELLED);
  assert.equal(stopped.stoppedWaiting, true);
  assert.equal(stopped.reconciliationPending, true);
  assert.match(IMPORT_STOP_WAITING_COPY.body, /服务端可能仍在/);
  assert.match(importFlowNotice(stopped), /可能仍在执行或已经生效/);

  const reconciled = reduceImportFlow(stopped, { type: 'RECONCILED' });
  assert.equal(reconciled.reconciliationPending, false);
  assert.match(importFlowNotice(reconciled), /延迟刷新核对/);
});

test('success notice identifies the version that became active', () => {
  const state = reduceImportFlow(createImportFlowState(), {
    type: 'SUCCESS',
    payload: { ontology_id: 'demo', active_version: '2.0.0', objects_count: 1, operations_count: 2 },
  });
  assert.match(importFlowNotice(state), /激活版本 2\.0\.0/);
});

test('success notice displays categorized import warnings', () => {
  const state = reduceImportFlow(createImportFlowState(), {
    type: 'SUCCESS',
    payload: {
      ontology_id: 'demo',
      objects_count: 1,
      operations_count: 2,
      warnings: {
        object_aliases: ['对象别名重复'],
        field_aliases: ['字段别名包含空值'],
      },
    },
  });
  const notice = importFlowNotice(state);
  assert.match(notice, /警告 2 条/);
  assert.match(notice, /对象别名重复/);
  assert.match(notice, /字段别名包含空值/);
});

test('success notice and preview include normalization warnings', () => {
  const state = reduceImportFlow(createImportFlowState(), {
    type: 'SUCCESS',
    payload: {
      ontology_id: 'demo',
      objects_count: 1,
      operations_count: 2,
      normalization_warnings: ['脚本路径已规范化'],
    },
  });

  const notice = importFlowNotice(state);
  assert.match(notice, /警告 1 条/);
  assert.match(notice, /脚本路径已规范化/);
  assert.deepEqual(state.result.preview, {
    warnings: {
      normalization_warnings: ['脚本路径已规范化'],
    },
  });
});

test('success notice and preview merge import and normalization warnings', () => {
  const state = reduceImportFlow(createImportFlowState(), {
    type: 'SUCCESS',
    payload: {
      ontology_id: 'demo',
      objects_count: 1,
      operations_count: 2,
      preview: { source: { total_files: 2 } },
      warnings: {
        object_aliases: ['对象别名重复'],
      },
      normalization_warnings: ['脚本路径已规范化'],
    },
  });

  const notice = importFlowNotice(state);
  assert.match(notice, /警告 2 条/);
  assert.match(notice, /对象别名重复/);
  assert.match(notice, /脚本路径已规范化/);
  assert.deepEqual(state.result.preview, {
    source: { total_files: 2 },
    warnings: {
      object_aliases: ['对象别名重复'],
      normalization_warnings: ['脚本路径已规范化'],
    },
  });
});

test('success notice tone is not inferred from warning text', () => {
  const state = reduceImportFlow(createImportFlowState(), {
    type: 'SUCCESS',
    payload: {
      ontology_id: 'demo',
      objects_count: 1,
      operations_count: 2,
      warnings: {
        operations: ['parameters声明解析失败: invalid YAML'],
      },
    },
  });

  assert.match(importFlowNotice(state), /解析失败/);
  assert.equal(importFlowNoticeTone(state), 'success');
});

test('import notice tone follows the import phase', () => {
  assert.equal(importFlowNoticeTone({ phase: IMPORT_PHASE.ERROR }), 'error');
  assert.equal(importFlowNoticeTone({ phase: IMPORT_PHASE.UPLOADING }), 'info');
  assert.equal(importFlowNoticeTone({ phase: IMPORT_PHASE.CANCELLED }), 'info');
  assert.equal(importFlowNoticeTone({ phase: IMPORT_PHASE.IDLE }), undefined);
});

test('import preview retains all categorized warnings', () => {
  const warnings = Array.from(
    { length: 12 },
    (_, index) => `警告项${String(index + 1).padStart(2, '0')}`,
  );
  const state = reduceImportFlow(createImportFlowState(), {
    type: 'SUCCESS',
    payload: {
      preview: {
        source: { total_files: 2 },
        result: { total_files: 3 },
      },
      warnings: {
        field_aliases: warnings,
        object_aliases: [],
      },
    },
  });

  assert.deepEqual(state.result.preview, {
    source: { total_files: 2 },
    result: { total_files: 3 },
    warnings: { field_aliases: warnings },
  });

  const cleared = reduceImportFlow(state, { type: 'CLEAR_PREVIEW' });
  assert.equal(cleared.result.preview, null);
});

test('success notice displays at most ten import warnings', () => {
  const warnings = Array.from(
    { length: 12 },
    (_, index) => `警告项${String(index + 1).padStart(2, '0')}`,
  );
  const state = reduceImportFlow(createImportFlowState(), {
    type: 'SUCCESS',
    payload: {
      ontology_id: 'demo',
      objects_count: 1,
      operations_count: 2,
      warnings: { field_aliases: warnings },
    },
  });

  const notice = importFlowNotice(state);
  assert.match(notice, /警告 12 条/);
  assert.match(notice, /警告项01/);
  assert.match(notice, /警告项10/);
  assert.doesNotMatch(notice, /警告项11/);
  assert.doesNotMatch(notice, /警告项12/);
  assert.match(notice, /其余 2 条未展示/);
});

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { refreshWorkbenchAfterConfirmation } from '../frontend/src/workbench/interaction-settlement.js';

import {
  isStudioNoChangesFailure,
  studioChangedFields,
  studioDeleteCopy,
  studioDraftFieldValue,
  studioIncompleteDraftField,
  studioRelationElements,
  studioSaveStateAfterDraftChange,
} from '../frontend/src/workbench/studio/studio-entry-state.js';

test('Studio 字段修正后清除对应错误并恢复 dirty 状态', () => {
  const state = studioSaveStateAfterDraftChange({
    phase: 'error',
    message: '部分字段不符合知识包约束',
    failure: { code: 'INVALID_STUDIO_INPUT' },
    fieldErrors: {
      'changes.description': '必填文本不能为空',
      'changes.relations[0].targetId': '关系目标不存在',
    },
  }, {
    baseline: { description: '原说明', relations: [] },
    draft: { description: '新说明', relations: [] },
    field: 'description',
  });

  assert.equal(state.phase, 'dirty');
  assert.equal(state.failure, null);
  assert.equal(state.fieldErrors['changes.description'], undefined);
  assert.equal(state.fieldErrors['changes.relations[0].targetId'], '关系目标不存在');
});

test('Studio 删除必填内容时立即返回字段错误，补回后清除错误', () => {
  const baseline = { title: '规则名称', content: '必须满足规则' };
  const empty = { ...baseline, content: '' };
  const invalid = studioSaveStateAfterDraftChange({ phase: 'idle', fieldErrors: {} }, {
    baseline,
    draft: empty,
    field: 'content',
    incompleteField: 'content',
  });
  assert.deepEqual(invalid, {
    phase: 'error',
    message: '请先补完整必填内容',
    failure: null,
    fieldErrors: { 'changes.content': '必填内容不能为空' },
  });

  const corrected = studioSaveStateAfterDraftChange(invalid, {
    baseline,
    draft: { ...baseline, content: '新的规则内容' },
    field: 'content',
  });
  assert.equal(corrected.phase, 'dirty');
  assert.deepEqual(corrected.fieldErrors, {});
});

test('Studio 草稿改回基线后立即回到已同步状态', () => {
  const baseline = { title: '记录', description: '原说明', aliases: ['record'] };
  const state = studioSaveStateAfterDraftChange({
    phase: 'dirty',
    message: '有未保存修改',
    failure: null,
    fieldErrors: { 'changes.description': '旧错误' },
  }, {
    baseline,
    draft: structuredClone(baseline),
    field: 'description',
  });

  assert.deepEqual(state, { phase: 'idle', message: '', failure: null, fieldErrors: {} });
  assert.deepEqual(studioChangedFields(baseline, structuredClone(baseline)), {});
});

test('Studio 只读冲突态优先显示本地草稿字段并回退服务端值', () => {
  const values = { title: '服务端名称', description: '服务端说明', evidence: ['material:server'] };
  const draft = { title: '本地名称', description: '' };

  assert.equal(studioDraftFieldValue(draft, values, 'title'), '本地名称');
  assert.equal(studioDraftFieldValue(draft, values, 'description'), '');
  assert.deepEqual(studioDraftFieldValue(draft, values, 'evidence'), ['material:server']);
});

test('Studio 保存进行中改回旧基线仍保留为待保存，避免覆盖竞态', () => {
  const baseline = { description: '原说明' };
  const state = studioSaveStateAfterDraftChange({ phase: 'saving', fieldErrors: {} }, {
    baseline,
    draft: structuredClone(baseline),
    field: 'description',
    saving: true,
  });

  assert.equal(state.phase, 'dirty');
});

test('Studio 将 NO_CHANGES 识别为同步结果并明确整条删除语义', () => {
  assert.equal(isStudioNoChangesFailure({ code: 'NO_CHANGES' }), true);
  assert.equal(isStudioNoChangesFailure({ code: 'REVISION_CONFLICT' }), false);
  assert.deepEqual(studioDeleteCopy('rule', '记录有效规则'), {
    button: '删除整个规则',
    heading: '确认删除整个规则',
    clarification: '这会删除整个规则《记录有效规则》，不是只撤销字段修改。',
  });
});

test('Studio 结构化空行在本地等待补全，关系目标合并完整图节点', () => {
  assert.equal(studioIncompleteDraftField({
    inputs: [{ param: '', type: '', required: false }],
  }, {
    fields: [{ key: 'inputs', type: 'parameter_table' }],
  }), 'inputs');
  assert.equal(studioIncompleteDraftField({
    fields: [{ key: 'status', type: '' }],
  }, {
    fields: [{ key: 'fields', type: 'field_table' }],
  }), 'fields');
  assert.equal(studioIncompleteDraftField({
    inputs: [{ param: 'record_id', type: 'string', required: true }],
  }, {
    fields: [{ key: 'inputs', type: 'parameter_table' }],
  }), '');
  assert.equal(studioIncompleteDraftField({
    title: '记录有效规则',
    content: '   ',
  }, {
    fields: [
      { key: 'title', type: 'text', required: true },
      { key: 'content', type: 'long_text', required: true },
    ],
  }), 'content');
  assert.equal(studioIncompleteDraftField({
    nullPolicy: [{ field: 'amount', handling: '按零处理' }],
  }, {
    fields: [{ key: 'nullPolicy', type: 'key_value_table' }],
  }), '');

  const elements = studioRelationElements([
    { id: 'object:first', kind: 'object', key: 'first', title: '第一个对象' },
  ], {
    nodes: [
      { id: 'object:first', nodeType: 'element', kind: 'object', label: '第一个对象' },
      { id: 'object:later', nodeType: 'element', kind: 'object', label: '后续对象' },
    ],
  });
  assert.deepEqual(elements.map((item) => item.id), ['object:first', 'object:later']);
  assert.equal(elements[1].key, 'later');

  const firstPage = Array.from({ length: 200 }, (_item, index) => ({
    id: `object:item_${index + 1}`,
    kind: 'object',
    key: `item_${index + 1}`,
    title: `合成对象 ${index + 1}`,
  }));
  const completeGraph = {
    nodes: Array.from({ length: 205 }, (_item, index) => ({
      id: `object:item_${index + 1}`,
      nodeType: 'element',
      kind: 'object',
      label: `合成对象 ${index + 1}`,
    })),
  };
  const relationTargets = studioRelationElements(firstPage, completeGraph);
  assert.equal(relationTargets.length, 205);
  assert.equal(relationTargets.at(-1).id, 'object:item_205');
});

test('StudioView 接线字段纠错、NO_CHANGES 同步和完整资源删除文案', () => {
  const source = readFileSync(
    new URL('../frontend/src/workbench/studio/StudioView.jsx', import.meta.url),
    'utf8',
  );

  assert.match(source, /pausedRef\.current = false;[\s\S]*studioSaveStateAfterDraftChange/);
  assert.match(source, /if \(isStudioNoChangesFailure\(failure\)\)/);
  assert.match(source, /studioDeleteCopy\(detail\.kind, detail\.title\)\.button/);
  assert.match(source, /remember\(\{ \.\.\.draftRef\.current, \[field\.key\]: value \}, field\.key\)/);
  assert.match(source, /studioIncompleteDraftField\(draft, kindSchema\)/);
  assert.match(source, /incompleteField && !previousIncompleteField[\s\S]*onIncompleteDraft/);
  assert.match(source, /onIncompleteDraft=\{probeStudioRevision\}/);
  assert.match(source, /if \(!canUpdate\) \{[\s\S]*当前项目处于只读状态/);
  assert.match(source, /failure\.code === 'STUDIO_WRITE_LOCKED'[\s\S]*getStudioSchema\(projectId\)/);
  assert.match(source, /studioDraftFieldValue\(draft, detail\.values, field\.key\)/);
  assert.match(source, /baseRevision: previousDraft\?\.baseRevision \|\| revisionRef\.current/);
  assert.match(source, /refreshPending: _refreshPending/);
  const deleteDialogSource = source.slice(
    source.indexOf('function StudioDeleteDialog'),
    source.indexOf('function StudioCreateDialog'),
  );
  assert.doesNotMatch(deleteDialogSource, /requestStudioReview/);
  const entrySource = source.slice(
    source.indexOf('function StudioEntry'),
    source.indexOf('function StudioSaveState'),
  );
  assert.doesNotMatch(entrySource, /requestStudioReview/);
  assert.match(source, /StudioPendingReviewBanner/);
  assert.match(source, /检查并应用修改/);
  assert.match(source, /response\.applied/);
  assert.match(source, /不调用建模助手/);
  assert.match(source, /studioHasPendingLocalEdits\(sessionRef\.current\)/);
  assert.match(source, /disabled=\{state\.phase === 'saving' \|\| blocked\}/);

  const centerPane = readFileSync(
    new URL('../frontend/src/workbench/center/CenterPane.jsx', import.meta.url),
    'utf8',
  );
  assert.match(centerPane, /focus\.source === 'agent'/);
  assert.match(centerPane, /停止自动定位/);

  const appStoreSource = readFileSync(
    new URL('../frontend/src/workbench/lib/appStore.js', import.meta.url),
    'utf8',
  );
  assert.match(appStoreSource, /const agentTarget = \{ \.\.\.visibleFocus, source: 'agent' \}/);
  assert.match(source, /description\.trim\(\)/);
  assert.match(source, /kind === 'operation' \? \{ flow: \[coreContent\.trim\(\)\] \}/);
  assert.match(source, /state\.preview\.textReferences/);

  const topBar = readFileSync(
    new URL('../frontend/src/workbench/shell/TopBar.jsx', import.meta.url),
    'utf8',
  );
  assert.match(topBar, /Math\.max\(interaction \? 1 : 0, Number\(studioPendingCount\) \|\| 0\)/);
  assert.match(topBar, /className="pill pill-wait wb-pending-pill"/);
  assert.match(topBar, /mw-pending-attention/);
  assert.match(topBar, /target: '知识包 Studio', view: 'table'/);

  const appStore = readFileSync(
    new URL('../frontend/src/workbench/lib/appStore.js', import.meta.url),
    'utf8',
  );
  assert.match(appStore, /interaction\?\.kind === 'confirmation'/);
  assert.match(appStore, /refreshWorkbenchAfterConfirmation/);

  const rightPane = readFileSync(
    new URL('../frontend/src/workbench/right/RightPane.jsx', import.meta.url),
    'utf8',
  );
  assert.match(rightPane, /window\.addEventListener\('mw-pending-attention', focusPending\)/);
  assert.match(rightPane, /answer\.displayText && answer\.text/);
  assert.match(rightPane, /补充说明：\$\{visible\(answer\.text\)\}/);

  const blocks = readFileSync(
    new URL('../frontend/src/workbench/right/blocks/Blocks.jsx', import.meta.url),
    'utf8',
  );
  assert.match(blocks, /接受后才会写入正式版本；退回调整会保留 Studio 工作草稿并解除编辑锁/);
});

test('确认结算后即使快照刷新失败也会通知 Studio 重新读取动态租约', async () => {
  const events = new EventTarget();
  let notified = 0;
  events.addEventListener('mw-package-updated', () => { notified += 1; });

  const success = await refreshWorkbenchAfterConfirmation({
    refreshSnapshot: async () => {},
    eventTarget: events,
  });
  assert.equal(success.refreshError, null);
  assert.equal(notified, 1);

  const failure = new Error('snapshot unavailable');
  const failed = await refreshWorkbenchAfterConfirmation({
    refreshSnapshot: async () => { throw failure; },
    eventTarget: events,
  });
  assert.equal(failed.refreshError, failure);
  assert.equal(notified, 2);
});

test('Studio 草稿持久化到 sessionStorage 并可在重新加载时恢复', async () => {
  const values = new Map<string, string>();
  const priorStorage = globalThis.sessionStorage;
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); },
      removeItem: (key: string) => { values.delete(key); },
    },
  });
  try {
    const studioModel = await import('../frontend/src/workbench/studio/studio-model.js');
    const projectId = `draft-project-${Date.now()}`;
    const session = studioModel.readStudioSession(projectId);
    studioModel.writeStudioSession(projectId, {
      ...session,
      entryDrafts: { 'object:record': { draft: { title: '本地草稿' } } },
    });
    assert.match([...values.values()][0], /本地草稿/u);
    studioModel.resetStudioSession(projectId);
    assert.equal(values.size, 0);
  } finally {
    Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: priorStorage });
  }
});

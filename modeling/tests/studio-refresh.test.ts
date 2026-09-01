import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const [
  { createProjectEventReplayGate },
  {
    createStudioProjectEventBaseline,
    markStudioDraftsForRefresh,
    STUDIO_PROJECT_REFRESH_EVENT_TYPES,
    planStudioProjectEvent,
    planStudioWindowRefresh,
    studioHasPendingLocalEdits,
    studioPendingDraftRefresh,
    studioRefreshNoticeMessage,
  },
] = await Promise.all([
  import('../frontend/src/projectEventReplay.js'),
  import('../frontend/src/workbench/studio/studioRefresh.js'),
]);

test('Studio 只对正式 revision 事件触发外部刷新', () => {
  assert.equal(STUDIO_PROJECT_REFRESH_EVENT_TYPES.has('package.revision.created'), true);
  assert.equal(STUDIO_PROJECT_REFRESH_EVENT_TYPES.has('package.candidate.created'), false);
  assert.equal(STUDIO_PROJECT_REFRESH_EVENT_TYPES.has('authoring.interaction.opened'), false);
});

test('Studio 事件基线绑定当前项目并采用最新可用游标', () => {
  assert.deepEqual(createStudioProjectEventBaseline({
    projectId: ' project-next ',
    snapshot: { authoring: { lastEventSeq: 18 } },
  }), {
    projectId: 'project-next',
    cursor: 18,
    ready: true,
  });

  assert.deepEqual(createStudioProjectEventBaseline({
    projectId: 'project-next',
    snapshot: { authoring: { lastEventSeq: -7 } },
  }), {
    projectId: 'project-next',
    cursor: 0,
    ready: true,
  });
});

test('Studio 事件基线在项目缺失时保持未就绪且安全回退到零游标', () => {
  assert.deepEqual(createStudioProjectEventBaseline({
    projectId: '',
    snapshot: { authoring: { lastEventSeq: -4 } },
  }), {
    projectId: '',
    cursor: 0,
    ready: false,
  });
});

test('Studio 在干净态会自动跟随外部正式 revision', () => {
  const plan = planStudioProjectEvent({
    event: { type: 'package.revision.created', seq: 12, payload: { revisionId: 'rev-next' } },
    gate: createProjectEventReplayGate(0),
    localEdits: false,
    currentRevision: 'rev-current',
  });

  assert.deepEqual(plan, {
    gate: { replayThrough: 0, replaying: false, pendingRefresh: false },
    historical: false,
    relevant: true,
    revisionId: 'rev-next',
    scheduleLoad: true,
    deferLoad: false,
  });
});

test('Studio 在条目有本地草稿时只延后外部正式 revision 刷新', () => {
  const plan = planStudioProjectEvent({
    event: { type: 'package.revision.created', seq: 15, payload: { revisionId: 'rev-next' } },
    gate: createProjectEventReplayGate(0),
    localEdits: true,
    currentRevision: 'rev-current',
  });

  assert.equal(plan.scheduleLoad, false);
  assert.equal(plan.deferLoad, true);
});

test('Studio 不会对同 revision 的事件重复刷新', () => {
  const plan = planStudioProjectEvent({
    event: { type: 'package.revision.created', seq: 18, payload: { revisionId: 'rev-same' } },
    gate: createProjectEventReplayGate(0),
    localEdits: false,
    currentRevision: 'rev-same',
  });

  assert.equal(plan.scheduleLoad, false);
  assert.equal(plan.deferLoad, false);
});

test('Studio 在 replay 边界的历史正式 revision 事件上只补一次 catch-up 刷新', () => {
  const plan = planStudioProjectEvent({
    event: { type: 'package.revision.created', seq: 7, payload: { revisionId: 'rev-catchup' } },
    gate: createProjectEventReplayGate(7),
    localEdits: false,
    currentRevision: 'rev-current',
  });

  assert.deepEqual(plan, {
    gate: { replayThrough: 7, replaying: false, pendingRefresh: false },
    historical: true,
    relevant: true,
    revisionId: 'rev-catchup',
    scheduleLoad: true,
    deferLoad: false,
  });
});

test('Studio 的 window 刷新在本地草稿存在时也会延后', () => {
  assert.deepEqual(planStudioWindowRefresh({ localEdits: false }), {
    scheduleLoad: true,
    deferLoad: false,
  });
  assert.deepEqual(planStudioWindowRefresh({ localEdits: true }), {
    scheduleLoad: false,
    deferLoad: true,
  });
  assert.equal(studioHasPendingLocalEdits({ entryDrafts: { 'object:record': { draft: { title: '记录' } } } }), true);
  assert.equal(studioHasPendingLocalEdits({ entryDrafts: {} }), false);
});

test('Studio 外部刷新提示会携带 revision 前缀', () => {
  assert.equal(
    studioRefreshNoticeMessage('1234567890abcdef'),
    '知识包已有新版本（1234567890ab），当前显示你的本地未保存修改。',
  );
  assert.equal(
    studioRefreshNoticeMessage(''),
    '知识包已有新版本，当前显示你的本地未保存修改。',
  );
});

test('Studio 待载入 revision 随本地草稿持久化并能在刷新后恢复', () => {
  const drafts = {
    'operation:read_record': {
      draft: { title: '本地名称' },
      baseRevision: 'rev-old',
      updatedAt: 1,
    },
  };
  const marked = markStudioDraftsForRefresh(drafts, 'rev-new');

  assert.deepEqual(marked['operation:read_record'], {
    ...drafts['operation:read_record'],
    refreshPending: true,
    newerRevision: 'rev-new',
  });
  assert.deepEqual(studioPendingDraftRefresh(JSON.parse(JSON.stringify(marked)), 'rev-new'), {
    pending: true,
    revisionId: 'rev-new',
  });
  assert.deepEqual(studioPendingDraftRefresh(drafts, 'rev-newer'), {
    pending: true,
    revisionId: 'rev-newer',
  });
  assert.deepEqual(studioPendingDraftRefresh(drafts, 'rev-old'), {
    pending: false,
    revisionId: '',
  });
});

test('StudioView 先建立当前项目事件基线再加载，并在加载页保持订阅', () => {
  const source = readFileSync(new URL('../frontend/src/workbench/studio/StudioView.jsx', import.meta.url), 'utf8');

  assert.match(source, /getProjectSession\(projectId\)/);
  assert.match(source, /setProjectEventBaseline\(\{ projectId: '', cursor: 0, ready: false \}\)/);
  assert.match(source, /if \(!projectEventBaseline\.ready \|\| projectEventBaseline\.projectId !== projectId\) return undefined;\s*void loadStudio\(\)/s);
  assert.match(source, /const projectEventsReady = projectEventBaseline\.ready && projectEventBaseline\.projectId === projectId/);
  assert.match(source, /key=\{projectId\}/);
  assert.match(source, /initialCursor=\{projectEventBaseline\.cursor\}/);
  assert.match(source, /return <>\s*\{projectEvents\}\s*<StudioLoading \/>\s*<\/>/s);
  assert.match(source, /function StudioProjectEvents\(\{ projectId, initialCursor, onEvent \}\) \{/);
  assert.match(source, /useProjectEvents\(projectId,\s*onEvent,\s*\{/);
  assert.match(source, /initialCursor,\s*\n\s*\}\);/);
});

test('StudioView 保留 revision 事件刷新、window 更新与脏态保护', () => {
  const source = readFileSync(new URL('../frontend/src/workbench/studio/StudioView.jsx', import.meta.url), 'utf8');

  assert.match(source, /planStudioProjectEvent\(\{/);
  assert.match(source, /planStudioWindowRefresh\(\{\s*localEdits:\s*localEditsRef\.current\s*\}\)/);
  assert.match(source, /entryDrafts: markStudioDraftsForRefresh\(current\.entryDrafts, plan\.revisionId\)/);
  assert.match(source, /studioPendingDraftRefresh\(previous\.entryDrafts, list\.workingRevision\)/);
  assert.match(source, /if \(!entryDraftDirty \|\| !externalRefreshNotice\.pending\) return;/);
  assert.match(source, /if \(!entryDraftDirty && deferredRefreshRef\.current\) \{/);
  assert.match(source, /window\.addEventListener\('mw-package-updated', refresh\)/);
  assert.match(source, /const discardLocalDraftsAndLoadStudio = useCallback\(async \(\) => \{/);
  assert.match(source, /update\(\{ entryDrafts: \{\}, details: \{\} \}\);\s*await loadStudio\(\)/s);
  assert.match(source, /onReload=\{discardLocalDraftsAndLoadStudio\}/);
  assert.match(source, /放弃草稿并载入新版/);
});

test('Studio schema 每次刷新都重新验证动态租约', () => {
  const source = readFileSync(new URL('../frontend/src/workbench/studio/studio-api.js', import.meta.url), 'utf8');

  assert.match(source, /getStudioSchema = \(projectId\) => api\(`\$\{P\(projectId\)\}\/schema`, \{ cache: 'no-cache' \}\)/);
});

import assert from 'node:assert/strict';
import test from 'node:test';

const { actions, mergePendingUploads, store } = await import('../frontend/src/workbench/lib/appStore.js');

const PROJECT_ID = 'project_goal';
const GOAL = '整理合成仓储场景的库存知识包。流程甲：入库登记与质检。流程乙：出库复核与盘点。';
const pendingProject = { id: PROJECT_ID, name: '合成仓储项目', background: GOAL, goalStatus: 'pending' };
const confirmedProject = { ...pendingProject, goalStatus: 'confirmed' };

const file = (name: string) => new File(['合成资料内容'], name, { type: 'text/markdown' });

function resetPendingProject() {
  store.set({
    projectId: PROJECT_ID,
    project: pendingProject,
    pendingUploads: [],
    autoStartSent: false,
    run: null,
    messages: [],
    uploads: [],
    materials: [],
    materialContents: {},
    revision: null,
    error: '',
  });
}

/**
 * 合成后端：记录请求，资料上传后直接解析完成，免去轮询。
 * 每次快照带递增的 revision，用来等后台刷新真正写回 store，避免拖到下一个用例里覆盖状态。
 */
function mockServer(t: test.TestContext, goal = (_body: string) => reply({ project: confirmedProject })) {
  const requests: Array<{ method: string; url: URL; body: unknown }> = [];
  let snapshots = 0;
  t.mock.method(globalThis, 'fetch', async (input: unknown, init: any = {}) => {
    const url = new URL(String(input), 'http://localhost');
    const method = init.method || 'GET';
    requests.push({ method, url, body: init.body });
    if (url.pathname.endsWith('/goal')) return goal(init.body);
    if (method === 'POST' && url.pathname.endsWith('/documents')) {
      const name = init.body.get('file').name;
      return reply({ document: {
        id: `doc-${name}`, kind: 'markdown', parseStatus: 'parsed',
        purpose: url.searchParams.get('purpose'), readingMode: url.searchParams.get('readingMode'),
      } });
    }
    if (url.pathname.endsWith('/agent/snapshot')) {
      snapshots += 1;
      return reply({ project: confirmedProject, revision: `rev-${snapshots}`, files: [], materials: [] });
    }
    if (url.pathname.endsWith('/agent/diff')) return reply({ files: [] });
    if (url.pathname.endsWith('/agent/steer')) return reply({ run: { id: 'run-synthetic', status: 'running' } });
    return reply({});
  });
  return requests;
}

function reply(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
}

function mockWindow(t: test.TestContext) {
  const previousWindow = globalThis.window;
  const replaced: string[] = [];
  globalThis.window = {
    location: new URL(`http://localhost/workbench.html?project=${PROJECT_ID}&start=1#/workbench`),
    history: { replaceState: (_state: unknown, _title: string, url: string) => replaced.push(url) },
  };
  t.after(() => { globalThis.window = previousWindow; });
  return replaced;
}

async function waitFor(predicate: () => boolean, message: string) {
  const deadline = Date.now() + 2000;
  while (!predicate()) {
    if (Date.now() > deadline) assert.fail(message);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

const isPost = (suffix: string) => (request: { method: string; url: URL }) => (
  request.method === 'POST' && request.url.pathname.endsWith(suffix)
);

test('暂存的多批资料合成一个上传批次，替换资料的批次单独上传', () => {
  const replacement = { projectId: PROJECT_ID, files: [file('旧资料.md')], options: { replacementOf: 'doc_old' } };
  const { merged, replacements } = mergePendingUploads([
    { projectId: PROJECT_ID, files: [file('流程.md'), file('字段.md')], options: { purpose: 'schema', readingMode: 'full' } },
    replacement,
    { projectId: PROJECT_ID, files: [file('术语.md')], options: {
      directivesByName: { '术语.md': { purpose: 'term', readingMode: 'on_demand' } },
    } },
  ]);

  assert.deepEqual(merged.files.map((item: File) => item.name), ['流程.md', '字段.md', '术语.md']);
  assert.deepEqual(merged.options, {
    followUp: true,
    directivesByName: {
      '流程.md': { purpose: 'schema', readingMode: 'full' },
      '字段.md': { purpose: 'schema', readingMode: 'full' },
      '术语.md': { purpose: 'term', readingMode: 'on_demand' },
    },
  });
  assert.deepEqual(replacements, [replacement]);
});

test('目标待确认时上传资料只暂存、发送消息被拦下，都不请求后端', async (t) => {
  resetPendingProject();
  const requests = mockServer(t);

  const queued = await actions.upload([file('流程.md'), file('字段.md')], { purpose: 'schema' });
  assert.deepEqual(queued, { parsed: 0, failed: 0, details: [], queued: 2 });
  assert.deepEqual(store.get().pendingUploads.map((batch: any) => batch.projectId), [PROJECT_ID]);
  assert.deepEqual(store.get().uploads, []);

  assert.equal(await actions.send('请开始建模'), null);
  assert.equal(store.get().error.scope, 'send');
  assert.match(store.get().error.message, /请先确认建模目标/u);
  assert.deepEqual(store.get().messages, []);

  assert.deepEqual(requests, []);
});

test('确认目标后暂存资料合成一批上传，由上传续跑而不另发开工消息', async (t) => {
  resetPendingProject();
  const requests = mockServer(t);
  const otherProjectBatch = { projectId: 'project_other', files: [file('其他.md')], options: {} };
  store.set({ pendingUploads: [otherProjectBatch] });
  await actions.upload([file('流程.md'), file('字段.md')], { purpose: 'schema', readingMode: 'full' });
  await actions.upload([file('术语.md')], { directivesByName: { '术语.md': { purpose: 'term', readingMode: 'on_demand' } } });
  await actions.upload([file('旧资料.md')], { replacementOf: 'doc_old' });

  const confirmed = await actions.confirmProjectGoal();

  assert.equal(confirmed.goalStatus, 'confirmed');
  assert.equal(store.get().project.goalStatus, 'confirmed');
  assert.deepEqual(store.get().pendingUploads, [otherProjectBatch]);
  // 合并批次和替换批次各自上传完都会刷新一次快照
  await waitFor(() => store.get().revision === 'rev-2', '暂存资料没有全部上传完成');

  const [goalRequest] = requests;
  assert.equal(goalRequest.method, 'PUT');
  assert.equal(goalRequest.url.pathname, `/api/projects/${PROJECT_ID}/goal`);
  assert.equal(goalRequest.body, '{}');

  const documents = requests.filter(isPost('/documents'));
  assert.deepEqual(documents.map((request: any) => request.body.get('file').name), ['流程.md', '字段.md', '术语.md', '旧资料.md']);
  const mergedBatch = documents.slice(0, 3).map((request) => Object.fromEntries(request.url.searchParams));
  const batchId = mergedBatch[0].uploadBatchId;
  assert.ok(batchId);
  assert.deepEqual(mergedBatch.map(({ purpose, readingMode, uploadBatchId, uploadBatchSize }) => (
    { purpose, readingMode, uploadBatchId, uploadBatchSize }
  )), [
    { purpose: 'schema', readingMode: 'full', uploadBatchId: batchId, uploadBatchSize: '3' },
    { purpose: 'schema', readingMode: 'full', uploadBatchId: batchId, uploadBatchSize: '3' },
    { purpose: 'term', readingMode: 'on_demand', uploadBatchId: batchId, uploadBatchSize: '3' },
  ]);
  // 不带 agentFollowUp=0：由这批资料解析完成后续跑助手，与创建页一次选好资料的流程一致
  assert.ok(mergedBatch.every((query) => !('agentFollowUp' in query)));

  const replacement = documents[3].url.searchParams;
  assert.equal(replacement.get('replacementOf'), 'doc_old');
  assert.equal(replacement.get('uploadBatchSize'), '1');
  assert.notEqual(replacement.get('uploadBatchId'), batchId);

  assert.deepEqual(requests.filter(isPost('/agent/steer')), []);
  assert.equal(store.get().autoStartSent, false);
});

test('没有暂存资料时确认改写后的目标，随即发出一次开工消息', async (t) => {
  resetPendingProject();
  const replaced = mockWindow(t);
  const requests = mockServer(t, (body) => reply({ project: { ...confirmedProject, background: JSON.parse(body).background } }));

  const confirmed = await actions.confirmProjectGoal('新目标：只覆盖流程乙。');

  assert.equal(confirmed.background, '新目标：只覆盖流程乙。');
  assert.equal(requests[0].body, JSON.stringify({ background: '新目标：只覆盖流程乙。' }));
  await waitFor(() => store.get().run?.id === 'run-synthetic', '确认后没有开工');
  const steers = requests.filter(isPost('/agent/steer'));
  assert.equal(steers.length, 1);
  assert.match(JSON.parse(String(steers[0].body)).message, /请开始为“合成仓储项目”建模/u);
  assert.equal(store.get().autoStartSent, true);
  // 清掉 URL 的 start 标记，刷新页面不会重复开工
  assert.deepEqual(replaced, [`/workbench.html?project=${PROJECT_ID}#/workbench`]);
  assert.deepEqual(requests.filter(isPost('/documents')), []);
});

test('目标已在别处确认时只同步状态，不重复开工', async (t) => {
  resetPendingProject();
  mockWindow(t);
  const requests = mockServer(t, () => reply({
    code: 'PROJECT_GOAL_NOT_PENDING', error: '建模目标已确认，如需调整请在对话中告诉助手',
  }, 409));

  const confirmed = await actions.confirmProjectGoal();

  assert.equal(confirmed.goalStatus, 'confirmed');
  assert.equal(store.get().project.goalStatus, 'confirmed');
  await waitFor(() => store.get().revision === 'rev-1', '没有刷新项目快照');
  assert.deepEqual(requests.filter(isPost('/agent/steer')), []);
  assert.equal(store.get().autoStartSent, false);
});

test('确认失败时抛出错误，目标仍待确认且暂存资料保留', async (t) => {
  resetPendingProject();
  const requests = mockServer(t, () => reply({ code: 'INVALID_INPUT', error: '请填写建模目标' }, 400));
  await actions.upload([file('流程.md')], { purpose: 'schema' });
  const staged = store.get().pendingUploads;

  await assert.rejects(actions.confirmProjectGoal('   '), /请填写建模目标/u);

  assert.equal(store.get().project.goalStatus, 'pending');
  assert.equal(store.get().pendingUploads, staged);
  assert.deepEqual(requests.map((request) => request.url.pathname), [`/api/projects/${PROJECT_ID}/goal`]);
});

test('确认请求在途时切换了项目，不覆盖新项目的状态', async (t) => {
  resetPendingProject();
  mockWindow(t);
  const nextProject = { id: 'project_next', name: '合成下一个项目', background: '', goalStatus: '' };
  const requests = mockServer(t, () => {
    store.set({ projectId: nextProject.id, project: nextProject });
    return reply({ project: confirmedProject });
  });

  await actions.confirmProjectGoal();

  assert.equal(store.get().projectId, nextProject.id);
  assert.equal(store.get().project, nextProject);
  assert.equal(store.get().autoStartSent, false);
  assert.deepEqual(requests.map((request) => request.url.pathname), [`/api/projects/${PROJECT_ID}/goal`]);
});

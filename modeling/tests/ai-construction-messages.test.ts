import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'ai-construction-messages-'));

const { AiConstructionMessageStore } = await import('../src/ai-construction/messages.js');
const { AiConstructionTaskGraph } = await import('../src/ai-construction/task-graph.js');
const { getDb } = await import('../src/db/index.js');
const { createProject } = await import('../src/projects/repo.js');

test('结构化建模消息只接受受控 block 并可在重启后恢复', () => {
  const project = createProject({ owner: 'alice', name: '结构化消息' });
  const taskId = new AiConstructionTaskGraph(getDb()).snapshot(project.id)!.tasks[0].id;
  const store = new AiConstructionMessageStore(getDb());
  const created = store.append(project.id, {
    role: 'assistant',
    taskId,
    blocks: [
      {
        type: 'status',
        title: '建模目标待确认',
        milestoneId: 'goal_confirmed',
        taskId,
        progress: 0,
        eta: { minMinutes: 5, maxMinutes: 15, confidence: 'low' },
      },
      { type: 'text', text: '我会先确认建模目标，再围绕目标分析资料。' },
      {
        type: 'next_actions',
        actions: [
          { id: 'upload_materials', label: '上传建模资料', style: 'primary' },
          { id: 'explain_goal', label: '先说明建模目标', style: 'secondary' },
        ],
      },
    ],
  });

  assert.equal(created.seq, 1);
  assert.equal(created.role, 'assistant');
  assert.equal(created.taskId, taskId);
  assert.deepEqual(new AiConstructionMessageStore(getDb()).list(project.id), [created]);

  assert.throws(() => store.append(project.id, {
    role: 'assistant',
    blocks: [{ type: 'arbitrary_html', html: '<button onclick="steal()">x</button>' } as any],
  }), (error: any) => error?.code === 'INVALID_BLOCK');
  assert.equal(store.list(project.id).length, 1);
});

test('欢迎消息由任务图生成且重复初始化只保存一次', () => {
  const project = createProject({ owner: 'alice', name: '欢迎消息' });
  const graph = new AiConstructionTaskGraph(getDb()).snapshot(project.id)!;
  const store = new AiConstructionMessageStore(getDb());

  const first = store.ensureWelcome(project.id, graph);
  const second = new AiConstructionMessageStore(getDb()).ensureWelcome(project.id, graph);

  assert.equal(second.id, first.id);
  assert.equal(store.list(project.id).length, 1);
  assert.deepEqual(first.blocks[0], {
    type: 'status',
    title: 'AI 辅助构建已就绪',
    summary: '当前处于「建模目标确认」，正在准备「确认建模目标」',
    milestoneId: 'goal_confirmed',
    taskId: graph.tasks[0].id,
    progress: 0,
    eta: { minMinutes: 5, maxMinutes: 15, confidence: 'low' },
  });
  assert.equal(first.blocks[2].type, 'task_plan');
  assert.deepEqual((first.blocks[2] as any).tasks.map((item: any) => item.id), [graph.tasks[0].id]);
  assert.equal(first.blocks.some((block) => block.type === 'next_actions'), false);
});

test('阶段进展使用同一消息 key 原地更新而不刷屏', () => {
  const project = createProject({ owner: 'alice', name: '分析进展' });
  const graph = new AiConstructionTaskGraph(getDb()).snapshot(project.id)!;
  const taskId = graph.tasks[0].id;
  const store = new AiConstructionMessageStore(getDb());

  const started = store.upsert(project.id, {
    key: 'analysis.progress.demo', role: 'assistant', taskId,
    blocks: [{ type: 'status', title: '正在读取资料', progress: 10 }],
  });
  const updated = store.upsert(project.id, {
    key: 'analysis.progress.demo', role: 'assistant', taskId,
    blocks: [{ type: 'status', title: '正在整理候选业务流程', progress: 65 }],
  });

  assert.equal(updated.id, started.id);
  assert.equal(updated.seq, started.seq);
  assert.equal(store.list(project.id).length, 1);
  assert.deepEqual(updated.blocks, [{ type: 'status', title: '正在整理候选业务流程', progress: 65 }]);
});

test('证据、分层校验和动态任务计划均使用受控消息块', () => {
  const project = createProject({ owner: 'alice', name: '丰富结构化消息' });
  const store = new AiConstructionMessageStore(getDb());
  const created = store.append(project.id, { role: 'assistant', blocks: [
    { type: 'evidence', title: '资料依据', items: [{ source: '流程说明.md', summary: '描述申请和审核流程', locator: '第 3 章', path: 'diagrams/review.mmd' }] },
    { type: 'validation', title: '知识结构校验', scope: 'milestone', counts: { errors: 1, warnings: 2, hints: 3 },
      findings: [{ severity: 'error', title: '引用断裂', summary: '操作引用的规则不存在', target: 'operations/review.md' }] },
    { type: 'task_plan', title: '当前建模计划', note: '新增冲突澄清，预计增加 3–5 分钟', tasks: [
      { id: 'task_interview', title: '确认业务流程', status: 'running', required: true },
      { id: 'task_conflict', title: '澄清资料冲突', status: 'pending', required: false },
    ] },
  ] });
  assert.deepEqual(created.blocks.map((block) => block.type), ['evidence', 'validation', 'task_plan']);
  assert.equal((created.blocks[0] as any).items[0].path, 'diagrams/review.mmd');
  assert.throws(() => store.append(project.id, { role: 'assistant', blocks: [
    { type: 'evidence', title: '空证据', items: [] } as any,
  ] }), (error: any) => error?.code === 'INVALID_BLOCK');
  const summaryOnly = store.append(project.id, { role: 'assistant', blocks: [
    { type: 'validation', title: '发布校验', scope: 'release', counts: { errors: 0, warnings: 0, hints: 0 } },
  ] });
  assert.deepEqual(summaryOnly.blocks[0], {
    type: 'validation', title: '发布校验', scope: 'release', counts: { errors: 0, warnings: 0, hints: 0 },
  });
  assert.throws(() => store.append(project.id, { role: 'assistant', blocks: [
    { type: 'change_plan', title: '越界变更方案' } as any,
  ] }), (error: any) => error?.code === 'INVALID_BLOCK');
});

test('消息写入后触发通知器且通知异常不影响写入', () => {
  const project = createProject({ owner: 'alice', name: '写入通知' });
  const notified: Array<{ projectId: string; messageId: string; role: string }> = [];
  const store = new AiConstructionMessageStore(getDb(), (projectId, message) => {
    notified.push({ projectId, messageId: message.id, role: message.role });
  });

  const appended = store.append(project.id, {
    role: 'user', blocks: [{ type: 'text', text: '你好' }],
  });
  assert.equal(notified.length, 1);
  assert.equal(notified[0].messageId, appended.id);
  assert.equal(notified[0].projectId, project.id);

  store.upsert(project.id, {
    key: 'chat.reply.test', role: 'assistant', blocks: [{ type: 'text', text: '初次' }],
  });
  const updated = store.upsert(project.id, {
    key: 'chat.reply.test', role: 'assistant', blocks: [{ type: 'text', text: '更新' }],
  });
  assert.equal(notified.length, 3);
  assert.equal(notified[2].messageId, updated.id);

  // 通知器抛错时消息仍正常落库
  const failing = new AiConstructionMessageStore(getDb(), () => { throw new Error('通道异常'); });
  const saved = failing.append(project.id, {
    role: 'assistant', blocks: [{ type: 'text', text: '仍在' }],
  });
  assert.equal(store.findByKey(project.id, saved.key ?? '') ?? store.list(project.id).at(-1)!.id, store.list(project.id).at(-1)!.id);
});

test('消息写入经通知器接入项目事件流后可被 replay 读取', async () => {
  const { getAuthoringState } = await import('../src/authoring/state.js');
  const { notifyAiConstructionMessageWritten } = await import('../src/ai-construction/messages.js');
  const project = createProject({ owner: 'alice', name: '消息事件流' });
  const state = getAuthoringState();
  const before = state.snapshot(project.id).lastEventSeq;
  const store = new AiConstructionMessageStore(
    getDb(),
    notifyAiConstructionMessageWritten((command) => state.apply(command)),
  );

  const message = store.append(project.id, {
    role: 'assistant', blocks: [{ type: 'text', text: '事件已接入' }],
  });
  const page = state.replay(project.id, before);
  const written = page.events.find((event) => event.type === 'ai_construction.message.written');
  assert.ok(written, '消息写入应产生 ai_construction.message.written 事件');
  assert.equal((written!.payload as any).messageId, message.id);
  assert.equal((written!.payload as any).role, 'assistant');
});

test('消息事件一律内联完整内容；progressOnly 由写入方显式声明而不是按 key 猜', async () => {
  const { getAuthoringState } = await import('../src/authoring/state.js');
  const { notifyAiConstructionMessageWritten } = await import('../src/ai-construction/messages.js');
  const project = createProject({ owner: 'alice', name: '进度消息局部刷新' });
  const state = getAuthoringState();
  const before = state.snapshot(project.id).lastEventSeq;
  const store = new AiConstructionMessageStore(
    getDb(),
    notifyAiConstructionMessageWritten((command) => state.apply(command)),
  );

  const progress = store.upsert(project.id, {
    key: 'analysis.progress.live', role: 'assistant', progressOnly: true, blocks: [{
      type: 'status', title: '正在检查资料质量和关键缺口', progress: 86,
    }],
  });
  // 同样落在 analysis.progress.* 下、但伴随任务状态变化的写入不标记，前端必须回源刷新；
  // 早先按 key 前缀猜，导致「开始分析」把任务转成 running 后前端一直拿着旧状态。
  const stateChange = store.upsert(project.id, {
    key: 'analysis.progress.current', role: 'assistant', blocks: [{
      type: 'status', title: '开始分析建模资料', progress: 3,
    }],
  });
  const ordinary = store.append(project.id, {
    role: 'assistant', blocks: [{ type: 'text', text: '普通消息' }],
  });

  const events = state.replay(project.id, before).events
    .filter((event) => event.type === 'ai_construction.message.written');
  const payloadOf = (id: string) => (events
    .find((event) => (event.payload as any).messageId === id)!.payload as any);

  assert.equal(payloadOf(progress.id).message.blocks[0].progress, 86);
  assert.equal(payloadOf(progress.id).progressOnly, true);
  assert.equal(payloadOf(stateChange.id).progressOnly, undefined);

  // 普通消息同样内联，前端收到即可上屏；但不标 progressOnly，仍会触发一次状态回源
  assert.equal(payloadOf(ordinary.id).message.blocks[0].text, '普通消息');
  assert.equal(payloadOf(ordinary.id).progressOnly, undefined);
});

test('超过内联上限的消息只发信号，由前端回源读取', async () => {
  const { getAuthoringState } = await import('../src/authoring/state.js');
  const { notifyAiConstructionMessageWritten } = await import('../src/ai-construction/messages.js');
  const project = createProject({ owner: 'alice', name: '超大消息' });
  const state = getAuthoringState();
  const before = state.snapshot(project.id).lastEventSeq;
  const store = new AiConstructionMessageStore(
    getDb(),
    notifyAiConstructionMessageWritten((command) => state.apply(command)),
  );

  // 单块文本上限 10000 字，用多块凑出超过内联上限的消息
  const huge = store.append(project.id, {
    role: 'assistant',
    blocks: Array.from({ length: 4 }, () => ({ type: 'text' as const, text: '巨'.repeat(10_000) })),
  });

  const [event] = state.replay(project.id, before).events
    .filter((item) => item.type === 'ai_construction.message.written');
  assert.equal((event.payload as any).messageId, huge.id);
  assert.equal((event.payload as any).message, undefined);
});

test('消息窗口默认返回最新内容并支持按最早序号向前翻页', () => {
  const project = createProject({ owner: 'alice', name: '长会话消息窗口' });
  const store = new AiConstructionMessageStore(getDb());
  for (let index = 1; index <= 205; index += 1) {
    store.append(project.id, {
      role: 'assistant', blocks: [{ type: 'text', text: `消息 ${index}` }],
    });
  }

  const latest = store.page(project.id, { limit: 200 });
  assert.equal(latest.messages.length, 200);
  assert.deepEqual([latest.messages[0].seq, latest.messages.at(-1)!.seq], [6, 205]);
  assert.equal(latest.hasMore, true);
  assert.equal(latest.oldestSeq, 6);
  assert.equal(latest.newestSeq, 205);

  const older = store.page(project.id, { beforeSeq: latest.oldestSeq, limit: 3 });
  assert.deepEqual(older.messages.map((message) => message.seq), [3, 4, 5]);
  assert.equal(older.hasMore, true);

  const earliest = store.page(project.id, { beforeSeq: older.oldestSeq, limit: 3 });
  assert.deepEqual(earliest.messages.map((message) => message.seq), [1, 2]);
  assert.equal(earliest.hasMore, false);
});

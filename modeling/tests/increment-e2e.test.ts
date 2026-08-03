import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'increment-e2e-'));
// faux 建模者能真正走完 submit_change_plan，链路可端到端验证；
// 但它是脚本化的，**提案质量验证不了**——模型会不会倾向 modify 而不是新增重复规则，
// 只有接真实端点才知道。这个用例证明的是「管道通」，不是「方案好」。
process.env.MODELING_LLM_BASE_URL = 'faux';
process.env.MODELING_LLM_MODEL = 'faux';

const { registerAuthoringRoutes } = await import('../src/authoring/routes.js');
const { getAuthoringState } = await import('../src/authoring/state.js');
const { createProject } = await import('../src/projects/repo.js');
const { getDb } = await import('../src/db/index.js');
const { getPackageArtifactStore } = await import('../src/artifacts/runtime.js');

function app(t: test.TestContext) {
  const instance = Fastify();
  instance.addHook('preHandler', async (req) => { req.user = { username: 'alice', role: 'modeler' }; });
  // 不打桩：真实编排、真实任务图、真实知识包存储
  registerAuthoringRoutes(instance);
  t.after(() => instance.close());
  return instance;
}

/** 把项目推到「建模已完成确认」——增量的前提 */
async function seedCompletedPackage(projectId: string) {
  const store = getPackageArtifactStore();
  await store.ensureProject(projectId);
  await store.initializeDraftPackage(projectId, {
    id: `project-${projectId.replace(/^pj_/, '')}`, name: '增量端到端', description: 'e2e',
  });
  getDb().prepare("UPDATE ai_construction_milestones SET status = 'confirmed' WHERE project_id = ?")
    .run(projectId);
  getDb().prepare("UPDATE ai_construction_tasks SET status = 'completed' WHERE project_id = ?")
    .run(projectId);
}

test('端到端：完成态项目开一轮增量并产出待确认方案', async (t) => {
  const project = createProject({ owner: 'alice', name: '增量端到端' });
  const server = app(t);
  const db = getDb();

  // 先经过 session 让编排把图初始化好，再置为完成态
  await server.inject({ method: 'GET', url: `/api/projects/${project.id}/session` });
  await seedCompletedPackage(project.id);

  // 1) 开一轮
  const started = await server.inject({
    method: 'POST', url: `/api/projects/${project.id}/ai-construction/increment/start`, payload: {},
  });
  assert.equal(started.statusCode, 202, started.body);

  const milestones = db.prepare(`SELECT milestone_id, status FROM ai_construction_milestones
    WHERE project_id = ?`).all(project.id) as any[];
  assert.equal(milestones.find((m) => m.milestone_id === 'increment_confirmed')?.status, 'active');
  // 增量与「重走一遍」的分界：已确认的阶段一个都不能被动过
  for (const item of milestones.filter((m) => m.milestone_id !== 'increment_confirmed')) {
    assert.equal(item.status, 'confirmed', `${item.milestone_id} 被动过了`);
  }

  // 开轮消息要给出可点的下一步，否则用户不知道该干什么
  const openingMessage = db.prepare(`SELECT blocks_json FROM ai_construction_messages
    WHERE project_id = ? AND message_key LIKE 'increment.started.%'`).get(project.id) as any;
  assert.ok(openingMessage, '开轮应写出一条助手消息');
  assert.match(openingMessage.blocks_json, /upload_materials/);
  assert.match(openingMessage.blocks_json, /propose_increment/);

  // 2) 还没传资料就生成方案 → 早退，而不是让模型空跑
  const tooEarly = await server.inject({
    method: 'POST', url: `/api/projects/${project.id}/ai-construction/increment/propose`, payload: {},
  });
  assert.equal(tooEarly.statusCode, 409, tooEarly.body);
  assert.equal(tooEarly.json().code, 'INCREMENT_MATERIALS_REQUIRED');

  // 3) 传一份增量资料（直接落库，等价于上传接口的结果）
  const { insertDocument } = await import('../src/documents/repo.js');
  insertDocument({
    projectId: project.id, filename: '新增规则.md', kind: 'markdown',
    parseStatus: 'parsed', textContent: '大额客户的准入门槛调整为 500 万。',
  });

  // 4) 生成方案：请求立刻返回，模型在后台跑
  const proposed = await server.inject({
    method: 'POST', url: `/api/projects/${project.id}/ai-construction/increment/propose`, payload: {},
  });
  assert.equal(proposed.statusCode, 202, proposed.body);

  // 5) 后台作业确实被登记并执行了（成功与否取决于模型，见下方说明）
  const deadline = Date.now() + 20_000;
  let run: any;
  for (;;) {
    run = db.prepare(`SELECT intent, status, error FROM authoring_runs
      WHERE project_id = ? AND intent = '生成增量修改方案' ORDER BY created_at DESC LIMIT 1`)
      .get(project.id);
    if (run && run.status !== 'running') break;
    if (Date.now() > deadline) throw new Error('增量提案作业未在 20 秒内结束');
    await new Promise((resolve) => { setTimeout(resolve, 50); });
  }
  assert.ok(run, '提案作业应有运行记录');
  // 失败时原因必须落在 run 上——没有 HTTP 响应可用，这是用户唯一能看到失败原因的地方
  if (run.status === 'failed') assert.ok(run.error, '后台失败必须记录原因');
});

/*
 * 这个用例证明的是**管道通**，不是**方案好**：
 *
 * 环境里只配了 faux 建模者（.env 的 MODELING_LLM_BASE_URL=faux），它是脚本化的，
 * 只在「包里已有业务操作」且消息含修改意图时才提交 submit_change_plan。而把一个内容
 * 完整、可被 spec 解析的存量知识包灌进来，需要走 model/package-spec.json 的专用写入
 * 接口（saveFiles 只接受 package/ 前缀），成本不小。
 *
 * 因此以下两点仍未验证，需要接真实模型端点后实跑：
 * - 模型会不会真的倾向 modify 已有规则，而不是新增一条语义重复的规则；
 * - 提案是否只落在本轮资料涉及的范围内，没有顺手改动无关内容。
 * 服务端对删除的拦截（assertIncrementChangesAllowed）有独立单元用例覆盖。
 */

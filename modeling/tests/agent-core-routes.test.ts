import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';
import XLSX from 'xlsx';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-agent-core-'));
process.env.MODELING_LLM_BASE_URL = 'faux';
process.env.MODELING_LLM_MODEL = 'faux';

const [
  { registerAgentCoreRoutes },
  { createProject, getProject },
  { getPackageArtifactStore },
  { getDb },
  { getDocument, insertDocument, insertDocumentRevision },
  { MaterialPurposeReviewStore, findMaterialPurposeReviewProblems },
  { requireAuth },
  { storePackageMergeSource },
  { renderPackage },
  { parseExcel },
  { config },
  { normalizePlanTargetKey },
  { storeMaterialImpact },
] = await Promise.all([
  import('../src/agent/core-routes.js'),
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/db/index.js'),
  import('../src/documents/repo.js'),
  import('../src/documents/material-purpose-review.js'),
  import('../src/auth/middleware.js'),
  import('../src/agent/package-merge.js'),
  import('../src/workflow/package-render.js'),
  import('../src/documents/parser.js'),
  import('../src/config.js'),
  import('../src/agent/change-control.js'),
  import('../src/documents/material-impact.js'),
]);

function appFor(t: test.TestContext, username = 'alice') {
  const app = Fastify();
  app.addHook('preHandler', async (request) => {
    request.user = { username, role: 'modeler' };
  });
  registerAgentCoreRoutes(app);
  t.after(() => app.close());
  return app;
}

function tool(app: ReturnType<typeof Fastify>, projectId: string, name: string,
  args: Record<string, unknown> = {}, runId = 'run-agent-1') {
  return app.inject({
    method: 'POST',
    url: `/internal/agent/tools/${name}`,
    headers: {
      'x-modeling-project': projectId,
      'x-modeling-run': runId,
      authorization: 'Bearer agent-test-token',
    },
    payload: { arguments: args },
  });
}

async function usePassingScriptSandbox(t: test.TestContext) {
  const socketPath = join(mkdtempSync(join(tmpdir(), 'modeling-agent-gate-sandbox-')), 'jiuwenbox.sock');
  const previousSocketPath = config.jiuwenboxSocketPath;
  const executedCases: Array<Array<{ argv: string[]; expected: unknown }>> = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
    request.on('end', () => {
      if (request.method === 'POST' && request.url?.endsWith('/exec')) {
        const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        const encodedCases = body.command.at(-1);
        const cases = JSON.parse(Buffer.from(encodedCases, 'base64').toString('utf8'));
        executedCases.push(cases);
        const results = cases.map((item: Record<string, unknown>, index: number) => ({
          index, passed: true, actual: item.expected, expected: item.expected,
        }));
        response.setHeader('Content-Type', 'application/json');
        response.end(JSON.stringify({
          exit_code: 0,
          stdout: `__MODELING_RESULT__${JSON.stringify(results)}\n`,
          stderr: '',
        }));
        return;
      }
      if (request.method === 'DELETE') {
        response.statusCode = 204;
        response.end();
        return;
      }
      response.setHeader('Content-Type', 'application/json');
      response.end('{}');
    });
  });
  await new Promise<void>((resolve) => server.listen(socketPath, resolve));
  config.jiuwenboxSocketPath = socketPath;
  t.after(async () => {
    config.jiuwenboxSocketPath = previousSocketPath;
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });
  return executedCases;
}

function confirmationReceipt(preflight: { json(): any }) {
  const payload = preflight.json();
  return {
    expected_candidate_revision: payload.candidate_revision,
    expected_candidate_hash: payload.validation_receipt.candidateHash,
    expected_validator_version: payload.validation_receipt.validatorVersion,
  };
}

async function confirmGoal(app: ReturnType<typeof Fastify>, projectId: string, runId: string) {
  const group = { id: 'prerequisite-goal', tier: 'caliber', title: '确认建模目标' };
  const written = await tool(app, projectId, 'write_file', {
    path: '.modeling/goal.md', content: '# 建模目标\n\n确认当前业务范围。\n',
    change_group: group.id, evidence: ['user-decision:test-goal'],
  }, runId);
  assert.equal(written.statusCode, 200, written.body);
  const preflight = await tool(app, projectId, 'request_confirmation_preflight', {
    summary: group.title, change_groups: [group],
  }, runId);
  assert.equal(preflight.statusCode, 200, preflight.body);
  const confirmed = await tool(app, projectId, 'request_confirmation', {
    summary: group.title, change_groups: [group],
    ...confirmationReceipt(preflight),
    review: { approved: true, review_mode: 'full', selected_groups: [group.id] },
  }, runId);
  assert.equal(confirmed.statusCode, 200, confirmed.body);
}

async function confirmProcess(app: ReturnType<typeof Fastify>, projectId: string, runId: string) {
  const group = { id: 'prerequisite-process', tier: 'caliber', title: '确认业务流程' };
  const written = await tool(app, projectId, 'write_file', {
    path: 'diagrams/prerequisite.mmd', content: 'flowchart TD\n  A[开始] --> B[结束]\n',
    change_group: group.id, evidence: ['user-decision:test-process'],
  }, runId);
  assert.equal(written.statusCode, 200, written.body);
  const preflight = await tool(app, projectId, 'request_confirmation_preflight', {
    summary: group.title, change_groups: [group],
  }, runId);
  assert.equal(preflight.statusCode, 200, preflight.body);
  const confirmed = await tool(app, projectId, 'request_confirmation', {
    summary: group.title, change_groups: [group],
    ...confirmationReceipt(preflight),
    review: { approved: true, review_mode: 'full', selected_groups: [group.id] },
  }, runId);
  assert.equal(confirmed.statusCode, 200, confirmed.body);
}

async function confirmKnowledge(app: ReturnType<typeof Fastify>, projectId: string, runId: string) {
  const group = { id: 'prerequisite-knowledge', tier: 'structural', title: '确认知识内容' };
  const written = await tool(app, projectId, 'upsert_object', {
    key: 'prerequisite',
    title: '前置对象',
    description: '用于建立脚本测试的已确认知识前置。',
    query_boundary: '只使用用户明确输入的数值。',
    fields: [{ key: 'value', cn_name: '数值', type: 'number', required: true }],
    relations: [],
    change_group: group.id,
    evidence: ['user-decision:test-knowledge'],
  }, runId);
  assert.equal(written.statusCode, 200, written.body);
  const preflight = await tool(app, projectId, 'request_confirmation_preflight', {
    summary: group.title, change_groups: [group],
  }, runId);
  assert.equal(preflight.statusCode, 200, preflight.body);
  const confirmed = await tool(app, projectId, 'request_confirmation', {
    summary: group.title, change_groups: [group],
    ...confirmationReceipt(preflight),
    review: { approved: true, review_mode: 'full', selected_groups: [group.id] },
  }, runId);
  assert.equal(confirmed.statusCode, 200, confirmed.body);
}

test('Agent 工作台接管项目后清除旧业务状态源', async (t) => {
  const project = createProject({ owner: 'alice', name: '唯一 Agent 状态接管' });
  const app = appFor(t);
  assert.ok(getDb().prepare('SELECT project_id FROM ai_construction_graphs WHERE project_id = ?').get(project.id));

  const snapshot = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/agent/snapshot` });

  assert.equal(snapshot.statusCode, 200, snapshot.body);
  assert.equal(getProject(project.id)?.runtime, 'agent');
  assert.equal(getDb().prepare('SELECT project_id FROM ai_construction_graphs WHERE project_id = ?').get(project.id), undefined);
  assert.equal(getDb().prepare('SELECT id FROM wf_nodes WHERE project_id = ?').get(project.id), undefined);
});

test('新 Agent 项目不初始化旧任务图', () => {
  const project = createProject({ owner: 'alice', name: '原生 Agent 项目', runtime: 'agent' });
  assert.equal(project.runtime, 'agent');
  assert.equal(getDb().prepare('SELECT project_id FROM ai_construction_graphs WHERE project_id = ?').get(project.id), undefined);
});

test('Agent diff 找不到 revision 时返回精炼的 404 而不是 500', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Agent diff 错误映射', runtime: 'agent' });
  const app = appFor(t);
  const snapshot = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/agent/snapshot`,
  });
  assert.equal(snapshot.statusCode, 200, snapshot.body);

  const runId = `run-missing-revision-${project.id}`;
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  const response = await tool(app, project.id, 'diff', { rev_b: 'not-a-revision' }, runId);

  assert.equal(response.statusCode, 404, response.body);
  assert.deepEqual(response.json(), {
    code: 'ARTIFACT_REVISION_NOT_FOUND',
    error: '找不到这个版本',
    reason: '找不到这个版本',
    next: ['核对当前候选和修订后重试'],
    display: { text: '找不到这个版本' },
  });
});

test('接管候选工作区时返回与核心校验器同源的生成契约', async (t) => {
  const project = createProject({ owner: 'alice', name: '生成契约注入', runtime: 'agent' });
  const app = appFor(t);

  const claimed = await tool(
    app,
    project.id,
    'claim_candidate_workspace',
    {},
    `run-contract-${project.id}`,
  );

  assert.equal(claimed.statusCode, 200, claimed.body);
  const contract = claimed.json().packageContract;
  assert.equal(contract.validatorVersion, 'package-contract-v7');
  assert.equal(contract.validators.goalEvidence, 'goal-evidence-v2');
  assert.equal(contract.validators.diagramEvidence, 'diagram-evidence-v2');
  assert.equal(contract.confirmation.repairRoundBoundary, 'model_call');
  assert.equal(contract.confirmation.aggregateParallelFindings, true);
  assert.deepEqual(contract.confirmation.recoveryToolsRemainAvailable, [
    'delete_file', 'discard_rejected_candidate_changes',
  ]);
  assert.match(contract.authoring.goalEvidence.invariants.join('\n'), /正文中的每个 material token/u);
  assert.match(contract.validators.packageFormat, /^package-format-/u);
  assert.ok(!contract.packageFormat.requiredFiles.includes('mappings/object-data-mappings.json'));
  assert.equal(contract.packageFormat.mappings.required, 'when_physical_schema_exists');
  assert.equal(contract.packageFormat.mappings.forbidden, 'when_physical_schema_absent');
  assert.equal(contract.packageFormat.mappings.allowEmptyObjects, false);
  assert.equal(contract.validators.mermaidSource, 'mermaid-source-v1');
  assert.deepEqual(contract.packageFormat.diagrams, {
    pathPattern: 'diagrams/*.mmd',
    rawMermaidSourceOnly: true,
    allowedDeclarations: ['flowchart TD', 'flowchart LR'],
    markdownCodeFencesAllowed: false,
  });
  assert.ok(contract.manifest.requiredFields.includes('status'));
  assert.deepEqual(
    contract.manifest.platformManagedFields,
    ['kind', 'schema_version', 'id', 'version', 'status'],
  );
  assert.deepEqual(
    contract.markdown.details.operation.requiredSections,
    ['业务目标', '输入', '输出', '流程', '规则、指标和注意事项', '知识证据'],
  );
  assert.deepEqual(
    contract.markdown.details.object.tables.字段,
    ['字段 key', '中文名', '类型', '必填', '业务说明', '别名'],
  );
  assert.equal(contract.markdown.references.referencedKeysMustExist, true);
});

test('Agent 全文精读进度来自分块检查点且不重复计算重叠字符', async (t) => {
  const project = createProject({ owner: 'alice', name: '全文精读真实进度', runtime: 'agent' });
  const app = appFor(t);
  const db = getDb();
  const now = new Date().toISOString();
  const runId = `material-progress-${project.id}`;
  db.prepare(`INSERT INTO material_analysis_runs (
    id, project_id, source_set_fingerprint, brief_fingerprint, model_id,
    protocol_version, plan_json, status, stage, created_at, updated_at
  ) VALUES (?,?,?,?,?,?,?,?,?,?,?)`).run(
    runId, project.id, `sources-${project.id}`, 'brief', 'deepseek-v4-flash', 'v1',
    JSON.stringify({ totalChars: 300 }), 'running', 'chunk_map', now, now,
  );
  const insertChunk = db.prepare(`INSERT INTO material_analysis_chunks (
    run_id, document_key, document_id, filename, revision_fingerprint, chunk_index,
    start_offset, end_offset, content_hash, status, attempt_count, updated_at
  ) VALUES (?,?,?,?,?,?,?,?,?,?,0,?)`);
  insertChunk.run(runId, 'doc-a', null, '规程甲.docx', 'rev-a', 0, 0, 120, 'hash-a-0', 'completed', now);
  insertChunk.run(runId, 'doc-a', null, '规程甲.docx', 'rev-a', 1, 100, 200, 'hash-a-1', 'completed', now);
  insertChunk.run(runId, 'doc-b', null, '规程乙.docx', 'rev-b', 0, 0, 100, 'hash-b-0', 'running', now);

  const response = await tool(app, project.id, 'get_material_analysis_progress', {}, `agent-${project.id}`);

  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().title, '正在全文精读《规程乙.docx》');
  assert.deepEqual(response.json().counters, {
    progress: 33, current: 2, total: 6, unit: '个处理检查点',
  });
  assert.deepEqual(response.json().coverage, { coveredChars: 200, totalChars: 300, percent: 66 });
  assert.match(response.json().detail, /2\/3 个分块完成 · 全文覆盖 66%/u);
  assert.equal(db.prepare('SELECT project_id FROM agent_candidate_workspaces WHERE project_id = ?')
    .get(project.id), undefined);

  const future = await tool(app, project.id, 'get_material_analysis_progress', {
    started_after: new Date(Date.now() + 60_000).toISOString(),
    filenames: ['规程乙.docx'],
  }, `agent-${project.id}`);
  assert.equal(future.json().status, 'preparing');
  assert.equal(future.json().counters.progress, undefined);

  db.prepare(`UPDATE material_analysis_chunks SET status = 'completed' WHERE run_id = ?`)
    .run(runId);
  db.prepare(`UPDATE material_analysis_runs SET stage = 'document_reduce', updated_at = ? WHERE id = ?`)
    .run(new Date().toISOString(), runId);
  db.prepare(`INSERT INTO material_analysis_document_summaries (
    run_id, document_key, document_id, filename, revision_fingerprint, status,
    attempt_count, payload_json, completed_at, updated_at
  ) VALUES (?,?,?,?,?,'completed',0,?,?,?)`).run(
    runId, 'doc-a', null, '规程甲.docx', 'rev-a', '{}', now, now,
  );
  const reducing = await tool(app, project.id, 'get_material_analysis_progress', {
    started_after: now, filenames: ['规程甲.docx', '规程乙.docx'],
  }, `agent-${project.id}`);
  assert.equal(reducing.json().title, '正在汇总全文阅读结果');
  assert.equal(reducing.json().coverage.percent, 100);
  assert.equal(reducing.json().counters.progress, 66);
  assert.ok(reducing.json().counters.progress < 100);
  assert.match(reducing.json().detail, /已完成 1\/2 份文档汇总 · 全文覆盖 100%/u);
});

test('候选路径按目标和流程确认结果确定性阻断跨阶段写入', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Agent 候选阶段门禁', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;

  const earlyDiagram = await tool(app, project.id, 'write_file', {
    path: 'diagrams/early.mmd', content: 'flowchart TD\nA --> B\n',
    change_group: 'early-process', evidence: [],
  }, runId);
  assert.equal(earlyDiagram.statusCode, 409, earlyDiagram.body);
  assert.equal(earlyDiagram.json().gate, 'goal_confirmed');

  const earlyKnowledge = await tool(app, project.id, 'write_file', {
    path: 'objects/early.md', content: '# 提前写入的对象\n',
    change_group: 'early-knowledge', evidence: [],
  }, runId);
  assert.equal(earlyKnowledge.statusCode, 409, earlyKnowledge.body);
  assert.equal(earlyKnowledge.json().code, 'MANAGED_FILE_REQUIRES_DOMAIN_TOOL');

  await confirmGoal(app, project.id, runId);
  const diagram = await tool(app, project.id, 'write_file', {
    path: 'diagrams/approved-goal.mmd', content: 'flowchart TD\nA[开始] --> B[结束]\n',
    change_group: 'approved-process', evidence: [],
  }, runId);
  assert.equal(diagram.statusCode, 200, diagram.body);
  const beforeProcessConfirmation = await tool(app, project.id, 'write_file', {
    path: 'manifest.yaml', content: 'kind: ontology-package\n',
    change_group: 'early-manifest', evidence: [],
  }, runId);
  assert.equal(beforeProcessConfirmation.statusCode, 409, beforeProcessConfirmation.body);
  assert.equal(beforeProcessConfirmation.json().gate, 'process_confirmed');

  const processGroup = { id: 'approved-process', tier: 'caliber', title: '确认业务流程' };
  const processPreflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: processGroup.title, change_groups: [processGroup],
  }, runId);
  assert.equal(processPreflight.statusCode, 200, processPreflight.body);
  const processConfirmed = await tool(app, project.id, 'request_confirmation', {
    summary: processGroup.title, change_groups: [processGroup],
    ...confirmationReceipt(processPreflight),
    review: { approved: true, review_mode: 'full', selected_groups: [processGroup.id] },
  }, runId);
  assert.equal(processConfirmed.statusCode, 200, processConfirmed.body);

  const knowledge = await tool(app, project.id, 'upsert_object', {
    key: 'after_process',
    title: '流程确认后的对象',
    description: '用于验证流程确认后的结构化知识写入。',
    fields: [{ key: 'record_id', cn_name: '记录编号', type: 'string', required: true }],
    relations: [],
    evidence: ['user-decision:process-confirmed'],
    change_group: 'knowledge-after-process',
  }, runId);
  assert.equal(knowledge.statusCode, 200, knowledge.body);
  const script = await tool(app, project.id, 'write_file', {
    path: 'scripts/too-early.py', content: 'def run(value):\n    return value\n',
    change_group: 'early-script', evidence: [],
  }, runId);
  assert.equal(script.statusCode, 409, script.body);
  assert.equal(script.json().gate, 'knowledge_confirmed');
  const pendingKnowledgeGate = await tool(app, project.id, 'gate_status', {
    gate: 'knowledge_confirmed',
  }, runId);
  assert.equal(pendingKnowledgeGate.json().pass, false);
  // 「确认」在专家手上，但把待确认内容整理成确认卡是助手的活。这里不能给空数组：
  // 知识建模阶段只看这一道门禁，判成「助手无事可做」等于整个阶段拿不到指派目标。
  assert.ok(
    pendingKnowledgeGate.json().blockers.some((item: string) => item.includes('确认知识条目')),
    '面向用户的说法不变',
  );
  assert.ok(
    pendingKnowledgeGate.json().agentActionable.some((item: string) => item.includes('发起确认卡')),
    '知识条目待确认时助手的活是发确认卡',
  );

  const pendingGoal = await tool(app, project.id, 'write_file', {
    path: '.modeling/goal.md', content: '# 调整中的建模目标\n',
    change_group: 'adjust-goal', evidence: [],
  }, runId);
  assert.equal(pendingGoal.statusCode, 200, pendingGoal.body);
  const pendingGoalGate = await tool(app, project.id, 'gate_status', {
    gate: 'goal_confirmed',
  }, runId);
  assert.equal(pendingGoalGate.json().pass, false);
  // 门禁要分清这一条是谁的活：blockers 面向用户说「还差什么」，
  // agentActionable 说「助手现在该干什么」，两者主语经常不同。
  assert.deepEqual(pendingGoalGate.json().blockers, ['确认当前建模目标']);
  assert.ok(
    pendingGoalGate.json().agentActionable.some((item: string) => item.includes('起草')),
    '目标确认的助手动作应当是起草并发卡，而不是替专家确认',
  );
  const releasableGate = await tool(app, project.id, 'gate_status', {
    gate: 'releasable',
  }, runId);
  // 发布只能由项目负责人完成，助手没有可独立推进的事，必须是空数组而不是缺字段——
  // 缺字段会让调用方退回 blockers，又把用户的活派给助手。
  assert.deepEqual(releasableGate.json().agentActionable, []);
  const staleGoalCannotAuthorizeProcess = await tool(app, project.id, 'write_file', {
    path: 'diagrams/stale-goal.mmd', content: 'flowchart TD\nA --> B\n',
    change_group: 'stale-goal-process', evidence: [],
  }, runId);
  assert.equal(staleGoalCannotAuthorizeProcess.statusCode, 409, staleGoalCannotAuthorizeProcess.body);
  assert.equal(staleGoalCannotAuthorizeProcess.json().gate, 'goal_confirmed');
});

test('阶段轨进度端点一次给出全部门禁，且没有工作区时如实说读不到', async (t) => {
  const project = createProject({ owner: 'alice', name: '阶段轨进度', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;

  // 助手还没认领工作区时不能编造门禁结论，前端据此把闸位画成中性连接线。
  const before = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/agent/progress` });
  assert.equal(before.statusCode, 200, before.body);
  assert.equal(before.json().available, false);
  assert.deepEqual(before.json().gates, []);

  await confirmGoal(app, project.id, runId);

  const after = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/agent/progress` });
  assert.equal(after.statusCode, 200, after.body);
  const body = after.json();
  assert.equal(body.available, true);
  assert.ok(body.revision);

  // 一次请求要拿到全部门禁，前端画一条轨不该发六次请求。
  const byId = new Map(body.gates.map((gate: any) => [gate.id, gate]));
  for (const id of ['materials_ready', 'goal_confirmed', 'process_confirmed',
    'knowledge_confirmed', 'ready_for_eval', 'releasable']) {
    assert.ok(byId.has(id), `进度端点缺少门禁 ${id}`);
    assert.equal(typeof byId.get(id).pass, 'boolean');
    assert.ok(Array.isArray(byId.get(id).blockers));
    assert.ok(byId.get(id).label, `门禁 ${id} 没有业务标签`);
  }
  assert.equal(byId.get('goal_confirmed').pass, true);
  assert.equal(byId.get('process_confirmed').pass, false);
  assert.ok(byId.get('process_confirmed').blockers.length > 0);

  // 拆分后单点查询必须和整体计算一致，否则助手和阶段轨会各说各话。
  const single = await tool(app, project.id, 'gate_status', { gate: 'process_confirmed' }, runId);
  assert.equal(single.statusCode, 200, single.body);
  assert.equal(single.json().pass, byId.get('process_confirmed').pass);
  assert.deepEqual(single.json().blockers, byId.get('process_confirmed').blockers);
});

test('产品清单台账在 advisory 模式下可持久化并确定性复核', async (t) => {
  const project = createProject({ owner: 'alice', name: '产品清单台账观测', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);

  const staged = await tool(app, project.id, 'stage_product_ledger', {
    schemaVersion: 'product-ledger-v1',
    sourceRevision: claimed.json().baseRevision,
    scope: 'product_catalog',
    rawCount: 3,
    canonicalCount: 2,
    mergedGroupCount: 1,
    entries: [
      {
        canonicalItemId: 'product-a', ruleId: 'rule-a', sourceRecordIds: ['source-a'],
        sourceLocator: 'manual#item-a', evidenceStatus: 'complete', processingStatus: 'ready',
      },
      {
        canonicalItemId: 'product-b', ruleId: 'rule-b', sourceRecordIds: ['source-b', 'source-c'],
        sourceLocator: 'manual#item-b', mergeGroupId: 'merge-1', evidenceStatus: 'complete',
        processingStatus: 'ready',
      },
    ],
  }, runId);
  assert.equal(staged.statusCode, 200, staged.body);
  assert.equal(staged.json().validation.pass, true);
  assert.equal(staged.json().counts.raw, 3);
  assert.equal(staged.json().counts.canonical, 2);

  const read = await tool(app, project.id, 'get_product_ledger', {}, runId);
  assert.equal(read.statusCode, 200, read.body);
  assert.equal(read.json().available, true);
  assert.equal(read.json().validation.pass, true);

  const gate = await tool(app, project.id, 'gate_status', {
    gate: 'product_ledger_verified',
  }, runId);
  assert.equal(gate.statusCode, 200, gate.body);
  assert.equal(gate.json().pass, true);
});

test('enforce 模式下产品台账未通过会阻断规则 Change Set', async (t) => {
  const project = createProject({ owner: 'alice', name: '产品台账规则门禁', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const previousMode = config.productLedger.mode;
  t.after(() => { config.productLedger.mode = previousMode; });
  config.productLedger.mode = 'advisory';
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  const staged = await tool(app, project.id, 'stage_product_ledger', {
    scope: 'product_catalog', rawCount: 2, canonicalCount: 1, mergedGroupCount: 1, entries: [],
  }, runId);
  assert.equal(staged.statusCode, 200, staged.body);
  assert.equal(staged.json().validation.pass, false);

  config.productLedger.mode = 'enforce';
  const blocked = await tool(app, project.id, 'stage_change_set', {
    plan_id: 'plan-product-ledger', expected_revision: 'revision-synthetic',
    mutation_id: 'product-ledger-block-1',
    actions: [{ task_id: 'task-rule', tool: 'create_rule', arguments: {} }],
  }, runId);
  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().code, 'PRODUCT_LEDGER_GATE_BLOCKED');
  assert.equal(blocked.json().gate, 'product_ledger_verified');
});

function seedProductInventoryAnalysis(projectId: string, itemCount = 2): string {
  const db = getDb();
  const now = new Date().toISOString();
  const id = `mar-${projectId}`;
  const items = Array.from({ length: itemCount }, (_, index) => ({
    canonicalItemId: `item-${index}`,
    name: `产品 ${index}`,
    ruleRefs: [`rule-${index}`],
    sourceRecordIds: [`record-${index}`],
    sourceLocator: `material:doc-synthetic#段落 ${index}`,
  }));
  db.prepare(`INSERT INTO material_analysis_runs (
    id, project_id, source_set_fingerprint, brief_fingerprint, model_id,
    protocol_version, plan_json, status, stage, result_json, created_at, updated_at, completed_at
  ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(
    id, projectId, `sources-${projectId}`, 'brief', 'synthetic-model', 'v1', '{}',
    'completed', 'complete', JSON.stringify({ productInventory: items }), now, now, now,
  );
  return id;
}

test('资料分析没有结构化产品事实时，拒绝启用台账门禁而不是把运行锁死', async (t) => {
  const project = createProject({ owner: 'alice', name: '台账门禁可满足性', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);

  const refused = await tool(app, project.id, 'require_product_ledger', { required: true }, runId);
  assert.equal(refused.statusCode, 409, refused.body);
  assert.equal(refused.json().code, 'PRODUCT_INVENTORY_SOURCE_UNAVAILABLE');
  assert.equal(refused.json().productInventory, false);

  // 拒绝必须是原子的：门禁没有落库，正式写入照常可用，不会留下无解状态。
  assert.equal(getDb().prepare('SELECT product_ledger_required FROM projects WHERE id = ?')
    .get(project.id).product_ledger_required, 0);

  const source = await tool(app, project.id, 'get_product_inventory_source', {}, runId);
  assert.equal(source.statusCode, 409, source.body);

  // 来源就位后同一调用应当放行，证明拒绝的是「不可满足」而不是这个工具本身。
  seedProductInventoryAnalysis(project.id);
  const allowed = await tool(app, project.id, 'require_product_ledger', { required: true }, runId);
  assert.equal(allowed.statusCode, 200, allowed.body);
  assert.equal(allowed.json().required, true);
});

test('创建计划不再因模型自己写的计划文案而启用台账门禁', async (t) => {
  const project = createProject({ owner: 'alice', name: '计划文案不触发门禁', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  const revision = claimed.json().baseRevision;

  // 这段措辞命中旧正则（产品/清单 + 完整/逐项/N 条），但它出自模型的规划，
  // 不是用户要求；不应被当作项目策略。
  const plan = await tool(app, project.id, 'create_change_plan', {
    goal: '完整提取 23 项终产品清单并逐项去重', mode: 'generation', expected_revision: revision,
    tasks: [{
      client_key: 'rule-a', title: '生成规则 A', resource_kind: 'rule', target_key: 'rule-a',
      allowed_tools: ['create_rule'], depends_on: [], acceptance: '规则具备来源证据', required: true,
    }],
  }, runId);
  assert.notEqual(plan.json().code, 'PRODUCT_LEDGER_REQUIRED');
  assert.equal(getDb().prepare('SELECT product_ledger_required FROM projects WHERE id = ?')
    .get(project.id).product_ledger_required, 0);
});

test('项目要求产品台账时，advisory 模式也会在计划和旧写入路径前置阻断', async (t) => {
  const project = createProject({ owner: 'alice', name: '产品台账前置门禁', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  const revision = claimed.json().baseRevision;

  seedProductInventoryAnalysis(project.id);
  const required = await tool(app, project.id, 'require_product_ledger', { required: true }, runId);
  assert.equal(required.statusCode, 200, required.body);
  assert.equal(required.json().required, true);

  const plan = await tool(app, project.id, 'create_change_plan', {
    goal: '完成产品清单规则生成', mode: 'generation', expected_revision: revision,
    tasks: [{
      client_key: 'rule-a', title: '生成规则 A', resource_kind: 'rule', target_key: 'rule-a',
      allowed_tools: ['create_rule'], depends_on: [], acceptance: '规则具备来源证据', required: true,
    }],
  }, runId);
  assert.equal(plan.statusCode, 409, plan.body);
  assert.equal(plan.json().code, 'PRODUCT_LEDGER_REQUIRED');
  assert.deepEqual(plan.json().allowedActions, ['get_product_inventory_source', 'get_product_ledger', 'stage_product_ledger']);
  assert.deepEqual(plan.json().protocolActions, [
    'get_product_inventory_source', 'begin_product_ledger', 'append_product_ledger_batch',
    'get_product_ledger_progress', 'finalize_product_ledger',
  ]);

  const legacy = await tool(app, project.id, 'upsert_rule', {
    key: 'rule-a', title: '规则 A', applies_to: { objects: ['object-a'] },
    conditions: [{ condition: '条件', conclusion: '结论' }], conclusion: '结论', evidence: ['user-decision:synthetic'],
  }, runId);
  assert.equal(legacy.statusCode, 409, legacy.body);
  assert.equal(legacy.json().code, 'PRODUCT_LEDGER_REQUIRED');
});

test('产品台账草稿按顺序分批、跨批次去重并由 finalize 重算统计', async (t) => {
  const project = createProject({ owner: 'alice', name: '产品台账批次协议', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  const revision = claimed.json().baseRevision;
  const begun = await tool(app, project.id, 'begin_product_ledger', {
    ledger_id: 'ledger-batch', source_revision: revision,
  }, runId);
  assert.equal(begun.statusCode, 200, begun.body);
  const entryA = {
    canonicalItemId: 'product-a', name: '合成产品 A', ruleRefs: ['rule-a'], sourceRecordIds: ['source-a'],
    sourceLocator: 'synthetic#1', evidenceStatus: 'complete', processingStatus: 'ready',
  };
  const entryB = {
    canonicalItemId: 'product-b', name: '合成产品 B', ruleRefs: ['rule-b'], sourceRecordIds: ['source-b', 'source-c'],
    sourceLocator: 'synthetic#2', mergeGroupId: 'merge-1', evidenceStatus: 'insufficient',
    insufficiencyReason: '合成缺口', processingStatus: 'blocked',
  };
  const outOfOrder = await tool(app, project.id, 'append_product_ledger_batch', {
    ledger_id: 'ledger-batch', batch_no: 2, entries: [entryB],
  }, runId);
  assert.equal(outOfOrder.statusCode, 409, outOfOrder.body);
  assert.equal(outOfOrder.json().code, 'PRODUCT_LEDGER_BATCH_ORDER');
  const first = await tool(app, project.id, 'append_product_ledger_batch', {
    ledger_id: 'ledger-batch', batch_no: 1, entries: [entryA],
  }, runId);
  assert.equal(first.statusCode, 200, first.body);
  const duplicate = await tool(app, project.id, 'append_product_ledger_batch', {
    ledger_id: 'ledger-batch', batch_no: 2, entries: [{ ...entryB, canonicalItemId: 'product-a' }],
  }, runId);
  assert.equal(duplicate.statusCode, 409, duplicate.body);
  assert.equal(duplicate.json().code, 'PRODUCT_LEDGER_DUPLICATE_CANONICAL_ID');
  const second = await tool(app, project.id, 'append_product_ledger_batch', {
    ledger_id: 'ledger-batch', batch_no: 2, entries: [entryB],
  }, runId);
  assert.equal(second.statusCode, 200, second.body);
  const finalized = await tool(app, project.id, 'finalize_product_ledger', {
    ledger_id: 'ledger-batch',
  }, runId);
  assert.equal(finalized.statusCode, 200, finalized.body);
  assert.equal(finalized.json().status, 'finalized');
  assert.deepEqual(finalized.json().counts, {
    raw: 3, canonical: 2, mergedGroups: 1, complete: 1, insufficient: 1,
  });
  const progress = await tool(app, project.id, 'get_product_ledger_progress', {
    ledger_id: 'ledger-batch',
  }, runId);
  assert.equal(progress.statusCode, 200, progress.body);
  assert.equal(progress.json().processed, 2);
  assert.equal(progress.json().remaining, 0);
  const appendAfterFinalize = await tool(app, project.id, 'append_product_ledger_batch', {
    ledger_id: 'ledger-batch', batch_no: 3, entries: [],
  }, runId);
  assert.equal(appendAfterFinalize.statusCode, 409, appendAfterFinalize.body);
  assert.equal(appendAfterFinalize.json().code, 'PRODUCT_LEDGER_FINALIZED');
});

test('Mermaid 围栏写入接入自动修复且确认预检阻断旁路候选', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Mermaid 源码契约', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  const group = { id: 'mermaid-contract', tier: 'caliber', title: '确认流程图源码' };

  const fenced = await tool(app, project.id, 'write_file', {
    path: 'diagrams/process.mmd',
    content: '```mermaid\nflowchart TD\nA --> B\n```\n',
    change_group: group.id,
    evidence: ['user-decision:confirmed-process'],
  }, runId);
  assert.equal(fenced.statusCode, 400, fenced.body);
  assert.equal(fenced.json().code, 'MERMAID_CONTENT_INVALID');
  assert.equal(fenced.json().retryable, true);
  assert.equal(fenced.json().validatorVersion, 'mermaid-source-v1');
  assert.deepEqual(fenced.json().findings.map((finding: any) => finding.ruleId), [
    'mermaid.no-markdown-fence',
    'mermaid.flowchart-declaration',
  ]);

  const corrected = await tool(app, project.id, 'write_file', {
    path: 'diagrams/process.mmd',
    content: 'flowchart TD\nA --> B\n',
    change_group: group.id,
    evidence: ['user-decision:confirmed-process'],
  }, runId);
  assert.equal(corrected.statusCode, 200, corrected.body);

  getDb().prepare(`UPDATE agent_candidate_changes SET content = ?
    WHERE project_id = ? AND run_id = ? AND path = 'diagrams/process.mmd'`)
    .run('```mermaid\nflowchart TD\nA --> B\n```\n', project.id, runId);
  const blocked = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: group.title,
    change_groups: [group],
  }, runId);
  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().code, 'CANDIDATE_FORMAT_INVALID');
  assert.equal(blocked.json().retryable, true);
  assert.deepEqual(blocked.json().findings.map((finding: any) => finding.ruleId), [
    'mermaid.no-markdown-fence',
    'mermaid.flowchart-declaration',
  ]);

  const repaired = await tool(app, project.id, 'write_file', {
    path: 'diagrams/process.mmd',
    content: '%% evidence: user-decision:confirmed-process\nflowchart TD\nA --> B\n',
    change_group: group.id,
    evidence: ['user-decision:confirmed-process'],
  }, runId);
  assert.equal(repaired.statusCode, 200, repaired.body);
  const accepted = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: group.title,
    change_groups: [group],
  }, runId);
  assert.equal(accepted.statusCode, 200, accepted.body);
});

test('Agent 候选写入只在确认后形成 Git 修订，并留下复核方式', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Agent 候选确认' });
  const app = appFor(t);
  const store = getPackageArtifactStore();

  const listed = await tool(app, project.id, 'list_documents');
  assert.equal(listed.statusCode, 200, listed.body);
  const base = await store.current(project.id);
  assert.ok(base);

  const written = await tool(app, project.id, 'write_file', {
    path: '.modeling/goal.md',
    content: '# 建模目标\n\n让专家核对采购判断。\n',
    change_group: 'goal',
    evidence: ['user-decision:project-goal'],
  });
  assert.equal(written.statusCode, 200, written.body);
  const candidateRead = await tool(app, project.id, 'read_file', { path: '.modeling/goal.md' });
  assert.match(candidateRead.json().content, /采购判断/u);
  const diff = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/agent/diff` });
  assert.equal(diff.statusCode, 200, diff.body);
  assert.equal(diff.json().files[0].path, '.modeling/goal.md');
  assert.match(diff.json().files[0].after, /采购判断/u);
  assert.equal((await store.current(project.id))?.revision.oid, base!.revision.oid);

  const plan = await tool(app, project.id, 'update_work_notes', { markdown: '- [ ] 确认采购判断目标' });
  assert.equal(plan.statusCode, 200, plan.body);
  getDb().prepare(`UPDATE agent_candidate_changes SET group_id = '__runtime_context'
    WHERE project_id = ? AND run_id = 'run-agent-1' AND path = '.modeling/plan.md'`).run(project.id);

  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认建模目标',
    change_groups: [{ id: 'goal', tier: 'caliber', title: '确认建模目标' }],
  });
  assert.equal(preflight.statusCode, 200, preflight.body);
  assert.equal(preflight.json().auto_approved, false);
  assert.deepEqual(preflight.json().focus, { kind: 'diff', target: '.modeling/goal.md' });
  assert.equal((await store.current(project.id))?.revision.oid, base!.revision.oid);

  const confirmed = await tool(app, project.id, 'request_confirmation', {
    summary: '确认建模目标',
    change_groups: [{ id: 'goal', tier: 'caliber', title: '确认建模目标' }],
    ...confirmationReceipt(preflight),
    review: {
      approved: true,
      review_mode: 'full',
      selected_groups: ['goal'],
      feedback: '建模目标已确认。\n按当前范围继续。',
    },
  });
  assert.equal(confirmed.statusCode, 200, confirmed.body);
  assert.notEqual(confirmed.json().revision, base!.revision.oid);
  assert.match((await store.readFile(project.id, 'package/.modeling/goal.md')).content, /采购判断/u);
  const acceptedCandidate = await store.getCandidateRevision(project.id, preflight.json().candidate_revision);
  assert.equal(acceptedCandidate.decision?.note, '建模目标已确认。 按当前范围继续。');

  const audit = getDb().prepare(`SELECT review_mode, actor_username FROM agent_confirmations
    WHERE project_id = ?`).get(project.id) as any;
  assert.equal(audit.review_mode, 'full');
  assert.equal(audit.actor_username, 'alice');
  const gate = await tool(app, project.id, 'gate_status', { gate: 'goal_confirmed' });
  assert.equal(gate.json().pass, true);

  const policy = await app.inject({
    method: 'PUT',
    url: `/api/projects/${project.id}/agent/confirm-policy`,
    payload: { tier: 'structural', mode: 'auto' },
  });
  assert.equal(policy.statusCode, 200, policy.body);
  assert.equal(policy.json().policy.find((item: any) => item.tier === 'structural').mode, 'auto');

  const locked = await app.inject({
    method: 'PUT',
    url: `/api/projects/${project.id}/agent/confirm-policy`,
    payload: { tier: 'caliber', mode: 'auto' },
  });
  assert.equal(locked.statusCode, 400, locked.body);
});

test('Agent 可将超过确认上限的历史候选分组原地压缩后继续预检', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Agent 候选重组' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  for (let index = 1; index <= 6; index += 1) {
    const written = await tool(app, project.id, 'write_file', {
      path: `diagrams/flow-${index}.mmd`,
      content: `flowchart TD\n  A${index}[开始] --> B${index}[结束]\n`,
      change_group: `legacy-group-${index}`,
      evidence: [],
    }, runId);
    assert.equal(written.statusCode, 200, written.body);
  }

  const blocked = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认流程',
    change_groups: Array.from({ length: 5 }, (_, index) => ({
      id: `legacy-group-${index + 1}`,
      tier: 'caliber',
      title: `流程 ${index + 1}`,
    })),
  }, runId);
  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().code, 'BROKEN_GROUP_SELECTION');
  assert.equal(blocked.json().regroup_required, true);

  const regrouped = await tool(app, project.id, 'regroup_changes', {}, runId);
  assert.equal(regrouped.statusCode, 200, regrouped.body);
  assert.ok(regrouped.json().change_groups.length <= 5);
  assert.equal(regrouped.json().assignments.flatMap((group: any) => group.paths).length, 6);

  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认重组后的流程',
    change_groups: regrouped.json().change_groups,
  }, runId);
  assert.equal(preflight.statusCode, 200, preflight.body);
});

test('Agent 候选在写入边界拒绝无法持久化的变更组标识', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Agent 非法候选分组' });
  const app = appFor(t);
  const runId = `run-${project.id}`;

  const invalid = await tool(app, project.id, 'write_file', {
    path: '.modeling/goal.md',
    content: '# 建模目标\n\n确认业务范围。\n',
    change_group: 'goal:建模目标草案',
    evidence: ['user-decision:goal'],
  }, runId);

  assert.equal(invalid.statusCode, 400, invalid.body);
  assert.equal(invalid.json().code, 'INVALID_CHANGE_GROUP_ID');
  assert.match(invalid.json().reason, /小写英文开头/u);
});

test('确认准备只规范历史非法分组且冻结后重放同一候选', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Agent 历史候选分组恢复' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const written = await tool(app, project.id, 'write_file', {
    path: '.modeling/goal.md',
    content: '# 建模目标\n\n确认业务范围。\n',
    change_group: 'goal',
    evidence: ['user-decision:goal'],
  }, runId);
  assert.equal(written.statusCode, 200, written.body);
  const analysis = await tool(app, project.id, 'write_file', {
    path: '.modeling/analysis/README.md',
    content: '# 资料分析\n\n仅用于 Agent 工作记录。\n',
    change_group: 'analysis',
    evidence: ['user-decision:goal'],
  }, runId);
  assert.equal(analysis.statusCode, 200, analysis.body);
  getDb().prepare(`UPDATE agent_candidate_changes SET group_id = ?
    WHERE project_id = ? AND run_id = ? AND path = ?`)
    .run('goal:建模目标草案', project.id, runId, '.modeling/goal.md');

  const prepared = await tool(app, project.id, 'prepare_confirmation_groups', {}, runId);
  assert.equal(prepared.statusCode, 200, prepared.body);
  assert.equal(prepared.json().normalized, true);
  assert.equal(prepared.json().change_groups.length, 1);
  assert.match(prepared.json().change_groups[0].id, /^[a-z][a-z0-9_.-]{0,63}$/u);
  assert.equal(prepared.json().change_groups[0].title, '建模目标确认');

  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: `确认建模目标：${'目标范围、资料依据与后续流程。'.repeat(50)}`,
    change_groups: prepared.json().change_groups,
  }, runId);
  assert.equal(preflight.statusCode, 200, preflight.body);
  const stale = await tool(app, project.id, 'request_confirmation', {
    expected_candidate_revision: 'candidate_stale',
    review: { approved: true, review_mode: 'full', selected_groups: [prepared.json().change_groups[0].id] },
  }, runId);
  assert.equal(stale.statusCode, 409, stale.body);
  assert.equal(stale.json().code, 'STALE_CONFIRMATION');
  const replayed = await tool(app, project.id, 'prepare_confirmation_groups', {}, runId);
  assert.equal(replayed.statusCode, 200, replayed.body);
  assert.equal(replayed.json().frozen, true);
  assert.equal(replayed.json().candidate_revision, preflight.json().candidate_revision);
  assert.deepEqual(replayed.json().change_groups, preflight.json().change_groups.map(({ autoApproved: _ignored, ...group }: any) => group));
});

test('确认准备保留合法流程分组的独立确认粒度', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Agent 流程分组保持' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  for (let index = 1; index <= 3; index += 1) {
    const written = await tool(app, project.id, 'write_file', {
      path: `diagrams/flow-${index}.mmd`,
      content: `flowchart TD\n  A${index}[开始] --> B${index}[结束]\n`,
      change_group: `flow_${index}`,
      evidence: [],
    }, runId);
    assert.equal(written.statusCode, 200, written.body);
  }

  const prepared = await tool(app, project.id, 'prepare_confirmation_groups', {}, runId);
  assert.equal(prepared.statusCode, 200, prepared.body);
  assert.equal(prepared.json().normalized, false);
  assert.deepEqual(prepared.json().change_groups.map((group: any) => group.id), ['flow_1', 'flow_2', 'flow_3']);
  assert.ok(prepared.json().change_groups.every((group: any) => group.title === '业务流程确认'));
});

test('项目级运行凭据在用户令牌过期后继续鉴权且不能跨 run 使用', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Agent 长运行鉴权' });
  const bootstrap = appFor(t);
  const runId = `run-${project.id}`;
  const claimed = await tool(bootstrap, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  const credential = String(claimed.json().runCredential ?? '');
  assert.match(credential, /^arc_/u);

  const internal = Fastify();
  internal.addHook('preHandler', requireAuth);
  registerAgentCoreRoutes(internal);
  t.after(() => internal.close());
  const continued = await internal.inject({
    method: 'POST',
    url: '/internal/agent/tools/list_documents',
    headers: {
      'x-modeling-project': project.id,
      'x-modeling-run': runId,
      'x-modeling-agent-credential': credential,
    },
    payload: { arguments: {} },
  });
  assert.equal(continued.statusCode, 200, continued.body);

  const crossed = await internal.inject({
    method: 'POST',
    url: '/internal/agent/tools/list_documents',
    headers: {
      'x-modeling-project': project.id,
      'x-modeling-run': 'run-other',
      'x-modeling-agent-credential': credential,
    },
    payload: { arguments: {} },
  });
  assert.equal(crossed.statusCode, 401, crossed.body);
});

test('候选工作区显式接续、分组层级和退回状态都由 core 确定性约束', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Agent 核心约束' });
  const app = appFor(t);

  const runId = `run-${project.id}`;
  const first = await tool(app, project.id, 'write_file', {
    path: '.modeling/goal.md',
    content: '# 建模目标\n\n确认安全库存判断目标。\n',
    change_group: 'stock-rule',
    evidence: [],
  }, runId);
  assert.equal(first.statusCode, 200, first.body);

  const nextRunId = 'run-agent-2';
  const otherRun = await tool(app, project.id, 'list_documents', {}, nextRunId);
  assert.equal(otherRun.statusCode, 409, otherRun.body);
  assert.equal(otherRun.json().code, 'LEASE_LOST');

  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, nextRunId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  assert.equal(claimed.json().resumed, true);
  assert.equal(claimed.json().preservedChanges, 1);
  const preserved = await tool(app, project.id, 'read_file', { path: '.modeling/goal.md' }, nextRunId);
  assert.equal(preserved.statusCode, 200, preserved.body);
  assert.match(preserved.json().content, /安全库存判断目标/u);
  const staleRun = await tool(app, project.id, 'list_documents', {}, runId);
  assert.equal(staleRun.statusCode, 409, staleRun.body);

  const wrongTier = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认库存规则',
    change_groups: [{ id: 'stock-rule', tier: 'structural', title: '安全库存规则' }],
  }, nextRunId);
  assert.equal(wrongTier.statusCode, 409, wrongTier.body);
  assert.equal(wrongTier.json().code, 'TIER_MISMATCH');

  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认库存规则',
    change_groups: [{ id: 'stock-rule', tier: 'caliber', title: '安全库存规则' }],
  }, nextRunId);
  assert.equal(preflight.statusCode, 200, preflight.body);
  const candidateId = preflight.json().candidate_revision;
  const confirmedBeforeRejection = await getPackageArtifactStore().current(project.id);

  const rejected = await tool(app, project.id, 'request_confirmation_rejected', {
    review: { approved: false, feedback: '补充例外条件' },
  }, nextRunId);
  assert.equal(rejected.statusCode, 200, rejected.body);
  assert.equal(rejected.json().candidateRevision, candidateId);
  assert.equal((await getPackageArtifactStore().getCandidateRevision(project.id, candidateId)).status, 'rejected');
  assert.equal(
    (await getPackageArtifactStore().current(project.id))?.revision.oid,
    confirmedBeforeRejection?.revision.oid,
  );
  const returnedDraft = await tool(app, project.id, 'read_file', { path: '.modeling/goal.md' }, nextRunId);
  assert.equal(returnedDraft.statusCode, 200, returnedDraft.body);
  assert.match(returnedDraft.json().content, /安全库存判断目标/u);

  const adjusted = await tool(app, project.id, 'write_file', {
    path: '.modeling/goal.md',
    content: '# 建模目标\n\n确认安全库存判断目标，停产件不自动补货。\n',
    change_group: 'stock-rule',
    evidence: ['user-decision:stock-exception'],
  }, nextRunId);
  assert.equal(adjusted.statusCode, 200, adjusted.body);
});

test('只有用户退回冻结候选后才能放弃业务修改且运行记录保持不变', async (t) => {
  const project = createProject({ owner: 'alice', name: '放弃已退回业务候选', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const artifacts = getPackageArtifactStore();

  const plan = await tool(app, project.id, 'update_plan', {
    markdown: '# 当前工作\n\n继续核对原有业务阶段。\n',
  }, runId);
  assert.equal(plan.statusCode, 200, plan.body);
  const written = await tool(app, project.id, 'write_file', {
    path: '.modeling/goal.md',
    content: '# 建模目标\n\n这项修改随后由专家决定不再需要。\n',
    change_group: 'discarded-goal',
    evidence: ['user-decision:discarded-goal'],
  }, runId);
  assert.equal(written.statusCode, 200, written.body);
  const confirmedBefore = await artifacts.current(project.id);
  assert.ok(confirmedBefore);

  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认目标修改',
    change_groups: [{ id: 'discarded-goal', tier: 'caliber', title: '建模目标确认' }],
  }, runId);
  assert.equal(preflight.statusCode, 200, preflight.body);
  const candidateId = String(preflight.json().candidate_revision);

  const blocked = await tool(app, project.id, 'discard_rejected_candidate_changes', {}, runId);
  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().code, 'CANDIDATE_DECISION_REQUIRED');
  assert.match(blocked.json().reason, /等待你的决定/u);
  assert.equal((await artifacts.getCandidateRevision(project.id, candidateId)).status, 'pending');

  const rejected = await tool(app, project.id, 'request_confirmation_rejected', {
    review: { approved: false, feedback: '撤销这项修改。\n保持已确认版本。' },
  }, runId);
  assert.equal(rejected.statusCode, 200, rejected.body);
  const rejectedCandidate = await artifacts.getCandidateRevision(project.id, candidateId);
  assert.equal(rejectedCandidate.status, 'rejected');
  assert.equal(rejectedCandidate.decision?.note, '撤销这项修改。 保持已确认版本。');

  const discarded = await tool(app, project.id, 'discard_rejected_candidate_changes', {}, runId);
  assert.equal(discarded.statusCode, 200, discarded.body);
  assert.equal(discarded.json().discarded, true);
  assert.equal(discarded.json().idempotent, false);
  assert.equal(discarded.json().discardedCount, 1);
  assert.deepEqual(discarded.json().discardedPaths, ['.modeling/goal.md']);
  assert.match(discarded.json().display.text, /已确认版本保持不变/u);

  const remaining = (getDb().prepare(`SELECT path, group_id FROM agent_candidate_changes
    WHERE project_id = ? AND run_id = ? ORDER BY path`).all(project.id, runId) as any[])
    .map((row) => ({ path: row.path, group_id: row.group_id }));
  assert.deepEqual(remaining, [{ path: '.modeling/plan.md', group_id: 'runtime_context' }]);
  const savedPlan = await tool(app, project.id, 'read_file', { path: '.modeling/plan.md' }, runId);
  assert.equal(savedPlan.statusCode, 200, savedPlan.body);
  assert.match(savedPlan.json().content, /继续核对原有业务阶段/u);
  assert.equal((await artifacts.current(project.id))?.revision.oid, confirmedBefore!.revision.oid);

  const replayed = await tool(app, project.id, 'discard_rejected_candidate_changes', {}, runId);
  assert.equal(replayed.statusCode, 200, replayed.body);
  assert.equal(replayed.json().discarded, false);
  assert.equal(replayed.json().idempotent, true);
  assert.equal(replayed.json().discardedCount, 0);
  assert.deepEqual(replayed.json().discardedPaths, []);
  assert.deepEqual(
    (getDb().prepare(`SELECT path, group_id FROM agent_candidate_changes
      WHERE project_id = ? AND run_id = ? ORDER BY path`).all(project.id, runId) as any[])
      .map((row) => ({ path: row.path, group_id: row.group_id })),
    remaining,
  );
});

test('删除尚未确认的新增文件会撤销候选写入和变更组', async (t) => {
  const project = createProject({ owner: 'alice', name: '撤销候选新增' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const path = '.modeling/temporary-model.md';

  const written = await tool(app, project.id, 'write_file', {
    path,
    content: '# 临时候选\n',
    change_group: 'temporary-model',
    evidence: [],
  }, runId);
  assert.equal(written.statusCode, 200, written.body);

  const removed = await tool(app, project.id, 'delete_file', {
    path,
    change_group: 'cleanup-temporary-model',
    reason: '这份候选不属于当前阶段',
  }, runId);
  assert.equal(removed.statusCode, 200, removed.body);
  assert.equal(removed.json().cancelledCandidateAddition, true);
  assert.equal(removed.json().removedChangeGroup, 'runtime_context');

  const missing = await tool(app, project.id, 'read_file', { path }, runId);
  assert.equal(missing.statusCode, 404, missing.body);
  const diff = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/agent/diff` });
  assert.deepEqual(diff.json().files, []);
  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '不应存在确认项',
    change_groups: [{ id: 'cleanup-temporary-model', tier: 'structural', title: '清理候选' }],
  }, runId);
  assert.equal(preflight.statusCode, 409, preflight.body);
  assert.equal(preflight.json().code, 'NO_CANDIDATE');
  assert.equal(preflight.json().retryable, false);
});

test('物理对象 mapping 是编译产物，Agent 不能按路径删除', async (t) => {
  const project = createProject({ owner: 'alice', name: '保护数据映射', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const current = await artifacts.initializeDraftPackage(project.id, {
    id: `mapping-delete-${project.id}`,
    name: '保护数据映射',
    description: '测试 mapping 编译产物保护',
  });
  await artifacts.savePackageVersion(project.id, {
    expectedRevision: current.revision.oid,
    spec: {
      ...structuredClone(current.spec),
      objects: [{
        key: 'synthetic_record', title: '合成记录', description: '只用于 mapping 删除保护测试。',
        sourceTable: 'synthetic_records',
        sourceRefs: [{ kind: 'table', table: 'synthetic_records' }],
        primaryKey: ['record_id'],
        fields: [{
          key: 'record_id', cnName: '记录编号', type: 'string', required: true, sourceColumn: 'record_id',
        }],
      }],
    },
    coverage: current.coverage,
    note: '建立物理 mapping 测试基线',
    action: {
      cause: 'test.mapping_seeded', summary: '建立物理 mapping 测试基线',
      actorKind: 'system', actorName: 'test',
    },
  });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const path = 'mappings/object-data-mappings.json';
  const before = await tool(app, project.id, 'read_file', { path }, runId);
  assert.equal(before.statusCode, 200, before.body);

  const blocked = await tool(app, project.id, 'delete_file', {
    path,
    change_group: 'remove-mapping',
    reason: '模拟 Agent 将映射文件误判为无用文件',
  }, runId);

  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().code, 'MANAGED_FILE_REQUIRES_DOMAIN_TOOL');
  assert.equal(getDb().prepare(`SELECT kind FROM agent_candidate_changes
    WHERE project_id = ? AND run_id = ? AND path = ?`).get(project.id, runId, path), undefined);
  const after = await tool(app, project.id, 'read_file', { path }, runId);
  assert.equal(after.statusCode, 200, after.body);
  assert.equal(after.json().content, before.json().content);
});

test('关系审阅依赖对象和 Operation 任务并直接写入中文关系', async (t) => {
  const project = createProject({ owner: 'alice', name: 'synthetic relation suggestions', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(project.id, {
    id: 'synthetic-relation-suggestions',
    name: 'synthetic relation suggestions',
    description: 'Synthetic package for deterministic object relation suggestions.',
  });
  await artifacts.savePackageVersion(project.id, {
    expectedRevision: initial.revision.oid,
    spec: {
      ...structuredClone(initial.spec),
      objects: [{
        key: 'customer',
        title: 'Customer',
        description: 'Synthetic customer object.',
        primaryKey: ['customer_id'],
        fields: [
          { key: 'customer_id', type: 'string', required: true, sourceColumn: 'customer_id' },
          { key: 'customer_name', type: 'string', required: false, sourceColumn: 'customer_name' },
        ],
      }, {
        key: 'order',
        title: 'Order',
        description: 'Synthetic order object.',
        primaryKey: ['order_id'],
        fields: [
          { key: 'order_id', type: 'string', required: true, sourceColumn: 'order_id' },
          { key: 'customer_id', type: 'string', required: false, sourceColumn: 'customer_id' },
        ],
        relations: [{ relation: 'belongs_to', target: 'customer', note: 'Already confirmed.' }],
      }, {
        key: 'invoice',
        title: 'Invoice',
        description: 'Synthetic invoice object.',
        primaryKey: ['invoice_id'],
        fields: [
          { key: 'invoice_id', type: 'string', required: true, sourceColumn: 'invoice_id' },
          { key: 'customer_id', type: 'string', required: false, sourceColumn: 'customer_id' },
        ],
      }],
      operations: [{
        key: 'review_invoice',
        title: 'Review invoice',
        description: 'Synthetic operation using related objects.',
        inputs: [],
        outputs: [],
        flow: ['read invoice'],
        relatedObjects: ['invoice', 'customer'],
      }],
    },
    coverage: initial.coverage,
    note: 'Seed synthetic relation suggestion package.',
    action: {
      cause: 'test.seed_relation_suggestions',
      summary: 'Seed synthetic relation suggestion package.',
      actorKind: 'system',
      actorName: 'test',
    },
  });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const listed = await tool(app, project.id, 'list_domain_resources', { kind: 'object' }, runId);
  assert.equal(listed.statusCode, 200, listed.body);

  const pending = getDb().prepare(`SELECT COUNT(*) AS count FROM agent_candidate_changes
    WHERE project_id = ? AND run_id = ?`).get(project.id, runId) as { count: number };
  assert.equal(pending.count, 0);

  const planResponse = await tool(app, project.id, 'create_change_plan', {
    goal: '统一审阅对象和业务操作形成的关系',
    mode: 'expert_edit',
    expected_revision: listed.json().working_revision,
    tasks: [{
      client_key: 'objects-ready',
      title: '确认对象',
      resource_kind: 'object',
      target_key: 'invoice',
      allowed_tools: ['patch_object'],
      acceptance: '对象已就绪',
    }, {
      client_key: 'operations-ready',
      title: '确认业务操作',
      resource_kind: 'operation',
      target_key: 'review_invoice',
      allowed_tools: ['patch_operation'],
      acceptance: '业务操作已就绪',
    }],
  }, runId);
  assert.equal(planResponse.statusCode, 200, planResponse.body);
  const relationTask = planResponse.json().plan.tasks.find((task: any) => (
    task.client_key === '__object_relation_review__'
  ));
  assert.ok(relationTask);
  assert.equal(relationTask.allowed_tools[0], 'review_object_relations');
  assert.deepEqual(relationTask.depends_on.sort(), planResponse.json().plan.tasks
    .filter((task: any) => ['objects-ready', 'operations-ready'].includes(task.client_key))
    .map((task: any) => task.id)
    .sort());

  const revisedPlan = await tool(app, project.id, 'revise_change_plan', {
    plan_id: planResponse.json().plan.id,
    expected_version: planResponse.json().plan.version,
    reason: '补充后续业务操作',
    add_tasks: [{
      client_key: 'followup-operation',
      title: '确认后续业务操作',
      resource_kind: 'operation',
      target_key: 'approve_invoice',
      allowed_tools: ['create_operation'],
      acceptance: '后续业务操作已就绪',
    }],
  }, runId);
  assert.equal(revisedPlan.statusCode, 200, revisedPlan.body);
  const revisedRelationTask = revisedPlan.json().plan.tasks.find((task: any) => (
    task.client_key === '__object_relation_review__'
  ));
  const followupOperation = revisedPlan.json().plan.tasks.find((task: any) => (
    task.client_key === 'followup-operation'
  ));
  assert.ok(revisedRelationTask.depends_on.includes(followupOperation.id));
  const approvedRevision = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/change-plans/${revisedPlan.json().plan.id}/respond`,
    payload: { approved: true },
  });
  assert.equal(approvedRevision.statusCode, 200, approvedRevision.body);
  getDb().prepare(`UPDATE agent_change_plan_tasks SET status = 'staged'
    WHERE plan_id = ? AND client_key = 'objects-ready'`)
    .run(planResponse.json().plan.id);

  const waitingReview = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/object-relations/review`,
  });
  assert.equal(waitingReview.statusCode, 200, waitingReview.body);
  assert.equal(waitingReview.json().ready, false);
  getDb().prepare(`UPDATE agent_change_plan_tasks SET status = 'staged'
    WHERE plan_id = ? AND client_key IN ('objects-ready', 'operations-ready', 'followup-operation')`)
    .run(planResponse.json().plan.id);

  const reviewResponse = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/object-relations/review?limit=10`,
  });
  assert.equal(reviewResponse.statusCode, 200, reviewResponse.body);
  assert.equal(reviewResponse.json().ready, true);
  assert.ok(reviewResponse.json().candidates.every((candidate: any) => !Object.hasOwn(candidate, 'confidence')));
  const reviewedInvoice = reviewResponse.json().candidates.find((candidate: any) => (
    candidate.source_object === 'invoice'
    && candidate.target_object === 'customer'
    && candidate.relation === '属于'
  ));
  assert.ok(reviewedInvoice, reviewResponse.body);
  assert.equal(reviewedInvoice.default_selected, true);
  assert.equal(reviewedInvoice.selected, true);
  assert.equal(reviewResponse.json().all_fingerprints.length, reviewResponse.json().total);
  assert.equal(reviewResponse.json().candidates.some((candidate: any) => (
    candidate.source_object === 'order'
    && candidate.target_object === 'customer'
    && candidate.relation === '属于'
  )), false);

  const agentReview = await tool(app, project.id, 'review_object_relations', {
    plan_task_id: relationTask.id,
    expected_revision: reviewResponse.json().working_revision,
    review_id: reviewResponse.json().review_id,
    limit: 20,
  }, runId);
  assert.equal(agentReview.statusCode, 200, agentReview.body);
  assert.equal(agentReview.json().review_id, reviewResponse.json().review_id);
  assert.ok(agentReview.json().candidates.some((candidate: any) => (
    candidate.fingerprint === reviewedInvoice.fingerprint
  )));

  const invalidRelations = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/object-relations/review`,
    payload: {
      review_id: reviewResponse.json().review_id,
      expected_revision: reviewResponse.json().working_revision,
      selected_fingerprints: [reviewedInvoice.fingerprint],
      relation_edits: [{ fingerprint: reviewedInvoice.fingerprint, relation: '1:N' }],
    },
  });
  assert.equal(invalidRelations.statusCode, 422, invalidRelations.body);
  assert.equal(invalidRelations.json().relations[0].fingerprint, reviewedInvoice.fingerprint);
  assert.match(invalidRelations.json().relations[0].message, /关系基数/u);
  const retainedReview = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/object-relations/review?limit=20`,
  });
  assert.equal(retainedReview.statusCode, 200, retainedReview.body);
  const retainedInvoice = retainedReview.json().candidates.find((candidate: any) => (
    candidate.fingerprint === reviewedInvoice.fingerprint
  ));
  assert.equal(retainedInvoice.relation, '1:N');
  assert.equal(retainedInvoice.selected, true);
  assert.match(retainedInvoice.error.message, /关系基数/u);

  const applied = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/object-relations/review`,
    payload: {
      review_id: reviewResponse.json().review_id,
      expected_revision: reviewResponse.json().working_revision,
      selected_fingerprints: [reviewedInvoice.fingerprint],
      relation_edits: [{ fingerprint: reviewedInvoice.fingerprint, relation: '属于' }],
    },
  });
  assert.equal(applied.statusCode, 200, applied.body);
  assert.equal(applied.json().selected_count, 1);
  const writtenObject = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/studio/elements/${encodeURIComponent('object:invoice')}`
      + `?workingRevision=${encodeURIComponent(applied.json().working_revision)}`,
  });
  assert.equal(writtenObject.statusCode, 200, writtenObject.body);
  assert.ok(writtenObject.json().element.relations.some((relation: any) => (
    relation.kind === 'object_relation'
    && relation.targetId === 'object:customer'
    && relation.label === '属于'
  )));
});

test('虚拟对象模式缺失 mapping 是合法状态', async (t) => {
  const project = createProject({ owner: 'alice', name: '虚拟对象无映射', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(project.id, {
    id: 'virtual-without-mapping',
    name: '虚拟对象无映射',
    description: '验证虚拟对象不需要 mapping',
  });
  const importedFiles = renderPackage(initial.spec);
  delete importedFiles['mappings/object-data-mappings.json'];
  await artifacts.importPackageVersion(project.id, {
    expectedRevision: initial.revision.oid,
    spec: initial.spec,
    coverage: initial.coverage,
    files: importedFiles,
    action: {
      cause: 'test.legacy_mapping_import',
      summary: '导入缺失 mapping 的历史包',
      actorKind: 'system',
      actorName: 'test',
    },
  });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const current = await artifacts.readPackageFiles(project.id);
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  const candidate = await artifacts.createCandidateRevision(project.id, {
    expectedRevision: current.revision.oid,
    groups: [{
      id: 'unrelated-manifest',
      summary: '更新知识包说明',
      changes: [{
        kind: 'write',
        path: 'package/manifest.yaml',
        content: current.files['manifest.yaml'].replace(
          /^description:.*$/mu,
          'description: 只修改与数据映射无关的知识包说明',
        ),
      }],
    }],
    action: {
      cause: 'test_unrelated_manifest',
      summary: '构造与 mapping 无关的候选',
      actorKind: 'agent',
      actorName: '测试 Agent',
    },
  });
  getDb().prepare(`UPDATE agent_candidate_workspaces
    SET candidate_id = ?, base_revision_oid = ? WHERE project_id = ? AND run_id = ?`)
    .run(candidate.id, current.revision.oid, project.id, runId);
  getDb().prepare(`INSERT INTO agent_candidate_group_metadata (
    candidate_id, group_id, tier, title, impact_json, warning
  ) VALUES (?,?,?,?,?,NULL)`)
    .run(candidate.id, 'unrelated-manifest', 'index', '更新知识包说明', '[]');

  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认无关的 manifest 修改',
    change_groups: [{ id: 'unrelated-manifest', tier: 'index', title: '更新知识包说明' }],
  }, runId);
  assert.equal(preflight.statusCode, 200, preflight.body);
});

test('大校验报告无损落库且只按页进入 Agent 上下文', async (t) => {
  const project = createProject({ owner: 'alice', name: '分页校验报告', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  const group = { id: 'validation-pages', tier: 'caliber', title: '批量流程校验' };
  const paths = ['diagrams/first.mmd', 'diagrams/second.mmd', 'diagrams/third.mmd'];
  for (const path of paths) {
    const written = await tool(app, project.id, 'write_file', {
      path,
      content: 'flowchart TD\nA --> B\n',
      change_group: group.id,
      evidence: ['user-decision:validation-pages'],
    }, runId);
    assert.equal(written.statusCode, 200, written.body);
  }
  getDb().prepare(`UPDATE agent_candidate_changes SET content = ?
    WHERE project_id = ? AND run_id = ? AND path = ?`).run(
    '```mermaid\nflowchart TD\nA --> B\n```\n', project.id, runId, paths[0],
  );
  getDb().prepare(`UPDATE agent_candidate_changes SET content = ?
    WHERE project_id = ? AND run_id = ? AND path = ?`).run(
    '```mermaid\nflowchart TD\nA --> B\n```\n', project.id, runId, paths[1],
  );
  getDb().prepare(`UPDATE agent_candidate_changes SET content = ?
    WHERE project_id = ? AND run_id = ? AND path = ?`).run(
    '```mermaid\nflowchart TD\nA --> B\n```\n', project.id, runId, paths[2],
  );

  const blocked = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: group.title,
    change_groups: [group],
  }, runId);
  assert.equal(blocked.statusCode, 409, blocked.body);
  const failure = blocked.json();
  assert.equal(failure.code, 'CANDIDATE_FORMAT_INVALID');
  assert.equal(failure.findingCount, 6);
  assert.equal(failure.findings, undefined);
  assert.equal(failure.findingsTruncated, true);
  assert.match(failure.findingFingerprint, /^sha256:/u);
  assert.match(failure.validationReportId, /^avr_/u);
  assert.equal(failure.findingSummary.paths.length, 3);

  const summary = await tool(app, project.id, 'get_validation_summary', {
    report_id: failure.validationReportId,
  }, runId);
  assert.equal(summary.statusCode, 200, summary.body);
  assert.equal(summary.json().findingCount, 6);
  assert.equal(summary.json().findingFingerprint, failure.findingFingerprint);

  const reconstructed: any[] = [];
  let offset = 0;
  while (offset < failure.findingCount) {
    const page = await tool(app, project.id, 'get_validation_findings', {
      report_id: failure.validationReportId,
      offset,
      limit: 2,
    }, runId);
    assert.equal(page.statusCode, 200, page.body);
    reconstructed.push(...page.json().findings);
    offset = page.json().nextOffset ?? failure.findingCount;
  }
  assert.equal(reconstructed.length, 6);
  assert.deepEqual(new Set(reconstructed.map((finding) => finding.path)), new Set(paths));
  assert.deepEqual(new Set(reconstructed.map((finding) => finding.ruleId)), new Set([
    'mermaid.no-markdown-fence',
    'mermaid.flowchart-declaration',
  ]));
  const stored = getDb().prepare(`SELECT report_json FROM agent_validation_reports
    WHERE id = ?`).get(failure.validationReportId) as { report_json: string };
  assert.equal(JSON.parse(stored.report_json).findings.length, 6);
  const findingRows = getDb().prepare(`SELECT COUNT(*) AS count FROM agent_validation_findings
    WHERE report_id = ?`).get(failure.validationReportId) as { count: number };
  assert.equal(findingRows.count, 6);
});

test('少量超长 finding 也按字符预算压缩且原文仍可分页读取', async (t) => {
  const project = createProject({ owner: 'alice', name: '超长校验投影', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const path = `diagrams/${'synthetic_'.repeat(260)}flow.mmd`;
  const blocked = await tool(app, project.id, 'write_file', {
    path,
    content: '```mermaid\nflowchart TD\nA --> B\n```\n',
    change_group: 'long-finding',
    evidence: [],
  }, runId);
  assert.equal(blocked.statusCode, 400, blocked.body);
  const failure = blocked.json();
  assert.equal(failure.findingCount, 2);
  assert.equal(failure.findings, undefined);
  assert.equal(failure.findingsTruncated, true);
  assert.ok(failure.reason.length < 200);
  assert.ok(failure.findingSummary.paths[0].path.length <= 240);

  const page = await tool(app, project.id, 'get_validation_findings', {
    report_id: failure.validationReportId,
    limit: 1,
  }, runId);
  assert.equal(page.statusCode, 200, page.body);
  assert.equal(page.json().findings[0].path, path);
});

test('无 Schema 时领域工具编译操作和规则，但拒绝对象物理绑定与 mapping', async (t) => {
  const project = createProject({ owner: 'alice', name: '领域工具编译', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(project.id, {
    id: 'domain-tools-package',
    name: '领域工具编译',
    description: '验证结构化输入与确定性文件生成。',
  });
  const baseline = await artifacts.savePackageVersion(project.id, {
    expectedRevision: initial.revision.oid,
    spec: {
      ...initial.spec,
      version: '0.2.0',
      dataSourceType: 'nl2sql',
      objects: [{
        key: 'inventory_item',
        title: '库存项',
        description: '表示一项可查询的库存记录。',
        fields: [
          { key: 'item_id', cnName: '记录编号', type: 'string', required: true },
          { key: 'quantity', cnName: '数量', type: 'number', required: true },
        ],
      }],
    },
    coverage: { covered: [], uncovered: [] },
    action: {
      cause: 'test_setup',
      summary: '建立领域工具合成基线',
      actorKind: 'user',
      actorName: 'alice',
    },
  });
  assert.ok(baseline.revision.oid);
  const material = insertDocumentRevision({
    projectId: project.id,
    filename: 'inventory.csv',
    kind: 'csv',
    parseStatus: 'parsed',
    tables: [{
      name: 'inventory_rows',
      rowCount: 1,
      fields: [
        { name: 'record_id', inferredType: 'string', sampleValues: ['R-1'], distinctRatio: 1 },
        { name: 'available_qty', inferredType: 'number', sampleValues: ['8'], distinctRatio: 1 },
      ],
      rows: [['R-1', '8']],
    }],
  }).document;
  const app = appFor(t);
  const selectedSource = await app.inject({
    method: 'PUT',
    url: `/api/projects/${project.id}/agent/package-data-source`,
    payload: { dataSourceType: 'nl2sql' },
  });
  assert.equal(selectedSource.statusCode, 200, selectedSource.body);
  const runId = `run-${project.id}`;
  const operationGroup = { id: 'domain-operation', tier: 'structural', title: '库存查询操作' };
  const ruleGroup = { id: 'domain-rule', tier: 'caliber', title: '库存可用规则' };
  const bindingGroup = { id: 'domain-binding', tier: 'structural', title: '库存数据绑定' };

  const operation = await tool(app, project.id, 'upsert_operation', {
    key: 'query_inventory',
    title: '查询库存',
    description: '按记录编号查询当前可用数量。',
    inputs: [{ param: 'item_id', cn_name: '记录编号', type: 'string', required: true }],
    outputs: [{ field: 'quantity', cn_name: '可用数量', type: 'number' }],
    flow: ['根据 item_id 定位 object:inventory_item', '返回 quantity'],
    related_objects: ['inventory_item'],
    uses_rules: [],
    uses_metrics: [],
    evidence: ['user-decision:domain-operation'],
    change_group: operationGroup.id,
  }, runId);
  assert.equal(operation.statusCode, 200, operation.body);
  assert.equal(operation.json().deterministic, true);

  const rule = await tool(app, project.id, 'upsert_rule', {
    key: 'inventory_available',
    title: '库存可用判断',
    applies_to: { objects: ['inventory_item'], operations: ['query_inventory'] },
    conditions: [{ condition: 'quantity 大于 0', conclusion: '库存可用' }],
    conclusion: '只有可用数量大于 0 时才视为库存可用。',
    evidence: ['user-decision:domain-rule'],
    change_group: ruleGroup.id,
  }, runId);
  assert.equal(rule.statusCode, 200, rule.body);

  const duplicateSourceBinding = await tool(app, project.id, 'bind_object_source', {
    key: 'inventory_item',
    document_id: material.id,
    table: 'inventory_rows',
    field_bindings: [
      { field_key: 'item_id', source_column: 'record_id' },
      { field_key: 'quantity', source_column: 'record_id' },
    ],
    primary_key: ['item_id'],
    evidence: [`material:${material.id}`],
    change_group: bindingGroup.id,
  }, runId);
  assert.equal(duplicateSourceBinding.statusCode, 409, duplicateSourceBinding.body);
  assert.equal(duplicateSourceBinding.json().code, 'PHYSICAL_SCHEMA_REQUIRED');

  const binding = await tool(app, project.id, 'bind_object_source', {
    key: 'inventory_item',
    document_id: material.id,
    table: 'inventory_rows',
    field_bindings: [
      { field_key: 'item_id', source_column: 'record_id' },
      { field_key: 'quantity', source_column: 'available_qty' },
    ],
    primary_key: ['item_id'],
    evidence: [`material:${material.id}`],
    change_group: bindingGroup.id,
  }, runId);
  assert.equal(binding.statusCode, 409, binding.body);
  assert.equal(binding.json().code, 'PHYSICAL_SCHEMA_REQUIRED');

  const directMapping = await tool(app, project.id, 'write_file', {
    path: 'mappings/object-data-mappings.json',
    content: '{}',
    change_group: 'manual-mapping',
    evidence: [],
  }, runId);
  assert.equal(directMapping.statusCode, 409, directMapping.body);
  assert.equal(directMapping.json().code, 'PHYSICAL_SCHEMA_REQUIRED');
  assert.equal(directMapping.json().object_source_mode, 'virtual');
  assert.equal(directMapping.json().requiredAction, undefined);
  assert.equal(directMapping.json().requiredTool, undefined);

  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认领域工具生成的知识',
    change_groups: [operationGroup, ruleGroup],
  }, runId);
  assert.equal(preflight.statusCode, 200, preflight.body);
  assert.equal(preflight.json().atomic_selection, true);
  const partial = await tool(app, project.id, 'request_confirmation', {
    summary: '只接受部分领域知识',
    change_groups: [operationGroup, ruleGroup],
    ...confirmationReceipt(preflight),
    review: {
      approved: true,
      review_mode: 'full',
      selected_groups: [operationGroup.id],
    },
  }, runId);
  assert.equal(partial.statusCode, 409, partial.body);
  assert.equal(partial.json().code, 'DOMAIN_CANDIDATE_PARTIAL_SELECTION_FORBIDDEN');
  assert.deepEqual(partial.json().missing.sort(), [ruleGroup.id]);
  const accepted = await tool(app, project.id, 'request_confirmation', {
    summary: '确认领域工具生成的知识',
    change_groups: [operationGroup, ruleGroup],
    ...confirmationReceipt(preflight),
    review: {
      approved: true,
      review_mode: 'full',
      selected_groups: [operationGroup.id, ruleGroup.id],
    },
  }, runId);
  assert.equal(accepted.statusCode, 200, accepted.body);

  const current = await artifacts.readPackageFiles(project.id);
  const storedOperation = current.spec.operations.find((item) => item.key === 'query_inventory');
  const storedRule = (current.spec.rules ?? []).find((item) => item.key === 'inventory_available');
  const storedObject = current.spec.objects.find((item) => item.key === 'inventory_item');
  assert.deepEqual(storedOperation?.relatedObjects, ['inventory_item']);
  assert.deepEqual(storedRule?.appliesToOperations, ['query_inventory']);
  assert.equal(storedObject?.fields[0].sourceColumn, undefined);
  assert.equal(storedObject?.fields[1].sourceColumn, undefined);
  assert.equal(current.files['mappings/object-data-mappings.json'], undefined);
  const rendered = renderPackage(current.spec);
  for (const path of [
    'objects/inventory_item.md',
    'objects/index.md',
    'operations/query_inventory.md',
    'operations/index.md',
    'rules/inventory_available.md',
    'rules/index.md',
  ]) assert.equal(current.files[path], rendered[path]);
});

test('无 Schema 时 upsert_object 不能通过 source 参数生成物理绑定', async (t) => {
  const project = createProject({ owner: 'alice', name: '对象领域工具编译', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(project.id, {
    id: 'object-domain-tool-package',
    name: '对象领域工具编译',
    description: '验证对象语义与数据绑定一次生成。',
  });
  await artifacts.savePackageVersion(project.id, {
    expectedRevision: initial.revision.oid,
    spec: { ...initial.spec, version: '0.2.0', dataSourceType: 'nl2sql' },
    coverage: { covered: [], uncovered: [] },
    action: { cause: 'test_setup', summary: '建立对象领域工具基线', actorKind: 'user', actorName: 'alice' },
  });
  const material = insertDocumentRevision({
    projectId: project.id,
    filename: 'leads.csv',
    kind: 'csv',
    parseStatus: 'parsed',
    tables: [{
      name: 'lead_rows',
      rowCount: 1,
      fields: [
        { name: 'lead_id', inferredType: 'string', sampleValues: ['L-1'], distinctRatio: 1 },
        { name: 'lead_name', inferredType: 'string', sampleValues: ['示例线索'], distinctRatio: 1 },
      ],
      rows: [['L-1', '示例线索']],
    }],
  }).document;
  const app = appFor(t);
  const selectedSource = await app.inject({
    method: 'PUT',
    url: `/api/projects/${project.id}/agent/package-data-source`,
    payload: { dataSourceType: 'nl2sql' },
  });
  assert.equal(selectedSource.statusCode, 200, selectedSource.body);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);
  const cardinalityOnly = await tool(app, project.id, 'upsert_object', {
    key: 'relation_probe',
    title: '合成关系对象',
    description: '用于验证关系名称输入边界的合成对象。',
    fields: [{ key: 'id', cn_name: '合成编号', type: 'string', required: true }],
    relations: [{ relation: '1:N', target: 'relation_probe' }],
    evidence: ['user-decision:synthetic-relation-check'],
    change_group: 'relation-probe',
  }, runId);
  assert.equal(cardinalityOnly.statusCode, 400, cardinalityOnly.body);
  assert.equal(cardinalityOnly.json().code, 'INVALID_DOMAIN_PAYLOAD');
  assert.match(cardinalityOnly.body, /关系名称不能只填写关系基数/);
  const rejectedCandidate = getDb().prepare(`SELECT COUNT(*) AS count FROM agent_candidate_changes
    WHERE project_id = ? AND run_id = ? AND path = 'objects/relation_probe.md'`)
    .get(project.id, runId) as { count: number };
  assert.equal(rejectedCandidate.count, 0);

  const group = { id: 'lead-object', tier: 'structural', title: '营销线索对象' };
  const upserted = await tool(app, project.id, 'upsert_object', {
    key: 'marketing_lead',
    title: '营销线索',
    description: '表示一条待跟进的营销线索。',
    aliases: ['销售线索'],
    query_boundary: '仅查询已登记的线索字段。',
    fields: [
      { key: 'id', cn_name: '线索编号', type: 'string', required: true },
      { key: 'name', cn_name: '线索名称', type: 'string', required: true },
    ],
    relations: [],
    source: {
      document_id: material.id,
      table: 'lead_rows',
      field_bindings: [
        { field_key: 'id', source_column: 'lead_id' },
        { field_key: 'name', source_column: 'lead_name' },
      ],
      primary_key: ['id'],
    },
    evidence: [`material:${material.id}`],
    change_group: group.id,
  }, runId);
  assert.equal(upserted.statusCode, 409, upserted.body);
  assert.equal(upserted.json().code, 'PHYSICAL_SCHEMA_REQUIRED');
  const current = await artifacts.readPackageFiles(project.id);
  assert.equal(current.spec.objects.some((item) => item.key === 'marketing_lead'), false);
  assert.equal(current.files['mappings/object-data-mappings.json'], undefined);
});

test('退回并放弃领域候选会同时清除隐藏编译规格', async (t) => {
  const project = createProject({ owner: 'alice', name: '撤销领域编译', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(project.id, {
    id: 'discard-domain-package',
    name: '撤销领域编译',
    description: '验证退回候选不残留内部规格。',
  });
  await artifacts.savePackageVersion(project.id, {
    expectedRevision: initial.revision.oid,
    spec: {
      ...initial.spec,
      version: '0.2.0',
      objects: [{
        key: 'record',
        title: '记录',
        description: '用于撤销候选测试的合成记录。',
        fields: [{ key: 'id', type: 'string', required: true }],
      }],
    },
    coverage: { covered: [], uncovered: [] },
    action: {
      cause: 'test_setup', summary: '建立撤销测试基线', actorKind: 'user', actorName: 'alice',
    },
  });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const group = { id: 'discard-operation', tier: 'structural', title: '待撤销操作' };
  const written = await tool(app, project.id, 'upsert_operation', {
    key: 'read_record',
    title: '读取记录',
    description: '根据记录编号读取合成记录。',
    inputs: [{ param: 'id', type: 'string', required: true }],
    outputs: [{ field: 'id', type: 'string' }],
    flow: ['根据 id 读取 object:record'],
    related_objects: ['record'],
    evidence: ['user-decision:discard-operation'],
    change_group: group.id,
  }, runId);
  assert.equal(written.statusCode, 200, written.body);
  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: group.title,
    change_groups: [group],
  }, runId);
  assert.equal(preflight.statusCode, 200, preflight.body);
  const rejected = await tool(app, project.id, 'request_confirmation_rejected', {
    review: { approved: false, feedback: '保持当前版本' },
  }, runId);
  assert.equal(rejected.statusCode, 200, rejected.body);
  const discarded = await tool(app, project.id, 'discard_rejected_candidate_changes', {}, runId);
  assert.equal(discarded.statusCode, 200, discarded.body);
  assert.equal(discarded.json().discarded, true);
  const remaining = getDb().prepare(`SELECT path, group_id FROM agent_candidate_changes
    WHERE project_id = ? AND run_id = ? ORDER BY path`).all(project.id, runId);
  assert.deepEqual(remaining, []);
  const current = await artifacts.readPackageFiles(project.id);
  assert.equal(current.spec.operations.some((item) => item.key === 'read_record'), false);
  assert.equal(current.files['operations/read_record.md'], undefined);
});


test('有可读资料时扫描件解析失败只告警，非受管说明仍可引用资料', async (t) => {
  const project = createProject({ owner: 'alice', name: '资料解析门禁' });
  const readable = insertDocumentRevision({
    projectId: project.id,
    filename: '流程说明.md',
    kind: 'text',
    parseStatus: 'parsed',
    textContent: '# 采购流程\n提交后由负责人审核。',
  });
  insertDocumentRevision({
    projectId: project.id,
    filename: '扫描附件.pdf',
    kind: 'pdf',
    parseStatus: 'failed',
    parseError: '扫描版 PDF 无可提取文字',
  });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);

  const written = await tool(app, project.id, 'write_file', {
    path: 'mappings/procurement-evidence.txt',
    content: `---
resource: rule
key: procurement
title: 采购规则
evidence:
  - material:流程说明.md#采购流程
---
# 规则内容

提交后由负责人审核。

# 知识证据

- material:流程说明.md#采购流程
`,
    change_group: 'procurement-rule',
    evidence: ['material:流程说明.md#采购流程'],
  }, runId);
  assert.equal(written.statusCode, 200, written.body);

  const referrers = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/materials/${readable.document.id}/referrers`,
  });
  assert.equal(referrers.statusCode, 200, referrers.body);
  assert.equal(referrers.json().referrers[0].path, 'mappings/procurement-evidence.txt');

  const located = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/evidence/locate?ref=${encodeURIComponent('material:流程说明.md#采购流程')}`,
  });
  assert.equal(located.statusCode, 200, located.body);
  assert.match(located.json().excerpt, /采购流程/u);

  const gate = await tool(
    app,
    project.id,
    'gate_status',
    { gate: 'materials_ready' },
    runId,
  );

  assert.equal(gate.statusCode, 200, gate.body);
  assert.equal(gate.json().pass, true);
  assert.deepEqual(gate.json().blockers, []);
  assert.match(gate.json().warnings[0], /扫描附件/u);
});

test('导入知识包用真实包内容建立资料基线，普通项目不获得豁免', async (t) => {
  const artifacts = getPackageArtifactStore();
  const imported = createProject({
    owner: 'alice', name: '导入基线门禁', kind: 'package', runtime: 'agent',
  });
  const ordinary = createProject({ owner: 'alice', name: '普通项目门禁', runtime: 'agent' });
  for (const project of [imported, ordinary]) {
    const initial = await artifacts.initializeDraftPackage(project.id, {
      id: `baseline-${project.id}`,
      name: project.name,
      description: '已有知识包内容。',
    });
    await artifacts.saveFiles(project.id, {
      expectedRevision: initial.revision.oid,
      files: {
        'package/objects/baseline.md': '# 已有对象\n\n导入包中的真实知识内容。\n',
      },
      action: {
        cause: 'package_import',
        summary: '导入已有知识包',
        actorKind: 'user',
        actorName: 'alice',
      },
    });
  }
  const app = appFor(t);
  const importedGate = await tool(
    app, imported.id, 'gate_status', { gate: 'materials_ready' }, `gate-${imported.id}`,
  );
  const ordinaryGate = await tool(
    app, ordinary.id, 'gate_status', { gate: 'materials_ready' }, `gate-${ordinary.id}`,
  );

  assert.equal(importedGate.statusCode, 200, importedGate.body);
  assert.equal(importedGate.json().pass, true);
  assert.deepEqual(importedGate.json().blockers, []);
  assert.equal(ordinaryGate.statusCode, 200, ordinaryGate.body);
  assert.equal(ordinaryGate.json().pass, false);
  assert.match(ordinaryGate.json().blockers[0], /上传/u);
});

test('资料修订影响由核心给出保持有效清单和显式传播链', async (t) => {
  const project = createProject({ owner: 'alice', name: '资料增量影响' });
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(project.id, {
    id: 'increment-impact',
    name: '资料增量影响',
    description: '验证资料修订不会推翻未受影响内容',
  });
  await artifacts.saveFiles(project.id, {
    expectedRevision: initial.revision.oid,
    files: {
      'package/rules/stock-limit.md': `---
resource: rule
key: stock-limit
title: 库存上限
---
# 库存上限

> 证据：material:库存规程.md#上限
`,
      'package/operations/replenish.md': `---
resource: operation
key: replenish
title: 补库
---
# 补库

usesRules: stock-limit
`,
      'package/objects/warehouse.md': `---
resource: object
key: warehouse
title: 仓库
---
# 仓库
`,
    },
    action: {
      cause: 'test.increment_fixture', summary: '准备增量知识', actorKind: 'system', actorName: 'test',
    },
  });
  const previous = insertDocumentRevision({
    projectId: project.id,
    filename: '库存规程.md',
    kind: 'text',
    parseStatus: 'parsed',
    textContent: '# 上限\n库存上限为 100。',
  }).document;
  const current = insertDocumentRevision({
    projectId: project.id,
    filename: '库存规程.md',
    kind: 'text',
    parseStatus: 'parsed',
    textContent: '# 上限\n库存上限调整为 120。',
  }).document;
  getDb().prepare(`INSERT INTO material_revision_impacts
    (id, project_id, source_key, previous_document_id, current_document_id,
      change_kind, semantic_changed, affected_json, created_at)
    VALUES (?,?,?,?,?,'replaced',1,?,?)`).run(
      `impact-${project.id}`,
      project.id,
      current.sourceKey,
      previous.id,
      current.id,
      JSON.stringify({
        affectedResources: [
          { kind: 'rule', key: 'stock-limit' },
          { kind: 'operation', key: 'replenish' },
        ],
      }),
      new Date().toISOString(),
    );
  const app = appFor(t);

  const response = await tool(app, project.id, 'analyze_material_impact', {
    source: current.sourceKey,
  }, `run-${project.id}`);

  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().semanticChanged, true);
  assert.equal(response.json().view.intact.n, 1);
  assert.deepEqual(response.json().view.intact.items, ['objects/warehouse.md']);
  assert.equal(response.json().view.todo.n, 2);
  assert.equal(response.json().sourceDocumentId, current.id);
  assert.ok(response.json().view.chains.every((item: any) => (
    item.srcDocumentId === current.id && item.dstPath === item.dst
  )));
  assert.equal(response.json().view.chains.find((item: any) => item.dst === 'rules/stock-limit.md').dstSub, '直接引用，需复核');
  assert.equal(response.json().view.chains.find((item: any) => item.dst === 'operations/replenish.md').dstSub, '间接连带，沿显式引用传播');

  const added = insertDocumentRevision({
    projectId: project.id,
    filename: '管理办法-v3.md',
    kind: 'text',
    parseStatus: 'parsed',
    textContent: '# 新口径\n可用量计算规则发生变化。',
  }).document;
  getDb().prepare(`INSERT INTO material_revision_impacts
    (id, project_id, source_key, previous_document_id, current_document_id,
      change_kind, semantic_changed, affected_json, created_at)
    VALUES (?,?,?,NULL,?,'added',1,?,?)`).run(
      `impact-added-${project.id}`,
      project.id,
      added.sourceKey,
      added.id,
      JSON.stringify({ scopeResolved: false, affectedResources: [] }),
      new Date(Date.now() + 1_000).toISOString(),
    );

  const unresolved = await tool(app, project.id, 'analyze_material_impact', {
    source: added.sourceKey,
  }, `run-${project.id}`);
  assert.equal(unresolved.statusCode, 200, unresolved.body);
  assert.equal(unresolved.json().scopeResolved, false);
  assert.equal(unresolved.json().view.intact.n, 0);
  assert.equal(unresolved.json().view.todo.n, 3);
  assert.equal(unresolved.json().view.scopeResolved, false);
  assert.match(unresolved.json().display.text, /尚不能判断现有内容保持有效/);
  assert.ok(unresolved.json().view.chains.every((item: any) => item.dstSub === '暂不能判断保持有效'));
  const unresolvedBatch = await tool(app, project.id, 'analyze_material_impact', {
    sources: [current.sourceKey, added.sourceKey],
  }, `run-${project.id}`);
  assert.equal(unresolvedBatch.statusCode, 200, unresolvedBatch.body);
  assert.equal(unresolvedBatch.json().scopeResolved, false);
  assert.equal(unresolvedBatch.json().view.intact.n, 0);
  assert.deepEqual(unresolvedBatch.json().view.todo.items, []);

  const resolved = await tool(app, project.id, 'analyze_material_impact', {
    source: added.sourceKey,
    direct_resources: [
      { kind: 'rule', key: 'stock-limit' },
      { kind: 'operation', key: 'replenish' },
    ],
  }, `run-${project.id}`);
  assert.equal(resolved.statusCode, 200, resolved.body);
  assert.equal(resolved.json().scopeResolved, true);
  assert.equal(resolved.json().view.scopeResolved, true);
  assert.deepEqual(resolved.json().view.intact.items, ['objects/warehouse.md']);
  assert.equal(resolved.json().view.todo.n, 2);
  assert.ok(resolved.json().view.chains.every((item: any) => item.dstSub === '直接引用，需复核'));
  assert.equal(JSON.parse((getDb().prepare(`SELECT affected_json FROM material_revision_impacts
    WHERE id = ?`).get(`impact-added-${project.id}`) as any).affected_json).scopeResolved, true);

  const combined = await tool(app, project.id, 'analyze_material_impact', {
    sources: [current.sourceKey, added.sourceKey],
  }, `run-${project.id}`);
  const expectedTodo = [...new Set([
    ...response.json().view.todo.items.map((item: any) => item.t),
    ...resolved.json().view.todo.items.map((item: any) => item.t),
  ])].sort();
  assert.equal(combined.statusCode, 200, combined.body);
  assert.equal(combined.json().scopeResolved, true);
  assert.equal(combined.json().sources.length, 2);
  assert.deepEqual(combined.json().view.todo.items.map((item: any) => item.t).sort(), expectedTodo);
  assert.equal(combined.json().view.todo.n, expectedTodo.length);
  assert.equal(combined.json().view.intact.n + combined.json().view.todo.n, 3);
  assert.equal(new Set(combined.json().view.chains.map((item: any) => (
    `${item.srcDocumentId}:${item.dstPath}`
  ))).size, combined.json().view.chains.length);
});

test('表格资料通过确定性工具完整生成数据集候选，不让模型搬运逐行正文', async (t) => {
  const project = createProject({ owner: 'alice', name: '增量表格同步' });
  const worksheet = XLSX.utils.aoa_to_sheet([
    ['yard_id', 'yard_name', 'capacity'],
    ['Y01', '渤中25-1', 100],
    ['Y02', '秦皇岛+南堡', ''],
  ]);
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, worksheet, 'crude_oil_yard');
  const buffer = Buffer.from(XLSX.write(workbook, { type: 'buffer', bookType: 'xlsx' }));
  const tables = parseExcel(buffer);
  const document = insertDocumentRevision({
    projectId: project.id,
    filename: '原油场增量.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables,
    rawContent: buffer,
  }).document;
  const app = appFor(t);
  const runId = `run-${project.id}`;

  const staged = await tool(app, project.id, 'stage_tabular_dataset', {
    source: document.filename,
  }, runId);

  assert.equal(staged.statusCode, 200, staged.body);
  assert.equal(staged.json().changed, true);
  assert.equal(staged.json().tables, 1);
  assert.equal(staged.json().rows, 2);
  assert.equal(staged.json().path, 'datasets/data.csv');
  const candidate = await tool(app, project.id, 'read_file', {
    path: 'datasets/data.csv',
  }, runId);
  assert.equal(candidate.statusCode, 200, candidate.body);
  assert.match(candidate.json().content, /^\uFEFFtable_name,row_json\n/u);
  assert.match(candidate.json().content, /"Y01"/u);
  assert.match(candidate.json().content, /"渤中25-1"/u);
  assert.match(candidate.json().content, /""capacity"":null/u);
  const diff = await tool(app, project.id, 'diff', { rev_b: 'candidate' }, runId);
  assert.equal(diff.statusCode, 200, diff.body);
  assert.equal(diff.json().files[0].path, 'datasets/data.csv');
});

test('导入基线的历史受管对象也必须通过结构化 Patch 修改', async (t) => {
  const project = createProject({ owner: 'alice', name: '历史格式增量' });
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(project.id, {
    id: 'legacy-increment', name: '历史格式增量', description: '验证增量门禁',
  });
  const legacyScript = {
    key: 'legacy_lookup', title: '历史查询脚本', description: '导入包中的旧式包装入口',
    boundOperation: 'legacy_lookup', runtime: 'python' as const, entrypoint: 'legacy_lookup.py',
    timeoutSeconds: 30, parameters: [], outputs: [], boundaries: [],
    sourceCode: 'print("legacy")\n',
  };
  const legacySpec = {
    ...initial.spec,
    version: '1.1.8',
    objects: [{ key: 'warehouse', title: '仓库', description: '历史格式对象', fields: [] }],
    operations: [{
      key: 'legacy_lookup', title: '历史查询', description: '查询历史对象',
      inputs: [], outputs: [], flow: ['调用旧式入口。'], relatedObjects: ['warehouse'],
      script: { key: legacyScript.key, required: true },
    }],
    scripts: [legacyScript],
  };
  const legacy = `---
resource: object
key: warehouse
title: 仓库
description: 历史格式对象
canonical_key: legacy-warehouse
---
# 仓库

## 业务含义

历史格式对象。
`;
  const importedFiles = renderPackage(legacySpec, { validateScripts: false });
  importedFiles['objects/warehouse.md'] = legacy;
  await artifacts.importPackageVersion(project.id, {
    expectedRevision: initial.revision.oid,
    spec: legacySpec,
    coverage: { covered: [], uncovered: [] },
    files: importedFiles,
    action: { cause: 'test.legacy_import', summary: '导入历史格式基线', actorKind: 'system', actorName: 'test' },
  });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const written = await tool(app, project.id, 'write_file', {
    path: 'objects/warehouse.md',
    content: legacy.replace('历史格式对象。', '历史格式对象，容量口径已更新。'),
    change_group: 'increment_object',
    evidence: ['user-decision:legacy-increment'],
  }, runId);
  assert.equal(written.statusCode, 409, written.body);
  assert.equal(written.json().code, 'MANAGED_FILE_REQUIRES_DOMAIN_TOOL');
});

test('来源知识包先缩范围和阻断引用断裂，裁决后只写候选并保留 provenance', async (t) => {
  const project = createProject({ owner: 'alice', name: '知识包融合' });
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(project.id, {
    id: 'merge-target', name: '知识包融合', description: '融合目标包',
  });
  const current = await artifacts.saveFiles(project.id, {
    expectedRevision: initial.revision.oid,
    files: {
      'package/objects/shared.md': `---
resource: object
key: shared
title: 当前共享对象
---
# 业务定义

当前定义。
`,
    },
    action: { cause: 'test.merge_target', summary: '准备融合目标', actorKind: 'system', actorName: 'test' },
  });
  const sourceSpec: any = {
    id: 'merge-source', name: '来源盘点包', description: '只读来源包', version: '0.9.0',
    language: 'zh-CN', status: 'released', defaultProviderAlias: 'data',
    objects: [{
      key: 'shared', title: '来源共享对象', description: '来源定义', fields: [], aliases: ['共享项'],
    }],
    operations: [{
      key: 'inspect', title: '盘点', description: '执行盘点', inputs: [], outputs: [],
      flow: ['读取共享对象'], relatedObjects: ['shared'],
    }],
    rules: [], metrics: [], scripts: [],
  };
  const source = storePackageMergeSource(getDb(), {
    projectId: project.id,
    filename: '来源盘点包-0.9.0.zip',
    uploadedBy: 'alice',
    archive: {
      spec: sourceSpec,
      coverage: { covered: [], uncovered: [] },
      files: renderPackage(sourceSpec),
      rawFiles: {},
      warnings: [],
    },
  });
  const app = appFor(t);
  const runId = `run-${project.id}`;

  const broken = await tool(app, project.id, 'analyze_package_merge', {
    source_id: source.id,
    scopes: ['operations'],
  }, runId);
  assert.equal(broken.statusCode, 200, broken.body);
  assert.match(broken.json().validation.blocker, /缺少对象 shared/u);
  assert.equal(broken.json().scope.find((item: any) => item.k === 'objects').selected, false);

  const analyzed = await tool(app, project.id, 'analyze_package_merge', {
    source_id: source.id,
    scopes: ['objects', 'operations'],
  }, runId);
  assert.equal(analyzed.statusCode, 200, analyzed.body);
  assert.equal(analyzed.json().validation.blocker, '');
  const collision = analyzed.json().conflicts.find((item: any) => item.sourcePath === 'objects/shared.md');
  assert.ok(collision);

  const unresolved = await tool(app, project.id, 'stage_package_merge', {
    source_id: source.id,
    scopes: ['objects', 'operations'],
    resolutions: [],
  }, runId);
  assert.equal(unresolved.statusCode, 409, unresolved.body);
  assert.equal(unresolved.json().code, 'MERGE_CONFLICT_UNRESOLVED');

  const staged = await tool(app, project.id, 'stage_package_merge', {
    source_id: source.id,
    scopes: ['objects', 'operations'],
    resolutions: analyzed.json().conflicts.map((item: any) => ({
      conflict_id: item.id,
      action: 'use_source',
    })),
  }, runId);
  assert.equal(staged.statusCode, 200, staged.body);
  assert.ok(staged.json().staged.some((item: any) => item.path === 'objects/shared.md'));
  assert.match(staged.json().provenance, /^pkg:来源盘点包@0\.9\.0$/u);
  assert.equal((await artifacts.current(project.id))?.revision.oid, current.revision.oid);
  const candidate = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/agent/diff` });
  const mergedObject = candidate.json().files.find((item: any) => item.path === 'objects/shared.md');
  assert.match(mergedObject.after, /provenance: pkg:来源盘点包@0\.9\.0/u);
  assert.match(mergedObject.after, /来源共享对象/u);
});

test('候选写入只接受能命中项目资料的 material 证据', async (t) => {
  const project = createProject({ owner: 'alice', name: '资料证据定位' });
  const inserted = insertDocumentRevision({
    projectId: project.id,
    filename: '150万吨2#加氢裂化装置工艺技术规程.docx',
    kind: 'docx',
    parseStatus: 'parsed',
    textContent: '',
  });
  const app = appFor(t);

  const invalid = await tool(app, project.id, 'write_file', {
    path: '.modeling/goal.md',
    content: '# 建模目标\n',
    change_group: 'goal',
    evidence: ['material:150万吨2#加氢裂化装置工艺技术规程.docx#工艺指标'],
  }, `run-${project.id}`);
  assert.equal(invalid.statusCode, 400, invalid.body);
  assert.equal(invalid.json().code, 'EVIDENCE_NOT_FOUND');

  const valid = await tool(app, project.id, 'write_file', {
    path: '.modeling/goal.md',
    content: '# 建模目标\n',
    change_group: 'goal',
    evidence: [`material:${inserted.document.id}#工艺指标`],
  }, `run-${project.id}`);
  assert.equal(valid.statusCode, 200, valid.body);
});

test('受管操作文件不能用 write_file 绕过领域编译器', async (t) => {
  const project = createProject({ owner: 'alice', name: '流程前置门禁' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);

  const blocked = await tool(app, project.id, 'write_file', {
    path: 'operations/create-order.md',
    content: '# 创建采购单\n',
    change_group: 'operation',
    evidence: [],
  }, runId);
  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().code, 'MANAGED_FILE_REQUIRES_DOMAIN_TOOL');

  const diagram = await tool(app, project.id, 'write_file', {
    path: 'diagrams/procurement.mmd',
    content: 'flowchart TD\nA[提交申请] --> B[创建采购单]\n',
    change_group: 'process',
    evidence: ['user-decision:procurement-flow'],
  }, runId);
  assert.equal(diagram.statusCode, 200, diagram.body);
  const processPreflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认采购流程',
    change_groups: [{ id: 'process', tier: 'caliber', title: '采购流程' }],
  }, runId);
  assert.equal(processPreflight.statusCode, 200, processPreflight.body);
  const confirmed = await tool(app, project.id, 'request_confirmation', {
    summary: '确认采购流程',
    change_groups: [{ id: 'process', tier: 'caliber', title: '采购流程' }],
    ...confirmationReceipt(processPreflight),
    review: { approved: true, review_mode: 'full', selected_groups: ['process'] },
  }, runId);
  assert.equal(confirmed.statusCode, 200, confirmed.body);

  const allowed = await tool(app, project.id, 'write_file', {
    path: 'operations/create-order.md',
    content: `---
resource: operation
key: create-order
title: 创建采购单
description: 根据已确认流程创建采购单。
evidence:
  - user-decision:procurement-flow
---
# 业务目标

创建采购单。

# 输入

| 参数 key | 中文名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |

# 输出

| 输出 key | 中文名 | 类型 | 说明 |
| --- | --- | --- | --- |

# 流程

1. 创建采购单。

# 规则、指标和注意事项

- 仅验证流程门禁。

# 知识证据

- user-decision:procurement-flow
`,
    change_group: 'operation',
    evidence: ['diagrams/procurement.mmd'],
  }, runId);
  assert.equal(allowed.statusCode, 409, allowed.body);
  assert.equal(allowed.json().code, 'MANAGED_FILE_REQUIRES_DOMAIN_TOOL');
});

test('知识条目拒绝写入 consumer 不读取的通用目录', async (t) => {
  const project = createProject({ owner: 'alice', name: '知识目录契约' });
  const app = appFor(t);

  const response = await tool(app, project.id, 'write_file', {
    path: 'knowledge/hydrocracking.md',
    content: '# 加氢裂化知识\n',
    change_group: 'knowledge',
    evidence: [],
  }, `run-${project.id}`);

  assert.equal(response.statusCode, 400, response.body);
  assert.equal(response.json().code, 'INVALID_KNOWLEDGE_PATH');
  assert.match(response.json().next[0], /operations\//);
});

test('Mermaid 候选必须写入 diagrams 目录', async (t) => {
  const project = createProject({ owner: 'alice', name: '流程图目录契约' });
  const app = appFor(t);

  const response = await tool(app, project.id, 'write_file', {
    path: 'operations/process.mmd',
    content: 'flowchart TD\nA[开始] --> B[完成]\n',
    change_group: 'process',
    evidence: [],
  }, `run-${project.id}`);

  assert.equal(response.statusCode, 400, response.body);
  assert.equal(response.json().code, 'INVALID_DIAGRAM_PATH');
  assert.match(response.json().next[0], /^将文件路径调整为 diagrams\//u);
});

test('Agent 保存数据 Schema 用途时返回真实 Sheet 与字段事实', async (t) => {
  const project = createProject({ owner: 'alice', name: '材料用途契约' });
  const app = appFor(t);
  const inserted = insertDocumentRevision({
    projectId: project.id, filename: '设备台账.xlsx', kind: 'excel', parseStatus: 'parsed',
    tables: [{
      name: '设备', rowCount: 2, fields: [{
        name: '设备编码', sourceColumn: 'A', inferredType: 'string', sampleValues: ['P-101', 'P-102'], distinctRatio: 1, nonEmptyRatio: 1,
      }, {
        name: '备注', sourceColumn: 'B', inferredType: 'string', sampleValues: ['仅一行'], distinctRatio: 1, nonEmptyRatio: 0.5,
      }],
    }],
  }).document;

  const response = await tool(app, project.id, 'set_material_directive', {
    filename: inserted.filename, purpose: 'schema', reading_mode: 'on_demand',
    sheet_purposes: [{
      sheet: '设备', semantic_role: 'schema', import_disposition: 'object',
      reason: '每行对应一个合成设备对象',
    }],
  }, `run-${project.id}`);

  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().document.purpose, 'schema');
  assert.equal(response.json().purposeReview.status, 'pending');
  assert.equal(response.json().purposeReview.suggestedBy, 'agent');
  assert.equal(response.json().requiresUserConfirmation, true);
  assert.equal(response.json().schema[0].table, '设备');
  assert.equal(response.json().schema[0].fields[0].name, '设备编码');
  assert.equal(response.json().schema[0].fields[0].primaryKeyCandidate, true);
  assert.equal(response.json().schema[0].fields[0].typeInferred, true);
  assert.equal(response.json().schema[0].fields[1].primaryKeyCandidate, false);
  assert.deepEqual(response.json().objects[0].source, {
    documentId: inserted.id, filename: inserted.filename, sheet: '设备',
  });
  assert.deepEqual(response.json().objects[0].attributes.map((field: any) => field.name), ['设备编码', '备注']);
});

test('Agent 为普通文档预判用途后等待用户确认', async (t) => {
  const project = createProject({ owner: 'alice', name: '普通资料用途契约' });
  const app = appFor(t);
  const inserted = insertDocumentRevision({
    projectId: project.id,
    filename: '业务目标.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '# 目标\n\n建立设备检修知识包。',
  }).document;

  const response = await tool(app, project.id, 'set_material_directive', {
    filename: inserted.filename,
    purpose: 'goal',
    reading_mode: 'on_demand',
  }, `run-${project.id}`);

  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().document.purpose, 'goal');
  assert.equal(response.json().purposeReview.status, 'pending');
  assert.equal(response.json().purposeReview.suggestedBy, 'agent');
  assert.equal(response.json().requiresUserConfirmation, true);
  assert.deepEqual(response.json().purposeReview.sheetSuggestions, []);

  const ambiguous = await tool(app, project.id, 'set_material_directive', {
    filename: inserted.filename,
    purpose: 'auto',
    reading_mode: 'on_demand',
  }, `run-${project.id}`);
  assert.equal(ambiguous.statusCode, 400, ambiguous.body);
  assert.equal(ambiguous.json().code, 'MATERIAL_PURPOSE_REQUIRED');
  assert.equal(getDocument(inserted.id)?.purpose, 'goal');

  const invalid = await tool(app, project.id, 'set_material_directive', {
    filename: inserted.filename,
    purpose: 'schema',
    reading_mode: 'on_demand',
  }, `run-${project.id}`);
  assert.equal(invalid.statusCode, 400, invalid.body);
  assert.match(invalid.json().reason, /可识别数据表/);
  assert.equal(getDocument(inserted.id)?.purpose, 'goal');
});

test('实体 Sheet 尚未映射时允许确认目标、流程和非知识资源候选', async (t) => {
  const project = createProject({ owner: 'alice', name: '流程与映射门禁顺序', runtime: 'agent' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: 'process-before-mapping-package',
    name: '流程与映射门禁顺序',
    description: '验证对象映射不会阻断流程确认。',
  });
  const material = insertDocumentRevision({
    projectId: project.id,
    filename: 'synthetic-entities.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: [{
      name: 'entities',
      rowCount: 1,
      fields: [{ name: 'entity_id', inferredType: 'string', sampleValues: ['E-1'], distinctRatio: 1 }],
      rows: [['E-1']],
    }],
  }).document;
  const reviews = new MaterialPurposeReviewStore(getDb());
  reviews.ensure(material);
  const suggestion = reviews.suggest(material, {
    purpose: 'schema',
    sheets: [{ sheet: 'entities', semanticRole: 'schema', importDisposition: 'object' }],
    suggestedBy: 'agent',
  });
  reviews.confirm(material, {
    purpose: 'schema',
    sheets: suggestion.sheetSuggestions,
    confirmedBy: 'alice',
    expectedDirectiveRevision: 0,
  });

  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);

  const plan = await tool(app, project.id, 'get_schema_mapping_plan', {}, runId);
  assert.equal(plan.statusCode, 200, plan.body);
  assert.equal(plan.json().complete, false);
  assert.equal(plan.json().objectSheetCount, 1);
  assert.equal(plan.json().mappedObjectSheetCount, 0);
  assert.equal(plan.json().unresolvedObjectSheetCount, 1);
  assert.equal(plan.json().sheets[0].mappingStatus, 'unmapped');
  assert.equal(plan.json().problems[0].code, 'SCHEMA_SHEET_OBJECT_MAPPING_MISSING');

  const current = await getPackageArtifactStore().readPackageFiles(project.id);
  const manifestGroup = { id: 'manifest-metadata', tier: 'index', title: '同步知识包说明' };
  const manifest = await tool(app, project.id, 'write_file', {
    path: 'manifest.yaml',
    content: current.files['manifest.yaml'].replace(
      /^description:.*$/mu,
      'description: 合成项目的知识包说明',
    ),
    change_group: manifestGroup.id,
    evidence: [],
  }, runId);
  assert.equal(manifest.statusCode, 200, manifest.body);
  const manifestPreflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: manifestGroup.title,
    change_groups: [manifestGroup],
  }, runId);
  assert.equal(manifestPreflight.statusCode, 200, manifestPreflight.body);
});

test('增量操作候选不被正式基线已有的 Sheet 映射缺口阻断', async (t) => {
  const project = createProject({ owner: 'alice', name: '增量基线映射差分', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(project.id, {
    id: 'increment-baseline-mapping-diff',
    name: '增量基线映射差分',
    description: '验证无关的历史映射问题不会阻断操作增量。',
  });
  const baseline = await artifacts.savePackageVersion(project.id, {
    expectedRevision: initial.revision.oid,
    spec: {
      ...initial.spec,
      version: '1.0.0',
      objects: [{
        key: 'record',
        title: '记录',
        description: '已有的虚拟记录对象。',
        fields: [{ key: 'id', type: 'string', required: true }],
      }],
    },
    coverage: { covered: [], uncovered: [] },
    action: {
      cause: 'test_setup', summary: '建立带历史映射缺口的正式基线',
      actorKind: 'user', actorName: 'alice',
    },
  });
  const material = insertDocumentRevision({
    projectId: project.id,
    filename: 'baseline-schema.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: [{
      name: 'records',
      rowCount: 1,
      fields: [{ name: 'record_id', inferredType: 'string', sampleValues: ['R-1'], distinctRatio: 1 }],
      rows: [['R-1']],
    }],
  }).document;
  const reviews = new MaterialPurposeReviewStore(getDb());
  const suggestion = reviews.suggest(material, {
    purpose: 'schema',
    sheets: [{ sheet: 'records', semanticRole: 'schema', importDisposition: 'object' }],
    suggestedBy: 'agent',
  });
  reviews.confirm(material, {
    purpose: 'schema',
    sheets: suggestion.sheetSuggestions,
    confirmedBy: 'alice',
    expectedDirectiveRevision: 0,
  });
  assert.equal(findMaterialPurposeReviewProblems(getDb(), project.id, baseline.spec).length, 1);

  const app = appFor(t);
  const runId = `run-${project.id}`;
  const group = { id: 'increment-operation', tier: 'structural', title: '补充记录查询操作' };
  const operation = await tool(app, project.id, 'upsert_operation', {
    key: 'read_record',
    title: '读取记录',
    description: '根据记录编号读取已有记录。',
    inputs: [{ param: 'id', type: 'string', required: true }],
    outputs: [{ field: 'id', type: 'string' }],
    flow: ['根据 id 查询 object:record'],
    related_objects: ['record'],
    uses_rules: [],
    uses_metrics: [],
    evidence: ['user-decision:increment-operation'],
    change_group: group.id,
  }, runId);
  assert.equal(operation.statusCode, 200, operation.body);

  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: group.title,
    change_groups: [group],
  }, runId);
  assert.equal(preflight.statusCode, 200, preflight.body);
  const payload = preflight.json();
  assert.equal(payload.validation_summary.baseline_finding_count, 1);
  assert.equal(payload.validation_summary.baseline_warning_count, 1);
  assert.equal(payload.validation_summary.introduced_finding_count, 0);
  const changedPaths = getDb().prepare(`SELECT path FROM agent_candidate_changes
    WHERE project_id = ? AND run_id = ? ORDER BY path`).all(project.id, runId) as Array<{ path: string }>;
  assert.equal(changedPaths.some((item) => item.path === 'mappings/object-data-mappings.json'), false);
});

test('没有实体 Sheet 时数据映射台账视为完整', async (t) => {
  const project = createProject({ owner: 'alice', name: '无实体表映射台账', runtime: 'agent' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: 'no-object-sheet-package',
    name: '无实体表映射台账',
    description: '验证没有实体表时无需补齐对象来源映射。',
  });

  const app = appFor(t);
  const plan = await tool(app, project.id, 'get_schema_mapping_plan', {}, `run-${project.id}`);
  assert.equal(plan.statusCode, 200, plan.body);
  assert.equal(plan.json().complete, true);
  assert.equal(plan.json().sheetCount, 0);
  assert.equal(plan.json().objectSheetCount, 0);
  assert.equal(plan.json().unresolvedObjectSheetCount, 0);
});

test('逐 Sheet 用途确认与对象来源映射共同阻断不完整知识候选', async (t) => {
  const project = createProject({ owner: 'alice', name: '逐表用途候选门禁', runtime: 'agent' });
  const artifacts = getPackageArtifactStore();
  await artifacts.initializeDraftPackage(project.id, {
    id: 'sheet-purpose-gate-package',
    name: '逐表用途候选门禁',
    description: '验证合成工作簿的用途确认和对象来源绑定。',
  });
  const material = insertDocumentRevision({
    projectId: project.id,
    filename: 'synthetic-records.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: [{
      name: 'records',
      rowCount: 1,
      fields: [
        { name: 'record_id', inferredType: 'string', sampleValues: ['R-1'], distinctRatio: 1 },
        { name: 'record_name', inferredType: 'string', sampleValues: ['合成记录'], distinctRatio: 1 },
      ],
      rows: [['R-1', '合成记录']],
    }, {
      name: 'notes',
      rowCount: 1,
      fields: [{ name: 'note', inferredType: 'string', sampleValues: ['合成说明'], distinctRatio: 1 }],
      rows: [['合成说明']],
    }],
  }).document;
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);

  const group = { id: 'record-object', tier: 'structural', title: '合成记录对象' };
  const unbound = await tool(app, project.id, 'upsert_object', {
    key: 'record',
    title: '合成记录',
    description: '用于验证逐表确认门禁。',
    fields: [
      { key: 'id', cn_name: '记录编号', type: 'string', required: true },
      { key: 'name', cn_name: '记录名称', type: 'string', required: true },
    ],
    relations: [],
    evidence: [`material:${material.id}`],
    change_group: group.id,
  }, runId);
  assert.equal(unbound.statusCode, 200, unbound.body);

  const reviews = new MaterialPurposeReviewStore(getDb());
  reviews.ensure(material);
  const pending = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: group.title,
    change_groups: [group],
  }, runId);
  assert.equal(pending.statusCode, 409, pending.body);
  assert.equal(pending.json().code, 'MATERIAL_PURPOSE_REVIEW_REQUIRED');

  const suggestion = reviews.suggest(material, {
    purpose: 'schema',
    sheets: [
      { sheet: 'records', semanticRole: 'schema', importDisposition: 'object' },
      { sheet: 'notes', semanticRole: 'evidence', importDisposition: 'reference' },
    ],
    suggestedBy: 'agent',
  });
  reviews.confirm(material, {
    purpose: 'schema',
    sheets: suggestion.sheetSuggestions,
    confirmedBy: 'alice',
    expectedDirectiveRevision: 0,
  });
  const missingMapping = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: group.title,
    change_groups: [group],
  }, runId);
  assert.equal(missingMapping.statusCode, 409, missingMapping.body);
  assert.equal(missingMapping.json().code, 'SCHEMA_SHEET_MAPPING_INCOMPLETE');
  assert.equal(
    missingMapping.json().reason,
    '数据 Schema 与对象映射不一致：还有 1 张 Sheet 的结构化对象来源尚未闭环',
  );
  assert.equal(missingMapping.json().problems[0].sheet, 'records');
  assert.equal(missingMapping.json().retryable, false);
  assert.equal(missingMapping.json().findingCount, 1);
  const incompletePlan = await tool(app, project.id, 'get_schema_mapping_plan', {}, runId);
  assert.equal(incompletePlan.statusCode, 200, incompletePlan.body);
  assert.equal(incompletePlan.json().complete, false);
  assert.deepEqual(incompletePlan.json().sheets[0].mappedObjects, []);

  const listed = await tool(app, project.id, 'list_domain_resources', { kind: 'object' }, runId);
  const planResponse = await tool(app, project.id, 'create_change_plan', {
    goal: '按已确认的逐 Sheet 用途同步物理对象',
    mode: 'generation',
    expected_revision: listed.json().working_revision,
    tasks: [{
      client_key: 'records-object',
      title: '同步记录对象',
      resource_kind: 'object',
      target_key: 'records',
      allowed_tools: ['create_object'],
      acceptance: '完整同步 records Sheet 的字段与来源映射',
    }],
  }, runId);
  assert.equal(planResponse.statusCode, 200, planResponse.body);
  const plan = planResponse.json().plan;
  assert.deepEqual(plan.tasks[0].allowed_tools, [
    'sync_schema_objects', 'stage_schema_semantic_changes', 'confirm_schema_semantics',
  ]);
  const approved = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/change-plans/${plan.id}/respond`,
    payload: { approved: true },
  });
  assert.equal(approved.statusCode, 200, approved.body);
  const synced = await tool(app, project.id, 'sync_schema_objects', {
    plan_task_id: plan.tasks[0].id,
    expected_revision: listed.json().working_revision,
    mutation_id: 'sheet-purpose-schema-sync',
    reason: '按用户确认的实体 Sheet 确定性同步',
  }, runId);
  assert.equal(synced.statusCode, 200, synced.body);
  assert.equal(synced.json().object_count, 1);
  const completePlan = await tool(app, project.id, 'get_schema_mapping_plan', {}, runId);
  assert.equal(completePlan.statusCode, 200, completePlan.body);
  assert.equal(completePlan.json().complete, true);
  assert.deepEqual(completePlan.json().sheets[0].mappedObjects, ['records']);
  const relationReview = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/object-relations/review`,
  });
  assert.equal(relationReview.statusCode, 200, relationReview.body);
  assert.equal(relationReview.json().ready, true);
  assert.deepEqual(relationReview.json().unresolved, []);
  const appliedRelations = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/object-relations/review`,
    payload: {
      review_id: relationReview.json().review_id,
      expected_revision: relationReview.json().working_revision,
      selected_fingerprints: [],
    },
  });
  assert.equal(appliedRelations.statusCode, 200, appliedRelations.body);
  const ready = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '按已确认用途同步 records 对象',
    change_groups: [{ id: 'schema_objects', tier: 'structural', title: 'Schema 对象同步' }],
  }, runId);
  assert.equal(ready.statusCode, 422, ready.body);
  assert.equal(ready.json().code, 'CANDIDATE_SCHEMA_SEMANTICS_INVALID');
});

test('对象领域工具原子生成跨 Sheet 主表与扩展表映射', async (t) => {
  const project = createProject({ owner: 'alice', name: '跨表对象合成验证', runtime: 'agent' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: 'multi-source-object-package',
    name: '跨表对象合成验证',
    description: '验证主表和一对一扩展表的结构化绑定。',
  });
  const material = insertDocumentRevision({
    projectId: project.id,
    filename: 'synthetic-entity.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: [{
      name: 'EntityBase', rowCount: 1,
      fields: [
        { name: 'base_id', inferredType: 'string', sampleValues: ['E-1'], distinctRatio: 1 },
        { name: 'base_name', inferredType: 'string', sampleValues: ['合成实体'], distinctRatio: 1 },
      ],
      rows: [['E-1', '合成实体']],
    }, {
      name: 'EntityExtension', rowCount: 1,
      fields: [
        { name: 'extension_id', inferredType: 'string', sampleValues: ['E-1'], distinctRatio: 1 },
        { name: 'detail_text', inferredType: 'string', sampleValues: ['合成说明'], distinctRatio: 1 },
      ],
      rows: [['E-1', '合成说明']],
    }],
  }).document;
  const app = appFor(t);
  const selectedSource = await app.inject({
    method: 'PUT',
    url: `/api/projects/${project.id}/agent/package-data-source`,
    payload: { dataSourceType: 'neo4j' },
  });
  assert.equal(selectedSource.statusCode, 200, selectedSource.body);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);
  const sourcePlan = {
    primary_key: ['entity_id'],
    sources: [{
      document_id: material.id,
      table: 'EntityBase',
      role: 'primary',
      field_bindings: [
        { field_key: 'entity_id', source_column: 'base_id' },
        { field_key: 'name', source_column: 'base_name' },
      ],
    }, {
      document_id: material.id,
      table: 'EntityExtension',
      role: 'extension',
      field_bindings: [
        { field_key: 'entity_id', source_column: 'extension_id' },
        { field_key: 'detail', source_column: 'detail_text' },
      ],
    }],
  };
  const created = await tool(app, project.id, 'upsert_object', {
    key: 'synthetic_entity',
    title: '合成实体',
    description: '由主表和扩展表共同描述的合成实体。',
    fields: [
      { key: 'entity_id', cn_name: '实体编号', type: 'string', required: true },
      { key: 'name', cn_name: '实体名称', type: 'string', required: true },
      { key: 'detail', cn_name: '补充说明', type: 'string', required: false },
    ],
    relations: [],
    source_plan: sourcePlan,
    evidence: [`material:${material.id}`],
    change_group: 'multi-source-object',
  }, runId);
  assert.equal(created.statusCode, 200, created.body);

  const rebound = await tool(app, project.id, 'bind_object_sources', {
    key: 'synthetic_entity',
    primary_key: sourcePlan.primary_key,
    sources: sourcePlan.sources,
    evidence: [`material:${material.id}`],
    change_group: 'multi-source-object',
  }, runId);
  assert.equal(rebound.statusCode, 200, rebound.body);
  assert.equal(rebound.json().sources.length, 2);
  const mappingFile = await tool(app, project.id, 'read_file', {
    path: 'mappings/object-data-mappings.json',
  }, runId);
  assert.equal(mappingFile.statusCode, 200, mappingFile.body);
  const mapping = JSON.parse(mappingFile.json().content).objects.synthetic_entity;
  assert.deepEqual(mapping.sources.map((source: any) => source.role), ['primary', 'extension']);
  assert.equal(mapping.sources[1].fields.detail.source_column, 'detail_text');
});

test('增量领域变更同步根索引的 Runtime 能力', async (t) => {
  const project = createProject({ owner: 'alice', name: '根索引能力同步', runtime: 'agent' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: 'root-index-runtime-package',
    name: '根索引能力同步',
    description: '验证对象绑定数据表后根索引的 Runtime 能力随之更新。',
  });
  const material = insertDocumentRevision({
    projectId: project.id,
    filename: 'synthetic-runtime.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: [{
      name: 'RuntimeBase', rowCount: 1,
      fields: [
        { name: 'base_id', inferredType: 'string', sampleValues: ['R-1'], distinctRatio: 1 },
        { name: 'base_name', inferredType: 'string', sampleValues: ['合成记录'], distinctRatio: 1 },
      ],
      rows: [['R-1', '合成记录']],
    }],
  }).document;
  const reviews = new MaterialPurposeReviewStore(getDb());
  reviews.ensure(material);
  const suggestion = reviews.suggest(material, {
    purpose: 'schema',
    sheets: [{ sheet: 'RuntimeBase', semanticRole: 'schema', importDisposition: 'object' }],
    suggestedBy: 'agent',
  });
  reviews.confirm(material, {
    purpose: 'schema',
    sheets: suggestion.sheetSuggestions,
    confirmedBy: 'alice',
    expectedDirectiveRevision: 0,
  });
  const app = appFor(t);
  const selectedSource = await app.inject({
    method: 'PUT',
    url: `/api/projects/${project.id}/agent/package-data-source`,
    payload: { dataSourceType: 'neo4j' },
  });
  assert.equal(selectedSource.statusCode, 200, selectedSource.body);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);

  const draftIndex = await tool(app, project.id, 'read_file', { path: 'index.md' }, runId);
  assert.equal(draftIndex.statusCode, 200, draftIndex.body);
  assert.match(draftIndex.json().content, /\| 业务数据查询 \| disabled \|/u);

  const listed = await tool(app, project.id, 'list_domain_resources', { kind: 'object' }, runId);
  const planResponse = await tool(app, project.id, 'create_change_plan', {
    goal: '按已确认的 Sheet 用途同步物理对象',
    mode: 'generation',
    expected_revision: listed.json().working_revision,
    tasks: [{
      client_key: 'runtime-base-object',
      title: '同步合成记录对象',
      resource_kind: 'object',
      target_key: 'RuntimeBase',
      allowed_tools: ['create_object'],
      acceptance: '完整同步 RuntimeBase Sheet 的字段与来源映射',
    }],
  }, runId);
  assert.equal(planResponse.statusCode, 200, planResponse.body);
  const plan = planResponse.json().plan;
  const approved = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/change-plans/${plan.id}/respond`,
    payload: { approved: true },
  });
  assert.equal(approved.statusCode, 200, approved.body);
  const synced = await tool(app, project.id, 'sync_schema_objects', {
    plan_task_id: plan.tasks[0].id,
    expected_revision: listed.json().working_revision,
    mutation_id: 'root-index-schema-sync',
    reason: '按用户确认的 Sheet 用途确定性同步',
  }, runId);
  assert.equal(synced.statusCode, 200, synced.body);

  const updatedIndex = await tool(app, project.id, 'read_file', { path: 'index.md' }, runId);
  assert.equal(updatedIndex.statusCode, 200, updatedIndex.body);
  const indexContent = updatedIndex.json().content;
  assert.match(indexContent, /\| 业务数据查询 \| enabled \| 通过 mappings\/object-data-mappings\.json/u);
  // 草稿模板在目录里列过流程图，重渲染根索引不得把这一行删掉
  assert.ok(indexContent.includes('- 流程图目录：diagrams/index.md'));

  const indexChange = getDb().prepare(`SELECT group_id FROM agent_candidate_changes
    WHERE project_id = ? AND run_id = ? AND path = 'index.md'`)
    .get(project.id, runId) as { group_id: string } | undefined;
  assert.equal(indexChange?.group_id, 'compiled_resources');
});

test('同名 Sheet 必须用资料 ID 稳定寻址', async (t) => {
  const project = createProject({ owner: 'alice', name: '同名 Sheet 寻址' });
  const app = appFor(t);
  const first = insertDocumentRevision({
    projectId: project.id, filename: '一号台账.xlsx', kind: 'excel', parseStatus: 'parsed',
    tables: [{ name: 'Sheet1', rowCount: 1, fields: [{
      name: '一号字段', inferredType: 'string', sampleValues: ['A'], distinctRatio: 1, nonEmptyRatio: 1,
    }], rows: [['A']] }],
  }).document;
  insertDocumentRevision({
    projectId: project.id, filename: '二号台账.xlsx', kind: 'excel', parseStatus: 'parsed',
    tables: [{ name: 'Sheet1', rowCount: 1, fields: [{
      name: '二号字段', inferredType: 'string', sampleValues: ['B'], distinctRatio: 1, nonEmptyRatio: 1,
    }], rows: [['B']] }],
  });

  const ambiguous = await tool(app, project.id, 'describe_table', { table: 'Sheet1' }, `run-${project.id}`);
  assert.equal(ambiguous.statusCode, 409, ambiguous.body);
  assert.equal(ambiguous.json().code, 'TABLE_AMBIGUOUS');

  const exact = await tool(app, project.id, 'describe_table', {
    document_id: first.id, table: 'Sheet1',
  }, `run-${project.id}`);
  assert.equal(exact.statusCode, 200, exact.body);
  assert.equal(exact.json().table.fields[0].name, '一号字段');
});

test('Schema 结构摘要一次返回字段统计且不搬运业务行', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Schema 结构摘要' });
  const app = appFor(t);
  insertDocumentRevision({
    projectId: project.id, filename: '合成结构.xlsx', kind: 'excel', parseStatus: 'parsed',
    tables: [{ name: '设备', rowCount: 10_000, fields: [
      {
        name: '设备编码', sourceColumn: 'A', inferredType: 'string', sampleValues: ['P-1', 'P-2', 'P-3', 'P-4'],
        distinctRatio: 1, nonEmptyRatio: 0.99, note: '唯一设备编号', primaryKey: true,
      },
      { name: '温度', sourceColumn: 'B', inferredType: 'number', sampleValues: ['350', '351'], distinctRatio: 0.2, nonEmptyRatio: 0.8 },
    ], rows: Array.from({ length: 10_000 }, (_, index) => [`P-${index + 1}`, String(350 + index % 2)]) }] as any,
  });

  const response = await tool(app, project.id, 'get_schema_overview', {}, `run-schema-overview-${project.id}`);
  assert.equal(response.statusCode, 200, response.body);
  const payload = response.json();
  assert.equal(payload.tableCount, 1);
  assert.equal(payload.tables[0].rowCount, 10_000);
  assert.equal(payload.tables[0].fields[0].nonEmptyRatio, 0.99);
  assert.equal(payload.tables[0].fields[0].primaryKey, true);
  assert.equal(payload.tables[0].fields[0].sampleValues.length, 3);
  assert.equal('rows' in payload.tables[0], false);
  assert.ok(JSON.stringify(payload).length < 2_000);
});

test('Schema 结构摘要命中持久化缓存并在资料版本变化后失效', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Schema 缓存验收' });
  const app = appFor(t);
  const first = insertDocumentRevision({
    projectId: project.id, filename: '缓存表.xlsx', kind: 'excel', parseStatus: 'parsed',
    tables: [{ name: '设备', rowCount: 2, fields: [{ name: 'id', inferredType: 'string', sampleValues: ['1'], distinctRatio: 1, nonEmptyRatio: 1 }], rows: [['1'], ['2']] }],
  }).document;
  const runId = `run-cache-${project.id}`;
  const cold = await tool(app, project.id, 'get_schema_overview', {}, runId);
  assert.equal(cold.statusCode, 200, cold.body);
  assert.equal(cold.json().cacheHits, 0);
  const hot = await tool(app, project.id, 'get_schema_overview', {}, runId);
  assert.equal(hot.statusCode, 200, hot.body);
  assert.equal(hot.json().cacheHits, 1);
  assert.equal(hot.json().tables[0].cacheHit, true);
  const second = insertDocumentRevision({
    projectId: project.id, filename: '缓存表.xlsx', kind: 'excel', parseStatus: 'parsed',
    tables: [{ name: '设备', rowCount: 3, fields: [{ name: 'id', inferredType: 'string', sampleValues: ['1'], distinctRatio: 1, nonEmptyRatio: 1 }], rows: [['1'], ['2'], ['3']] }],
  }).document;
  assert.notEqual(second.contentHash, first.contentHash);
  const changed = await tool(app, project.id, 'get_schema_overview', {}, runId);
  assert.equal(changed.statusCode, 200, changed.body);
  assert.equal(changed.json().cacheHits, 0);
  assert.equal(changed.json().tables[0].rowCount, 3);
});

test('表格全文精读从原文件覆盖超过普通解析上限的全部行', async (t) => {
  const project = createProject({ owner: 'alice', name: '表格全文精读覆盖' });
  const app = appFor(t);
  const workbook = XLSX.utils.book_new();
  const rows = [
    ['设备编码', '设备名称'],
    ...Array.from({ length: 2_105 }, (_, index) => [`P-${index + 1}`, `设备 ${index + 1}`]),
  ];
  XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet(rows), '设备');
  const rawContent = Buffer.from(XLSX.write(workbook, { type: 'buffer', bookType: 'xlsx' }));
  const ordinaryTables = parseExcel(rawContent);
  assert.equal(ordinaryTables[0].rowCount, 2_105);
  assert.equal(ordinaryTables[0].rows?.length, 2_000);
  const document = insertDocumentRevision({
    projectId: project.id,
    filename: '完整设备台账.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    tables: ordinaryTables,
    rawContent,
    purpose: 'schema',
    readingMode: 'full',
  }).document;

  const response = await tool(app, project.id, 'read_documents_fully', {
    filenames: [document.filename],
  }, `run-${project.id}`);

  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().coverage[0].complete, true);
  assert.equal(response.json().coverage[0].coveredRows, 2_105);
  assert.equal(response.json().coverage[0].totalRows, 2_105);
  assert.equal(response.json().documents[0].schema[0].rowCount, 2_105);
});

test('规则目录索引只能由领域编译器同步，不能手工指定确认层级', async (t) => {
  const project = createProject({ owner: 'alice', name: '规则索引层级' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);

  const written = await tool(app, project.id, 'write_file', {
    path: 'rules/index.md',
    content: `---
resource: rule_index
key: rules
title: 规则目录
---
# 规则目录

| rule key | 标题 | 说明 |
| --- | --- | --- |
`,
    change_group: 'rule-index',
    evidence: [],
  }, runId);
  assert.equal(written.statusCode, 409, written.body);
  assert.equal(written.json().code, 'MANAGED_FILE_REQUIRES_DOMAIN_TOOL');
});

test('受管知识 Markdown 不能绕过结构化领域工具', async (t) => {
  const project = createProject({ owner: 'alice', name: '知识 Markdown 门禁' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);
  const objectGroup = 'feedstock-object';
  const metricGroup = 'quality-metric';
  const indexGroup = 'knowledge-index';

  const objectWritten = await tool(app, project.id, 'write_file', {
    path: 'objects/feedstock.md',
    content: `---
resource: object
key: feedstock
title: 原料
description: 用于工艺处理的原料对象。
sources:
  - 文档：流程说明.md / 原料
---
# 业务定义

用于工艺处理的原料对象。

# 字段

| 字段 key | 中文名 | 类型 | 必填 | 业务说明 | 别名 |
| --- | --- | --- | --- | --- | --- |
| batch | 批次 | string | 是 | 原料批次 | 批号 |

# 查询边界

只使用资料明确声明的字段。
`,
    change_group: objectGroup,
    evidence: ['user-decision:feedstock'],
  }, runId);
  assert.equal(objectWritten.statusCode, 409, objectWritten.body);
  assert.equal(objectWritten.json().code, 'MANAGED_FILE_REQUIRES_DOMAIN_TOOL');
  return;
  const invalidMetric = await tool(app, project.id, 'write_file', {
    path: 'metrics/quality.md',
    content: `---
resource: metric
key: quality
title: 原料质量指标
description: 按批次记录原料质量。
uses.objects:
  - feedstock
evidence:
  - user-decision:quality
---
# 业务口径

按原料批次记录质量检验结果。

# 计算边界

- 每个原料批次独立记录，不跨批次汇总。

# 知识证据

- user-decision:quality
`,
    change_group: metricGroup,
    evidence: ['user-decision:quality'],
  }, runId);
  assert.equal(invalidMetric.statusCode, 400, invalidMetric.body);
  const failure = invalidMetric.json();
  assert.equal(failure.code, 'KNOWLEDGE_FILE_INVALID');
  assert.equal(failure.retryable, true);
  assert.match(failure.validatorVersion, /^knowledge-markdown-/u);
  assert.ok(failure.findings.length >= 1);
  assert.ok(failure.findings.every((finding: any) => (
    finding.ruleId && finding.path && finding.message && finding.suggestion
  )));
  assert.match(invalidMetric.body, /uses\.objects/u);

  const correctedMetric = await tool(app, project.id, 'write_file', {
    path: 'metrics/quality.md',
    content: `---
resource: metric
key: quality
title: 原料质量指标
description: 按批次记录原料质量。
uses:
  objects:
    - feedstock
evidence:
  - user-decision:quality
---
# 业务口径

按原料批次记录质量检验结果。

# 计算边界

- 每个原料批次独立记录，不跨批次汇总。

# 知识证据

- user-decision:quality
`,
    change_group: metricGroup,
    evidence: ['user-decision:quality'],
  }, runId);
  assert.equal(correctedMetric.statusCode, 200, correctedMetric.body);
  await tool(app, project.id, 'write_file', {
    path: 'objects/index.md',
    content: `---
resource: object_index
key: objects
title: 对象目录
---
# 对象目录

| object key | 标题 | 说明 | 来源Sheet |
| --- | --- | --- | --- |
| feedstock | 原料 | 用于工艺处理的原料对象 | — |
`,
    change_group: indexGroup,
    evidence: [],
  }, runId);
  await tool(app, project.id, 'write_file', {
    path: 'metrics/index.md',
    content: `---
resource: metric_index
key: metrics
title: 指标目录
---
# 指标目录

| metric key | 标题 | 说明 |
| --- | --- | --- |
| quality | 原料质量指标 | 按批次记录原料质量 |
`,
    change_group: indexGroup,
    evidence: [],
  }, runId);

  const accepted = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '确认知识结构',
    change_groups: [
      { id: objectGroup, tier: 'structural', title: '原料对象' },
      { id: metricGroup, tier: 'structural', title: '质量指标' },
      { id: indexGroup, tier: 'index', title: '知识索引' },
    ],
  }, runId);
  assert.equal(accepted.statusCode, 200, accepted.body);
});

test('平台保留 manifest 管理字段，底层预检仍把详细错误返回给 Agent', async (t) => {
  const project = createProject({ owner: 'alice', name: '候选格式闭环', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);

  const current = await getPackageArtifactStore().readPackageFiles(project.id);
  const manifest = current.files['manifest.yaml'];
  assert.match(manifest, /^status:/mu);
  const proposedManifest = manifest
    .replace(/^kind:.*$/mu, 'kind: agent-invented-kind')
    .replace(/^status:.*$/mu, 'status: deprecated')
    .replace(/^description:.*$/mu, 'description: Agent 只修改业务描述');
  const group = { id: 'manifest-index', tier: 'index', title: '同步知识索引' };
  const written = await tool(app, project.id, 'write_file', {
    path: 'manifest.yaml',
    content: proposedManifest,
    change_group: group.id,
    evidence: [],
  }, runId);
  assert.equal(written.statusCode, 200, written.body);
  assert.deepEqual(written.json().manifestNormalization.ignoredFields, ['kind', 'status']);
  const normalized = getDb().prepare(`SELECT content FROM agent_candidate_changes
    WHERE project_id = ? AND run_id = ? AND path = 'manifest.yaml'`).get(project.id, runId) as { content: string };
  assert.match(normalized.content, /^kind: ontology-package$/mu);
  assert.match(normalized.content, /^status: review$/mu);
  assert.match(normalized.content, /^description: Agent 只修改业务描述$/mu);

  // 模拟旧版本或旁路写入留下的坏候选，验证最终预检仍是独立防线。
  getDb().prepare(`UPDATE agent_candidate_changes SET content = ?
    WHERE project_id = ? AND run_id = ? AND path = 'manifest.yaml'`)
    .run(manifest.replace(/^status:.*\n?/mu, ''), project.id, runId);

  const blocked = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: group.title,
    change_groups: [group],
  }, runId);

  assert.equal(blocked.statusCode, 409, blocked.body);
  const failure = blocked.json();
  assert.equal(failure.code, 'CANDIDATE_FORMAT_INVALID');
  assert.equal(failure.retryable, true);
  assert.match(failure.validatorVersion, /^package-format-/u);
  assert.equal(failure.findingCount, 1);
  assert.deepEqual(failure.findings.map((finding: any) => ({
    ruleId: finding.ruleId,
    path: finding.path,
    message: finding.message,
  })), [{
    ruleId: 'manifest.required.status',
    path: 'manifest.yaml',
    message: 'manifest 缺少必选字段 status',
  }]);
  assert.match(failure.findings[0].suggestion, /补回并保留 status/u);

  const correctedManifest = manifest.replace(
    /^description:.*$/mu,
    'description: 候选格式闭环修正后的知识包描述',
  );
  const corrected = await tool(app, project.id, 'write_file', {
    path: 'manifest.yaml',
    content: correctedManifest,
    change_group: group.id,
    evidence: [],
  }, runId);
  assert.equal(corrected.statusCode, 200, corrected.body);
  const accepted = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: group.title,
    change_groups: [group],
  }, runId);
  assert.equal(accepted.statusCode, 200, accepted.body);
  const receipt = accepted.json().validation_receipt;
  assert.equal(receipt.candidateRevision, accepted.json().candidate_revision);
  assert.equal(receipt.baseRevision, current.revision.oid);
  assert.match(receipt.candidateHash, /^sha256:[a-f0-9]{64}$/u);
  assert.equal(receipt.validatorVersion, 'package-contract-v7');
});

test('最终确认会校验用户实际选中的变更组快照', async (t) => {
  const project = createProject({ owner: 'alice', name: '部分确认格式门禁', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const detailGroup = { id: 'selected-detail', tier: 'structural', title: '新增对象详情' };
  const indexGroup = { id: 'selected-index', tier: 'index', title: '更新对象索引' };
  const detailContent = `---
resource: object
key: selected-object
title: 候选对象
description: 用于验证部分确认的候选对象。
sources:
  - 文档：用户口述 / 部分确认测试
---
# 业务定义

用于验证部分确认的候选对象。

# 字段

| 字段 key | 中文名 | 类型 | 必填 | 业务说明 | 别名 |
| --- | --- | --- | --- | --- | --- |
| id | 标识 | string | 是 | 候选对象标识 | 编号 |

# 查询边界

只使用用户确认的候选对象。
`;
  const indexContent = `---
resource: object_index
key: objects
title: 对象目录
---
# 对象目录

| object key | 标题 | 说明 | 来源Sheet |
| --- | --- | --- | --- |
| selected-object | 候选对象 | 用于验证部分确认 | — |
`;

  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  const artifacts = getPackageArtifactStore();
  const current = await artifacts.current(project.id);
  assert.ok(current);
  const candidate = await artifacts.createCandidateRevision(project.id, {
    expectedRevision: current.revision.oid,
    groups: [{
      id: detailGroup.id,
      summary: detailGroup.title,
      changes: [{ kind: 'write', path: 'package/objects/selected-object.md', content: detailContent }],
    }, {
      id: indexGroup.id,
      summary: indexGroup.title,
      changes: [{ kind: 'write', path: 'package/objects/index.md', content: indexContent }],
    }],
    action: {
      cause: 'test_selected_snapshot',
      summary: '构造无依赖的冻结候选',
      actorKind: 'agent',
      actorName: '测试 Agent',
    },
  });
  getDb().prepare(`UPDATE agent_candidate_workspaces
    SET candidate_id = ?, base_revision_oid = ? WHERE project_id = ? AND run_id = ?`)
    .run(candidate.id, current.revision.oid, project.id, runId);
  const insertGroup = getDb().prepare(`INSERT INTO agent_candidate_group_metadata (
    candidate_id, group_id, tier, title, impact_json, warning
  ) VALUES (?,?,?,?,?,NULL)`);
  insertGroup.run(candidate.id, detailGroup.id, detailGroup.tier, detailGroup.title, '[]');
  insertGroup.run(candidate.id, indexGroup.id, indexGroup.tier, indexGroup.title, '[]');

  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: '验证部分确认快照',
    change_groups: [detailGroup, indexGroup],
  }, runId);
  assert.equal(preflight.statusCode, 200, preflight.body);

  const blocked = await tool(app, project.id, 'request_confirmation', {
    summary: '验证部分确认快照',
    change_groups: [detailGroup, indexGroup],
    ...confirmationReceipt(preflight),
    review: { approved: true, review_mode: 'partial', selected_groups: [indexGroup.id] },
  }, runId);
  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().code, 'CANDIDATE_FORMAT_INVALID');
  assert.ok(blocked.json().findings.some((finding: any) => (
    finding.path === 'objects/index.md' && /selected-object/u.test(finding.message)
  )));
  assert.equal((await artifacts.getCandidateRevision(project.id, candidate.id)).status, 'pending');
  assert.equal((await artifacts.current(project.id))?.revision.oid, current.revision.oid);
});

test('接受候选前必须匹配预检回执中的内容哈希和校验器版本', async (t) => {
  const project = createProject({ owner: 'alice', name: '候选确认回执', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);
  const group = { id: 'receipt-check', tier: 'index', title: '确认候选回执' };
  const current = await getPackageArtifactStore().readPackageFiles(project.id);
  const written = await tool(app, project.id, 'write_file', {
    path: 'manifest.yaml',
    content: current.files['manifest.yaml'].replace(
      /^description:.*$/mu,
      'description: 用于验证候选确认回执',
    ),
    change_group: group.id,
    evidence: [],
  }, runId);
  assert.equal(written.statusCode, 200, written.body);
  const preflight = await tool(app, project.id, 'request_confirmation_preflight', {
    summary: group.title,
    change_groups: [group],
  }, runId);
  assert.equal(preflight.statusCode, 200, preflight.body);
  const payload = preflight.json();

  const missingReceipt = await tool(app, project.id, 'request_confirmation', {
    summary: group.title,
    change_groups: [group],
    review: { approved: true, review_mode: 'full', selected_groups: [group.id] },
  }, runId);
  assert.equal(missingReceipt.statusCode, 409, missingReceipt.body);
  assert.equal(missingReceipt.json().code, 'CANDIDATE_REVALIDATION_REQUIRED');

  const stale = await tool(app, project.id, 'request_confirmation', {
    summary: group.title,
    change_groups: [group],
    expected_candidate_revision: payload.candidate_revision,
    expected_candidate_hash: 'sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
    expected_validator_version: payload.validation_receipt.validatorVersion,
    review: { approved: true, review_mode: 'full', selected_groups: [group.id] },
  }, runId);
  assert.equal(stale.statusCode, 409, stale.body);
  assert.equal(stale.json().code, 'CANDIDATE_REVALIDATION_REQUIRED');
  assert.equal(stale.json().retryable, false);

  const confirmed = await tool(app, project.id, 'request_confirmation', {
    summary: group.title,
    change_groups: [group],
    expected_candidate_revision: payload.candidate_revision,
    expected_candidate_hash: payload.validation_receipt.candidateHash,
    expected_validator_version: payload.validation_receipt.validatorVersion,
    review: { approved: true, review_mode: 'full', selected_groups: [group.id] },
  }, runId);
  assert.equal(confirmed.statusCode, 200, confirmed.body);
});

test('脚本沙箱测试要求明确输入输出契约', async (t) => {
  const project = createProject({ owner: 'alice', name: '脚本契约门禁' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);
  await confirmKnowledge(app, project.id, runId);
  const written = await tool(app, project.id, 'write_file', {
    path: 'scripts/stock.py',
    content: 'def run(value):\n    return value * 2\n',
    change_group: 'script-stock',
    evidence: ['user-decision:script-contract'],
  }, runId);
  assert.equal(written.statusCode, 200, written.body);

  const blocked = await tool(app, project.id, 'run_script_test', {
    script: 'scripts/stock.py',
    cases: [{ input: { value: 2 } }],
  }, runId);
  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().code, 'CONTRACT_REQUIRED');
  assert.match(blocked.json().reason, /expected/u);
});

test('确定性计算候选必须经人工确认且知识变化后旧决定自动失效', async (t) => {
  const project = createProject({
    owner: 'alice', name: '确定性脚本范围门禁', kind: 'package', runtime: 'agent',
  });
  const artifacts = getPackageArtifactStore();
  const initial = await artifacts.initializeDraftPackage(project.id, {
    id: 'deterministic-scope-package',
    name: '确定性脚本范围门禁',
    description: '验证确定性分析、人工范围决定和知识变更失效。',
  });
  const seeded = await artifacts.savePackageVersion(project.id, {
    expectedRevision: initial.revision.oid,
    spec: {
      ...structuredClone(initial.spec),
      objects: [{
        key: 'device_plan',
        title: '装置计划',
        description: '装置加工计划及其计算输入。',
        fields: [
          { key: 'amount', cnName: '加工量', type: 'number', required: true },
          { key: 'days', cnName: '运行天数', type: 'number', required: true },
        ],
        primaryKey: ['id'],
        aliases: [],
        queryBoundary: '',
        relations: [],
      }],
      operations: [{
        key: 'calculate_device_load',
        title: '计算装置负荷率',
        description: '按加工量和运行天数精确计算装置负荷率。',
        inputs: [
          { param: 'amount', type: 'number', required: true },
          { param: 'days', type: 'number', required: true },
        ],
        outputs: [{ field: 'load_rate', type: 'number' }],
        flow: ['汇总加工量', '换算日均负荷', '计算负荷率'],
        relatedObjects: ['device_plan'],
      }],
    },
    coverage: initial.coverage,
    note: '建立确定性计算测试基线',
    action: {
      cause: 'test.deterministic_scope_seeded',
      summary: '建立确定性计算测试基线',
      actorKind: 'system',
      actorName: 'test',
    },
  });
  const app = appFor(t);
  const runId = `run-${project.id}`;

  const analysis = await tool(app, project.id, 'analyze_deterministic_capabilities', {}, runId);
  assert.equal(analysis.statusCode, 200, analysis.body);
  assert.equal(analysis.json().status, 'decision_required');
  assert.equal(analysis.json().candidates.length, 1);
  assert.equal(analysis.json().candidates[0].operation_key, 'calculate_device_load');
  assert.equal(analysis.json().candidates[0].already_covered, false);

  const blocked = await tool(app, project.id, 'gate_status', {
    gate: 'deterministic_confirmed',
  }, runId);
  assert.equal(blocked.statusCode, 200, blocked.body);
  assert.equal(blocked.json().pass, false);
  assert.match(blocked.json().blockers[0], /确认当前确定性计算候选/u);
  // 等专家拍板不等于助手没事做：发 scope_confirmation 确认卡本来就是 SKILL
  // 要求助手做的动作，禁止的只是代替专家调用 confirm_deterministic_scope。
  assert.equal(blocked.json().agentActionable.length, 1);
  assert.match(blocked.json().agentActionable[0], /scope_confirmation/u);
  assert.match(blocked.json().agentActionable[0], /不得代替专家/u);

  const stale = await tool(app, project.id, 'confirm_deterministic_scope', {
    expected_scope_hash: 'sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
    selected_operation_keys: [],
    reason: '专家明确决定本轮不生成脚本',
  }, runId);
  assert.equal(stale.statusCode, 409, stale.body);
  assert.equal(stale.json().code, 'DETERMINISTIC_SCOPE_CHANGED');

  const skipped = await tool(app, project.id, 'confirm_deterministic_scope', {
    expected_scope_hash: analysis.json().scope_hash,
    selected_operation_keys: [],
    reason: '专家明确决定本轮不生成脚本',
  }, runId);
  assert.equal(skipped.statusCode, 200, skipped.body);
  assert.equal(skipped.json().status, 'ready');
  const skippedGate = await tool(app, project.id, 'gate_status', {
    gate: 'deterministic_confirmed',
  }, runId);
  assert.equal(skippedGate.json().pass, true);
  assert.match(skippedGate.json().warnings[0], /明确跳过 1 个/u);

  const changed = await artifacts.savePackageVersion(project.id, {
    expectedRevision: seeded.revision.oid,
    spec: {
      ...structuredClone(seeded.spec),
      operations: seeded.spec.operations.map((operation) => ({
        ...operation,
        description: `${operation.description} 缺少运行天数时必须明确失败。`,
      })),
    },
    coverage: seeded.coverage,
    note: '调整确定性计算边界',
    action: {
      cause: 'test.deterministic_scope_changed',
      summary: '调整确定性计算边界',
      actorKind: 'user',
      actorName: 'alice',
    },
  });
  assert.notEqual(changed.revision.oid, seeded.revision.oid);
  const nextRunId = runId;
  const refreshed = await tool(app, project.id, 'analyze_deterministic_capabilities', {}, nextRunId);
  assert.equal(refreshed.statusCode, 200, refreshed.body);
  assert.equal(refreshed.json().status, 'decision_required');
  assert.notEqual(refreshed.json().scope_hash, analysis.json().scope_hash);

  const operations = await tool(app, project.id, 'list_domain_resources', {
    kind: 'operation',
  }, nextRunId);
  assert.equal(operations.statusCode, 200, operations.body);
  const operation = await tool(app, project.id, 'get_domain_resource', {
    resource_id: 'operation:calculate_device_load',
    working_revision: operations.json().working_revision,
  }, nextRunId);
  assert.equal(operation.statusCode, 200, operation.body);
  const contractArgs = {
    expected_revision: operation.json().working_revision,
    mutation_id: 'define-device-load-contract',
    reason: '保存专家确认的输入输出和测试边界',
    script_key: 'calculate_device_load',
    title: '装置负荷率计算脚本',
    description: '根据加工量与运行天数返回确定性负荷率。',
    operation_id: 'operation:calculate_device_load',
    expected_operation_hash: operation.json().resource.operation_contract_hash,
    entrypoint: 'calculate_device_load.py',
    timeout_seconds: 30,
    parameters: [
      { name: 'amount', flag: '--amount', type: 'number', required: true },
      { name: 'days', flag: '--days', type: 'number', required: true },
      {
        name: 'strict', flag: '--strict', type: 'boolean', required: false,
        action: 'store_true',
      },
    ],
    outputs: [{ key: 'load_rate', type: 'number' }],
    boundaries: ['运行天数必须大于零', '缺少必填输入时明确失败'],
    evidence: ['user-decision:deterministic-contract'],
  };
  const contractBeforeScope = await tool(
    app, project.id, 'define_script_contract', contractArgs, nextRunId,
  );
  assert.equal(contractBeforeScope.statusCode, 409, contractBeforeScope.body);
  assert.equal(contractBeforeScope.json().code, 'SCRIPT_SCOPE_NOT_CONFIRMED');

  const selected = await tool(app, project.id, 'confirm_deterministic_scope', {
    expected_scope_hash: refreshed.json().scope_hash,
    selected_operation_keys: ['calculate_device_load'],
    reason: '专家确认该负荷率必须由确定性脚本计算',
  }, nextRunId);
  assert.equal(selected.statusCode, 200, selected.body);
  assert.equal(selected.json().status, 'implementation_required');
  assert.deepEqual(selected.json().missing_script_bindings, ['calculate_device_load']);
  assert.deepEqual(selected.json().missing_script_tests, []);

  const staleOperationHash = await tool(app, project.id, 'define_script_contract', {
    ...contractArgs,
    mutation_id: 'define-device-load-contract-stale-hash',
    expected_operation_hash: `sha256:${'0'.repeat(64)}`,
  }, nextRunId);
  assert.equal(staleOperationHash.statusCode, 409, staleOperationHash.body);
  assert.equal(staleOperationHash.json().code, 'RESOURCE_HASH_CONFLICT');
  assert.equal(staleOperationHash.json().retryable, false);

  const defined = await tool(app, project.id, 'define_script_contract', contractArgs, nextRunId);
  assert.equal(defined.statusCode, 200, defined.body);
  const stillBlocked = await tool(app, project.id, 'gate_status', {
    gate: 'ready_for_eval',
  }, nextRunId);
  assert.equal(stillBlocked.json().pass, false);
  assert.ok(stillBlocked.json().blockers.some((item: string) => /完成已确认业务操作/u.test(item)));

  const scriptIndex = await tool(app, project.id, 'get_script_edit_context', {
    script_key: 'calculate_device_load',
  }, nextRunId);
  assert.equal(scriptIndex.statusCode, 200, scriptIndex.body);
  assert.ok(scriptIndex.json().symbols.some(
    (item: any) => item.qualified_name === 'run_business_logic',
  ));
  const editContext = await tool(app, project.id, 'get_script_edit_context', {
    script_key: 'calculate_device_load',
    symbols: ['run_business_logic'],
  }, nextRunId);
  assert.equal(editContext.statusCode, 200, editContext.body);
  const businessNode = editContext.json().symbols.find(
    (item: any) => item.qualified_name === 'run_business_logic',
  );
  assert.ok(businessNode);

  const dataAccessPatch = await tool(app, project.id, 'stage_script_patch', {
    script_key: 'calculate_device_load',
    expected_revision: defined.json().working_revision,
    expected_contract_hash: defined.json().contract_hash,
    base_source_hash: editContext.json().base_source_hash,
    mutation_id: 'stage-device-load-script-patch-data-access',
    reason: '尝试在脚本里自行取数',
    edits: [{
      kind: 'replace_function',
      symbol: 'run_business_logic',
      expected_node_hash: businessNode.node_hash,
      source: `def run_business_logic(inputs):
    from shared_capabilities.ontology_script_sdk import DataProviderClient
    client = DataProviderClient()
    result = client.query("device")
    return {"load_rate": len(result.data)}
`,
    }],
  }, nextRunId);
  assert.equal(dataAccessPatch.statusCode, 409, dataAccessPatch.body);
  assert.equal(dataAccessPatch.json().code, 'SCRIPT_DATA_ACCESS_FORBIDDEN');
  // 模型改掉源码就能过，必须进 Agent 的受控修复轮次，而不是标成不可重试堵死。
  assert.equal(dataAccessPatch.json().retryable, true);
  assert.equal(dataAccessPatch.json().findings.length, dataAccessPatch.json().findingCount);
  assert.equal(dataAccessPatch.json().findings[0].ruleId, 'script.pure-computation');

  const businessSource = `def run_business_logic(inputs):
    days = inputs["days"]
    if days <= 0:
        raise ValueError("days must be greater than zero")
    return {"load_rate": inputs["amount"] / days}
`;
  const staged = await tool(app, project.id, 'stage_script_patch', {
    script_key: 'calculate_device_load',
    expected_revision: defined.json().working_revision,
    expected_contract_hash: defined.json().contract_hash,
    base_source_hash: editContext.json().base_source_hash,
    mutation_id: 'stage-device-load-script-patch',
    reason: '按专家确认的契约实现装置负荷率计算',
    edits: [{
      kind: 'replace_function',
      symbol: 'run_business_logic',
      expected_node_hash: businessNode.node_hash,
      source: businessSource,
    }],
  }, nextRunId);
  assert.equal(staged.statusCode, 200, staged.body);
  const beforeTest = await tool(app, project.id, 'gate_status', {
    gate: 'deterministic_confirmed',
  }, nextRunId);
  assert.equal(beforeTest.json().pass, false);
  assert.ok(beforeTest.json().blockers.some((item: string) => /隔离测试/u.test(item)));

  const executedCases = await usePassingScriptSandbox(t);
  const tested = await tool(app, project.id, 'run_script_test', {
    script: 'scripts/calculate_device_load.py',
    cases: [
      { input: { amount: 80, days: 2, strict: true }, expected: { load_rate: 40 } },
      { input: { amount: 0, days: 1, strict: false }, expected: { load_rate: 0 } },
      { input: { amount: 80, days: 0, strict: false }, expected: { error: 'days must be greater than zero' } },
    ],
  }, nextRunId);
  assert.equal(tested.statusCode, 200, tested.body);
  assert.equal(tested.json().failed, 0);
  assert.deepEqual(executedCases[0].map((item) => item.argv), [
    ['--amount', '80.0', '--days', '2.0', '--strict'],
    ['--amount', '0.0', '--days', '1.0'],
    ['--amount', '80.0', '--days', '0.0'],
  ]);
  const deterministicReady = await tool(app, project.id, 'gate_status', {
    gate: 'deterministic_confirmed',
  }, nextRunId);
  assert.equal(deterministicReady.json().pass, true, deterministicReady.body);
});

test('脚本候选的预检和最终确认都只接受当前源码的沙箱通过回执', async (t) => {
  const project = createProject({ owner: 'alice', name: '脚本回执门禁' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);
  await confirmKnowledge(app, project.id, runId);
  const source = (expression: string) => `import argparse
import json

def run(value):
    return ${expression}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--value", required=True, type=int)
    args = parser.parse_args()
    print(json.dumps({"value": run(args.value)}))

if __name__ == "__main__":
    main()
`;
  const artifacts = getPackageArtifactStore();
  const current = await artifacts.current(project.id);
  assert.ok(current);
  const seeded = await artifacts.savePackageVersion(project.id, {
    expectedRevision: current.revision.oid,
    spec: {
      ...structuredClone(current.spec),
      operations: [...current.spec.operations, {
        key: 'calculate_stock', title: '计算库存', description: '按输入值计算库存。',
        inputs: [{ param: 'value', type: 'number', required: true }],
        outputs: [{ field: 'value', type: 'number' }],
        flow: ['读取输入', '计算库存'], relatedObjects: [],
        script: { key: 'stock', required: true },
      }],
      scripts: [{
        key: 'stock', title: '库存计算脚本', description: '执行确定性库存计算。',
        boundOperation: 'calculate_stock', runtime: 'python', entrypoint: 'stock.py', timeoutSeconds: 30,
        parameters: [{ name: 'value', flag: '--value', type: 'integer', required: true }],
        outputs: [{ key: 'value', type: 'number' }],
        boundaries: ['只处理显式输入'], evidence: ['user-decision:script-current-source'],
        sourceCode: source('value'),
      }],
    },
    coverage: current.coverage,
    note: '建立已发布脚本基线',
    action: {
      cause: 'test.script_baseline_seeded', summary: '建立已发布脚本基线',
      actorKind: 'system', actorName: 'test',
    },
  });
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  assert.equal(claimed.json().supersededChanges, 0);

  const operation = await tool(app, project.id, 'get_domain_resource', {
    resource_id: 'operation:calculate_stock',
  }, runId);
  assert.equal(operation.statusCode, 200, operation.body);
  const defined = await tool(app, project.id, 'define_script_contract', {
    expected_revision: operation.json().working_revision,
    mutation_id: 'define-stock-contract-v2',
    reason: '定义已发布库存脚本的当前执行契约',
    script_key: 'stock', title: '库存计算脚本', description: '执行确定性库存计算。',
    operation_id: 'operation:calculate_stock',
    expected_operation_hash: operation.json().resource.operation_contract_hash,
    entrypoint: 'stock.py', timeout_seconds: 30,
    parameters: [{ name: 'value', flag: '--value', type: 'integer', required: true }],
    outputs: [{ key: 'value', type: 'number' }],
    boundaries: ['只处理显式输入'], evidence: ['user-decision:script-current-source'],
  }, runId);
  assert.equal(defined.statusCode, 200, defined.body);
  let workingRevision = defined.json().working_revision as string;
  let mutation = 0;
  const writeScript = async (sourceCode: string) => {
    mutation += 1;
    const staged = await tool(app, project.id, 'stage_script_source', {
      script_key: 'stock', expected_revision: workingRevision,
      expected_contract_hash: defined.json().contract_hash,
      mutation_id: `stage-stock-source-${mutation}`,
      reason: '更新当前库存脚本源码', source_code: sourceCode,
    }, runId);
    if (staged.statusCode === 200) workingRevision = staged.json().working_revision;
    return staged;
  };
  let currentGroups: any[] = [];
  const preflight = async () => {
    const prepared = await tool(app, project.id, 'prepare_confirmation_groups', {}, runId);
    assert.equal(prepared.statusCode, 200, prepared.body);
    currentGroups = prepared.json().change_groups;
    assert.equal(currentGroups.length, 1);
    assert.deepEqual(currentGroups[0].impact, ['scripts/stock.py']);
    return tool(app, project.id, 'request_confirmation_preflight', {
      summary: '确认当前脚本实现', change_groups: currentGroups,
    }, runId);
  };
  const runTest = () => tool(app, project.id, 'run_script_test', {
    script: 'scripts/stock.py',
    cases: [{ input: { value: 2 }, expected: { value: 4 } }],
  }, runId);

  assert.equal((await writeScript(source('value * 2'))).statusCode, 200);
  assert.notEqual(workingRevision, seeded.revision.oid);
  const neverTested = await preflight();
  assert.equal(neverTested.statusCode, 409, neverTested.body);
  assert.equal(neverTested.json().code, 'SCRIPT_TEST_REQUIRED');

  await usePassingScriptSandbox(t);
  assert.equal((await runTest()).statusCode, 200);
  assert.equal((await writeScript(source('value + value'))).statusCode, 200);
  const staleReceipt = await preflight();
  assert.equal(staleReceipt.statusCode, 409, staleReceipt.body);
  assert.equal(staleReceipt.json().code, 'SCRIPT_TEST_REQUIRED');

  assert.equal((await runTest()).statusCode, 200);
  const ready = await preflight();
  assert.equal(ready.statusCode, 200, ready.body);
  assert.equal(getDb().prepare(`SELECT COUNT(*) AS count FROM agent_script_test_receipts
    WHERE project_id = ? AND run_id = ?`).get(project.id, runId)?.count, 2);

  getDb().prepare('DELETE FROM agent_script_test_receipts WHERE project_id = ? AND run_id = ?')
    .run(project.id, runId);
  const blockedConfirmation = await tool(app, project.id, 'request_confirmation', {
    summary: '确认当前脚本实现',
    change_groups: currentGroups,
    ...confirmationReceipt(ready),
    review: {
      approved: true, review_mode: 'full',
      selected_groups: currentGroups.map((group: any) => group.id),
    },
  }, runId);
  assert.equal(blockedConfirmation.statusCode, 409, blockedConfirmation.body);
  assert.equal(blockedConfirmation.json().code, 'SCRIPT_TEST_REQUIRED');

  assert.equal((await runTest()).statusCode, 200);
  const confirmed = await tool(app, project.id, 'request_confirmation', {
    summary: '确认当前脚本实现',
    change_groups: currentGroups,
    ...confirmationReceipt(ready),
    review: {
      approved: true, review_mode: 'full',
      selected_groups: currentGroups.map((group: any) => group.id),
    },
  }, runId);
  assert.equal(confirmed.statusCode, 200, confirmed.body);
});

test('Agent 停止运行时取消评测工具可幂等执行', async (t) => {
  const project = createProject({ owner: 'alice', name: '评测停止幂等' });
  const app = appFor(t);

  const cancelled = await tool(app, project.id, 'cancel_evaluation', {}, `run-${project.id}`);

  assert.equal(cancelled.statusCode, 200, cancelled.body);
  assert.equal(cancelled.json().cancelled, false);
  assert.equal(cancelled.json().idempotent, true);
});

test('Agent 项目状态读取最近评测终态且重复停止指定终态轮次不产生副作用', async (t) => {
  const project = createProject({ owner: 'alice', name: '评测终态快照' });
  const app = appFor(t);
  const m3 = await import('../src/workflow/m3-repo.js');
  const previous = m3.createRound(project.id, '1.0.0-rc.1', 4);
  m3.roundProgress(previous.id, 2, 1);
  m3.finishRound(previous.id, 'failed');
  const latest = m3.createRound(project.id, '1.0.0-rc.2', 6);
  m3.roundProgress(latest.id, 6, 5);
  m3.finishRound(latest.id, 'done');

  const status = await tool(app, project.id, 'evaluation_status', {}, `run-${project.id}`);
  assert.equal(status.statusCode, 200, status.body);
  assert.deepEqual(status.json(), {
    available: true,
    roundId: latest.id,
    status: 'done',
    terminal: true,
    done: 6,
    total: 6,
    passed: 5,
    createdAt: latest.createdAt,
    finishedAt: m3.getRound(latest.id)?.finishedAt,
    scope: { kind: 'current' },
    display: { text: '最近一轮评测已经结束' },
  });

  const repeated = await tool(app, project.id, 'cancel_evaluation', {
    round_id: previous.id,
  }, `run-${project.id}`);
  assert.equal(repeated.statusCode, 200, repeated.body);
  assert.equal(repeated.json().cancelled, false);
  assert.equal(repeated.json().idempotent, true);
  assert.equal(repeated.json().roundId, previous.id);
  assert.equal(repeated.json().status, 'failed');
  assert.equal(m3.getRound(latest.id)?.status, 'done');
});

test('评测启动服从确定性门禁，评测库拒绝写 SQL', async (t) => {
  const project = createProject({ owner: 'alice', name: '评测工具门禁' });
  const app = appFor(t);
  const runId = `run-${project.id}`;

  const inventedCases = await tool(app, project.id, 'write_file', {
    path: 'tests/invented-evaluation.md',
    content: '# 从知识包反推的评测题',
    change_group: 'invented-tests',
    evidence: [],
  }, runId);
  assert.equal(inventedCases.statusCode, 400, inventedCases.body);
  assert.equal(inventedCases.json().code, 'EVALUATION_CASES_USER_ONLY');

  const evaluation = await tool(app, project.id, 'run_evaluation', {}, runId);
  assert.equal(evaluation.statusCode, 409, evaluation.body);
  assert.equal(evaluation.json().code, 'GATE_BLOCKED');
  assert.equal(evaluation.json().gate, 'ready_for_eval');

  const writeQuery = await tool(app, project.id, 'query_eval_db', {
    sql: 'DELETE FROM orders',
  }, runId);
  assert.equal(writeQuery.statusCode, 400, writeQuery.body);
  assert.equal(writeQuery.json().code, 'READ_ONLY_SQL_REQUIRED');

  const unavailableQuery = await tool(app, project.id, 'query_eval_db', {
    sql: 'SELECT 1',
  }, runId);
  assert.equal(unavailableQuery.statusCode, 409, unavailableQuery.body);
  assert.equal(unavailableQuery.json().code, 'EVAL_DB_TOOL_UNAVAILABLE');
  assert.doesNotMatch(unavailableQuery.body, /EVAL_DB_URL/);
  assert.match(unavailableQuery.json().reason, /直连评测库的诊断能力/);
});

test('Agent 内部数据源状态只返回脱敏摘要', async (t) => {
  const project = createProject({ owner: 'alice', name: '数据源摘要' });
  const app = appFor(t);
  const { saveProjectDataSource } = await import('../src/evaluation/data-source-repo.js');
  saveProjectDataSource({
    projectId: project.id,
    mode: 'connection',
    provider: 'neo4j',
    workspaceId: 'workspace_demo',
    ontologyId: 'ontology_demo',
    config: {
      uri: 'bolt://graph.example.com:7687',
      username: 'graph_user',
      password: '<redacted>',
      database: 'neo4j',
      workspace_property: 'workspace_id',
      workspace_value: 'workspace_demo',
      ontology_property: 'ontology_id',
      ontology_value: 'ontology_demo',
    },
  });

  const response = await tool(app, project.id, 'project_data_source_status');

  assert.equal(response.statusCode, 200, response.body);
  assert.deepEqual(response.json(), {
    configured: true,
    mode: 'connection',
    provider: 'neo4j',
    providerMeaning: 'project_evaluation_data_source_type',
    mappingAliasNote: 'mappings/object-data-mappings.json 中的 default_provider_alias/provider_alias 是知识包内部逻辑数据源别名，不是项目评测数据源 provider',
    workspaceLinked: true,
    ontologyLinked: true,
    neo4j: {
      workspaceIsolationConfigured: true,
      ontologyIsolationConfigured: true,
      graphDatabaseConfigured: true,
    },
    display: { text: '已配置 Neo4j 评测数据源' },
  });
  assert.doesNotMatch(response.body, /bolt:\/\/|graph_user|<redacted>|workspace_id|ontology_id/);
});

test('可选评测库未配置时只记录活动，不要求用户处理', async (t) => {
  const project = createProject({ owner: 'alice', name: '可选评测库未配置' });
  const previousEvalDbUrl = config.evalDbUrl;
  config.evalDbUrl = '';
  t.after(() => { config.evalDbUrl = previousEvalDbUrl; });
  const app = appFor(t);

  const response = await tool(app, project.id, 'query_eval_db', {
    sql: 'SELECT 1',
  }, `run-${project.id}`);

  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'EVAL_DB_TOOL_UNAVAILABLE');
  assert.equal(response.json().tracePhase, 'rejected');
  assert.equal(response.json().problemVisibility, 'activity');
  assert.equal(response.json().retryable, false);
});

test('Agent 评测忽略转发的旧 Access Token，只接受 Core 托管的可续期会话', async (t) => {
  const [{ backgroundAuthSessions }, { saveProjectDataSource }, m3] = await Promise.all([
    import('../src/auth/background-session.js'),
    import('../src/evaluation/data-source-repo.js'),
    import('../src/workflow/m3-repo.js'),
  ]);
  backgroundAuthSessions.forget('alice');
  const project = createProject({
    owner: 'alice',
    name: 'Agent 评测认证隔离',
    kind: 'package',
    runtime: 'agent',
  });
  const store = getPackageArtifactStore();
  const initial = await store.initializeDraftPackage(project.id, {
    id: 'agent-auth-package',
    name: 'Agent 评测认证隔离',
    description: '验证 Agent 旧令牌不能直接授权评测',
  });
  await store.saveFiles(project.id, {
    files: {
      'package/objects/auth-check.md': '# 认证检查\n\n用于确定性门禁测试。\n',
    },
    expectedRevision: initial.revision.oid,
    action: {
      cause: 'test_setup',
      summary: '建立已导入知识基线',
      actorKind: 'user',
      actorName: 'alice',
    },
  });
  m3.addTestcase(project.id, '当前认证是否有效？', '应使用 Core 中的最新登录会话', 'manual');
  saveProjectDataSource({
    projectId: project.id,
    mode: 'none',
    workspaceId: `mw-shadow-${project.id.replace(/[^a-zA-Z0-9-]/g, '-')}`.slice(0, 60),
  });
  const app = appFor(t);

  const response = await tool(app, project.id, 'run_evaluation', {}, `run-${project.id}`);

  assert.equal(response.statusCode, 401, response.body);
  assert.equal(response.json().code, 'AUTH_REQUIRED');
  assert.match(response.json().reason, /可续期的登录凭据/u);
});

test('Agent 评测创建后台轮次后立即返回，不等待 Consumer 执行结束', async (t) => {
  const [{ backgroundAuthSessions }, { saveProjectDataSource }, m3, settings] = await Promise.all([
    import('../src/auth/background-session.js'),
    import('../src/evaluation/data-source-repo.js'),
    import('../src/workflow/m3-repo.js'),
    import('../src/settings/repo.js'),
  ]);
  const project = createProject({
    owner: 'alice', name: 'Agent 异步评测回执', kind: 'package', runtime: 'agent',
  });
  const store = getPackageArtifactStore();
  const initial = await store.initializeDraftPackage(project.id, {
    id: 'async-evaluation-package',
    name: 'Agent 异步评测回执',
    description: '验证评测创建回执不等待后台执行',
  });
  await store.saveFiles(project.id, {
    files: {
      'package/objects/evaluation-check.md': '# 评测检查\n\n用于异步评测回执测试。\n',
    },
    expectedRevision: initial.revision.oid,
    action: {
      cause: 'test_setup', summary: '建立合成知识基线', actorKind: 'user', actorName: 'alice',
    },
  });
  m3.addTestcase(project.id, '当前回答是否符合预期？', '回答应符合预期', 'manual');
  saveProjectDataSource({
    projectId: project.id,
    mode: 'none',
    workspaceId: `evaluation-${project.id.replace(/[^a-zA-Z0-9-]/g, '-')}`.slice(0, 60),
  });

  let released = false;
  let markConsumerRequestStarted = () => {};
  const consumerRequestStarted = new Promise<void>((resolve) => {
    markConsumerRequestStarted = resolve;
  });
  const pendingResponses = new Set<any>();
  const evaluationServer = createServer((_request, serverResponse) => {
    markConsumerRequestStarted();
    if (released) {
      serverResponse.writeHead(503, { 'content-type': 'application/json' });
      serverResponse.end(JSON.stringify({ error: 'synthetic unavailable response' }));
      return;
    }
    pendingResponses.add(serverResponse);
  });
  await new Promise<void>((resolve) => evaluationServer.listen(0, '127.0.0.1', resolve));
  const address = evaluationServer.address();
  assert.ok(address && typeof address === 'object');
  const previousConsumerUrl = settings.getSetting('consumer_api_url');
  settings.setSetting('consumer_api_url', `http://127.0.0.1:${address.port}`);
  backgroundAuthSessions.remember('alice', 'synthetic-access-token', 'synthetic-refresh-token');
  const releaseEvaluation = () => {
    released = true;
    for (const serverResponse of pendingResponses) {
      serverResponse.writeHead(503, { 'content-type': 'application/json' });
      serverResponse.end(JSON.stringify({ error: 'synthetic unavailable response' }));
    }
    pendingResponses.clear();
  };
  t.after(async () => {
    releaseEvaluation();
    backgroundAuthSessions.forget('alice');
    if (previousConsumerUrl === null) settings.deleteSetting('consumer_api_url');
    else settings.setSetting('consumer_api_url', previousConsumerUrl);
    await new Promise<void>((resolve) => evaluationServer.close(() => resolve()));
  });

  const app = appFor(t);
  const response = await Promise.race([
    tool(app, project.id, 'run_evaluation', {}, `run-${project.id}`),
    new Promise<never>((_resolve, reject) => {
      setTimeout(() => reject(new Error('评测启动接口仍在等待后台执行')), 3000);
    }),
  ]);

  assert.equal(response.statusCode, 200, response.body);
  assert.deepEqual(response.json(), {
    roundId: response.json().roundId,
    status: 'running',
    pending: true,
    terminal: false,
    scope: { kind: 'current', all: true },
    display: { text: '正式评测任务已创建，可在评测界面查看实时进度' },
  });
  assert.ok(response.json().roundId);
  await consumerRequestStarted;
  assert.equal(m3.getRound(response.json().roundId)?.status, 'running');

  releaseEvaluation();
  const { waitForEvaluation } = await import('../src/evaluation/engine.js');
  await waitForEvaluation(project.id, response.json().roundId);
});

test('Agent 可读取最近一轮真实评测的运行错误和轨迹', async (t) => {
  const project = createProject({ owner: 'alice', name: '评测错误归因' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const m3 = await import('../src/workflow/m3-repo.js');
  const testcase = m3.addTestcase(project.id, '装置当前压力是否越界？', '超过 14.2 MPa 即越界');
  const round = m3.createRound(project.id, '1.0.0-rc.1', 1);
  m3.addResult(round.id, {
    testcaseId: testcase.id,
    question: testcase.question,
    groundTruth: testcase.groundTruth,
    verdict: 'error',
    reason: 'Workflow execution failed',
    trace: {
      status: 'failed',
      error: 'model access denied',
      toolCalls: [],
      toolFailures: [{ tool: 'chat', error: 'model access denied' }],
    },
  });
  m3.roundProgress(round.id, 1, 0);
  m3.finishRound(round.id, 'done');

  const response = await tool(app, project.id, 'get_evaluation_results', {}, runId);
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().round.id, round.id);
  assert.deepEqual(response.json().counts, { error: 1 });
  assert.equal(response.json().results[0].question, testcase.question);
  assert.equal(response.json().results[0].reason, 'Workflow execution failed');
  assert.equal(response.json().results[0].trace.status, 'failed');
  assert.match(response.json().results[0].trace.toolFailures[0].error, /access denied/u);
});

test('发布预检合并同修订定向重评结果且不跨修订继承', async (t) => {
  const project = createProject({ owner: 'alice', name: '定向重评发布预检' });
  const app = appFor(t);
  const store = getPackageArtifactStore();
  const draft = await store.initializeDraftPackage(project.id, {
    id: 'targeted-release-package', name: '定向重评发布预检', description: '验证最新有效结果视图',
  });
  const rc = await store.saveCurrentAsPackageVersion(project.id, {
    expectedRevision: draft.revision.oid,
    version: '1.0.0-rc.1',
    status: 'review',
    note: '准备定向重评',
    action: {
      cause: 'test_targeted_release', summary: '创建定向重评候选', actorKind: 'agent', actorName: '测试助手',
    },
  });
  const m3 = await import('../src/workflow/m3-repo.js');
  const first = m3.addTestcase(project.id, '问题一', '答案一');
  const second = m3.addTestcase(project.id, '问题二', '答案二');
  const finish = (items: Array<{ testcase: typeof first; verdict: 'pass' | 'fail' }>) => {
    const round = m3.createRound(project.id, rc.packageVersion, items.length, rc.revision.oid);
    for (const item of items) {
      m3.addResult(round.id, {
        testcaseId: item.testcase.id,
        question: item.testcase.question,
        groundTruth: item.testcase.groundTruth,
        finalAnswer: item.verdict === 'pass' ? item.testcase.groundTruth : '错误回答',
        verdict: item.verdict,
        reason: item.verdict === 'pass' ? '符合预期' : '定向重评失败',
      });
    }
    m3.roundProgress(round.id, items.length, items.filter((item) => item.verdict === 'pass').length);
    m3.finishRound(round.id, 'done');
    return round;
  };

  finish([
    { testcase: first, verdict: 'pass' },
    { testcase: second, verdict: 'pass' },
  ]);
  finish([{ testcase: first, verdict: 'pass' }]);
  const runId = `run-targeted-${project.id}`;

  const green = await tool(app, project.id, 'release_preflight', {}, runId);
  assert.equal(green.statusCode, 200, green.body);
  assert.equal(green.json().ready, true);

  finish([{ testcase: first, verdict: 'fail' }]);
  const failed = await tool(app, project.id, 'release_preflight', {}, runId);
  assert.equal(failed.statusCode, 200, failed.body);
  assert.equal(failed.json().ready, false);
  assert.ok(failed.json().blockers.some((item: string) => /每条当前真实用例都必须执行并通过/u.test(item)));

  const changed = await store.saveFile(project.id, {
    path: 'package/index.md',
    content: '# 当前修订已经变化\n',
    expectedRevision: rc.revision.oid,
    action: { cause: 'test_revision_change', summary: '模拟修订变化', actorKind: 'agent', actorName: '测试助手' },
  });
  const stale = await tool(app, project.id, 'release_preflight', {}, runId);
  assert.equal(stale.statusCode, 200, stale.body);
  assert.equal(stale.json().ready, false);
  assert.equal(stale.json().source_revision, changed.revision.oid);
  assert.ok(stale.json().blockers.some((item: string) => /最近一次评测后有改动/u.test(item)));
});

test('正式发布绑定当前全量评测并要求项目负责人确认覆盖和版本', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Agent 正式发布' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const store = getPackageArtifactStore();
  const draftView = await store.initializeDraftPackage(project.id, {
    id: 'agent-release-package', name: 'Agent 正式发布', description: '发布确认测试包',
  });
  const rc = await store.saveCurrentAsPackageVersion(project.id, {
    expectedRevision: draftView.revision.oid,
    version: '1.0.0-rc.1',
    status: 'review',
    note: '准备正式评测',
    action: {
      cause: 'test_release_candidate', summary: '创建发布候选', actorKind: 'agent', actorName: '测试助手',
    },
  });
  const m3 = await import('../src/workflow/m3-repo.js');
  const testcase = m3.addTestcase(project.id, '真实业务问题', '真实业务答案');
  const round = m3.createRound(project.id, rc.packageVersion, 1, rc.revision.oid);
  m3.addResult(round.id, {
    testcaseId: testcase.id,
    question: testcase.question,
    groundTruth: testcase.groundTruth,
    finalAnswer: testcase.groundTruth,
    verdict: 'pass',
    reason: '回答与已确认口径一致',
  });
  m3.roundProgress(round.id, 1, 1);
  m3.finishRound(round.id, 'done');

  const preflight = await tool(app, project.id, 'release_preflight', {}, runId);
  assert.equal(preflight.statusCode, 200, preflight.body);
  assert.equal(preflight.json().ready, true);
  assert.equal(preflight.json().version, '1.0.0');
  assert.equal(preflight.json().testcase_count, 1);

  const unconfirmed = await tool(app, project.id, 'request_release', {
    summary: '发布正式版本', version: '1.0.0',
  }, runId);
  assert.equal(unconfirmed.statusCode, 409, unconfirmed.body);
  assert.equal(unconfirmed.json().code, 'RELEASE_CONFIRMATION_REQUIRED');

  const released = await tool(app, project.id, 'request_release', {
    summary: '发布正式版本',
    version: '1.0.0',
    review: { approved: true, coverage_confirmed: true, version: '1.0.0' },
  }, runId);
  assert.equal(released.statusCode, 200, released.body);
  assert.equal(released.json().version, '1.0.0');
  assert.equal((await store.current(project.id))?.spec.status, 'released');

  const gate = await tool(app, project.id, 'gate_status', { gate: 'releasable' }, runId);
  assert.equal(gate.statusCode, 200, gate.body);
  assert.equal(gate.json().pass, true);
  const audit = getDb().prepare(`SELECT package_version, summary, actor_name
    FROM artifact_revisions WHERE project_id = ? AND cause = 'agent_release_confirmed'`).get(project.id) as any;
  assert.equal(audit.package_version, '1.0.0');
  assert.match(audit.summary, /已确认 1 条真实用例覆盖/u);
  assert.equal(audit.actor_name, 'alice');

  const edited = await store.saveFile(project.id, {
    path: 'package/index.md',
    content: '# 临时修改\n',
    expectedRevision: released.json().revision,
    action: { cause: 'agent_update', summary: '模拟发布后误改', actorKind: 'agent', actorName: '测试助手' },
  });
  const restored = await store.restore(project.id, {
    targetRevision: released.json().revision,
    expectedRevision: edited.revision.oid,
    action: { cause: 'restore', summary: '恢复正式发布内容', actorKind: 'user', actorName: 'alice' },
  });
  assert.notEqual(restored.revision.oid, released.json().revision);

  const snapshot = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/agent/snapshot`,
  });
  assert.equal(snapshot.statusCode, 200, snapshot.body);
  assert.equal(snapshot.json().revision, restored.revision.oid);

  const restoredManifest = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/agent/file?path=manifest.yaml`,
  });
  assert.equal(restoredManifest.statusCode, 200, restoredManifest.body);
  assert.equal(restoredManifest.json().revision, restored.revision.oid);
  assert.equal(restoredManifest.json().candidate, false);
  assert.match(restoredManifest.json().content, /version: 1\.0\.0/u);
  assert.match(restoredManifest.json().content, /status: released/u);

  const staleDiff = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/agent/diff`,
  });
  assert.equal(staleDiff.statusCode, 200, staleDiff.body);
  assert.deepEqual(staleDiff.json(), { candidateRevision: null, files: [] });

  const restoredRunId = `run-restored-${project.id}`;
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, restoredRunId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  assert.equal(claimed.json().resumed, false);
  assert.equal(claimed.json().supersededChanges, 0);

  const restoredPreflight = await tool(app, project.id, 'release_preflight', {}, restoredRunId);
  assert.equal(restoredPreflight.statusCode, 200, restoredPreflight.body);
  assert.equal(restoredPreflight.json().already_released, true);
  assert.equal(restoredPreflight.json().version, '1.0.0');
  assert.equal(restoredPreflight.json().revision, restored.revision.oid);
  assert.equal(restoredPreflight.json().release_revision, released.json().revision);

  const restoredGate = await tool(app, project.id, 'gate_status', { gate: 'releasable' }, restoredRunId);
  assert.equal(restoredGate.statusCode, 200, restoredGate.body);
  assert.equal(restoredGate.json().pass, true);
});

test('Schema 语义摘要路由返回分页问题而不暴露样例值', async (t) => {
  const project = createProject({ owner: 'alice', name: '合成 Schema 语义路由', runtime: 'agent' });
  const app = appFor(t);
  insertDocument({
    projectId: project.id,
    filename: 'synthetic_schema.xlsx',
    kind: 'excel',
    parseStatus: 'parsed',
    purpose: 'schema',
    tables: [{
      name: 'synthetic_customer',
      rowCount: 1,
      fields: [{
        name: 'customer_id', inferredType: 'string', sampleValues: ['record_0001'], distinctRatio: 1,
      }],
    }],
  });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: `schema-semantic-route-${project.id}`,
    name: '合成 Schema 语义路由',
    description: '合成测试包',
  });
  const summary = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/schema-semantics/summary`,
  });
  assert.equal(summary.statusCode, 200, summary.body);
  assert.equal(summary.json().summary.available, true);
  const items = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/schema-semantics/items?level=error&offset=0&limit=10`,
  });
  assert.equal(items.statusCode, 200, items.body);
  assert.equal(items.json().offset, 0);
  assert.ok(Array.isArray(items.json().items));
  assert.ok(items.json().items.every((item: any) => !('sampleValues' in item)));
});

test('资料检索合并重复位置并限制单次回执大小', async (t) => {
  const project = createProject({ owner: 'alice', name: '合成资料检索边界', runtime: 'agent' });
  const app = appFor(t);
  insertDocument({
    projectId: project.id,
    filename: 'synthetic_material.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: Array.from({ length: 20 }, (_, index) => `客户营销规则${index}：客户需要按区域和行业筛选。`).join('\n'),
  });
  const runId = `run-search-${project.id}`;
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  const response = await tool(app, project.id, 'search_documents', { query: '客户 营销' }, runId);
  assert.equal(response.statusCode, 200, response.body);
  const payload = response.json();
  assert.ok(payload.hits.length <= 8);
  assert.equal(new Set(payload.hits.map((hit: any) => `${hit.filename}:${hit.offset}`)).size, payload.hits.length);
  assert.ok(payload.hits.every((hit: any) => Array.isArray(hit.terms) && hit.terms.length >= 1));
  assert.equal(payload.truncated, true);
});

test('建模目标按结构化分段提交并由核心最终组装', async (t) => {
  const project = createProject({ owner: 'alice', name: '合成结构化目标草稿', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-goal-draft-${project.id}`;
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  const currentFile = await tool(app, project.id, 'read_file', { path: 'manifest.yaml' }, runId);
  assert.equal(currentFile.statusCode, 200, currentFile.body);
  const baseRevision = String(currentFile.json().working_revision ?? currentFile.json().revision ?? '');
  assert.ok(baseRevision);
  const begun = await tool(app, project.id, 'begin_goal_draft', {
    draft_id: 'goal-demo', change_group: 'goal_draft', base_revision: baseRevision, request_id: 'goal-begin-1',
  }, runId);
  assert.equal(begun.statusCode, 200, begun.body);
  for (const [section, summary] of [
    ['objective', '识别营销机会'],
    ['scope', '覆盖企业客户营销场景'],
    ['constraints', '必须保留人工确认'],
    ['outputs', '输出可追溯的营销建议'],
  ] as const) {
    const response = await tool(app, project.id, 'put_goal_section', {
      draft_id: 'goal-demo', section,
      base_revision: baseRevision, request_id: `goal-section-${section}`,
      data: section === 'objective'
        ? { problem: summary, target_users: '合成用户', success_criteria: '可追溯', evidence: ['user-decision:synthetic'] }
        : section === 'scope'
          ? { included: [summary], excluded: [], evidence: ['user-decision:synthetic'] }
          : { items: [summary], evidence: ['user-decision:synthetic'] },
    }, runId);
    assert.equal(response.statusCode, 200, response.body);
  }
  const finalized = await tool(app, project.id, 'finalize_goal_draft', { draft_id: 'goal-demo', base_revision: baseRevision, request_id: 'goal-finalize-1' }, runId);
  assert.equal(finalized.statusCode, 200, finalized.body);
  const goal = await tool(app, project.id, 'read_file', { path: '.modeling/goal.md' }, runId);
  assert.equal(goal.statusCode, 200, goal.body);
  assert.match(goal.json().content, /## 目标/u);
  assert.match(goal.json().content, /## 范围/u);
  assert.match(goal.json().content, /识别营销机会/u);
});

test('create_* 动作省略 key 时按计划补齐，写错 key 则报错并给出计划登记的目标', async (t) => {
  const project = createProject({ owner: 'alice', name: '计划目标标识对齐', runtime: 'agent' });
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: 'plan-target-package',
    name: '计划目标标识对齐',
    description: '验证 create_* 动作与计划目标标识的对齐方式。',
  });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);

  const listed = await tool(app, project.id, 'list_domain_resources', { kind: 'rule' }, runId);
  assert.equal(listed.statusCode, 200, listed.body);
  const planResponse = await tool(app, project.id, 'create_change_plan', {
    goal: '登记一条融资规则',
    mode: 'generation',
    expected_revision: listed.json().working_revision,
    tasks: [{
      client_key: 'procurement-rule',
      title: '政府采购订单融资规则',
      resource_kind: 'rule',
      target_key: 'government-procurement-order-financing',
      allowed_tools: ['create_rule'],
      acceptance: '写入融资比例与金额阈值',
    }],
  }, runId);
  assert.equal(planResponse.statusCode, 200, planResponse.body);
  const plan = planResponse.json().plan;
  const approved = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/agent/change-plans/${plan.id}/respond`,
    payload: { approved: true },
  });
  assert.equal(approved.statusCode, 200, approved.body);

  // 模型写了另一个 key：仍然拒绝，但必须把计划登记的目标交回去，让模型能自行改正。
  const mismatched = await tool(app, project.id, 'stage_change_set', {
    plan_id: plan.id,
    expected_revision: listed.json().working_revision,
    mutation_id: 'plan-target-mismatch',
    actions: [{
      task_id: plan.tasks[0].id,
      tool: 'create_rule',
      arguments: { key: 'gov-procurement-financing' },
    }],
  }, runId);
  assert.equal(mismatched.statusCode, 409, mismatched.body);
  assert.equal(mismatched.json().code, 'CHANGE_SET_OUT_OF_SCOPE');
  assert.equal(mismatched.json().expected_target, 'government-procurement-order-financing');
  assert.ok(
    mismatched.json().next.some((step: string) => step.includes('government-procurement-order-financing')),
    `恢复提示没有给出计划登记的目标：${mismatched.body}`,
  );

  // 省略 key：按计划补齐，不再要求模型跨轮次复述自己写下的字符串。
  const omitted = await tool(app, project.id, 'stage_change_set', {
    plan_id: plan.id,
    expected_revision: listed.json().working_revision,
    mutation_id: 'plan-target-filled',
    actions: [{
      task_id: plan.tasks[0].id,
      tool: 'create_rule',
      arguments: {},
    }],
  }, runId);
  assert.notEqual(omitted.json().code, 'CHANGE_SET_OUT_OF_SCOPE', omitted.body);
});

test('计划 target_key 剥掉资源类型前缀，避免生成带冒号的文件名', () => {
  // 模型常把 resource_id 的 `<kind>:<key>` 写法搬进计划的 target_key；
  // 不归一化就会落成 rules/rule:xxx.md（Windows 上非法）。
  assert.equal(normalizePlanTargetKey('rule', 'rule:marketing-decision-ipo'), 'marketing-decision-ipo');
  assert.equal(normalizePlanTargetKey('metric', 'metric:customer-relationship-rating'), 'customer-relationship-rating');
  assert.equal(normalizePlanTargetKey('operation', 'OPERATION:customer-intelligence-query'), 'customer-intelligence-query');
  // 裸 key 原样保留
  assert.equal(normalizePlanTargetKey('rule', 'marketing-decision-ipo'), 'marketing-decision-ipo');
  // 不同类型的前缀不误剥
  assert.equal(normalizePlanTargetKey('rule', 'metric:customer-relationship-rating'), 'metric:customer-relationship-rating');
  // 只有前缀时保留原值，不产生空 key
  assert.equal(normalizePlanTargetKey('rule', 'rule:'), 'rule:');
});

test('长任务进度文案不把「单步耗时长」说成「没有进度」', () => {
  // 一个分块或一次汇总本身就可能跑好几分钟，期间 run.updated_at 不动是正常的。
  // 判据必须区分「还有在途工作」和「真的停了」，否则进度条在走、文案却报停滞。
  const source = readFileSync('src/agent/core-service.ts', 'utf8');
  assert.match(source, /const working = chunks\.some\(\(chunk\) => chunk\.status === 'running'\)/);
  assert.match(source, /run\.stage === 'document_reduce'/);
  assert.match(source, /idleMs > \(working \? 300_000 : 120_000\)/);
  assert.match(source, /working \? '这一步耗时较长，仍在处理' : '较久没有新进度'/);
  assert.doesNotMatch(source, /较久未收到新进度/);
});

test('目标分段校验失败时说明其它分段不受影响，且点破锚点不能带空格', () => {
  // 分段各自独立存储，一段失败不该让助手把 5 段全重交一遍。
  const source = readFileSync('src/agent/core-service.ts', 'utf8');
  assert.match(source, /function goalSectionRetryHint\(sectionsJson: unknown, section: string\): string/);
  assert.match(source, /已保存的分段（\$\{saved\.join\('、'\)\}）不受影响，只需重新提交 \$\{section\} 这一段/);
  assert.match(source, /只重新提交 \$\{section\} 这一段即可，其它分段不受影响/);
  // 六个分段级校验错误都要带上这条提示
  for (const code of [
    'GOAL_SECTION_UNKNOWN_FIELD', 'GOAL_SECTION_TOO_LONG', 'GOAL_SECTION_TOO_LARGE',
    'GOAL_SECTION_EMPTY', 'GOAL_SECTION_EVIDENCE_REQUIRED', 'GOAL_SECTION_EVIDENCE_INVALID',
  ]) {
    const index = source.indexOf(code);
    assert.ok(index > 0, `缺少 ${code}`);
    assert.match(source.slice(index, index + 900), /goalSectionRetryHint\(row\.sections_json, section\)/, code);
  }
  // 证据格式错误要点破真正的坑：锚点里不能有空格
  assert.match(source, /锚点内不能出现空格，也不要在锚点后追加中文说明/);
});

test('表头第二行的中文名直接种进 cnName，不再全量触发语义阻断', () => {
  // 国网汇总表 9 个 Sheet、361 个字段的中文名都写在第二行；解析器已识别为
  // 注释行，但 cnName 回退到技术名，导致 361 项语义阻断，再让模型把文件里
  // 已有的名字逐个重新生成一遍。
  const source = readFileSync('src/workflow/schema-manifest.ts', 'utf8');
  assert.match(source, /function fieldNameFromNote\(note: unknown\): string/);
  assert.match(source, /cnName: semantic\?\.businessName \|\| noteName \|\| field\.name/);
  // 采纳为名称后不再重复写进 note
  assert.match(source, /const usedNoteAsName = !semantic\?\.businessName && Boolean\(noteName\)/);
  // 只认「像名称」的注释：短、无句读、含中文
  assert.match(source, /value\.length > 20/);
  assert.match(source, /\[。；;，,：:！!？\?\\n\]/u);
  assert.match(source, /\\u4e00-\\u9fff/);
});

test('资料按章节读取，分页终点吸附到块边界不切断表格', async (t) => {
  const project = createProject({ owner: 'alice', name: '资料章节读取' });
  const table = [
    '| 产品 | 收率 |',
    '|------|------|',
    '| 航煤 | 8.81% |',
    '| 燃料油DMB | 14.81% |',
    '| 燃料油DMX | 14.51% |',
    '| 加氢尾油 | 32.43% |',
  ].join('\n');
  const textContent = [
    '# 全流程业务判决逻辑',
    '',
    '总纲部分，描述原油进厂到终产品的整体链路。'.repeat(4),
    '',
    '## 产品线7：减压渣油路线',
    '',
    '减压渣油走 DCC 与溶脱两条支线。'.repeat(4),
    '',
    table,
    '',
    '## 8.2 能源成本计算',
    '',
    'energy 表关键字段说明。'.repeat(4),
    '',
  ].join('\n');
  const document = insertDocumentRevision({
    projectId: project.id,
    filename: '业务流程梳理.md',
    kind: 'text',
    parseStatus: 'parsed',
    textContent,
  }).document;
  const app = appFor(t);
  const runId = `run-${project.id}`;

  const outline = await tool(app, project.id, 'list_document_outline', {
    filename: document.filename,
  }, runId);
  assert.equal(outline.statusCode, 200, outline.body);
  assert.deepEqual(outline.json().sections.map((item: any) => item.title), [
    '全流程业务判决逻辑', '产品线7：减压渣油路线', '8.2 能源成本计算',
  ]);
  assert.equal(outline.json().sections[0].offset, 0);
  assert.equal(outline.json().totalChars, textContent.length);

  // 按章节取：正好是那一节，不多不少
  const section = await tool(app, project.id, 'read_document', {
    filename: document.filename, section: '产品线7：减压渣油路线',
  }, runId);
  assert.equal(section.statusCode, 200, section.body);
  assert.ok(section.json().content.startsWith('## 产品线7'));
  assert.ok(section.json().content.includes('| 加氢尾油 | 32.43% |'), '整张表都要在这一节里');
  assert.ok(!section.json().content.includes('8.2 能源成本计算'), '不能串到下一节');
  assert.equal(section.json().section.title, '产品线7：减压渣油路线');

  // 序号取法等价
  const byIndex = await tool(app, project.id, 'read_document', {
    filename: document.filename, section: 1,
  }, runId);
  assert.equal(byIndex.json().content, section.json().content);

  // 分页：终点落在表格中间时要吸附到块边界，不能把表劈开
  const tableStart = textContent.indexOf('| 产品 | 收率 |');
  const cut = await tool(app, project.id, 'read_document', {
    filename: document.filename, offset: tableStart, limit: 40,
  }, runId);
  assert.equal(cut.statusCode, 200, cut.body);
  const content = cut.json().content;
  assert.equal(cut.json().boundarySnapped, true);
  const openedTable = content.includes('| 产品 | 收率 |');
  assert.ok(openedTable, '这一页应当从表头开始');
  assert.ok(content.includes('| 加氢尾油 | 32.43% |'), '开了表头就要把整张表给完，不能只给前两行');
  assert.equal(cut.json().section.title, '产品线7：减压渣油路线', '要告诉模型这一页属于哪一节');

  const missing = await tool(app, project.id, 'read_document', {
    filename: document.filename, section: '不存在的章节',
  }, runId);
  assert.equal(missing.statusCode, 404, missing.body);
  assert.equal(missing.json().code, 'DOCUMENT_SECTION_NOT_FOUND');
});

test('增量影响门禁只在有正式基线和语义影响记录时出现，并按进度派活', async (t) => {
  const artifacts = getPackageArtifactStore();
  const project = createProject({ owner: 'alice', name: '增量影响门禁', runtime: 'agent' });
  const initial = await artifacts.initializeDraftPackage(project.id, {
    id: `baseline-${project.id}`, name: project.name, description: '已发布基线。',
  });
  await artifacts.saveFiles(project.id, {
    expectedRevision: initial.revision.oid,
    files: { 'package/objects/customer.md': '---\nkey: customer\n---\n\n# 客户\n\n基线内容。\n' },
    action: {
      cause: 'package_release', summary: '建立正式基线', actorKind: 'user', actorName: 'alice',
    },
  });
  const app = appFor(t);
  const runId = `increment-gate-${project.id}`;

  // 没有资料影响记录时这道门禁不该出现，普通建模流程不多一条视觉闸位
  const absent = await tool(app, project.id, 'gate_status', {
    gate: 'increment_resolved',
  }, runId);
  assert.equal(absent.statusCode, 400, absent.body);
  assert.equal(absent.json().code, 'GATE_NOT_FOUND');

  storeMaterialImpact(getDb(), project.id, {
    sourceKey: '结算口径说明',
    kind: 'replaced',
    semanticChanged: true,
    previous: { id: 'doc_prev', filename: '结算口径说明.md' },
    current: { id: 'doc_now', filename: '结算口径说明.md' },
  } as any, {
    semanticChanged: true, scopeResolved: false, affectedResources: [], staleWorkflowSteps: [],
  });

  const unscoped = await tool(app, project.id, 'gate_status', {
    gate: 'increment_resolved',
  }, runId);
  assert.equal(unscoped.statusCode, 200, unscoped.body);
  assert.equal(unscoped.json().pass, false);
  // 增量阶段只看这一道门禁，判成「助手无事可做」等于本轮拿不到指派目标
  assert.equal(unscoped.json().agentActionable.length, 1);
  assert.match(unscoped.json().agentActionable[0], /analyze_material_impact/u);
});

/** 种一份已完成的资料分析运行，用来驱动候选分页和产品事实来源的路由测试。 */
function seedMaterialAnalysis(projectId: string, runId: string, analysis: Record<string, unknown>,
  options: { sourceSetFingerprint?: string; completedAt?: string } = {}) {
  const timestamp = options.completedAt ?? new Date().toISOString();
  getDb().prepare(`INSERT INTO material_analysis_runs (
    id, project_id, source_set_fingerprint, brief_fingerprint, model_id,
    protocol_version, plan_json, status, stage, result_json, created_at, updated_at, completed_at
  ) VALUES (?,?,?,?,?,?,?,'completed','completed',?,?,?,?)`).run(
    runId, projectId, options.sourceSetFingerprint ?? `sources-${runId}`, 'brief-synthetic',
    'synthetic-model', 'facts-inventory-v3', JSON.stringify({ totalChars: 0 }),
    JSON.stringify(analysis), timestamp, timestamp, timestamp,
  );
  return runId;
}

function inventoryItem(index: number, overrides: Record<string, unknown> = {}) {
  return {
    completenessContract: 'source-fields-v1',
    canonicalItemId: `product-${index}`,
    name: `合成产品 ${index}`,
    ruleRefs: [`rule-${index}`],
    sourceRecordIds: [`source-${index}`],
    sourceLocator: `manual#${index}`,
    evidenceStatus: 'complete',
    processingStatus: 'ready',
    applicableSubjects: ['合成主体'],
    triggerScenarios: ['合成场景'],
    businessNeeds: ['合成需求'],
    admissionConditions: ['合成准入条件'],
    exclusionConditions: ['合成排除条件'],
    structuredGaps: [],
    ...overrides,
  };
}

test('目标上下文如实自报候选被截断，完整候选集由分页工具取回', async (t) => {
  const project = createProject({ owner: 'alice', name: '候选分页读取', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const candidateArtifacts = Array.from({ length: 45 }, (_, index) => ({
    kind: index % 3 === 0 ? 'object' : 'rule',
    name: `候选 ${index}`,
    rationale: '合成依据',
    evidence: [`manual#${index}`],
    sources: ['manual.md'],
  }));
  seedMaterialAnalysis(project.id, `analysis-paging-${project.id}`, {
    domainOverview: '合成领域概览', candidateArtifacts, openQuestions: [], coverageComplete: true,
  });

  const context = await tool(app, project.id, 'get_goal_context', {}, runId);
  assert.equal(context.statusCode, 200, context.body);
  // 只给前 40 条却不说还有多少，调用方会把"看到的"当成"全部的"。
  assert.equal(context.json().candidates.length, 40);
  assert.equal(context.json().candidateTotal, 45);
  assert.equal(context.json().candidatesTruncated, true);
  assert.equal(context.json().candidatePagingTool, 'get_analysis_candidates');

  const collected: string[] = [];
  let cursor: number | null = 0;
  let pages = 0;
  while (cursor !== null) {
    const page = await tool(app, project.id, 'get_analysis_candidates', { cursor, limit: 20 }, runId);
    assert.equal(page.statusCode, 200, page.body);
    assert.equal(page.json().total, 45);
    assert.equal(page.json().cursor, cursor);
    collected.push(...page.json().items.map((item: any) => item.name));
    cursor = page.json().nextCursor;
    assert.equal(page.json().hasMore, cursor !== null);
    pages += 1;
  }
  assert.equal(pages, 3);
  // 分页不得漏项也不得重复，否则"逐条盘点"本身就不成立。
  assert.deepEqual(collected, candidateArtifacts.map((item) => item.name));

  const filtered = await tool(app, project.id, 'get_analysis_candidates', { kind: 'object', limit: 100 }, runId);
  assert.equal(filtered.json().total, 15);
  assert.equal(filtered.json().kind, 'object');
  assert.ok(filtered.json().items.every((item: any) => item.kind === 'object'));
  assert.equal(filtered.json().nextCursor, null);

  const overLimit = await tool(app, project.id, 'get_analysis_candidates', { limit: 500 }, runId);
  assert.equal(overLimit.json().items.length, 45, 'limit 超出上限应收敛到 100 而不是报错');

  const invalid = await tool(app, project.id, 'get_analysis_candidates', { cursor: 999 }, runId);
  assert.equal(invalid.statusCode, 400, invalid.body);
  assert.equal(invalid.json().code, 'ANALYSIS_CANDIDATE_CURSOR_INVALID');
});

test('只含摘要的新分析不得顶掉已有的结构化产品事实来源', async (t) => {
  const project = createProject({ owner: 'alice', name: '产品事实来源绑定', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const withInventory = seedMaterialAnalysis(project.id, `analysis-inventory-${project.id}`, {
    domainOverview: '含产品事实的分析', candidateArtifacts: [],
    productInventory: [inventoryItem(1), inventoryItem(2)],
    productInventorySummary: {
      rawCount: 2, canonicalCount: 2, mergedGroupCount: 0, completeCount: 2, insufficientCount: 0,
    },
  }, { completedAt: '2026-09-01T00:00:00.000Z' });
  const summaryOnly = seedMaterialAnalysis(project.id, `analysis-summary-${project.id}`, {
    domainOverview: '精读汇总，没有逐项产品事实', candidateArtifacts: [], openQuestions: [],
  }, { completedAt: '2026-09-02T00:00:00.000Z' });

  // 精读运行更新，但它没有产品事实；按"最新完成"取来源会让合格来源永远取不到。
  const source = await tool(app, project.id, 'get_product_inventory_source', {}, runId);
  assert.equal(source.statusCode, 200, source.body);
  assert.equal(source.json().analysisRevision, withInventory);
  assert.equal(source.json().total, 2);
  assert.deepEqual(source.json().sourceSummary, {
    rawCount: 2, canonicalCount: 2, mergedGroupCount: 0, completeCount: 2, insufficientCount: 0,
  });
  assert.equal(source.json().sourceFingerprint, `sources-${withInventory}`);

  const conflict = await tool(app, project.id, 'get_product_inventory_source', {
    analysis_revision: summaryOnly,
  }, runId);
  assert.equal(conflict.statusCode, 409, conflict.body);
  assert.equal(conflict.json().code, 'PRODUCT_INVENTORY_REVISION_CONFLICT');
  assert.equal(conflict.json().currentAnalysisRevision, withInventory);
  assert.equal(conflict.json().expectedAnalysisRevision, summaryOnly);
});

test('来源申报的完整性计数与逐项事实不符时整页作废，而不是照单放行', async (t) => {
  const project = createProject({ owner: 'alice', name: '产品事实来源计数闭合', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  seedMaterialAnalysis(project.id, `analysis-count-${project.id}`, {
    domainOverview: '计数不闭合的分析', candidateArtifacts: [],
    productInventory: [
      inventoryItem(1),
      inventoryItem(2, { evidenceStatus: 'insufficient', processingStatus: 'blocked', insufficiencyReason: '原文缺少准入条件' }),
    ],
    // 条数对得上、完整性对不上：这种来源一旦放行就会产出假的"全部完整"结论。
    productInventorySummary: {
      rawCount: 2, canonicalCount: 2, mergedGroupCount: 0, completeCount: 2, insufficientCount: 0,
    },
  });

  const source = await tool(app, project.id, 'get_product_inventory_source', {}, runId);
  assert.equal(source.statusCode, 409, source.body);
  assert.equal(source.json().code, 'PRODUCT_INVENTORY_SOURCE_UNAVAILABLE');
});

test('台账草稿记下来源资料集指纹，来源被换掉后收尾如实报冲突', async (t) => {
  const project = createProject({ owner: 'alice', name: '台账来源指纹漂移', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);
  const revision = claimed.json().baseRevision;
  // 门禁要求启用时来源已存在，所以先种分析再 require_product_ledger。
  const analysisRevision = seedMaterialAnalysis(project.id, `analysis-drift-${project.id}`, {
    domainOverview: '含产品事实的分析', candidateArtifacts: [],
    productInventory: [inventoryItem(1)],
    productInventorySummary: {
      rawCount: 1, canonicalCount: 1, mergedGroupCount: 0, completeCount: 1, insufficientCount: 0,
    },
  }, { sourceSetFingerprint: 'fingerprint-before' });
  const required = await tool(app, project.id, 'require_product_ledger', { required: true }, runId);
  assert.equal(required.statusCode, 200, required.body);

  const begun = await tool(app, project.id, 'begin_product_ledger', {
    ledger_id: 'ledger-drift', source_revision: revision, analysis_revision: analysisRevision,
  }, runId);
  assert.equal(begun.statusCode, 200, begun.body);
  assert.equal(begun.json().analysisRevision, analysisRevision);
  assert.equal(begun.json().sourceFingerprint, 'fingerprint-before');
  assert.equal(
    (getDb().prepare('SELECT source_fingerprint FROM product_ledger_drafts WHERE project_id = ? AND ledger_id = ?')
      .get(project.id, 'ledger-drift') as any)?.source_fingerprint,
    'fingerprint-before',
    '草稿必须落盘建立时的来源指纹，否则无从证明对的是同一批资料',
  );

  const appended = await tool(app, project.id, 'append_product_ledger_batch', {
    ledger_id: 'ledger-drift',
    batch_no: 1,
    entries: [{
      canonicalItemId: 'product-1', name: '合成产品 1', ruleRefs: ['rule-1'],
      sourceRecordIds: ['source-1'], sourceLocator: 'manual#1',
      evidenceStatus: 'complete', processingStatus: 'ready',
    }],
  }, runId);
  assert.equal(appended.statusCode, 200, appended.body);

  // 换掉来源资料集：条数仍然凑得上，只有指纹能证明这不是同一批资料。
  getDb().prepare('UPDATE material_analysis_runs SET source_set_fingerprint = ? WHERE id = ?')
    .run('fingerprint-after', analysisRevision);

  const finalized = await tool(app, project.id, 'finalize_product_ledger', {
    ledger_id: 'ledger-drift',
  }, runId);
  assert.equal(finalized.statusCode, 200, finalized.body);
  assert.equal(finalized.json().status, 'needs_review');
  assert.equal(finalized.json().validation.pass, false);
  assert.equal(finalized.json().validation.blocking, true);
  assert.deepEqual(finalized.json().validation.errors, ['来源资料集指纹已变化，台账必须基于当前分析重新建立'],
    '除指纹漂移外其余闭合等式都应成立，错误必须精确指向漂移');
  assert.deepEqual(finalized.json().validation.counts, {
    raw: 1, canonical: 1, mergedGroups: 0, complete: 1, insufficient: 0,
  });
});

test('enforce 模式下被拒绝的台账不落库，读取方看到的是"没有台账"而不是"有台账"', async (t) => {
  const project = createProject({ owner: 'alice', name: '台账先判后写', runtime: 'agent' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  const previousMode = config.productLedger.mode;
  t.after(() => { config.productLedger.mode = previousMode; });
  config.productLedger.mode = 'enforce';
  const claimed = await tool(app, project.id, 'claim_candidate_workspace', {}, runId);
  assert.equal(claimed.statusCode, 200, claimed.body);

  const rejected = await tool(app, project.id, 'stage_product_ledger', {
    scope: 'product_catalog', rawCount: 2, canonicalCount: 1, mergedGroupCount: 1, entries: [],
  }, runId);
  assert.equal(rejected.statusCode, 409, rejected.body);
  assert.equal(rejected.json().code, 'PRODUCT_LEDGER_INVALID');

  assert.equal(
    getDb().prepare('SELECT source_revision FROM product_ledger_snapshots WHERE project_id = ?').get(project.id),
    undefined,
    '先写再抛会把被拒绝的快照留在库里',
  );
  assert.equal(
    getDb().prepare('SELECT source_revision FROM product_ledger_v2_snapshots WHERE project_id = ?').get(project.id),
    undefined,
  );
  const read = await tool(app, project.id, 'get_product_ledger', {}, runId);
  assert.equal(read.statusCode, 200, read.body);
  assert.equal(read.json().available, false);
});

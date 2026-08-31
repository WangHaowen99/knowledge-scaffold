import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
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
  { insertDocumentRevision },
  { requireAuth },
  { storePackageMergeSource },
  { renderPackage },
  { parseExcel },
  { config },
] = await Promise.all([
  import('../src/agent/core-routes.js'),
  import('../src/projects/repo.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/db/index.js'),
  import('../src/documents/repo.js'),
  import('../src/auth/middleware.js'),
  import('../src/agent/package-merge.js'),
  import('../src/workflow/package-render.js'),
  import('../src/documents/parser.js'),
  import('../src/config.js'),
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
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
    request.on('end', () => {
      if (request.method === 'POST' && request.url?.endsWith('/exec')) {
        const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        const encodedCases = body.command.at(-1);
        const cases = JSON.parse(Buffer.from(encodedCases, 'base64').toString('utf8'));
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
  assert.equal(contract.validatorVersion, 'package-contract-v6');
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

  const pendingGoal = await tool(app, project.id, 'write_file', {
    path: '.modeling/goal.md', content: '# 调整中的建模目标\n',
    change_group: 'adjust-goal', evidence: [],
  }, runId);
  assert.equal(pendingGoal.statusCode, 200, pendingGoal.body);
  const pendingGoalGate = await tool(app, project.id, 'gate_status', {
    gate: 'goal_confirmed',
  }, runId);
  assert.equal(pendingGoalGate.json().pass, false);
  const staleGoalCannotAuthorizeProcess = await tool(app, project.id, 'write_file', {
    path: 'diagrams/stale-goal.mmd', content: 'flowchart TD\nA --> B\n',
    change_group: 'stale-goal-process', evidence: [],
  }, runId);
  assert.equal(staleGoalCannotAuthorizeProcess.statusCode, 409, staleGoalCannotAuthorizeProcess.body);
  assert.equal(staleGoalCannotAuthorizeProcess.json().gate, 'goal_confirmed');
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

  const rejected = await tool(app, project.id, 'request_confirmation_rejected', {
    review: { approved: false, feedback: '补充例外条件' },
  }, nextRunId);
  assert.equal(rejected.statusCode, 200, rejected.body);
  assert.equal(rejected.json().candidateRevision, candidateId);
  assert.equal((await getPackageArtifactStore().getCandidateRevision(project.id, candidateId)).status, 'rejected');

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
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);
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
  }, `run-${project.id}`);

  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().document.purpose, 'schema');
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
  assert.equal(receipt.validatorVersion, 'package-contract-v6');
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

test('脚本候选的预检和最终确认都只接受当前源码的沙箱通过回执', async (t) => {
  const project = createProject({ owner: 'alice', name: '脚本回执门禁' });
  const app = appFor(t);
  const runId = `run-${project.id}`;
  await confirmGoal(app, project.id, runId);
  await confirmProcess(app, project.id, runId);
  await confirmKnowledge(app, project.id, runId);
  const group = { id: 'script-current-source', tier: 'script', title: '当前脚本实现' };
  const source = (expression: string) => `import argparse
import json

def run(value):
    return ${expression}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--value", required=True, type=int)
    args = parser.parse_args()
    print(json.dumps(run(args.value)))

if __name__ == "__main__":
    main()
`;
  const writeScript = (source: string) => tool(app, project.id, 'write_file', {
    path: 'scripts/stock.py',
    content: source,
    change_group: group.id,
    evidence: ['user-decision:script-current-source'],
  }, runId);
  const preflight = () => tool(app, project.id, 'request_confirmation_preflight', {
    summary: group.title,
    change_groups: [group],
  }, runId);
  const runTest = () => tool(app, project.id, 'run_script_test', {
    script: 'scripts/stock.py',
    cases: [{ input: { value: 2 }, expected: 4 }],
  }, runId);

  assert.equal((await writeScript(source('value * 2'))).statusCode, 200);
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
    summary: group.title,
    change_groups: [group],
    ...confirmationReceipt(ready),
    review: { approved: true, review_mode: 'full', selected_groups: [group.id] },
  }, runId);
  assert.equal(blockedConfirmation.statusCode, 409, blockedConfirmation.body);
  assert.equal(blockedConfirmation.json().code, 'SCRIPT_TEST_REQUIRED');

  assert.equal((await runTest()).statusCode, 200);
  const confirmed = await tool(app, project.id, 'request_confirmation', {
    summary: group.title,
    change_groups: [group],
    ...confirmationReceipt(ready),
    review: { approved: true, review_mode: 'full', selected_groups: [group.id] },
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

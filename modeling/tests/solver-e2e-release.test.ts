import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import multipart from '@fastify/multipart';
import Fastify from 'fastify';

import type { ConsumerRequest } from '../src/evaluation/shadow-workspace.js';
import {
  authoritativeResult,
  batches,
  fakeConsumer,
  infeasibleResult,
  json,
  multipartBody,
  packageSpec,
  solverCase,
  solverToolEvents,
  VERSION,
  type AgentAsk,
  type AgentScript,
} from './helpers/solver-consumer-fake.js';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-solver-e2e-'));

const [
  { registerSolverContractValidationRoutes },
  { runSolverContractValidation },
  { createProject },
  { saveProjectDataSource },
  { shadowWorkspaceId },
  { zipPackage },
  { renderPackage },
] = await Promise.all([
  import('../src/evaluation/solver-contract-validation-routes.js'),
  import('../src/evaluation/solver-contract-validation.js'),
  import('../src/projects/repo.js'),
  import('../src/evaluation/data-source-repo.js'),
  import('../src/evaluation/shadow-workspace.js'),
  import('../src/workflow/export.js'),
  import('../src/workflow/package-render.js'),
]);

// 发布前端到端验证（mw 一侧）：onto-modeling 调验证路由 → 编排 → 合成 Consumer（Agent 运行、
// 事件流、候选结果）→ 报告。报告里的 authoritative、证据字段与 candidate_count 是 onto-modeling
// 发布门禁的判定输入；门禁判定见 onto-modeling/backend/tests/test_solver_validation_run.py 的同名场景。
// 知识包、批次、工作空间与引用全部为测试虚构。

// 与 onto-modeling 门禁要求的权威证据一致（solver_validation_run._REQUIRED_EVIDENCE + 候选成对字段）
const REQUIRED_EVIDENCE = [
  'feasibility',
  'job_id',
  'result_artifact_ref',
  'verification_evidence_ref',
  'knowledge_revision_id',
  'fact_snapshot_digest',
  'problem_digest',
];
const CANDIDATE_EVIDENCE = ['candidate_results_artifact_ref', 'candidate_results_artifact_digest'];
const ACCESS_TOKEN = 'synthetic-access';

type Scripted = (ask: AgentAsk) => AgentScript | undefined;

function toolScript(ask: AgentAsk, result: Record<string, unknown>, extra: Record<string, unknown> = {}): AgentScript {
  return {
    events: solverToolEvents({ operationKey: ask.operationKey, arguments: ask.arguments, result, ...extra }),
  };
}

function toolError(code: string, phase: string, retryable: boolean, message: string) {
  return { status: 'error', code, phase, retryable, message };
}

/** 每个场景一个独立项目与影子工作空间；request 用于模拟整个 Consumer 不可达。 */
async function releaseValidation(
  cases: ReturnType<typeof solverCase>[],
  options: Parameters<typeof fakeConsumer>[0] & { request?: ConsumerRequest } = {},
) {
  const project = createProject({ owner: 'alice', name: '合成发布验证项目', kind: 'package' });
  const workspaceId = shadowWorkspaceId(project.id);
  saveProjectDataSource({ projectId: project.id, mode: 'none', workspaceId });
  const consumer = fakeConsumer({ ...options, workspaceId });
  const app = Fastify();
  await app.register(multipart);
  app.addHook('preHandler', async (req) => {
    req.user = { username: 'alice', role: 'user' } as any;
  });
  registerSolverContractValidationRoutes(app, {
    isEvaluationActive: () => false,
    authSessions: { get: () => undefined },
    run: (input) => runSolverContractValidation({ ...input, request: options.request ?? consumer.request }),
  });
  await app.ready();
  try {
    const body = multipartBody(
      { run_id: 'release1', cases: JSON.stringify(cases) },
      zipPackage(renderPackage(packageSpec())),
    );
    const response = await app.inject({
      method: 'POST',
      url: `/api/projects/${project.id}/solver-contract-validations`,
      headers: { 'content-type': body.contentType, authorization: `Bearer ${ACCESS_TOKEN}` },
      payload: body.payload,
    });
    assert.doesNotMatch(response.body, new RegExp(ACCESS_TOKEN), '报告不回传访问令牌');
    return { response, consumer, workspaceId, cases: response.json().validation?.cases ?? [] };
  } finally {
    await app.close();
  }
}

function byCase(script: Record<string, Scripted>): Scripted {
  return (ask) => script[ask.idempotencyKey.split(':')[1]]?.(ask);
}

const candidateQueries = (calls: { path: string }[]) => calls.filter((call) => call.path.includes('candidate-results'));

test('7 个批次得到 14 个候选、9 个批次得到 18 个候选，权威证据齐全', async () => {
  const { response, consumer, workspaceId, cases } = await releaseValidation([
    solverCase('n7', batches(7)),
    solverCase('n9', batches(9)),
  ]);
  assert.equal(response.statusCode, 200, response.body);
  assert.deepEqual(
    cases.map((item: any) => [item.id, item.status, item.authoritative, item.candidate_count]),
    [['n7', 'completed', true, 14], ['n9', 'completed', true, 18]],
  );
  for (const item of cases) {
    assert.equal(item.solver_tool_called, true);
    assert.equal(item.arguments_match, true);
    assert.equal(item.knowledge_revision_id, VERSION);
    for (const key of [...REQUIRED_EVIDENCE, ...CANDIDATE_EVIDENCE]) assert.ok(item[key], `${item.id} 缺少 ${key}`);
  }
  const runs = consumer.calls.filter((call) => call.path === '/agentic/runs');
  assert.ok(runs.every((call) => call.workspaceId === workspaceId), '在项目影子工作空间里求解');
  assert.deepEqual(runs.map((call) => call.headers['Idempotency-Key']), ['release1:n7', 'release1:n9']);
});

test('全不可行是有效的权威终态：有候选时按候选计数，没有候选时不读候选', async () => {
  const { response, consumer, cases } = await releaseValidation([
    solverCase('i3', batches(3)),
    solverCase('i0', batches(3)),
  ], {
    agent: byCase({
      i3: (ask) => toolScript(ask, infeasibleResult('job-i3')),
      i0: (ask) => toolScript(ask, infeasibleResult('job-i0', {
        candidate_results_artifact_ref: undefined,
        candidate_results_artifact_digest: undefined,
      })),
    }),
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.deepEqual(
    cases.map((item: any) => [item.id, item.status, item.feasibility, item.authoritative, item.candidate_count, item.code]),
    [
      ['i3', 'completed', 'infeasible', true, 6, undefined],
      ['i0', 'completed', 'infeasible', true, null, undefined],
    ],
  );
  for (const key of REQUIRED_EVIDENCE) assert.ok(cases[1][key], `不可行结果仍需 ${key}`);
  assert.equal('candidate_results_artifact_ref' in cases[1], false);
  assert.deepEqual(candidateQueries(consumer.calls).map((call) => call.path), [
    '/api/solver/history/job-i3/candidate-results',
  ]);
});

test('非权威结果、缺少或对不上权威结果事件的结果都标为非权威，不能作为通过依据', async () => {
  const { response, consumer, cases } = await releaseValidation([
    solverCase('weak', batches(2)),
    solverCase('missing', batches(2)),
    solverCase('forged', batches(2)),
  ], {
    agent: byCase({
      weak: (ask) => toolScript(ask, authoritativeResult('job-weak', { authoritative: false })),
      missing: (ask) => toolScript(ask, authoritativeResult('job-missing'), { authoritativeEvent: false }),
      forged: (ask) => toolScript(ask, authoritativeResult('job-forged'), {
        authoritativeEvent: { result_artifact_ref: 'artifact://result/job-other' },
      }),
    }),
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.deepEqual(cases.map((item: any) => [item.id, item.status, item.authoritative, item.code]), [
    ['weak', 'completed', false, undefined],
    ['missing', 'completed', false, 'solver_authoritative_event_missing'],
    ['forged', 'completed', false, 'solver_authoritative_event_mismatch'],
  ]);
  assert.ok(cases.every((item: any) => item.candidate_count === null));
  assert.equal(candidateQueries(consumer.calls).length, 0);
});

test('Consumer 不可用：整体不可达时报 502，求解器不可用时按用例回传可重试错误', async () => {
  const down = await releaseValidation([solverCase('n1', batches(1))], {
    request: async () => {
      throw new Error('Consumer 请求失败：fetch failed（ECONNREFUSED：connect 192.0.2.10:8000）');
    },
  });
  assert.equal(down.response.statusCode, 502);
  assert.deepEqual(down.response.json(), {
    code: 'CONSUMER_UNAVAILABLE', error: 'Consumer 暂不可用，请稍后重试', stage: 'workspace',
  });

  const solverDown = await releaseValidation([solverCase('n1', batches(1)), solverCase('n2', batches(2))], {
    agent: byCase({
      n1: (ask) => toolScript(ask, toolError('solver_unavailable', 'solve', true, 'solver job disappeared'), {
        toolStatus: 'failed',
      }),
    }),
  });
  assert.equal(solverDown.response.statusCode, 200, solverDown.response.body);
  const [unavailable, ok] = solverDown.cases;
  assert.deepEqual(
    [unavailable.status, unavailable.code, unavailable.phase, unavailable.retryable, unavailable.authoritative],
    ['error', 'solver_unavailable', 'solve', true, false],
  );
  assert.deepEqual([ok.status, ok.authoritative, ok.candidate_count], ['completed', true, 4], '其余用例不受影响');
});

test('契约不兼容：Consumer 拒绝导入时报包被拒，求解器能力漂移时按用例回传', async () => {
  const rejected = await releaseValidation([solverCase('n1', batches(1))], {
    importZip: () => json({ error: '求解契约与 Consumer 不兼容，详见 https://consumer.example.com/docs' }, 422),
  });
  assert.equal(rejected.response.statusCode, 422);
  const failure = rejected.response.json();
  assert.deepEqual([failure.code, failure.stage], ['SOLVER_PACKAGE_REJECTED', 'package']);
  assert.doesNotMatch(failure.error, /example\.com/);
  assert.equal(rejected.consumer.calls.some((call) => call.path === '/agentic/runs'), false, '导入被拒后不再提问');

  const drifted = await releaseValidation([solverCase('n1', batches(1))], {
    agent: (ask) => toolScript(
      ask,
      toolError('solver_capability_drift', 'select', false, 'solver Connector capabilities do not match the binding revision'),
      { toolStatus: 'failed' },
    ),
  });
  assert.equal(drifted.response.statusCode, 200, drifted.response.body);
  const [result] = drifted.cases;
  assert.deepEqual([result.status, result.code, result.phase, result.retryable, result.authoritative], [
    'error', 'solver_capability_drift', 'select', false, false,
  ]);
});

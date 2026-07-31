import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import Fastify, { type FastifyInstance } from 'fastify';
import multipart from '@fastify/multipart';
import { exportJWK, generateKeyPair, SignJWT, type CryptoKey } from 'jose';

const ISSUER = 'http://release-auth:5056';
const AUDIENCE = 'meta-wise';
const KEY_ID = 'release-test-key';

let app: FastifyInstance;
let authServer: Server;
let privateKey: CryptoKey;

before(async () => {
  const keys = await generateKeyPair('RS256');
  privateKey = keys.privateKey;
  const jwk = await exportJWK(keys.publicKey);
  authServer = createServer((req, reply) => {
    if (req.url !== '/jwks.json') {
      reply.writeHead(404).end();
      return;
    }
    reply.writeHead(200, { 'Content-Type': 'application/json' });
    reply.end(JSON.stringify({ keys: [{ ...jwk, kid: KEY_ID, alg: 'RS256', use: 'sig' }] }));
  });
  await new Promise<void>((resolve) => authServer.listen(0, '127.0.0.1', resolve));
  const address = authServer.address() as { port: number };

  process.env.AUTH_ISSUER_URL = `http://127.0.0.1:${address.port}`;
  process.env.AUTH_JWT_ISSUER = ISSUER;
  process.env.AUTH_JWT_AUDIENCE = AUDIENCE;
  process.env.MODELING_LLM_BASE_URL = 'faux';
  process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-release-test-'));

  const { registerRoutes } = await import('../src/routes.js');
  app = Fastify({ logger: false });
  await app.register(multipart);
  await app.register(registerRoutes);
  await app.ready();
});

after(async () => {
  await app.close();
  await new Promise<void>((resolve) => authServer.close(() => resolve()));
});

function tokenFor(subject = 'alice') {
  return new SignJWT({ typ: 'access', role: 'user', ver: 1, pwd_cr: false })
    .setProtectedHeader({ alg: 'RS256', kid: KEY_ID })
    .setIssuedAt()
    .setIssuer(ISSUER)
    .setAudience(AUDIENCE)
    .setSubject(subject)
    .setExpirationTime('15m')
    .sign(privateKey);
}

async function createPackage(name: string) {
  const [{ createProject }, { getPackageArtifactStore }] = await Promise.all([
    import('../src/projects/repo.js'),
    import('../src/artifacts/runtime.js'),
  ]);
  const project = createProject({
    owner: 'alice', kind: 'package', name, domain: '对公营销', background: '',
  });
  const store = getPackageArtifactStore();
  const base = await store.ensureProject(project.id);
  const current = await store.savePackageVersion(project.id, {
    expectedRevision: base.oid,
    spec: {
      id: project.id,
      name,
      description: '企业客户营销知识包',
      version: '1.2.10-rc.1',
      language: 'zh-CN',
      status: 'review',
      defaultProviderAlias: 'data',
      objects: [{
        key: 'Customer',
        title: '企业客户',
        description: '参与营销活动的企业客户',
        fields: [{ key: 'customer_id', cnName: '客户号', type: 'string' }],
        sourceRefs: [{ kind: 'document', filename: '客户说明.md' }],
      }],
      operations: [{
        key: 'Recommend',
        title: '产品推荐',
        description: '按客户情况推荐产品',
        inputs: [],
        outputs: [],
        flow: ['读取客户资料', '匹配合适产品'],
        relatedObjects: ['Customer'],
      }],
    },
    coverage: { covered: [], uncovered: [] },
    action: { cause: 'generate', summary: '生成知识包', actorKind: 'agent', actorName: '智能助手' },
  });
  return { project, store, current };
}

const warningReviewReady = {
  warningTotal: 0,
  pendingValidation: 0,
  pendingReleaseConfirmation: 0,
  releaseBlocking: 0,
  readyForRelease: true,
};

test('导入型知识包不要求 AI 建模阶段的领域问题', async () => {
  const { buildReleaseReadiness } = await import('../src/workflow/release.js');
  const readiness = buildReleaseReadiness({
    currentRevision: 'revision-a',
    packageCheck: { revision: 'revision-a', findings: [] },
    warningReview: warningReviewReady,
    flowchartRequired: false,
    latestRound: { status: 'done', artifactRevision: 'revision-a' },
    requiredTestcases: [{ id: 'tc-1', question: '如何审批？', groundTruth: '按规则审批' }],
    latestResults: [{ testcaseId: 'tc-1', question: '如何审批？', groundTruth: '按规则审批', verdict: 'pass' }],
  });

  assert.equal(readiness.ready, true);
  assert.deepEqual(readiness.gates.map((gate) => gate.id), [
    'package_check', 'warning_review', 'evaluation',
  ]);
});

test('AI 建模项目必须完成建模确认且没有待处理任务或候选修改', async () => {
  const { buildReleaseReadiness } = await import('../src/workflow/release.js');
  const base = {
    currentRevision: 'revision-a',
    packageCheck: { revision: 'revision-a', findings: [] },
    warningReview: warningReviewReady,
    flowchartRequired: false,
    constructionRequired: true,
    latestRound: { status: 'done' as const, artifactRevision: 'revision-a' },
    requiredTestcases: [{ id: 'tc-1', question: '如何处理申请？', groundTruth: '按确认流程处理' }],
    latestResults: [{
      testcaseId: 'tc-1', question: '如何处理申请？', groundTruth: '按确认流程处理', verdict: 'pass' as const,
    }],
  };

  const incomplete = buildReleaseReadiness({
    ...base,
    constructionCompleted: false,
    pendingRequiredTasks: 1,
    pendingCandidateChanges: 0,
  });
  assert.equal(incomplete.ready, false);
  assert.deepEqual(incomplete.gates.map((gate) => gate.id), [
    'package_check', 'warning_review', 'construction', 'evaluation',
  ]);
  assert.match(incomplete.gates.find((gate) => gate.id === 'construction')!.detail, /尚未确认/);

  const pending = buildReleaseReadiness({
    ...base,
    constructionCompleted: true,
    pendingRequiredTasks: 0,
    pendingCandidateChanges: 2,
  });
  assert.equal(pending.ready, false);
  assert.match(pending.gates.find((gate) => gate.id === 'construction')!.detail, /2 个候选修改/);

  const ready = buildReleaseReadiness({
    ...base,
    constructionCompleted: true,
    pendingRequiredTasks: 0,
    pendingCandidateChanges: 0,
  });
  assert.equal(ready.ready, true);
  assert.equal(ready.gates.find((gate) => gate.id === 'construction')!.passed, true);
});

test('发布评测必须绑定当前修订并覆盖全部真实用例', async () => {
  const { buildReleaseReadiness } = await import('../src/workflow/release.js');
  const requiredTestcases = [
    { id: 'tc-1', question: '申请如何处理？', groundTruth: '先校验再审批' },
    { id: 'tc-2', question: '退回后怎么办？', groundTruth: '补充后重新提交' },
  ];
  const results = requiredTestcases.map((item) => ({ ...item, testcaseId: item.id, verdict: 'pass' as const }));

  const stale = buildReleaseReadiness({
    currentRevision: 'revision-current', packageCheck: { revision: 'revision-current', findings: [] },
    warningReview: warningReviewReady,
    flowchartRequired: false,
    latestRound: { status: 'done', artifactRevision: 'revision-old' },
    requiredTestcases, latestResults: results,
  });
  assert.equal(stale.ready, false);
  assert.match(stale.gates.find((gate) => gate.id === 'evaluation')!.detail, /旧修订/);

  const missingRevision = buildReleaseReadiness({
    currentRevision: 'revision-current', packageCheck: { revision: 'revision-current', findings: [] },
    warningReview: warningReviewReady,
    flowchartRequired: false,
    latestRound: { status: 'done' },
    requiredTestcases, latestResults: results,
  });
  assert.equal(missingRevision.ready, false);
  assert.match(
    missingRevision.gates.find((gate) => gate.id === 'evaluation')!.detail,
    /未记录工件修订/,
  );

  const incomplete = buildReleaseReadiness({
    currentRevision: 'revision-current', packageCheck: { revision: 'revision-current', findings: [] },
    warningReview: warningReviewReady,
    flowchartRequired: false,
    latestRound: { status: 'done', artifactRevision: 'revision-current' },
    requiredTestcases, latestResults: results.slice(0, 1),
  });
  assert.equal(incomplete.ready, false);
  assert.match(incomplete.gates.find((gate) => gate.id === 'evaluation')!.detail, /1\/2/);

  const completedWithFailures = buildReleaseReadiness({
    currentRevision: 'revision-current', packageCheck: { revision: 'revision-current', findings: [] },
    warningReview: warningReviewReady,
    flowchartRequired: false,
    latestRound: { status: 'done', artifactRevision: 'revision-current' },
    requiredTestcases,
    latestResults: [results[0], { ...results[1], verdict: 'fail' as const }],
  });
  assert.equal(completedWithFailures.ready, true);
  assert.match(completedWithFailures.gates.find((gate) => gate.id === 'evaluation')!.detail, /不阻断发布/);
});

test('发布接口列出未通过项，且他人不能访问项目', async () => {
  const { project, store, current } = await createPackage('发布门禁');
  const alice = await tokenFor();
  const blocked = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/release`,
    headers: { authorization: `Bearer ${alice}` },
  });
  assert.equal(blocked.statusCode, 409, blocked.body);
  assert.equal(blocked.json().error, '发布条件未满足');
  assert.deepEqual(blocked.json().gates.map((gate: any) => [gate.id, gate.passed]), [
    ['package_check', false],
    ['warning_review', true],
    ['evaluation', false],
  ]);
  assert.equal((await store.current(project.id))?.revision.oid, current.revision.oid);

  const bob = await tokenFor('bob');
  const denied = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/release`,
    headers: { authorization: `Bearer ${bob}` },
  });
  assert.equal(denied.statusCode, 404);
});

test('发布成功后写入新版本并发出版本事件', async () => {
  const { project, store, current } = await createPackage('发布成功');
  const [m3repo, { saveCheck }] = await Promise.all([
    import('../src/workflow/m3-repo.js'),
    import('../src/workflow/package-check.js'),
  ]);
  saveCheck(project.id, current.packageVersion, [], current.revision.oid);
  const testcase = m3repo.addTestcase(project.id, '该推荐什么产品？', '按客户情况推荐');
  const round = m3repo.createRound(project.id, current.packageVersion, 1, current.revision.oid);
  m3repo.addResult(round.id, {
    testcaseId: testcase.id,
    question: testcase.question,
    groundTruth: testcase.groundTruth,
    finalAnswer: '推荐适配客户需求的产品',
    verdict: 'pass',
  });
  m3repo.roundProgress(round.id, 1, 1);
  m3repo.finishRound(round.id, 'done');
  const token = await tokenFor();
  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/release`,
    headers: { authorization: `Bearer ${token}` },
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().version, '1.2.10');
  assert.match(response.json().revision, /^[0-9a-f]{40}$/);

  const released = await store.current(project.id);
  assert.equal(released?.packageVersion, '1.2.10');
  assert.equal(released?.spec.status, 'released');
  assert.equal(released?.revision.oid, response.json().revision);

  const events = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/events?after=0`,
    headers: { authorization: `Bearer ${token}`, accept: 'application/json' },
  });
  assert.equal(events.statusCode, 200, events.body);
  const revisionEvent = events.json().events.find((event: any) => event.type === 'package.revision.created');
  assert.equal(revisionEvent.payload.revisionId, response.json().revision);
  assert.equal(revisionEvent.payload.packageVersion, '1.2.10');
});

test('发布候选通过全部门禁后提升为对应稳定版本而不是再加补丁号', async () => {
  const { project, store, current } = await createPackage('发布候选转正式版');
  const candidate = await store.saveCurrentAsPackageVersion(project.id, {
    expectedRevision: current.revision.oid,
    version: '1.3.0-rc.2',
    status: 'review',
    note: '验证候选',
    action: {
      cause: 'validation_candidate', summary: '创建验证候选',
      actorKind: 'user', actorName: 'alice',
    },
  });
  const [m3repo, { saveCheck }] = await Promise.all([
    import('../src/workflow/m3-repo.js'),
    import('../src/workflow/package-check.js'),
  ]);
  saveCheck(project.id, candidate.packageVersion, [], candidate.revision.oid);
  const testcase = m3repo.addTestcase(project.id, '如何处理申请？', '按确认流程处理');
  const round = m3repo.createRound(
    project.id, candidate.packageVersion, 1, candidate.revision.oid,
  );
  m3repo.addResult(round.id, {
    testcaseId: testcase.id,
    question: testcase.question,
    groundTruth: testcase.groundTruth,
    finalAnswer: '按确认流程处理',
    verdict: 'pass',
  });
  m3repo.roundProgress(round.id, 1, 1);
  m3repo.finishRound(round.id, 'done');
  const token = await tokenFor();
  const response = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/release`,
    headers: { authorization: `Bearer ${token}` },
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().version, '1.3.0');
  const released = await store.current(project.id);
  assert.equal(released?.packageVersion, '1.3.0');
  assert.equal(released?.spec.status, 'released');
});

test('警告接受接口按验证与发布阶段保存审计记录', async () => {
  const { project, current } = await createPackage('警告审计');
  const token = await tokenFor();
  const { saveCheck } = await import('../src/workflow/package-check.js');
  const { getPackageWarningReview } = await import('../src/workflow/warning-acceptance.js');
  saveCheck(project.id, current.packageVersion, [{
    id: 'warning-route', source: 'rule', severity: '警告', category: '口径待确认',
    target: 'metrics/score.md', message: '边界口径尚未确认',
  }], current.revision.oid);
  const initial = getPackageWarningReview(project.id, current.revision.oid);
  const headers = { authorization: `Bearer ${token}` };

  const validation = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/package/warnings/accept`, headers,
    payload: {
      revision: current.revision.oid,
      fingerprints: [initial.items[0].fingerprint],
      note: '已查看影响，不改变主要业务结论', phase: 'validation',
    },
  });
  assert.equal(validation.statusCode, 200, validation.body);
  assert.equal(validation.json().warningReview.readyForValidation, true);
  assert.equal(validation.json().warningReview.readyForRelease, false);

  const release = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/package/warnings/accept`, headers,
    payload: {
      revision: current.revision.oid,
      fingerprints: [initial.items[0].fingerprint],
      note: '同意作为本次发布已知限制', phase: 'release',
    },
  });
  assert.equal(release.statusCode, 200, release.body);
  assert.equal(release.json().warningReview.readyForRelease, true);
});

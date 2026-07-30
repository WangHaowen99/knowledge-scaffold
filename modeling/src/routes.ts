import type { FastifyInstance } from 'fastify';
import { requireAuth } from './auth/middleware.js';
import { getAuthClient, PasswordChangeRequiredError } from './auth/client.js';
import { createProject, getProject, listProjects, setStage } from './projects/repo.js';
import { deleteProjectLocalData } from './projects/delete-service.js';
import { insertDocument, listDocuments, getDocument, deleteDocument } from './documents/repo.js';
import { kindOf, parseDocument } from './documents/parser.js';
import { latestNodeOf, listNodes, runAnalyze, upsertNode } from './workflow/engine.js';
import * as m2 from './workflow/m2.js';
import * as m2repo from './workflow/m2-repo.js';
import { renderPackage } from './workflow/package-render.js';
import { zipPackage } from './workflow/export.js';
import { advanceStage as setProjectStage } from './projects/repo.js';
import { getSetting, setSetting, deleteSetting, getConsumerApiUrl, getLlmSettings, getEvalBindingDefaults } from './settings/repo.js';
import { packageChatPlan, packageChatApply } from './workflow/package-chat.js';
import { migrateLegacyPackageOverrides } from './workflow/package-override-migration.js';
import { runRuleChecks, runSemanticCheck, runFormatChecks, saveCheck, getCheck } from './workflow/package-check.js';
import * as m3repo from './workflow/m3-repo.js';
import * as m4repo from './workflow/m4-repo.js';
import { isEvaluationActive, startEvaluation } from './evaluation/engine.js';
import { getProjectDataSource } from './evaluation/data-source-repo.js';
import { configureProjectDataSource } from './evaluation/data-source-service.js';
import type { DataSourceConnectionTester } from './evaluation/data-source-connection-test.js';
import type { DataSourceConsumerSync } from './evaluation/data-source-consumer-sync.js';
import { getRunningEvaluationDatabaseImportRun } from './evaluation/eval-db-import-repo.js';
import {
  assertProjectEvaluationDatabaseImportAvailable,
  EvaluationDatabaseImportAlreadyRunningError,
  EvaluationDatabaseImportCapacityError,
  getProjectEvaluationDatabaseImports,
  startProjectEvaluationDatabaseImport,
  type EvaluationDatabaseImportServiceDependencies,
} from './evaluation/eval-db-import-service.js';
import {
  createProjectEvaluationDatabase,
  projectEvaluationDatabaseName,
  type ProjectDatabaseCreationDependencies,
} from './evaluation/project-evaluation-database.js';
import {
  evaluationDatabaseUrlSummary,
  selectEvaluationDatabaseUrlOverride,
} from './evaluation/evaluation-database-target.js';
import {
  cancelGraphImportUpload,
  executeGraphImport,
  GraphImportAlreadyRunningError,
  getGraphImportHistory,
  uploadGraphImportExcel,
  validateGraphImportUpload,
  type GraphImportExecutionDependencies,
} from './graph-import/service.js';
import { GraphIsolationDataExistsError } from './graph-import/neo4j-writer.js';
import { getRunningGraphImportRun } from './graph-import/repo.js';
import { proposeOptimizations, applyOptimizations, findingsToSuggestions } from './evaluation/optimizer.js';
import { testConsumerConnectivity } from './consumer/client.js';
import { config } from './config.js';
import type { DataDictTable } from './types.js';
import { InFlightOperations, OperationAlreadyRunningError } from './in-flight-operations.js';
import { parsePackageDataSourceType } from './workflow/package-data-source.js';
import {
  assertNoActivePackageDraft,
  createOrGetPackageDraft,
  discardPackageDraft,
  getActivePackageDraft,
  getPackageDraftFile,
  PackageDraftError,
  publishPackageDraft,
  revertPackageDraftFile,
  savePackageDraftFile,
} from './workflow/package-draft.js';

export interface RegisterRoutesOptions {
  dataSourceConnectionTester?: DataSourceConnectionTester;
  dataSourceConsumerSync?: DataSourceConsumerSync;
  evaluationDatabaseDependencies?: EvaluationDatabaseImportServiceDependencies
    & ProjectDatabaseCreationDependencies;
  graphImportExecutionDependencies?: GraphImportExecutionDependencies;
  packageChatPlan?: typeof packageChatPlan;
  packageChatApply?: typeof packageChatApply;
}

function parseDraftRevision(value: unknown): number | null {
  if (typeof value === 'number') return Number.isInteger(value) && value >= 0 ? value : null;
  if (typeof value !== 'string' || !/^\d+$/.test(value)) return null;
  const revision = Number(value);
  return Number.isSafeInteger(revision) ? revision : null;
}

function sendPackageDraftError(reply: any, error: unknown) {
  if (!(error instanceof PackageDraftError)) throw error;
  return reply.code(error.statusCode).send({
    code: error.code,
    error: error.message,
    ...error.details,
  });
}

function blockForActivePackageDraft(reply: any, projectId: string, operation: string) {
  try {
    assertNoActivePackageDraft(projectId, operation);
    return null;
  } catch (error) {
    return sendPackageDraftError(reply, error);
  }
}

async function readEvaluationDatabaseUpload(req: any): Promise<{
  filename: string;
  buffer: Buffer;
  replaceConfirmed: boolean;
  databaseUrl?: string;
}> {
  let filename = '';
  let buffer: Buffer | undefined;
  let replaceConfirmed = false;
  let databaseUrlMode = '';
  let customDatabaseUrl = '';
  for await (const part of req.parts()) {
    if (part.type === 'file') {
      if (buffer) throw new Error('一次只能上传一个 Excel 文件');
      filename = part.filename;
      buffer = await part.toBuffer();
    } else if (part.fieldname === 'replace_confirmation') {
      replaceConfirmed = String(part.value ?? '') === 'replace_same_name_tables';
    } else if (part.fieldname === 'database_url_mode') {
      databaseUrlMode = String(part.value ?? '');
    } else if (part.fieldname === 'database_url') {
      customDatabaseUrl = String(part.value ?? '');
    }
  }
  if (!buffer) throw new Error('缺少 Excel 文件');
  return {
    filename,
    buffer,
    replaceConfirmed,
    databaseUrl: selectEvaluationDatabaseUrlOverride(databaseUrlMode, customDatabaseUrl),
  };
}

export async function registerRoutes(app: FastifyInstance, options: RegisterRoutesOptions = {}) {
  const runPackageChatPlan = options.packageChatPlan ?? packageChatPlan;
  const runPackageChatApply = options.packageChatApply ?? packageChatApply;
  const legacyMigration = migrateLegacyPackageOverrides();
  if (legacyMigration.projectCount) {
    const log = legacyMigration.failedCount || legacyMigration.pendingReviewCount
      ? app.log.warn.bind(app.log)
      : app.log.info.bind(app.log);
    log({ legacyMigration }, '旧版知识包人工覆盖迁移完成');
  }
  app.get('/health', async () => ({ status: 'ok' }));

  // ---- 认证：登录/刷新/注册转发给 issuer ----
  app.post('/auth/login', async (req, reply) => {
    const { username, password } = (req.body ?? {}) as { username?: string; password?: string };
    if (!username || !password) return reply.code(400).send({ error: '用户名和密码必填' });
    let result;
    try {
      result = await getAuthClient().login(username, password);
    } catch (e) {
      if (e instanceof PasswordChangeRequiredError) {
        return reply.code(403).send({ error: e.message, code: 'PASSWORD_CHANGE_REQUIRED' });
      }
      req.log.warn({ err: e }, '登录上游不可用或配置错误');
      return reply.code(503).send({ error: '认证服务暂不可用' });
    }
    if (!result) return reply.code(401).send({ error: '登录失败' });
    const body: Record<string, unknown> = { token: result.token, user: result.user };
    // 上游提供 refresh 能力时加法追加（冻结契约 {token, user} 不变）
    if (result.refreshToken) {
      body.refresh_token = result.refreshToken;
      body.expires_in = result.expiresIn;
    }
    return body;
  });

  app.post('/auth/refresh', async (req, reply) => {
    const { refresh_token } = (req.body ?? {}) as { refresh_token?: string };
    if (!refresh_token) return reply.code(400).send({ error: 'refresh_token 必填' });
    try {
      const tokens = await getAuthClient().refresh(refresh_token);
      // 401 → 前端清 token 登出；503（上游不可达）→ 前端不登出
      if (!tokens) return reply.code(401).send({ error: 'Unauthorized' });
      return { token: tokens.token, refresh_token: tokens.refreshToken, expires_in: tokens.expiresIn };
    } catch {
      return reply.code(503).send({ error: '认证服务暂不可用' });
    }
  });

  // 注册代理：一步建号，上游状态码 + body 原样透传（限流 429 / 密码策略 400 / 未开启 503 语义由 auth-server 决定）
  app.post('/auth/register', async (req, reply) => {
    try {
      const { status, body } = await getAuthClient().register(req.body ?? {}, req.ip);
      return reply.code(status).send(body);
    } catch {
      return reply.code(502).send({ error: '注册服务暂不可用，请稍后再试' });
    }
  });

  // 以下均需认证
  app.register(async (r) => {
    r.addHook('preHandler', requireAuth);

    r.get('/api/me', async (req) => ({ user: req.user }));

    // ---- 服务设置（consumer 评测端点页面可配置） ----
    r.get('/api/settings', async () => {
      const llm = getLlmSettings();
      const binding = getEvalBindingDefaults();
      return {
        consumerApiUrl: getConsumerApiUrl(),
        consumerApiUrlSource: getSetting('consumer_api_url') ? 'settings' : 'env',
        authIssuerUrl: config.auth.issuerUrl, // 只读展示：登录 issuer（auth-server）由部署配置
        llm: {
          baseUrl: llm.baseUrl, model: llm.model, maxTokens: llm.maxTokens,
          apiKeySet: !!llm.apiKey, // key 不回显
          source: getSetting('llm_base_url') ? 'settings' : 'env',
        },
        evalBinding: binding,
      };
    });

    r.put('/api/settings', async (req, reply) => {
      const b = (req.body ?? {}) as Record<string, unknown>;
      const put = (key: string, v: unknown) => {
        if (v === undefined) return; // 未提交 = 不修改
        const s = String(v).trim();
        s ? setSetting(key, s) : deleteSetting(key); // 空串 = 清除（回退 env）
      };
      if (b.consumerApiUrl !== undefined) {
        const v = String(b.consumerApiUrl).trim();
        if (v && !/^https?:\/\//.test(v)) return reply.code(400).send({ error: 'consumerApiUrl 必须是 http(s) 地址' });
        put('consumer_api_url', v);
      }
      put('llm_base_url', b.llmBaseUrl);
      put('llm_model', b.llmModel);
      put('llm_max_tokens', b.llmMaxTokens);
      if (b.llmApiKey !== undefined && String(b.llmApiKey) !== '') put('llm_api_key', b.llmApiKey); // key 留空 = 保持不变
      if (b.llmApiKeyClear === true) deleteSetting('llm_api_key');
      put('eval_binding_provider', b.evalBindingProvider);
      put('eval_binding_url', b.evalBindingUrl);
      put('eval_binding_token', b.evalBindingToken);
      put('eval_binding_scene', b.evalBindingScene);
      return { ok: true };
    });

    // LLM 有效性测试：最小 chat/completions 调用；失败时返回原始错误（供排障）
    r.post('/api/settings/llm/test', async (req) => {
      const b = (req.body ?? {}) as Record<string, string>;
      const saved = getLlmSettings();
      const baseUrl = (b.baseUrl || saved.baseUrl).replace(/\/+$/, '');
      const apiKey = b.apiKey || saved.apiKey;
      const model = b.model || saved.model;
      if (baseUrl === 'faux') return { ok: true, detail: 'faux 演示模式（不调用真实端点）' };
      if (!baseUrl || !model) return { ok: false, error: 'base url 与模型名必填' };
      const t0 = Date.now();
      try {
        const res = await fetch(`${baseUrl}/chat/completions`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey ?? ''}` },
          body: JSON.stringify({ model, messages: [{ role: 'user', content: 'ping' }], max_tokens: 1 }),
          signal: AbortSignal.timeout(20_000),
        });
        const raw = await res.text();
        if (!res.ok) return { ok: false, status: res.status, latencyMs: Date.now() - t0, error: raw.slice(0, 1000) };
        return { ok: true, status: res.status, latencyMs: Date.now() - t0 };
      } catch (e) {
        return { ok: false, latencyMs: Date.now() - t0, error: e instanceof Error ? `${e.name}: ${e.message}${e.cause ? `（${e.cause}）` : ''}` : String(e) };
      }
    });

    // 连通性测试：health / auth（token 透传）/ 模型就绪
    r.post('/api/settings/consumer/test', async (req) => {
      const { url } = (req.body ?? {}) as { url?: string };
      const token = (req.headers.authorization ?? '').slice(7);
      return testConsumerConnectivity(token, url);
    });

    // ---- 项目（节点 1）----
    r.get('/api/projects', async (req) => ({ projects: listProjects(req.user!.username) }));

    r.post('/api/projects', async (req, reply) => {
      const b = (req.body ?? {}) as any;
      if (!b.name) return reply.code(400).send({ error: '项目名必填' });
      const project = createProject({
        owner: req.user!.username, name: b.name, domain: b.domain,
        background: b.background, targetUsers: b.targetUsers, glossaryPrefs: b.glossaryPrefs,
      });
      return reply.code(201).send({ project });
    });

    r.get('/api/projects/:id', async (req, reply) => {
      const p = getProject((req.params as any).id);
      if (!p || p.owner !== req.user!.username) return reply.code(404).send({ error: '项目不存在' });
      const cqs = m2repo.listCompetency(p.id);
      return {
        project: p, documents: listDocuments(p.id), nodes: listNodes(p.id),
        competency: { total: cqs.length, selected: cqs.filter((q) => q.selected).length },
        dataSource: getProjectDataSource(p.id),
      };
    });

    // ---- 资料上传与解析（节点 2）----
    r.post('/api/projects/:id/documents', async (req, reply) => {
      const p = getProject((req.params as any).id);
      if (!p || p.owner !== req.user!.username) return reply.code(404).send({ error: '项目不存在' });
      return withLock(`${p.id}:document-upload`, reply, async () => {
        const file = await (req as any).file();
        if (!file) return reply.code(400).send({ error: '缺少上传文件' });
        const buffer = await file.toBuffer();
        const kind = kindOf(file.filename);
        const result = await parseDocument(kind, buffer);
        const doc = insertDocument({
          projectId: p.id, filename: file.filename, kind,
          parseStatus: result.error ? 'failed' : 'parsed', parseError: result.error,
          textContent: result.textContent, tables: result.tables,
        });
        if (p.stage === 'project_setup') setStage(p.id, 'ingest');
        return reply.code(201).send({ document: doc });
      });
    });

    r.delete('/api/projects/:id/documents/:docId', async (req, reply) => {
      const p = getProject((req.params as any).id);
      if (!p || p.owner !== req.user!.username) return reply.code(404).send({ error: '项目不存在' });
      const doc = getDocument((req.params as any).docId);
      if (!doc || doc.projectId !== p.id) return reply.code(404).send({ error: '文档不存在' });
      deleteDocument(doc.id);
      // 资料变更使下游产物「可能过时」：已完成节点标记 stale，由专家决定何时重跑（不强制清数据）
      const stale: string[] = [];
      for (const n of listNodes(p.id)) {
        if (n.status === 'done' && ['analyze', 'interview', 'competency', 'generate'].includes(n.type)) {
          upsertNode({ id: n.id, projectId: p.id, type: n.type, status: 'stale', ordinal: n.ordinal });
          stale.push(n.type);
        }
      }
      return { deleted: doc.id, staleNodes: stale, documents: listDocuments(p.id) };
    });

    // ---- 初步分析（节点 3）----
    r.post('/api/projects/:id/analyze', async (req, reply) => {
      const p = getProject((req.params as any).id);
      if (!p || p.owner !== req.user!.username) return reply.code(404).send({ error: '项目不存在' });
      const docs = listDocuments(p.id);
      const tables: DataDictTable[] = docs.flatMap((d) => d.tables ?? []);
      const textDocs = docs.filter((d) => d.textContent).map((d) => ({ filename: d.filename, textContent: d.textContent }));
      if (!tables.length && !textDocs.length) return reply.code(400).send({ error: '尚无可分析的资料，请先上传文档' });
      return withLock(`${p.id}:analyze`, reply, async () => {
      try {
        const { node, analysis } = await runAnalyze(p.id, tables, textDocs);
        // 重跑早期节点不能让已到达的项目阶段倒退。
        setProjectStage(p.id, 'analyze');
        // 领域分析完成即生成「领域问题」候选（勾选后驱动包生成）；失败不阻断分析结果
        let domainQuestions: unknown[] = [];
        try {
          domainQuestions = await m2.proposeCompetency(p, { tables, documents: textDocs }, analysis) as unknown[];
        } catch { /* 面板可手动重试生成 */ }
        return { node, analysis, domainQuestions };
      } catch (e) {
        return reply.code(500).send({ error: e instanceof Error ? e.message : String(e) });
      }
      });
    });

    // 长任务防重入（同项目同操作并发触发 → 409），并登记操作类型供前端恢复运行状态
    const inFlight = new InFlightOperations();
    const withLock = async (
      key: string,
      reply: any,
      fn: () => Promise<unknown>,
      operation = key.slice(key.lastIndexOf(':') + 1),
    ) => {
      try {
        return await inFlight.run(key, operation, fn);
      } catch (error) {
        if (error instanceof OperationAlreadyRunningError) {
          return reply.code(409).send({ error: '该操作正在进行中，请稍候（切换页面不会中断执行）' });
        }
        throw error;
      }
    };
    const blockForActivePackageRepair = (reply: any, projectId: string) => {
      const operation = inFlight.current(`${projectId}:chat`);
      if (!operation) return null;
      return reply.code(409).send({
        code: 'PACKAGE_REPAIR_IN_PROGRESS',
        error: '知识包修缮正在进行中，暂不能人工编辑',
        operation,
      });
    };

    r.delete('/api/projects/:id', async (req, reply) => {
      const p = getProject((req.params as any).id);
      if (!p || p.owner !== req.user!.username) return reply.code(404).send({ error: '项目不存在' });
      if ((req.body as { confirmed?: boolean } | null)?.confirmed !== true) {
        return reply.code(400).send({ error: '请确认删除项目' });
      }
      const runningOperation = inFlight.currentByPrefix(`${p.id}:`)
        ?? (isEvaluationActive(p.id) ? 'evaluate' : null)
        ?? (getRunningGraphImportRun(p.id) ? 'graph-import' : null)
        ?? (getRunningEvaluationDatabaseImportRun(p.id) ? 'evaluation-database-import' : null);
      if (runningOperation) {
        return reply.code(409).send({
          code: 'PROJECT_BUSY',
          error: '项目正在执行任务，请等待完成后再删除',
          operation: runningOperation,
        });
      }
      try {
        if (!deleteProjectLocalData(p.id, req.user!.username)) {
          return reply.code(404).send({ error: '项目不存在' });
        }
        return { deleted: p.id };
      } catch (error) {
        req.log.error({ err: error, projectId: p.id }, '删除项目失败');
        return reply.code(500).send({ error: error instanceof Error ? error.message : '项目删除失败，请稍后重试' });
      }
    });

    // 取项目 + 数据字典上下文的公共辅助
    const loadCtx = (id: string, username: string) => {
      const p = getProject(id);
      if (!p || p.owner !== username) return null;
      const docs = listDocuments(p.id);
      const tables: DataDictTable[] = docs.flatMap((d) => d.tables ?? []);
      const textDocs = docs.filter((d) => d.textContent).map((d) => ({ filename: d.filename, textContent: d.textContent }));
      // stale（资料变更后）也返回旧分析——结果仍可用，由前端提示重跑
      const analyzeNode = latestNodeOf(listNodes(p.id), 'analyze', ['done', 'stale']);
      return { p, ctx: { tables, documents: textDocs }, analysis: analyzeNode?.output };
    };

    // ---- 节点 4 访谈 ----
    r.get('/api/projects/:id/interview', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return { batches: m2repo.listBatches(c.p.id).map((b) => ({ ...b, answers: m2repo.getAnswers(b.id) })) };
    });

    r.post('/api/projects/:id/interview/start', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const selectedCq = m2repo.listCompetency(c.p.id).filter((q) => q.selected).length;
      if (selectedCq < 5) return reply.code(400).send({ error: `请先在领域分析中勾选至少 5 条领域问题（当前 ${selectedCq} 条）` });
      return withLock(`${c.p.id}:interview`, reply, async () => {
        try {
          const res = await m2.startInterview(c.p, c.ctx, c.analysis);
          setProjectStage(c.p.id, 'interview');
          return res;
        } catch (e) { return reply.code(500).send({ error: e instanceof Error ? e.message : String(e) }); }
      });
    });

    r.post('/api/projects/:id/interview/answer', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const { batchId, answers } = (req.body ?? {}) as any;
      if (!batchId || !Array.isArray(answers)) return reply.code(400).send({ error: 'batchId 与 answers 必填' });
      return withLock(`${c.p.id}:interview`, reply, async () => {
        try {
          return await m2.answerInterview(c.p, c.ctx, batchId, answers);
        } catch (e) { return reply.code(500).send({ error: e instanceof Error ? e.message : String(e) }); }
      });
    });

    // ---- 节点 5 胜任力 ----
    r.get('/api/projects/:id/competency', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return { questions: m2repo.listCompetency(c.p.id) };
    });
    r.post('/api/projects/:id/competency', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const question = String((req.body as any)?.question ?? '').trim();
      if (!question) return reply.code(400).send({ error: '领域问题不能为空' });
      if (question.length > 500) return reply.code(400).send({ error: '领域问题不能超过 500 个字符' });
      const added = m2repo.addCustomCompetency(c.p.id, question);
      return reply.code(201).send({ question: added, questions: m2repo.listCompetency(c.p.id) });
    });
    r.post('/api/projects/:id/competency/propose', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return withLock(`${c.p.id}:competency`, reply, async () => {
      try {
        const questions = await m2.proposeCompetency(c.p, c.ctx, c.analysis);
        setProjectStage(c.p.id, 'competency');
        return { questions };
      } catch (e) { return reply.code(500).send({ error: e instanceof Error ? e.message : String(e) }); }
      });
    });
    r.post('/api/projects/:id/competency/select', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const { selectedIds } = (req.body ?? {}) as any;
      m2repo.setCompetencySelection(c.p.id, selectedIds ?? []);
      return { questions: m2repo.listCompetency(c.p.id) };
    });

    // ---- 节点 6 知识包生成 ----
    r.post('/api/projects/:id/generate', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'generate');
      if (blocked) return blocked;
      let dataSourceType;
      try {
        dataSourceType = parsePackageDataSourceType((req.body as any)?.dataSourceType);
      } catch (error) {
        return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
      }
      return withLock(`${c.p.id}:generate`, reply, async () => {
      const interviewNode = latestNodeOf(listNodes(c.p.id), 'interview');
      if (interviewNode?.status !== 'done') {
        return reply.code(400).send({ error: '请先基于最新领域分析重新完成知识萃取访谈' });
      }
      const selected = m2repo.listCompetency(c.p.id).filter((q) => q.selected);
      if (selected.length < 5) return reply.code(400).send({ error: '请先在胜任力问题中至少勾选 5 条' });
      try {
        const { spec, coverage, version } = await m2.generatePackage(c.p, c.ctx, undefined, dataSourceType);
        setProjectStage(c.p.id, 'generate');
        return { version, coverage, objectCount: spec.objects.length, operationCount: spec.operations.length, dataSourceType };
      } catch (e) { return reply.code(500).send({ error: e instanceof Error ? e.message : String(e) }); }
      });
    });

    // ---- 节点 7 包评审：读文件树 / 单文件 / 导出 ----
    // ?version= 指定历史版本，缺省为最新版
    const pickVersion = (projectId: string, req: any) => {
      const v = String((req.query as any)?.version ?? '');
      return v ? m2repo.getPackageVersion(projectId, v) : m2repo.latestPackageVersion(projectId);
    };
    r.get('/api/projects/:id/package', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const pv = pickVersion(c.p.id, req);
      if (!pv) return reply.code(404).send({ error: '尚未生成知识包或版本不存在' });
      const files = renderPackage(pv.spec);
      return {
        version: pv.version, coverage: pv.coverage,
        files: Object.keys(files).sort(),
        versions: m2repo.listPackageVersions(c.p.id),
      };
    });
    r.get('/api/projects/:id/package/file', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const pv = pickVersion(c.p.id, req);
      if (!pv) return reply.code(404).send({ error: '尚未生成知识包或版本不存在' });
      const files = renderPackage(pv.spec);
      const path = String((req.query as any).path || '');
      if (!(path in files)) return reply.code(404).send({ error: '文件不存在' });
      return { path, content: files[path] };
    });

    // 人工编辑草稿后端。草稿发布后固化为一个正式版本；活动草稿期间消费与修缮入口统一门禁。
    r.get('/api/projects/:id/package/draft', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return { draft: getActivePackageDraft(c.p.id) };
    });
    r.post('/api/projects/:id/package/draft', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageRepair(reply, c.p.id);
      if (blocked) return blocked;
      try {
        const result = createOrGetPackageDraft(c.p.id, req.user!.username);
        return reply.code(result.created ? 201 : 200).send(result);
      } catch (error) {
        return sendPackageDraftError(reply, error);
      }
    });
    r.get('/api/projects/:id/package/draft/file', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const path = String((req.query as any).path ?? '').trim();
      if (!path) return reply.code(400).send({ error: 'path 必填' });
      try {
        return { file: getPackageDraftFile(c.p.id, path) };
      } catch (error) {
        return sendPackageDraftError(reply, error);
      }
    });
    r.put('/api/projects/:id/package/draft/file', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageRepair(reply, c.p.id);
      if (blocked) return blocked;
      const { path, content, revision, expectedContentHash, draftId } = (req.body ?? {}) as Record<string, unknown>;
      const expectedRevision = parseDraftRevision(revision);
      if (typeof path !== 'string' || !path.trim()) return reply.code(400).send({ error: 'path 必填' });
      if (typeof content !== 'string') return reply.code(400).send({ error: 'content 必填' });
      if (expectedRevision == null) return reply.code(400).send({ error: 'revision 必须是非负整数' });
      if (expectedContentHash !== undefined
        && (typeof expectedContentHash !== 'string' || !/^[a-f0-9]{64}$/.test(expectedContentHash))) {
        return reply.code(400).send({ error: 'expectedContentHash 必须是 SHA-256 十六进制字符串' });
      }
      if (draftId !== undefined && (typeof draftId !== 'string' || !draftId.trim())) {
        return reply.code(400).send({ error: 'draftId 必须是非空字符串' });
      }
      try {
        return savePackageDraftFile({
          projectId: c.p.id,
          path: path.trim(),
          content,
          expectedRevision,
          expectedContentHash: typeof expectedContentHash === 'string' ? expectedContentHash : undefined,
          expectedDraftId: typeof draftId === 'string' ? draftId.trim() : undefined,
        });
      } catch (error) {
        return sendPackageDraftError(reply, error);
      }
    });
    r.delete('/api/projects/:id/package/draft/file', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageRepair(reply, c.p.id);
      if (blocked) return blocked;
      const path = String((req.query as any).path ?? '').trim();
      const expectedRevision = parseDraftRevision((req.query as any).revision);
      const expectedDraftId = String((req.query as any).draftId ?? '').trim() || undefined;
      if (!path) return reply.code(400).send({ error: 'path 必填' });
      if (expectedRevision == null) return reply.code(400).send({ error: 'revision 必须是非负整数' });
      try {
        return revertPackageDraftFile({ projectId: c.p.id, path, expectedRevision, expectedDraftId });
      } catch (error) {
        return sendPackageDraftError(reply, error);
      }
    });
    r.delete('/api/projects/:id/package/draft', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageRepair(reply, c.p.id);
      if (blocked) return blocked;
      const { confirmed, revision, draftId } = (req.body ?? {}) as Record<string, unknown>;
      if (confirmed !== true) return reply.code(400).send({ error: '请确认放弃人工编辑草稿' });
      const expectedRevision = parseDraftRevision(revision);
      if (expectedRevision == null) return reply.code(400).send({ error: 'revision 必须是非负整数' });
      if (draftId !== undefined && (typeof draftId !== 'string' || !draftId.trim())) {
        return reply.code(400).send({ error: 'draftId 必须是非空字符串' });
      }
      try {
        return { discarded: discardPackageDraft({
          projectId: c.p.id,
          expectedRevision,
          expectedDraftId: typeof draftId === 'string' ? draftId.trim() || undefined : undefined,
        }) };
      } catch (error) {
        return sendPackageDraftError(reply, error);
      }
    });
    r.post('/api/projects/:id/package/draft/publish', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageRepair(reply, c.p.id);
      if (blocked) return blocked;
      const { confirmed, revision, summary, draftId } = (req.body ?? {}) as Record<string, unknown>;
      if (confirmed !== true) return reply.code(400).send({ error: '请确认发布人工编辑草稿' });
      const expectedRevision = parseDraftRevision(revision);
      if (expectedRevision == null) return reply.code(400).send({ error: 'revision 必须是非负整数' });
      if (draftId !== undefined && (typeof draftId !== 'string' || !draftId.trim())) {
        return reply.code(400).send({ error: 'draftId 必须是非空字符串' });
      }
      if (summary !== undefined && typeof summary !== 'string') {
        return reply.code(400).send({ error: 'summary 必须是字符串' });
      }
      if (typeof summary === 'string' && summary.length > 500) {
        return reply.code(400).send({ error: 'summary 不能超过 500 个字符' });
      }
      return withLock(`${c.p.id}:package-draft-publish`, reply, async () => {
        try {
          const selectedQuestions = m2repo.listCompetency(c.p.id)
            .filter((question) => question.selected)
            .map((question) => question.question);
          const published = publishPackageDraft({
            projectId: c.p.id,
            expectedRevision,
            expectedDraftId: typeof draftId === 'string' ? draftId.trim() || undefined : undefined,
            tables: c.ctx.tables,
            selectedQuestions,
            summary: typeof summary === 'string' ? summary : undefined,
          });
          return reply.code(201).send({ published });
        } catch (error) {
          return sendPackageDraftError(reply, error);
        }
      });
    });

    // 旧版 override 写接口已停用。保留 410 响应，避免旧客户端误以为修改成功。
    r.put('/api/projects/:id/package/file', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return reply.code(410).send({
        code: 'PACKAGE_FILE_OVERRIDE_REMOVED',
        error: '旧版单文件覆盖接口已停用，请通过人工编辑草稿修改并发布新版本',
      });
    });
    r.delete('/api/projects/:id/package/file', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return reply.code(410).send({
        code: 'PACKAGE_FILE_OVERRIDE_REMOVED',
        error: '旧版单文件覆盖接口已停用；历史覆盖已由升级迁移处理',
      });
    });

    // 包修缮 agent 对话·两段式：① plan 影响分析（咨询直接回答）② 专家确认后 apply 落新版本
    r.post('/api/projects/:id/package/chat', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'package-chat');
      if (blocked) return blocked;
      return withLock(`${c.p.id}:chat`, reply, async () => {
      const { message, history } = (req.body ?? {}) as { message?: string; history?: { role: 'user' | 'assistant'; text: string }[] };
      if (!message?.trim()) return reply.code(400).send({ error: 'message 必填' });
      try {
        return await runPackageChatPlan(c.p, history ?? [], message.trim());
      } catch (e) {
        return reply.code(500).send({ error: e instanceof Error ? e.message : String(e) });
      }
      }, 'package-chat');
    });
    r.post('/api/projects/:id/package/chat/apply', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'package-chat-apply');
      if (blocked) return blocked;
      return withLock(`${c.p.id}:chat`, reply, async () => {
      const { message, history, plan } = (req.body ?? {}) as any;
      if (!plan?.changes?.length) return reply.code(400).send({ error: 'plan 必填（先调用 chat 获取修缮计划）' });
      try {
        return await runPackageChatApply(c.p, history ?? [], String(message ?? ''), plan, c.ctx.tables);
      } catch (e) {
        return reply.code(500).send({ error: e instanceof Error ? e.message : String(e) });
      }
      }, 'package-chat-apply');
    });

    // 包体检：规则引擎（确定性）+ 语义检查（LLM）；结果落库供回看
    r.post('/api/projects/:id/package/check', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'package-check');
      if (blocked) return blocked;
      return withLock(`${c.p.id}:check`, reply, async () => {
      const latest = m2repo.latestPackageVersion(c.p.id);
      if (!latest) return reply.code(404).send({ error: '尚未生成知识包' });
      const selected = m2repo.listCompetency(c.p.id).filter((q) => q.selected).map((q) => q.question);
      try {
        // 双层体检：spec 语义层（规则 + LLM 语义）+ 正式版本渲染产物层。
        const rendered = renderPackage(latest.spec);
        const formatFindings = runFormatChecks(latest.spec, rendered);
        const ruleFindings = runRuleChecks(latest.spec, c.ctx.tables, selected);
        const semanticFindings = await runSemanticCheck(c.p, latest.spec);
        const findings = [...formatFindings, ...ruleFindings, ...semanticFindings]
          .sort((a, b) => ['错误', '警告', '提示'].indexOf(a.severity) - ['错误', '警告', '提示'].indexOf(b.severity));
        saveCheck(c.p.id, latest.version, findings);
        return { version: latest.version, findings, counts: {
          错误: findings.filter((f) => f.severity === '错误').length,
          警告: findings.filter((f) => f.severity === '警告').length,
          提示: findings.filter((f) => f.severity === '提示').length,
        } };
      } catch (e) {
        return reply.code(500).send({ error: `体检失败：${e instanceof Error ? e.message : e}` });
      }
      });
    });
    r.get('/api/projects/:id/package/check', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return { check: getCheck(c.p.id) };
    });
    r.get('/api/projects/:id/package/export', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'package-export');
      if (blocked) return blocked;
      const pv = pickVersion(c.p.id, req);
      if (!pv) return reply.code(404).send({ error: '尚未生成知识包或版本不存在' });
      const rendered = renderPackage(pv.spec);
      // 导出前仍执行确定性渲染格式校验，防止异常规格产出无效交付包。
      const bad = runFormatChecks(pv.spec, rendered).filter((f) => f.severity === '错误');
      if (bad.length) {
        return reply.code(400).send({
          error: `导出中止：包格式校验未通过——${bad.map((f) => f.message).join('；')}`,
          findings: bad,
        });
      }
      const zip = zipPackage(rendered);
      reply.header('Content-Type', 'application/zip');
      reply.header('Content-Disposition', `attachment; filename="${pv.spec.id}-${pv.version}.zip"`);
      return reply.send(Buffer.from(zip));
    });

    // ---- 节点 8 数据源配置 ----
    r.get('/api/projects/:id/data-source', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return { dataSource: getProjectDataSource(c.p.id) };
    });
    r.put('/api/projects/:id/data-source', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'data-source-update');
      if (blocked) return blocked;
      const token = (req.headers.authorization ?? '').slice(7);
      const input = { ...((req.body ?? {}) as any) };
      return withLock(`${c.p.id}:data-source`, reply, async () => {
        try {
          const dataSource = await configureProjectDataSource({
            projectId: c.p.id,
            token,
            input,
            testConnection: options.dataSourceConnectionTester,
            syncConnection: options.dataSourceConsumerSync,
          });
          setProjectStage(c.p.id, 'data_source');
          return { dataSource };
        } catch (e) {
          return reply.code(400).send({ error: e instanceof Error ? e.message : String(e) });
        }
      });
    });

    r.get('/api/projects/:id/evaluation-database/target', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      try {
        return {
          database: projectEvaluationDatabaseName(c.p.id),
          ...evaluationDatabaseUrlSummary(options.evaluationDatabaseDependencies?.databaseUrl),
        };
      } catch (error) {
        return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
      }
    });

    r.post('/api/projects/:id/evaluation-database/project-database', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return withLock(`${c.p.id}:evaluation-database-create`, reply, async () => {
        try {
          const body = (req.body ?? {}) as Record<string, unknown>;
          const databaseUrl = selectEvaluationDatabaseUrlOverride(
            body.database_url_mode,
            body.database_url,
          );
          const result = await createProjectEvaluationDatabase({
            projectId: c.p.id,
            databaseUrl,
          }, options.evaluationDatabaseDependencies);
          return reply.code(result.created ? 201 : 200).send(result);
        } catch (error) {
          return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
        }
      });
    });

    r.post('/api/projects/:id/evaluation-database/import', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return withLock(`${c.p.id}:evaluation-database-import`, reply, async () => {
        try {
          assertProjectEvaluationDatabaseImportAvailable(c.p.id);
          const upload = await readEvaluationDatabaseUpload(req);
          const result = startProjectEvaluationDatabaseImport({
            projectId: c.p.id,
            ...upload,
          }, options.evaluationDatabaseDependencies);
          return reply.code(202).send(result);
        } catch (error) {
          if (error instanceof EvaluationDatabaseImportAlreadyRunningError) {
            return reply.code(409).send({ error: error.message, run: error.run });
          }
          if (error instanceof EvaluationDatabaseImportCapacityError) {
            return reply.code(429).send({ error: error.message });
          }
          return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
        }
      });
    });

    r.get('/api/projects/:id/evaluation-database/imports', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return { imports: getProjectEvaluationDatabaseImports(c.p.id) };
    });


    // Neo4j 图数据导入：Excel 仅短期暂存，Password 只存在于本次后台任务的进程内存中。
    r.post('/api/projects/:id/graph-import/uploads', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'graph-import-upload');
      if (blocked) return blocked;
      return withLock(`${c.p.id}:graph-upload`, reply, async () => {
        try {
          const file = await (req as any).file();
          if (!file) return reply.code(400).send({ error: '缺少 Excel 文件' });
          const upload = uploadGraphImportExcel({
            projectId: c.p.id,
            owner: req.user!.username,
            filename: file.filename,
            buffer: await file.toBuffer(),
          });
          return reply.code(201).send({ upload });
        } catch (error) {
          return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
        }
      });
    });

    r.post('/api/projects/:id/graph-import/uploads/:uploadId/validate', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'graph-import-validate');
      if (blocked) return blocked;
      const { mapping } = (req.body ?? {}) as any;
      if (!mapping) return reply.code(400).send({ error: 'mapping 必填' });
      try {
        const validation = validateGraphImportUpload({
          projectId: c.p.id,
          owner: req.user!.username,
          uploadId: (req.params as any).uploadId,
          mapping,
        });
        return { validation };
      } catch (error) {
        return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
      }
    });

    r.post('/api/projects/:id/graph-import/uploads/:uploadId/execute', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'graph-import-execute');
      if (blocked) return blocked;
      const { mapping, password, replaceExisting } = (req.body ?? {}) as any;
      if (!mapping) return reply.code(400).send({ error: 'mapping 必填' });
      return withLock(`${c.p.id}:graph-import`, reply, async () => {
        try {
          const started = await executeGraphImport({
            projectId: c.p.id,
            owner: req.user!.username,
            uploadId: (req.params as any).uploadId,
            mapping,
            password: String(password ?? ''),
            replaceExisting: replaceExisting === true,
          }, options.graphImportExecutionDependencies);
          return reply.code(202).send(started);
        } catch (error) {
          if (error instanceof GraphIsolationDataExistsError) {
            return reply.code(409).send({
              code: error.code,
              error: error.message,
              existing: error.existing,
            });
          }
          if (error instanceof GraphImportAlreadyRunningError) {
            return reply.code(409).send({
              code: error.code,
              error: error.message,
              run: error.run,
            });
          }
          return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
        }
      });
    });

    r.delete('/api/projects/:id/graph-import/uploads/:uploadId', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      cancelGraphImportUpload({
        projectId: c.p.id,
        owner: req.user!.username,
        uploadId: (req.params as any).uploadId,
      });
      return { ok: true };
    });

    r.get('/api/projects/:id/graph-imports', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return { imports: getGraphImportHistory(c.p.id) };
    });

    // ---- 节点 9 评测用例录入 ----
    r.get('/api/projects/:id/testcases', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return { testcases: m3repo.listTestcases(c.p.id) };
    });
    r.post('/api/projects/:id/testcases', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const { question, groundTruth } = (req.body ?? {}) as any;
      if (!question) return reply.code(400).send({ error: 'question 必填' });
      const testcase = m3repo.addTestcase(c.p.id, question, groundTruth ?? '');
      setProjectStage(c.p.id, 'testcases');
      return { testcase };
    });
    r.post('/api/projects/:id/testcases/prefill', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const selected = m2repo.listCompetency(c.p.id).filter((q) => q.selected).map((q) => q.question);
      const added = m3repo.prefillFromCompetency(c.p.id, selected);
      setProjectStage(c.p.id, 'testcases');
      return { added, testcases: m3repo.listTestcases(c.p.id) };
    });
    r.patch('/api/projects/:id/testcases/:tcId', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      m3repo.updateTestcase((req.params as any).tcId, (req.body ?? {}) as any);
      setProjectStage(c.p.id, 'testcases');
      return { testcases: m3repo.listTestcases(c.p.id) };
    });
    r.delete('/api/projects/:id/testcases/:tcId', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      m3repo.deleteTestcase((req.params as any).tcId);
      return { testcases: m3repo.listTestcases(c.p.id) };
    });

    // ---- 节点 10 评测运行 ----
    r.post('/api/projects/:id/evaluate', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'evaluate');
      if (blocked) return blocked;
      const token = (req.headers.authorization ?? '').slice(7);
      try {
        const { roundId } = startEvaluation({ projectId: c.p.id, token });
        setProjectStage(c.p.id, 'evaluate');
        return { roundId };
      } catch (e) {
        return reply.code(400).send({ error: e instanceof Error ? e.message : String(e) });
      }
    });
    r.get('/api/projects/:id/rounds', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return { rounds: m3repo.listRounds(c.p.id) };
    });
    r.get('/api/projects/:id/rounds/:roundId', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const round = m3repo.getRound((req.params as any).roundId);
      if (!round || round.projectId !== c.p.id) return reply.code(404).send({ error: '轮次不存在' });
      return { round, results: m3repo.listResults(round.id) };
    });
    r.patch('/api/projects/:id/results/:resultId', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const { verdict } = (req.body ?? {}) as any;
      if (!['pass', 'partial', 'fail'].includes(verdict)) return reply.code(400).send({ error: 'verdict 非法' });
      const res = m3repo.overrideVerdict((req.params as any).resultId, verdict);
      return { result: res };
    });

    // ---- 节点 11 优化建议与应用 ----
    r.post('/api/projects/:id/optimize/propose', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'optimize-propose');
      if (blocked) return blocked;
      return withLock(`${c.p.id}:optimize`, reply, async () => {
      const { roundId } = (req.body ?? {}) as any;
      const round = roundId ? m3repo.getRound(roundId) : m3repo.listRounds(c.p.id).find((x) => x.status === 'done');
      if (!round || round.projectId !== c.p.id) return reply.code(404).send({ error: '评测轮次不存在' });
      if (round.status !== 'done') return reply.code(400).send({ error: '该轮评测尚未完成' });
      const pv = m2repo.getPackageVersion(c.p.id, round.packageVersion) ?? m2repo.latestPackageVersion(c.p.id);
      if (!pv) return reply.code(404).send({ error: '尚未生成知识包' });
      const failed = m3repo.listResults(round.id).filter((x) => x.verdict !== 'pass');
      if (!failed.length) return reply.code(400).send({ error: '该轮评测全部通过，无需优化' });
      // 回退标注：上一轮通过、本轮未通过的用例——多为新增/修改操作抢答相邻场景（边界侵蚀）
      const prevRound = m3repo.listRounds(c.p.id).filter((x) => x.status === 'done' && x.id !== round.id)[0];
      if (prevRound) {
        const prevPass = new Set(m3repo.listResults(prevRound.id).filter((x) => x.verdict === 'pass').map((x) => x.question));
        for (const f of failed) {
          if (prevPass.has(f.question)) f.reason = `【回退：上一轮（${prevRound.packageVersion}）曾通过】${f.reason ?? ''}`;
        }
      }
      try {
        // 包体检确定性发现：直接转建议（不经 LLM），同时作为优化器的诊断上下文
        const selected = m2repo.listCompetency(c.p.id).filter((q) => q.selected).map((q) => q.question);
        const ruleFindings = runRuleChecks(pv.spec, c.ctx.tables, selected);
        const checkSuggestions = findingsToSuggestions(ruleFindings);
        const raw = await proposeOptimizations(c.p, pv.spec, failed, undefined, ruleFindings);
        // 幂等：同一问题/同一体检发现已有未拒绝的建议则跳过，防重复点击累积重复建议
        const seen = new Set(m4repo.listSuggestions(c.p.id, round.id)
          .filter((s) => s.status !== 'rejected').map((s) => s.relatedQuestion ?? s.title));
        const fresh = [...checkSuggestions, ...raw].filter((s) => {
          const k = s.relatedQuestion ?? s.title;
          if (seen.has(k)) return false;
          seen.add(k);
          return true;
        });
        const suggestions = m4repo.insertSuggestions(c.p.id, round.id, round.packageVersion, fresh);
        return { suggestions };
      } catch (e) {
        return reply.code(500).send({ error: `优化建议生成失败：${e instanceof Error ? e.message : e}` });
      }
      }, 'propose');
    });
    r.get('/api/projects/:id/optimize/suggestions', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const roundId = (req.query as any).roundId ? String((req.query as any).roundId) : undefined;
      return {
        suggestions: m4repo.listSuggestions(c.p.id, roundId),
        runningOperation: inFlight.current(`${c.p.id}:optimize`),
      };
    });
    r.post('/api/projects/:id/optimize/apply', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'optimize-apply');
      if (blocked) return blocked;
      return withLock(`${c.p.id}:optimize`, reply, async () => {
      const { suggestionIds } = (req.body ?? {}) as any;
      if (!Array.isArray(suggestionIds) || !suggestionIds.length) return reply.code(400).send({ error: 'suggestionIds 必填' });
      const adopted = m4repo.getSuggestions(suggestionIds).filter((s) => s.projectId === c.p.id && s.status === 'pending');
      if (!adopted.length) return reply.code(400).send({ error: '没有可应用的建议（可能已应用过）' });
      const latest = m2repo.latestPackageVersion(c.p.id);
      if (!latest) return reply.code(404).send({ error: '尚未生成知识包' });
      try {
        const revised = await applyOptimizations(c.p, latest.spec, adopted);
        const version = `1.${m2repo.listPackageVersions(c.p.id).length}.0`;
        const spec = { ...latest.spec, version, objects: revised.objects, operations: revised.operations };
        const selected = m2repo.listCompetency(c.p.id).filter((q) => q.selected).map((q) => q.question);
        const coverage = m2.checkCoverage(spec, selected);
        m2repo.insertPackageVersion(c.p.id, version, spec, coverage,
          `优化迭代：基于 ${adopted[0].basedVersion} 应用 ${adopted.length} 条建议`);
        m4repo.markApplied(adopted.map((s) => s.id), version);
        setProjectStage(c.p.id, 'optimize');
        return { version, applied: adopted.length, coverage };
      } catch (e) {
        return reply.code(500).send({ error: `优化应用失败：${e instanceof Error ? e.message : e}` });
      }
      }, 'apply');
    });
    r.post('/api/projects/:id/optimize/reject', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const { suggestionIds } = (req.body ?? {}) as any;
      if (!Array.isArray(suggestionIds) || !suggestionIds.length) return reply.code(400).send({ error: 'suggestionIds 必填' });
      m4repo.markRejected(suggestionIds, c.p.id);
      return { suggestions: m4repo.listSuggestions(c.p.id) };
    });

    // ---- 版本与评测汇总（评测中心 / 交付选版） ----
    r.get('/api/projects/:id/versions', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const rounds = m3repo.listRounds(c.p.id);
      const versions = m2repo.listPackageVersions(c.p.id).map((v) => ({
        ...v,
        rounds: rounds.filter((rd) => rd.packageVersion === v.version)
          .map((rd) => ({ id: rd.id, status: rd.status, passCount: rd.passCount, progressTotal: rd.progressTotal, createdAt: rd.createdAt })),
      }));
      return { versions };
    });
  });
}

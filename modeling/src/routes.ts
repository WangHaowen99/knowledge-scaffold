import type { FastifyInstance } from 'fastify';
import { AiConstructionOrchestrator } from './ai-construction/orchestrator.js';
import { AiConstructionTaskGraph } from './ai-construction/task-graph.js';
import { packageRepairAvailability } from './ai-construction/repair-availability.js';
import { errorNextActions } from './error-next-actions.js';
import {
  AiConstructionMessageStore,
  notifyAiConstructionMessageWritten,
} from './ai-construction/messages.js';
import { PackageChatConversation } from './ai-construction/package-chat-conversation.js';
import { requireAuth } from './auth/middleware.js';
import { getAuthClient, PasswordChangeRequiredError } from './auth/client.js';
import { getDb } from './db/index.js';
import { createProject, getProject, listProjects, setStage } from './projects/repo.js';
import { deleteProjectLocalData } from './projects/delete-service.js';
import {
  insertDocumentRevision,
  listDocuments,
  listDocumentRevisions,
  getDocument,
  deleteDocument,
} from './documents/repo.js';
import {
  analyzeMaterialImpact,
  applyMaterialInvalidation,
  listMaterialImpacts,
  storeMaterialImpact,
} from './documents/material-impact.js';
import { kindOf, parseDocument } from './documents/parser.js';
import { latestNodeOf, listNodes, runAnalyze, upsertNode } from './workflow/engine.js';
import * as m2 from './workflow/m2.js';
import * as m2repo from './workflow/m2-repo.js';
import { renderPackage } from './workflow/package-render.js';
import { zipPackage } from './workflow/export.js';
import { parsePackageArchive } from './workflow/package-import.js';
import { normalizeFlowchartDocument } from './flowchart/document.js';
import { renderDiagramFiles } from './flowchart/projection.js';
import { obsoleteDiagramFilePaths } from './flowchart/file-path.js';
import { advanceStage as setProjectStage } from './projects/repo.js';
import {
  deleteSetting,
  getConsumerApiUrl,
  getEvalBindingDefaults,
  getLlmSettings,
  getLlmSource,
  getSetting,
  getUserLlmSetting,
  setSetting,
  updateUserLlmSettings,
  UserLlmSettingsValidationError,
} from './settings/repo.js';
import { packageChatPlan, packageChatApply } from './workflow/package-chat.js';
import { migrateLegacyPackageOverrides } from './workflow/package-override-migration.js';
import {
  currentPackageCheckView,
  getCheck,
  runFormatChecks,
  runRuleChecks,
  runSemanticCheck,
  saveCheck,
} from './workflow/package-check.js';
import { getPackageErrorDeferral } from './workflow/package-error-deferral.js';
import {
  acceptPackageWarnings,
  getPackageWarningReview,
  saveReleaseKnownLimitations,
  type WarningAcceptancePhase,
} from './workflow/warning-acceptance.js';
import { buildReleaseReadiness } from './workflow/release.js';
import {
  isReleaseCandidateVersion,
  nextValidationCandidateVersion,
  promoteReleaseCandidate,
} from './workflow/package-version-lifecycle.js';
import * as m3repo from './workflow/m3-repo.js';
import * as m4repo from './workflow/m4-repo.js';
import {
  cancelEvaluation,
  cancelEvaluationsForProject,
  startCurrentEvaluation,
} from './evaluation/engine.js';
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
  cancelGraphImportRun,
  cancelGraphImportsForProject,
  executeGraphImport,
  GraphImportAlreadyRunningError,
  GraphImportPackageStaleError,
  getGraphImportHistory,
  uploadGraphImportExcel,
  validateGraphImportUpload,
  type GraphImportExecutionDependencies,
} from './graph-import/service.js';
import { GraphIsolationDataExistsError } from './graph-import/neo4j-writer.js';
import {
  applyOptimizations,
  normalizeOptimizationSuggestionTargets,
  OptimizationExpertReviewRequiredError,
  proposeOptimizations,
  validateOptimizationSuggestions,
} from './evaluation/optimizer.js';
import { diagnoseEvaluationFailures } from './evaluation/failure-diagnosis.js';
import * as diagnosisRepo from './evaluation/failure-diagnosis-repo.js';
import { diagnosisResolutionPlan } from './evaluation/diagnosis-resolution.js';
import {
  completeOptimizationCandidateTask,
  failOptimizationApplyTask,
  latestOptimizationApplyTask,
  markOptimizationCandidateSaving,
  recordOptimizationApplyProgress,
  resolveOptimizationExpertReview,
  startOptimizationApplyTask,
  waitOptimizationExpertReview,
} from './evaluation/optimization-apply-progress.js';
import {
  createOptimizationCandidate,
  optimizationCandidateApplyResponse,
  retireOptimizationCandidatesForDiagnosisRerun,
} from './evaluation/optimization-candidate.js';
import { buildLlm } from './llm/provider.js';
import { runWithLlmRequest } from './llm/user-context.js';
import { registerAdminLlmRoutes } from './settings/admin-llm-routes.js';
import { testLlmConnection } from './settings/llm-connection-test.js';
import {
  initializeNewUserLlmDefaults,
  isPlatformLlmDefaultAvailable,
  LlmDefaultSyncError,
  syncUserLlmToPlatformDefault,
} from './settings/llm-default-sync.js';
import { testConsumerConnectivity } from './consumer/client.js';
import { config } from './config.js';
import type { AnalysisResult, DataDictTable } from './types.js';
import { InFlightOperations, OperationAlreadyRunningError } from './in-flight-operations.js';
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
import { registerArtifactRoutes, sendArtifactError } from './artifacts/routes.js';
import { registerAuthoringRoutes } from './authoring/routes.js';
import { buildProjectAuditFiles } from './authoring/audit-export.js';
import { getAuthoringState } from './authoring/state.js';
import { getPackageArtifactStore } from './artifacts/runtime.js';
import { ArtifactRepositoryError } from './artifacts/repository.js';
import { packageIntentQuestions, validateSpecIntegrity } from './workflow/spec-validation.js';
import {
  applySemanticBindingSelection,
  previewSemanticBindingCandidates,
} from './workflow/semantic-binding-service.js';

const semanticBindingIgnoreKey = (projectId: string, revision: string) =>
  `semantic_binding_ignored:${projectId}:${revision}`;

function isSemanticBindingIgnored(projectId: string, revision: string): boolean {
  return Boolean(getSetting(semanticBindingIgnoreKey(projectId, revision)));
}

function ignoreSemanticBinding(projectId: string, revision: string): void {
  setSetting(semanticBindingIgnoreKey(projectId, revision), new Date().toISOString());
}

function semanticBindingPreview(projectId: string, revision: string, spec: any, tables: any[]) {
  const preview = previewSemanticBindingCandidates(spec, tables);
  if (!isSemanticBindingIgnored(projectId, revision)) return preview;
  return {
    ...preview,
    suggestions: [],
    fieldSuggestions: [],
    bindingCount: 0,
    fieldRefCount: 0,
  };
}

export interface RegisterRoutesOptions {
  dataSourceConnectionTester?: DataSourceConnectionTester;
  dataSourceConsumerSync?: DataSourceConsumerSync;
  evaluationDatabaseDependencies?: EvaluationDatabaseImportServiceDependencies
    & ProjectDatabaseCreationDependencies;
  graphImportExecutionDependencies?: GraphImportExecutionDependencies;
  packageChatPlan?: typeof packageChatPlan;
  packageChatApply?: typeof packageChatApply;
  runAnalyze?: typeof runAnalyze;
}

function parseDraftRevision(value: unknown): number | null {
  if (typeof value === 'number') return Number.isInteger(value) && value >= 0 ? value : null;
  if (typeof value !== 'string' || !/^\d+$/.test(value)) return null;
  const revision = Number(value);
  return Number.isSafeInteger(revision) ? revision : null;
}

function buildMaterialContext(documents: ReturnType<typeof listDocuments>): {
  tables: DataDictTable[];
  documents: Array<{
    id: string;
    filename: string;
    textContent: string;
    revisionFingerprint: string;
  }>;
} {
  return {
    tables: documents.flatMap((document) => (document.tables ?? []).map((table) => ({
      ...table,
      documentId: document.id,
    }))),
    documents: documents.flatMap((document) => document.textContent
      ? [{
        id: document.id,
        filename: document.filename,
        textContent: document.textContent,
        revisionFingerprint: [
          document.id,
          document.sourceKey ?? '',
          document.revisionNo ?? 0,
          document.contentHash ?? '',
          document.semanticHash ?? '',
        ].join(':'),
      }]
      : []),
  };
}

function materialRevisionFingerprint(documents: ReturnType<typeof listDocuments>): string {
  return documents
    .map((document) => [
      document.id,
      document.sourceKey ?? '',
      document.revisionNo ?? 0,
      document.contentHash ?? '',
      document.semanticHash ?? '',
    ].join(':'))
    .sort()
    .join('|');
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
  // 错误响应统一按 code 挂上「接下来能做什么」：逐个路由补出口既改不动也守不住，
  // 集中派生后新增错误码只要在 error-next-actions 登记一次即可。
  app.addHook('onSend', async (_req, reply, payload) => {
    if (reply.statusCode < 400 || typeof payload !== 'string') return payload;
    if (!String(reply.getHeader('content-type') ?? '').includes('application/json')) return payload;
    let body: unknown;
    try { body = JSON.parse(payload); } catch { return payload; }
    if (!body || typeof body !== 'object' || Array.isArray(body)) return payload;
    const record = body as Record<string, unknown>;
    if (record.nextActions !== undefined) return payload;
    const actions = errorNextActions(record.code);
    if (!actions) return payload;
    const next = JSON.stringify({ ...record, nextActions: actions });
    reply.header('content-length', Buffer.byteLength(next));
    return next;
  });

  const runPackageChatPlan = options.packageChatPlan ?? packageChatPlan;
  const runPackageChatApply = options.packageChatApply ?? packageChatApply;
  const runMaterialAnalysis = options.runAnalyze ?? runAnalyze;
  const packageArtifacts = getPackageArtifactStore();
  const authoringState = getAuthoringState();
  const packageChatConversation = new PackageChatConversation(new AiConstructionMessageStore(
    getDb(),
    notifyAiConstructionMessageWritten((command) => authoringState.apply(command)),
  ));
  const aiConstruction = new AiConstructionTaskGraph();
  const aiConstructionOrchestrator = new AiConstructionOrchestrator();
  const legacyMigration = await migrateLegacyPackageOverrides();
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
      if (status >= 200 && status < 300) {
        const registeredUsername = typeof body === 'object' && body !== null
          && 'user' in body && typeof body.user === 'object' && body.user !== null
          && 'username' in body.user
          ? String(body.user.username).trim()
          : '';
        if (registeredUsername) {
          try {
            initializeNewUserLlmDefaults(registeredUsername);
          } catch (error) {
            // 认证服务已完成建号，不能再把响应伪装成注册失败；记录错误供运维修复后让用户主动同步。
            req.log.error({ err: error, username: registeredUsername }, '初始化新用户 LLM 默认快照失败');
          }
        }
      }
      return reply.code(status).send(body);
    } catch {
      return reply.code(502).send({ error: '注册服务暂不可用，请稍后再试' });
    }
  });

  // admin 平台默认配置单独封装认证/角色边界，避免模板作用域与同步路由注册顺序耦合。
  app.register(async (adminRoutes) => {
    adminRoutes.addHook('onRequest', (_req, _reply, done) => runWithLlmRequest(done));
    registerAdminLlmRoutes(adminRoutes, true);
  });

  // 以下均需认证
  app.register(async (r) => {
    r.addHook('onRequest', (_req, _reply, done) => runWithLlmRequest(done));
    r.addHook('preHandler', requireAuth);

    r.get('/api/me', async (req) => ({ user: req.user }));

    // ---- 服务设置（consumer 评测端点页面可配置） ----
    r.get('/api/settings', async (req) => {
      const username = req.user!.username;
      const llm = getLlmSettings(username);
      const binding = getEvalBindingDefaults();
      return {
        consumerApiUrl: getConsumerApiUrl(),
        consumerApiUrlSource: getSetting('consumer_api_url') ? 'settings' : 'env',
        authIssuerUrl: config.auth.issuerUrl, // 只读展示：登录 issuer（auth-server）由部署配置
        llm: {
          baseUrl: llm.baseUrl, model: llm.model, maxTokens: llm.maxTokens,
          apiKeySet: !!llm.apiKey, // key 不回显
          personalApiKeySet: getUserLlmSetting(username, 'api_key') !== null,
          source: getLlmSource(username),
          userIsolated: true,
          platformDefaultAvailable: isPlatformLlmDefaultAvailable(),
        },
        evalBinding: binding,
      };
    });

    r.put('/api/settings', async (req, reply) => {
      const b = (req.body ?? {}) as Record<string, unknown>;
      const username = req.user!.username;
      const put = (key: string, v: unknown) => {
        if (v === undefined) return; // 未提交 = 不修改
        const s = String(v).trim();
        s ? setSetting(key, s) : deleteSetting(key); // 空串 = 清除（回退 env）
      };
      if (b.consumerApiUrl !== undefined) {
        const v = String(b.consumerApiUrl).trim();
        if (v && !/^https?:\/\//.test(v)) return reply.code(400).send({ error: 'consumerApiUrl 必须是 http(s) 地址' });
      }
      const hasUserLlmPatch = [
        'llmBaseUrl', 'llmModel', 'llmApiKey', 'llmApiKeyClear',
        'llmProviderClear', 'llmMaxTokens',
      ].some((key) => Object.hasOwn(b, key));
      if (hasUserLlmPatch) {
        try {
          updateUserLlmSettings(username, {
            baseUrl: b.llmBaseUrl === undefined ? undefined : String(b.llmBaseUrl),
            model: b.llmModel === undefined ? undefined : String(b.llmModel),
            apiKey: b.llmApiKey === undefined ? undefined : String(b.llmApiKey),
            apiKeyClear: b.llmApiKeyClear === true,
            providerClear: b.llmProviderClear === true,
            maxTokens: b.llmMaxTokens === undefined ? undefined : String(b.llmMaxTokens),
          });
        } catch (error) {
          if (error instanceof UserLlmSettingsValidationError) {
            return reply.code(400).send({ error: error.message });
          }
          throw error;
        }
      }
      if (b.consumerApiUrl !== undefined) put('consumer_api_url', b.consumerApiUrl);
      put('eval_binding_provider', b.evalBindingProvider);
      put('eval_binding_url', b.evalBindingUrl);
      put('eval_binding_token', b.evalBindingToken);
      put('eval_binding_scene', b.evalBindingScene);
      return { ok: true };
    });

    r.post('/api/settings/llm/sync-default', async (req, reply) => {
      try {
        syncUserLlmToPlatformDefault(req.user!.username);
        return { ok: true };
      } catch (error) {
        if (error instanceof LlmDefaultSyncError) {
          return reply.code(error.statusCode).send({ error: error.message });
        }
        throw error;
      }
    });

    // LLM 有效性测试：最小 chat/completions 调用；失败时返回原始错误（供排障）
    r.post('/api/settings/llm/test', async (req) => {
      const b = (req.body ?? {}) as Record<string, string>;
      const username = req.user!.username;
      const saved = getLlmSettings(username);
      const baseUrl = b.baseUrl || saved.baseUrl;
      const model = b.model || saved.model;
      const providerChanged = baseUrl.trim().replace(/\/+$/, '') !== saved.baseUrl.trim().replace(/\/+$/, '')
        || model.trim() !== saved.model.trim();
      // 编辑快照/部署 provider 时不得把其 Key 带到新端点；已有个人 Key 则按个人草稿保留语义复用。
      const apiKey = b.apiKey || (providerChanged
        ? (getUserLlmSetting(username, 'api_key') ?? '')
        : saved.apiKey);
      return testLlmConnection({
        baseUrl,
        apiKey,
        model,
      });
    });

    // 连通性测试：health / auth（token 透传）/ 模型就绪
    r.post('/api/settings/consumer/test', async (req) => {
      const { url } = (req.body ?? {}) as { url?: string };
      const token = (req.headers.authorization ?? '').slice(7);
      return testConsumerConnectivity(token, url);
    });

    // ---- 项目（节点 1）----
    r.get('/api/projects', async (req) => ({
      projects: listProjects(req.user!.username).map((project) => {
        const construction = aiConstruction.snapshot(project.id);
        const currentTask = construction?.tasks.find(
          (task) => task.id === construction.progress.currentTaskId,
        );
        return {
          ...project,
          construction: construction ? {
            status: construction.status,
            progress: construction.progress,
            currentTask: currentTask ? { id: currentTask.id, title: currentTask.title } : null,
          } : null,
        };
      }),
    }));

    r.post('/api/projects', async (req, reply) => {
      const b = (req.body ?? {}) as any;
      if (!b.name) return reply.code(400).send({ error: '项目名必填' });
      const project = createProject({
        owner: req.user!.username, name: b.name, domain: b.domain,
        background: b.background, targetUsers: b.targetUsers, glossaryPrefs: b.glossaryPrefs,
      });
      return reply.code(201).send({ project });
    });

    r.post('/api/package-workspaces/import', async (req, reply) => {
      const file = await (req as any).file();
      if (!file) return reply.code(400).send({ error: '请选择知识包 ZIP' });
      if (!String(file.filename || '').toLowerCase().endsWith('.zip')) {
        return reply.code(400).send({ error: '仅支持 .zip 知识包' });
      }

      let imported: ReturnType<typeof parsePackageArchive>;
      try {
        imported = parsePackageArchive(new Uint8Array(await file.toBuffer()));
      } catch (error) {
        return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
      }

      const project = createProject({
        owner: req.user!.username,
        kind: 'package',
        name: imported.spec.name,
        domain: '已有知识包',
        background: `从 ${file.filename} 导入，进入知识包工作区继续检查和改进。`,
      });
      try {
        const initial = await packageArtifacts.ensureProject(project.id);
        const saved = await packageArtifacts.importPackageVersion(project.id, {
          expectedRevision: initial.oid,
          spec: imported.spec,
          coverage: imported.coverage,
          files: imported.rawFiles,
          note: `导入 ${file.filename}`,
          action: {
            cause: 'package_import',
            summary: `导入知识包 ${imported.spec.name}`,
            actorKind: 'user',
            actorName: req.user!.username,
          },
        });
        setStage(project.id, 'review');
        const findings = runFormatChecks(imported.spec, imported.files);
        saveCheck(project.id, saved.packageVersion, findings, saved.revision.oid);
        return reply.code(201).send({
          project: getProject(project.id),
          version: saved.packageVersion,
          revision: saved.revision.oid,
          findings,
          warnings: imported.warnings,
        });
      } catch (error) {
        req.log.error({ err: error, projectId: project.id }, '导入知识包失败');
        deleteProjectLocalData(project.id, req.user!.username);
        try {
          await packageArtifacts.removeProjectArtifacts(project.id);
        } catch (cleanupError) {
          req.log.error({ err: cleanupError, projectId: project.id }, '清理失败的知识包工件仓库失败');
        }
        return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
      }
    });

    r.get('/api/projects/:id', async (req, reply) => {
      const p = getProject((req.params as any).id);
      if (!p || p.owner !== req.user!.username) return reply.code(404).send({ error: '项目不存在' });
      return {
        project: p, documents: listDocuments(p.id), nodes: listNodes(p.id),
        dataSource: getProjectDataSource(p.id),
      };
    });

    // ---- 资料上传与解析（节点 2）----
    r.get('/api/projects/:id/documents/revisions', async (req, reply) => {
      const project = getProject((req.params as any).id);
      if (!project || project.owner !== req.user!.username) {
        return reply.code(404).send({ error: '项目不存在' });
      }
      const sourceKey = String((req.query as any)?.sourceKey ?? '').trim() || undefined;
      return {
        revisions: listDocumentRevisions(project.id, sourceKey),
        impacts: listMaterialImpacts(getDb(), project.id),
      };
    });

    r.post('/api/projects/:id/documents', async (req, reply) => {
      const p = getProject((req.params as any).id);
      if (!p || p.owner !== req.user!.username) return reply.code(404).send({ error: '项目不存在' });
      return withLock(`${p.id}:document-upload`, reply, async () => {
        const file = await (req as any).file();
        if (!file) return reply.code(400).send({ error: '缺少上传文件' });
        const buffer = await file.toBuffer();
        const kind = kindOf(file.filename);
        const result = await parseDocument(kind, buffer);
        const revision = insertDocumentRevision({
          projectId: p.id, filename: file.filename, kind,
          parseStatus: result.error ? 'failed' : 'parsed', parseError: result.error,
          textContent: result.textContent, tables: result.tables, rawContent: buffer,
        });
        const doc = revision.document;
        const currentPackage = await packageArtifacts.current(p.id);
        const impactAnalysis = analyzeMaterialImpact(revision.change, currentPackage?.spec);
        const materialImpact = storeMaterialImpact(getDb(), p.id, revision.change, impactAnalysis);
        const invalidation = applyMaterialInvalidation(getDb(), p.id, impactAnalysis);
        if (p.stage === 'project_setup') setStage(p.id, 'ingest');
        const materialUpdate = aiConstructionOrchestrator.materialsUploaded(
          p.id,
          doc,
          listDocuments(p.id),
        );
        return reply.code(201).send({
          document: doc,
          materialRevision: {
            sourceKey: doc.sourceKey,
            revisionNo: doc.revisionNo,
            changeKind: revision.change.kind,
            semanticChanged: revision.change.semanticChanged,
          },
          materialImpact,
          invalidation,
          ...(materialUpdate ? {
            aiConstruction: materialUpdate.aiConstruction,
            aiConstructionMessage: materialUpdate.message,
          } : {}),
        });
      });
    });

    r.delete('/api/projects/:id/documents/:docId', async (req, reply) => {
      const p = getProject((req.params as any).id);
      if (!p || p.owner !== req.user!.username) return reply.code(404).send({ error: '项目不存在' });
      const doc = getDocument((req.params as any).docId);
      if (!doc || doc.projectId !== p.id) return reply.code(404).send({ error: '文档不存在' });
      return withLock(`${p.id}:document-delete`, reply, async () => {
        const change = deleteDocument(doc.id);
        const currentPackage = await packageArtifacts.current(p.id);
        const impactAnalysis = analyzeMaterialImpact(change, currentPackage?.spec);
        const materialImpact = storeMaterialImpact(getDb(), p.id, change, impactAnalysis);
        const invalidation = applyMaterialInvalidation(getDb(), p.id, impactAnalysis);
        return {
          deleted: doc.id,
          documents: listDocuments(p.id),
          materialImpact,
          invalidation,
        };
      });
    });

    // ---- 初步分析（节点 3）----
    r.post('/api/projects/:id/analyze', async (req, reply) => {
      const p = getProject((req.params as any).id);
      if (!p || p.owner !== req.user!.username) return reply.code(404).send({ error: '项目不存在' });
      const docs = listDocuments(p.id);
      const { tables, documents: textDocs } = buildMaterialContext(docs);
      if (!tables.length && !textDocs.length) return reply.code(400).send({ error: '尚无可分析的资料，请先上传文档' });
      const analyzeKey = `${p.id}:analyze`;
      if (inFlight.current(analyzeKey)) {
        return reply.code(409).send({ error: '项目正在处理中', code: 'busy' });
      }
      // 同步段：资料和知识包草稿的前置校验必须当场回，不能受理完再在后台失败
      const materialFingerprint = materialRevisionFingerprint(docs);
      const packageAtStart = await packageArtifacts.current(p.id);
      if (!packageAtStart) {
        return reply.code(409).send({
          code: 'PACKAGE_REVISION_REQUIRED',
          error: '请先进入统一建模工作台初始化知识包草稿',
        });
      }
      const startedAt = Date.now();
      // 先把任务标成执行中再返回，界面立刻就能显示「正在分析」
      const startedUpdate = aiConstructionOrchestrator.startMaterialAnalysis(p.id);
      // 资料分析要跑几分钟到几十分钟，不占请求：进度和结果都走事件流回前端
      void inFlight.run(analyzeKey, 'analyze', async () => {
        {
          const confirmedBrief = aiConstructionOrchestrator.latestConfirmedModelingBrief(p.id) ?? undefined;
          const { node, analysis } = await runMaterialAnalysis(
            p.id,
            tables,
            textDocs,
            undefined,
            (progress) => aiConstructionOrchestrator.reportMaterialAnalysisProgress(p.id, {
              title: progress.title,
              summary: progress.summary,
              percent: progress.percent,
              elapsedSeconds: Math.max(1, Math.round((Date.now() - startedAt) / 1_000)),
            }),
            confirmedBrief,
          );
          const [packageAtCompletion, currentDocuments] = await Promise.all([
            packageArtifacts.current(p.id),
            Promise.resolve(listDocuments(p.id)),
          ]);
          if (packageAtCompletion?.revision.oid !== packageAtStart.revision.oid) {
            const error = new Error('分析期间知识包修订已变化，请基于最新修订重试');
            error.name = 'MaterialAnalysisStaleError';
            throw error;
          }
          if (materialRevisionFingerprint(currentDocuments) !== materialFingerprint) {
            const error = new Error('分析期间建模资料已变化，请基于最新资料重试');
            error.name = 'MaterialAnalysisStaleError';
            throw error;
          }
          const completed = aiConstructionOrchestrator.completeMaterialAnalysis(p.id, {
            packageRevisionOid: packageAtStart.revision.oid,
            messageKey: `materials.analyzed.${node.id}`,
            analysis,
          });
          // 重跑早期节点不能让已到达的项目阶段倒退。
          setProjectStage(p.id, 'analyze');
          void completed;
        }
      }).catch((error) => {
        // 后台失败没有 HTTP 响应可用：failMaterialAnalysis 会写一条带
        // 「重新分析资料 / 补充建模资料」的助手消息，界面据此给出口
        try {
          aiConstructionOrchestrator.failMaterialAnalysis(
            p.id,
            error instanceof Error ? error.message : String(error),
          );
        } catch { /* 收尾失败不再向上抛 */ }
      });
      return reply.code(202).send({
        accepted: true,
        packageRevision: packageAtStart.revision.oid,
        aiConstruction: startedUpdate.aiConstruction,
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
        ?? (getRunningEvaluationDatabaseImportRun(p.id) ? 'evaluation-database-import' : null);
      if (runningOperation) {
        return reply.code(409).send({
          code: 'PROJECT_BUSY',
          error: '项目正在执行任务，请等待完成后再删除',
          operation: runningOperation,
        });
      }
      try {
        await cancelGraphImportsForProject(p.id);
        await cancelEvaluationsForProject(p.id);
        if (!deleteProjectLocalData(p.id, req.user!.username)) {
          return reply.code(404).send({ error: '项目不存在' });
        }
        try {
          await packageArtifacts.removeProjectArtifacts(p.id);
        } catch (cleanupError) {
          req.log.warn({ err: cleanupError, projectId: p.id }, '项目已删除，但知识包工件仓库清理失败');
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
      const { tables, documents: textDocs } = buildMaterialContext(docs);
      // stale（资料变更后）也返回旧分析——结果仍可用，由前端提示重跑
      const analyzeNode = latestNodeOf(listNodes(p.id), 'analyze', ['done', 'stale']);
      return { p, ctx: { tables, documents: textDocs }, analysis: analyzeNode?.output };
    };
    const packageForRound = async (projectId: string, round: m3repo.EvalRound) => {
      if (!round.artifactRevision) return null;
      const snapshot = await packageArtifacts.readPackageFiles(projectId, round.artifactRevision);
      return {
        version: snapshot.spec.version,
        spec: snapshot.spec,
        coverage: snapshot.coverage,
        revisionOid: snapshot.revision.oid,
      };
    };

    // ---- 节点 7 包评审：读文件树 / 单文件 / 导出 ----
    // ?version= 指定历史版本，缺省为最新版
    const pickVersion = async (projectId: string, req: any) => {
      const v = String((req.query as any)?.version ?? '');
      return v ? packageArtifacts.byPackageVersion(projectId, v) : packageArtifacts.current(projectId);
    };
    r.get('/api/projects/:id/package', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      try {
        const pv = await pickVersion(c.p.id, req);
        if (!pv) return reply.code(404).send({ error: '尚未生成知识包或版本不存在' });
        const [snapshot, activeDraft] = await Promise.all([
          packageArtifacts.readPackageFiles(c.p.id, pv.revision.oid),
          getActivePackageDraft(c.p.id),
        ]);
        const rendered = renderPackage(snapshot.spec);
        const indexedVersion = getDb().prepare(`SELECT revision_oid FROM package_versions
          WHERE project_id = ? AND version = ? AND revision_oid IS NOT NULL
          ORDER BY rowid DESC LIMIT 1`)
          .get(c.p.id, pv.packageVersion) as { revision_oid: string } | undefined;
        const hasUnversionedChanges = indexedVersion?.revision_oid !== pv.revision.oid;
        let nextCandidateVersion: string | null = null;
        if (activeDraft) {
          try {
            nextCandidateVersion = nextValidationCandidateVersion({
              currentVersion: pv.packageVersion,
              currentStatus: pv.spec.status ?? 'draft',
            });
          } catch { /* 已废弃版本不能继续建立候选 */ }
        }
        return {
          version: pv.packageVersion,
          status: pv.spec.status ?? 'draft',
          revision: pv.revision.oid,
          hasUnversionedChanges: hasUnversionedChanges || Boolean(activeDraft?.changedFileCount),
          nextCandidateVersion,
          coverage: pv.coverage,
          files: [...Object.keys(snapshot.files), ...snapshot.binaryFiles].sort(),
          binaryPaths: snapshot.binaryFiles,
          overriddenPaths: Object.keys(snapshot.files)
            .filter((path) => rendered[path] !== undefined && rendered[path] !== snapshot.files[path])
            .sort(),
          versions: m2repo.listPackageVersions(c.p.id),
          draft: activeDraft,
        };
      } catch (error) {
        return sendArtifactError(reply, error);
      }
    });
    r.get('/api/projects/:id/package/file', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const path = String((req.query as any).path || '');
      try {
        const pv = await pickVersion(c.p.id, req);
        if (!pv) return reply.code(404).send({ error: '尚未生成知识包或版本不存在' });
        const file = await packageArtifacts.readFile(c.p.id, `package/${path}`, pv.revision.oid);
        const rendered = renderPackage(pv.spec)[path];
        return {
          path,
          content: file.content,
          revision: file.revision.oid,
          overridden: rendered !== undefined && rendered !== file.content,
          rendered,
        };
      } catch (error) {
        return sendArtifactError(reply, error);
      }
    });

    r.get('/api/projects/:id/package/semantic-bindings/suggestions', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      try {
        const current = await packageArtifacts.current(c.p.id);
        if (!current) return reply.code(404).send({ error: '尚未生成知识包' });
        return {
          revision: current.revision.oid,
          ...semanticBindingPreview(c.p.id, current.revision.oid, current.spec, c.ctx.tables),
        };
      } catch (error) {
        return sendArtifactError(reply, error);
      }
    });

    r.post('/api/projects/:id/package/semantic-bindings/ignore', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const body = (req.body ?? {}) as Record<string, unknown>;
      const expectedRevision = String(body.expectedRevision ?? '').trim();
      if (!expectedRevision) return reply.code(400).send({ error: 'expectedRevision 必填' });
      const current = await packageArtifacts.current(c.p.id);
      if (!current) return reply.code(404).send({ error: '尚未生成知识包' });
      if (current.revision.oid !== expectedRevision) {
        return reply.code(409).send({
          code: 'REVISION_CONFLICT',
          error: '知识包已更新，请刷新后重试',
          currentRevision: current.revision.oid,
        });
      }
      ignoreSemanticBinding(c.p.id, current.revision.oid);
      return {
        revision: current.revision.oid,
        ...semanticBindingPreview(c.p.id, current.revision.oid, current.spec, c.ctx.tables),
      };
    });

    r.post('/api/projects/:id/package/semantic-bindings/apply', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageRepair(reply, c.p.id);
      if (blocked) return blocked;
      const body = (req.body ?? {}) as Record<string, unknown>;
      const expectedRevision = String(body.expectedRevision ?? '').trim();
      const bindingKeys = Array.isArray(body.bindingKeys) ? body.bindingKeys.map(String) : [];
      const fieldRefs = Array.isArray(body.fieldRefs)
        ? body.fieldRefs.map((item: any) => ({
          objectKey: String(item?.objectKey ?? '').trim(),
          fieldKey: String(item?.fieldKey ?? '').trim(),
          bindingKey: String(item?.bindingKey ?? '').trim(),
        }))
        : [];
      if (!expectedRevision) return reply.code(400).send({ error: 'expectedRevision 必填' });
      if (!bindingKeys.length && !fieldRefs.length) return reply.code(400).send({ error: '请至少选择一个术语绑定或字段关联' });
      return withLock(`${c.p.id}:semantic-binding`, reply, async () => {
        try {
          const current = await packageArtifacts.current(c.p.id);
          if (!current) return reply.code(404).send({ error: '尚未生成知识包' });
          if (current.revision.oid !== expectedRevision) {
            return reply.code(409).send({
              code: 'REVISION_CONFLICT',
              error: '知识包已更新，请刷新后重试',
              currentRevision: current.revision.oid,
            });
          }
          const applied = applySemanticBindingSelection(current.spec, c.ctx.tables, { bindingKeys, fieldRefs });
          const saved = await packageArtifacts.savePackageVersion(c.p.id, {
            expectedRevision,
            spec: applied.spec,
            coverage: current.coverage,
            action: {
              cause: 'semantic_binding_confirmed',
              summary: '确认术语绑定和字段关联',
              actorKind: 'user',
              actorName: req.user!.username,
            },
          });
          getAuthoringState().apply({
            kind: 'publish_event',
            projectId: c.p.id,
            type: 'package.revision.created',
            payload: { revisionId: saved.revision.oid, cause: 'semantic_binding_confirmed' },
          });
          return {
            revision: saved.revision.oid,
            appliedBindings: applied.appliedBindings,
            appliedFieldRefs: applied.appliedFieldRefs,
          };
        } catch (error) {
          return sendArtifactError(reply, error);
        }
      }, 'semantic-binding');
    });

    // 人工编辑草稿后端。草稿发布后固化为一个正式版本；活动草稿期间消费与修缮入口统一门禁。
    r.get('/api/projects/:id/package/draft', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return { draft: await getActivePackageDraft(c.p.id) };
    });
    r.post('/api/projects/:id/package/draft', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageRepair(reply, c.p.id);
      if (blocked) return blocked;
      try {
        const result = await createOrGetPackageDraft(c.p.id, req.user!.username);
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
        return { file: await getPackageDraftFile(c.p.id, path) };
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
        return await savePackageDraftFile({
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
        return await revertPackageDraftFile({
          projectId: c.p.id,
          path,
          expectedRevision,
          expectedDraftId,
        });
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
          const published = await publishPackageDraft({
            projectId: c.p.id,
            expectedRevision,
            expectedDraftId: typeof draftId === 'string' ? draftId.trim() || undefined : undefined,
            tables: c.ctx.tables,
            summary: typeof summary === 'string' ? summary : undefined,
            actorUsername: req.user!.username,
          });
          return reply.code(201).send({ published });
        } catch (error) {
          return sendPackageDraftError(reply, error);
        }
      });
    });

    // 已确认流程以 processes.json 为唯一事实源；Mermaid 文件只由该文档单向投影。
    r.put('/api/projects/:id/package/diagram', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'package-diagram-edit');
      if (blocked) return blocked;
      const { document: rawDocument, expectedRevision } = (req.body ?? {}) as Record<string, unknown>;
      if (typeof expectedRevision !== 'string' || !expectedRevision) {
        return reply.code(400).send({ error: '流程图保存基线不完整' });
      }
      let document;
      try {
        document = normalizeFlowchartDocument(rawDocument);
      } catch (error) {
        return reply.code(400).send({
          error: error instanceof Error ? error.message : '流程图内容不正确',
        });
      }
      if (!document.diagrams.length) {
        return reply.code(400).send({ error: '已确认知识包至少需要保留一个业务流程' });
      }
      let projected: Record<string, string>;
      try {
        projected = renderDiagramFiles(document);
      } catch (error) {
        return reply.code(400).send({
          error: error instanceof Error ? error.message : '流程图文件名不正确',
        });
      }
      return withLock(`${c.p.id}:package-diagram`, reply, async () => {
        try {
          const current = await packageArtifacts.current(c.p.id);
          if (!current) return reply.code(404).send({ error: '尚未生成知识包' });
          if (current.revision.oid !== expectedRevision) {
            return reply.code(409).send({
              code: 'REVISION_CONFLICT',
              error: '知识包流程图已更新，请刷新后重试',
              currentRevision: current.revision.oid,
            });
          }
          const currentFiles = await packageArtifacts.readPackageFiles(c.p.id, current.revision.oid);
          const deletePaths = obsoleteDiagramFilePaths(currentFiles.files, document)
            .map((path) => `package/${path}`);
          const saved = await packageArtifacts.saveFiles(c.p.id, {
            expectedRevision,
            files: Object.fromEntries(Object.entries(projected).map(([path, content]) => (
              [`package/${path}`, content]
            ))),
            deletePaths,
            action: {
              cause: 'flowchart_edit',
              summary: '更新已确认业务流程',
              actorKind: 'user',
              actorName: req.user!.username,
            },
          });
          getAuthoringState().apply({
            kind: 'publish_event',
            projectId: c.p.id,
            type: 'package.revision.created',
            payload: { revisionId: saved.revision.oid, cause: 'flowchart_edit' },
          });
          return { document, revision: saved.revision.oid };
        } catch (error) {
          return sendArtifactError(reply, error);
        }
      }, 'package-diagram');
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

    // 建模助手请求只登记消息并启动后台分析；结果经结构化消息和项目事件流回传。
    r.post('/api/projects/:id/package/chat', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'package-chat');
      if (blocked) return blocked;
      const { message, requestId } = (req.body ?? {}) as { message?: string; requestId?: string };
      if (!message?.trim()) return reply.code(400).send({ error: '请输入内容' });
      const chatKey = `${c.p.id}:chat`;
      if (inFlight.current(chatKey)) return reply.code(409).send({ error: '项目正在处理中' });

      const turnId = String(requestId || req.id);
      const history = packageChatConversation.history(c.p.id);
      const userMessage = packageChatConversation.beginTurn(c.p.id, turnId, message.trim());
      const started = authoringState.apply({
        kind: 'start_run',
        projectId: c.p.id,
        intent: '建模助手分析',
        state: { purpose: 'package_chat', turnId },
      });
      if (started.kind !== 'run') throw new Error('建模助手任务创建失败');
      const runId = started.run.id;
      const finishRun = (status: 'completed' | 'failed', error?: unknown) => {
        try {
          authoringState.apply({
            kind: 'update_run',
            projectId: c.p.id,
            runId,
            status,
            ...(status === 'failed' ? {
              error: (error instanceof Error ? error.message : String(error)).slice(0, 2_000)
                || '建模助手处理失败',
            } : {}),
          });
        } catch { /* 对话消息已经落库，运行状态收尾失败不覆盖结果 */ }
      };

      void inFlight.run(chatKey, 'package-chat', async () => {
        const result = await runPackageChatPlan(c.p, history, message.trim(), {
          documents: c.ctx.documents,
          analysis: c.analysis as AnalysisResult | undefined,
          documentAccess: 'full-read-only',
        });
        packageChatConversation.finishTurn(c.p.id, turnId, result);
      }).then(
        () => finishRun('completed'),
        (error) => {
          packageChatConversation.failTurn(c.p.id, turnId, '建模助手处理失败', error);
          finishRun('failed', error);
        },
      );
      return reply.code(202).send({
        status: 'accepted',
        requestId: turnId,
        runId,
        aiConstructionMessages: [userMessage],
      });
    });
    r.post('/api/projects/:id/package/chat/apply', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return reply.code(410).send({
        code: 'CONFIRMED_EXECUTION_REQUIRED',
        error: '该接口已停用，请通过建模助手待办卡确认或取消修改方案',
      });
    });

    // 包体检：规则引擎（确定性）+ 语义检查（LLM）；结果落库供回看
    r.post('/api/projects/:id/package/check', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'package-check');
      if (blocked) return blocked;
      return withLock(`${c.p.id}:check`, reply, async () => {
      const latest = await packageArtifacts.current(c.p.id);
      if (!latest) return reply.code(404).send({ error: '尚未生成知识包' });
      try {
        // 双层体检：结构化 spec 规则 + 当前 Git 修订中的真实交付文件。
        const snapshot = await packageArtifacts.readPackageFiles(c.p.id, latest.revision.oid);
        const formatFindings = runFormatChecks(latest.spec, snapshot.files);
        const ruleFindings = runRuleChecks(latest.spec, c.ctx.tables, packageIntentQuestions(latest.spec));
        const semanticFindings = await runSemanticCheck(c.p, latest.spec);
        const findings = [
          ...formatFindings,
          ...ruleFindings,
          ...semanticFindings,
        ]
          .sort((a, b) => ['错误', '警告', '提示'].indexOf(a.severity) - ['错误', '警告', '提示'].indexOf(b.severity));
        saveCheck(c.p.id, latest.packageVersion, findings, latest.revision.oid);
        return {
          version: latest.packageVersion,
          revision: latest.revision.oid,
          findings,
          errorDeferral: getPackageErrorDeferral(c.p.id, latest.revision.oid),
          warningReview: getPackageWarningReview(c.p.id, latest.revision.oid),
          repair: packageRepairAvailability({
            errorCount: findings.filter((f) => f.severity === '错误').length,
            hasActiveDraft: Boolean(getActivePackageDraft(c.p.id)),
            graph: aiConstruction.snapshot(c.p.id),
          }),
          counts: {
          错误: findings.filter((f) => f.severity === '错误').length,
          警告: findings.filter((f) => f.severity === '警告').length,
          提示: findings.filter((f) => f.severity === '提示').length,
          },
        };
      } catch (e) {
        return reply.code(500).send({ error: `体检失败：${e instanceof Error ? e.message : e}` });
      }
      });
    });
    r.get('/api/projects/:id/package/check', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const currentPackage = await packageArtifacts.current(c.p.id);
      const view = currentPackageCheckView(
        getCheck(c.p.id),
        currentPackage?.revision.oid ?? null,
      );
      const check = view.check;
      return {
        ...view,
        errorDeferral: check?.revision ? getPackageErrorDeferral(c.p.id, check.revision) : null,
        warningReview: check?.revision ? getPackageWarningReview(c.p.id, check.revision) : null,
        repair: packageRepairAvailability({
          errorCount: check?.findings.filter((f) => f.severity === '错误').length ?? 0,
          hasActiveDraft: Boolean(getActivePackageDraft(c.p.id)),
          graph: aiConstruction.snapshot(c.p.id),
        }),
      };
    });
    r.post('/api/projects/:id/package/warnings/accept', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const current = await packageArtifacts.current(c.p.id);
      if (!current) return reply.code(404).send({ error: '尚未生成知识包' });
      const body = (req.body ?? {}) as any;
      const revision = String(body.revision ?? '');
      const fingerprints = Array.isArray(body.fingerprints)
        ? body.fingerprints.map(String).filter(Boolean)
        : [];
      const phase = String(body.phase ?? '') as WarningAcceptancePhase;
      if (revision !== current.revision.oid) {
        return reply.code(409).send({ error: '知识包内容已更新，请重新检查警告' });
      }
      if (!['validation', 'release'].includes(phase)) {
        return reply.code(400).send({ error: '警告确认阶段不正确' });
      }
      try {
        return {
          warningReview: acceptPackageWarnings({
            projectId: c.p.id,
            revision,
            fingerprints,
            note: String(body.note ?? ''),
            actor: req.user!.username,
            actorRole: 'project_owner',
            phase,
          }),
        };
      } catch (error) {
        return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
      }
    });
    r.get('/api/projects/:id/package/export', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'package-export');
      if (blocked) return blocked;
      const pv = await pickVersion(c.p.id, req);
      if (!pv) return reply.code(404).send({ error: '尚未生成知识包或版本不存在' });
      const [snapshot, archive] = await Promise.all([
        packageArtifacts.readPackageFiles(c.p.id, pv.revision.oid),
        packageArtifacts.readPackageArchiveFiles(c.p.id, pv.revision.oid),
      ]);
      // 导出前仍执行确定性渲染格式校验，防止异常规格产出无效交付包。
      const bad = runFormatChecks(pv.spec, snapshot.files).filter((f) => f.severity === '错误');
      if (bad.length) {
        return reply.code(400).send({
          error: `导出中止：包格式校验未通过——${bad.map((f) => f.message).join('；')}`,
          findings: bad,
        });
      }
      const zip = zipPackage(archive.files);
      reply.header('Content-Type', 'application/zip');
      reply.header('Content-Disposition', `attachment; filename="${pv.spec.id}-${pv.packageVersion}.zip"`);
      return reply.send(Buffer.from(zip));
    });
    r.get('/api/projects/:id/audit/export', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const current = await packageArtifacts.current(c.p.id);
      const snapshot = current
        ? await packageArtifacts.readPackageFiles(c.p.id, current.revision.oid)
        : null;
      const files = buildProjectAuditFiles(getDb(), c.p.id, {
        ...(current ? { packageRevision: current.revision.oid } : {}),
        packageFiles: snapshot?.files ?? {},
      });
      const zip = zipPackage(files, `modeling-audit-${c.p.id}`);
      reply.header('Content-Type', 'application/zip');
      reply.header('Content-Disposition', `attachment; filename="${c.p.id}-modeling-audit.zip"`);
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
          const upload = await uploadGraphImportExcel({
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
        const validation = await validateGraphImportUpload({
          projectId: c.p.id,
          owner: req.user!.username,
          uploadId: (req.params as any).uploadId,
          mapping,
        });
        return { validation };
      } catch (error) {
        if (error instanceof GraphImportPackageStaleError) {
          return reply.code(409).send({
            code: error.code,
            error: error.message,
            uploadRevision: error.uploadRevision,
            currentRevision: error.currentRevision,
          });
        }
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
          if (error instanceof GraphImportPackageStaleError) {
            return reply.code(409).send({
              code: error.code,
              error: error.message,
              uploadRevision: error.uploadRevision,
              currentRevision: error.currentRevision,
            });
          }
          return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
        }
      });
    });

    r.delete('/api/projects/:id/graph-import/uploads/:uploadId', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      await cancelGraphImportUpload({
        projectId: c.p.id,
        owner: req.user!.username,
        uploadId: (req.params as any).uploadId,
      });
      return { ok: true };
    });

    r.post('/api/projects/:id/graph-imports/:runId/cancel', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      try {
        const run = await cancelGraphImportRun(c.p.id, (req.params as any).runId);
        return { run };
      } catch (error) {
        return reply.code(409).send({ error: error instanceof Error ? error.message : String(error) });
      }
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
    r.post('/api/projects/:id/testcases/batch', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const { cases } = (req.body ?? {}) as { cases?: Array<{ question?: string; groundTruth?: string }> };
      if (!Array.isArray(cases)) return reply.code(400).send({ error: '请提供真实评测用例数组' });
      try {
        const result = m3repo.addTestcases(c.p.id, cases.map((item) => ({
          question: String(item?.question ?? ''),
          groundTruth: String(item?.groundTruth ?? ''),
        })));
        setProjectStage(c.p.id, 'testcases');
        return result;
      } catch (error) {
        return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
      }
    });
    r.post('/api/projects/:id/testcases/prefill', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const current = await packageArtifacts.current(c.p.id);
      if (!current) return reply.code(409).send({ error: '请先生成知识包' });
      const added = m3repo.prefillFromPackage(c.p.id, packageIntentQuestions(current.spec));
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
      const { refresh_token: refreshToken } = (req.body ?? {}) as { refresh_token?: string };
      try {
        const { roundId } = await startCurrentEvaluation({
          projectId: c.p.id,
          owner: req.user!.username,
          token,
          refreshToken,
        });
        setProjectStage(c.p.id, 'evaluate');
        return { roundId };
      } catch (e) {
        return reply.code(400).send({ error: e instanceof Error ? e.message : String(e) });
      }
    });
    r.post('/api/projects/:id/evaluate/cancel', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const roundId = String((req.body as any)?.roundId ?? '').trim() || undefined;
      if (roundId) {
        const round = m3repo.getRound(roundId);
        if (!round || round.projectId !== c.p.id) return reply.code(404).send({ error: '评测轮次不存在' });
      }
      try {
        return { round: await cancelEvaluation(c.p.id, roundId) };
      } catch (error) {
        return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
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
      return m3repo.getRoundDetails(round.id);
    });
    r.patch('/api/projects/:id/results/:resultId', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const { verdict, reason } = (req.body ?? {}) as any;
      if (!['pass', 'partial', 'fail'].includes(verdict)) return reply.code(400).send({ error: '评测结果不正确' });
      const resultId = String((req.params as any).resultId);
      if (!m3repo.resultBelongsToProject(resultId, c.p.id)) {
        return reply.code(404).send({ error: '评测结果不存在' });
      }
      try {
        const res = m3repo.overrideVerdict(resultId, verdict, {
          reason: String(reason ?? ''), actor: req.user!.username,
        });
        return { result: res };
      } catch (error) {
        return reply.code(400).send({ error: error instanceof Error ? error.message : String(error) });
      }
    });

    // ---- 节点 11 失败诊断 → 优化建议 → 候选修订确认 ----
    r.post('/api/projects/:id/optimize/diagnose', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return withLock(`${c.p.id}:optimize`, reply, async () => {
        const { roundId } = (req.body ?? {}) as any;
        const round = roundId
          ? m3repo.getRound(roundId)
          : m3repo.listRounds(c.p.id).find((item) => ['done', 'failed'].includes(item.status));
        if (!round || round.projectId !== c.p.id) return reply.code(404).send({ error: '评测轮次不存在' });
        if (!['done', 'failed'].includes(round.status)) return reply.code(400).send({ error: '该轮评测尚未完成' });
        const version = await packageForRound(c.p.id, round);
        if (!version) return reply.code(409).send({ error: '该轮评测未记录工件修订，不能据此生成诊断' });
        const failed = m3repo.listResults(round.id).filter((item) => item.verdict !== 'pass');
        if (round.status === 'done' && !failed.length) return reply.code(400).send({ error: '该轮评测全部通过，无需诊断' });
        const analyzeNode = latestNodeOf(listNodes(c.p.id), 'analyze', ['done', 'stale']);
        const qualityGaps = ((analyzeNode?.output as any)?.quality?.gaps ?? []) as any[];
        try {
          const report = await diagnoseEvaluationFailures({
            projectId: c.p.id,
            results: failed,
            spec: version.spec,
            qualityGaps,
            traces: m3repo.listResultTraces(round.id),
            tables: c.ctx.tables,
            documents: c.ctx.documents,
            roundFailure: round.status === 'failed' ? round.log : undefined,
          }, buildLlm());
          const diagnoses = diagnosisRepo.replaceFailureDiagnoses(
            c.p.id, round.id, round.packageVersion, version.revisionOid, report.diagnoses,
          );
          // 重跑诊断会换一组诊断 ID；同步关闭只由该轮优化建议关联的旧候选。
          await retireOptimizationCandidatesForDiagnosisRerun({
            store: packageArtifacts,
            projectId: c.p.id,
            roundId: round.id,
          });
          return {
            diagnoses,
            canProposeOptimizations: diagnosisRepo.hasConfirmedOptimizableDiagnoses(diagnoses),
          };
        } catch (error) {
          return reply.code(500).send({ error: `失败原因分析失败：${error instanceof Error ? error.message : error}` });
        }
      }, 'diagnose');
    });
    r.get('/api/projects/:id/optimize/diagnoses', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const roundId = (req.query as any).roundId ? String((req.query as any).roundId) : undefined;
      const diagnoses = diagnosisRepo.listFailureDiagnoses(c.p.id, roundId);
      return {
        diagnoses,
        canProposeOptimizations: diagnosisRepo.hasConfirmedOptimizableDiagnoses(diagnoses),
      };
    });
    // 评测发现的知识包缺陷回流建模：登记成 evaluation_repair 任务并在助手对话里给出下一步。
    // 只登记不执行——实际修改由专家在优化更新里发起（那里才有防过拟合校验）。
    r.post('/api/projects/:id/optimize/diagnoses/plan-repair', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const { roundId, diagnosisIds } = (req.body ?? {}) as { roundId?: string; diagnosisIds?: unknown };
      const requested = Array.isArray(diagnosisIds) ? diagnosisIds.map((id) => String(id)) : [];
      if (!roundId || !requested.length) {
        return reply.code(400).send({ error: '请选择要回流建模的失败原因', code: 'INVALID_INPUT' });
      }
      const round = m3repo.getRound(String(roundId));
      if (!round || round.projectId !== c.p.id) return reply.code(404).send({ error: '评测轮次不存在' });

      const selected = new Set(requested);
      const diagnoses = diagnosisRepo.listFailureDiagnoses(c.p.id, round.id)
        .filter((diagnosis) => selected.has(diagnosis.id));
      if (diagnoses.length !== selected.size) {
        return reply.code(404).send({ error: '部分失败原因不存在或不属于该评测轮次' });
      }
      const notPackageDefects = diagnoses.filter((diagnosis) => diagnosis.repairScope !== 'package');
      if (notPackageDefects.length) {
        return reply.code(409).send({
          error: `有 ${notPackageDefects.length} 项失败原因不属于知识包缺陷，请先处理运行环境、数据或用例问题`,
          code: 'NOT_PACKAGE_DEFECT',
        });
      }
      const blockedPackageDefects = diagnoses.filter((diagnosis) => diagnosis.blocking);
      if (blockedPackageDefects.length) {
        return reply.code(409).send({
          error: `有 ${blockedPackageDefects.length} 项知识包诊断仍会阻断优化，请先完成专家确认`,
          code: 'BLOCKING_PACKAGE_DEFECT',
        });
      }
      const closedDiagnoses = diagnoses.filter((diagnosis) => ['resolved', 'irrelevant']
        .includes(diagnosis.resolutionStatus));
      if (closedDiagnoses.length) {
        return reply.code(409).send({
          error: `有 ${closedDiagnoses.length} 项失败诊断已关闭，请刷新后重新选择`,
          code: 'CLOSED_DIAGNOSIS',
        });
      }
      const latest = await packageArtifacts.current(c.p.id);
      try {
        const result = aiConstructionOrchestrator.recordEvaluationDefects(c.p.id, {
          roundId: round.id,
          packageVersion: round.packageVersion,
          ...(latest ? { packageRevisionOid: latest.revision.oid } : {}),
          defects: diagnoses.map((diagnosis) => ({
            id: diagnosis.id,
            title: diagnosis.title,
            summary: diagnosis.summary,
            target: diagnosis.relatedQuestions[0],
            suggestedAction: diagnosis.suggestedAction,
          })),
        });
        const optimizableIds = diagnoses
          .filter((diagnosis) => !diagnosis.blocking)
          .map((diagnosis) => diagnosis.id);
        if (optimizableIds.length) {
          // 用户在失败诊断页的明确勾选就是本轮知识包修复范围确认，后续建议/候选链路统一读取该状态。
          diagnosisRepo.markFailureDiagnosesForRepair({
            projectId: c.p.id,
            diagnosisIds: optimizableIds,
            actor: req.user!.username,
            note: '用户选择转为建模修缮任务',
            payload: { source: 'evaluation_repair', roundId: round.id },
          });
        }
        const currentDiagnoses = diagnosisRepo.listFailureDiagnoses(c.p.id, round.id);
        return {
          aiConstruction: result.aiConstruction,
          ...(result.message ? { aiConstructionMessage: result.message } : {}),
          diagnoses: currentDiagnoses,
          canProposeOptimizations: diagnosisRepo.hasConfirmedOptimizableDiagnoses(currentDiagnoses),
        };
      } catch (error) {
        return reply.code(409).send({ error: error instanceof Error ? error.message : String(error) });
      }
    });

    r.post('/api/projects/:id/optimize/diagnoses/:diagnosisId/resolve', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const diagnosisId = String((req.params as any).diagnosisId ?? '');
      const { action, note } = (req.body ?? {}) as {
        action?: diagnosisRepo.DiagnosisResolutionAction;
        note?: string;
      };
      const allowed: diagnosisRepo.DiagnosisResolutionAction[] = [
        'confirm_rule', 'clarify_semantics', 'data_updated', 'not_applicable', 'defer',
      ];
      if (!action || !allowed.includes(action)) return reply.code(400).send({ error: '专家处置动作不正确' });
      const expertNote = String(note ?? '').trim();
      if (['confirm_rule', 'clarify_semantics', 'not_applicable'].includes(action) && !expertNote) {
        return reply.code(400).send({ error: '请填写专家确认的规则、语义或不适用原因' });
      }
      try {
        const stored = diagnosisRepo.listFailureDiagnoses(c.p.id)
          .find((item) => item.id === diagnosisId);
        if (!stored) return reply.code(404).send({ error: '失败诊断不存在' });
        const current = await packageArtifacts.current(c.p.id);
        if (!current || stored.basedRevisionOid !== current.revision.oid) {
          return reply.code(409).send({ error: '失败诊断基于旧工件修订，请对当前修订重新评测并诊断' });
        }
        const plan = diagnosisResolutionPlan(action);
        const diagnosis = diagnosisRepo.resolveFailureDiagnosis({
          projectId: c.p.id,
          diagnosisId,
          action,
          note: expertNote,
          actor: req.user!.username,
          payload: { ...plan },
        });
        try {
          getAuthoringState().apply({
            kind: 'publish_event',
            projectId: c.p.id,
            type: 'diagnosis.expert_resolution.recorded',
            payload: {
              diagnosisId,
              action,
              note: expertNote,
              nextSteps: plan.nextSteps,
            },
          });
        } catch { /* 兼容尚未启用事件消费的 PR5 环境 */ }
        const diagnoses = diagnosisRepo.listFailureDiagnoses(c.p.id, diagnosis.roundId);
        return {
          diagnosis,
          diagnoses,
          canProposeOptimizations: diagnosisRepo.hasConfirmedOptimizableDiagnoses(diagnoses),
          nextSteps: plan.nextSteps,
        };
      } catch (error) {
        return reply.code(404).send({ error: error instanceof Error ? error.message : String(error) });
      }
    });
    r.post('/api/projects/:id/optimize/propose', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'optimize-propose');
      if (blocked) return blocked;
      return withLock(`${c.p.id}:optimize`, reply, async () => {
        const { roundId } = (req.body ?? {}) as any;
        const round = roundId
          ? m3repo.getRound(roundId)
          : m3repo.listRounds(c.p.id).find((item) => ['done', 'failed'].includes(item.status));
        if (!round || round.projectId !== c.p.id) return reply.code(404).send({ error: '评测轮次不存在' });
        if (!['done', 'failed'].includes(round.status)) return reply.code(400).send({ error: '该轮评测尚未完成' });
        const pv = await packageForRound(c.p.id, round);
        if (!pv) return reply.code(409).send({ error: '该轮评测未记录工件修订，不能据此生成优化建议' });
        const current = await packageArtifacts.current(c.p.id);
        if (!current || current.revision.oid !== pv.revisionOid) {
          return reply.code(409).send({ error: '该轮评测基于旧工件修订，请对当前修订重新评测并诊断' });
        }
        const failed = m3repo.listResults(round.id).filter((item) => item.verdict !== 'pass');
        if (!failed.length) return reply.code(400).send({ error: '该轮评测全部通过，无需优化' });
        const diagnoses = diagnosisRepo.listFailureDiagnoses(c.p.id, round.id)
          .filter((item) => item.basedRevisionOid === pv.revisionOid);
        if (!diagnoses.length) return reply.code(409).send({ error: '请先分析该轮评测的失败原因' });
        const eligibleDiagnoses = diagnoses.filter((item) => item.resolutionStatus === 'confirmed'
          && item.packageOptimizable
          && !item.blocking);
        const eligibleResultIds = new Set(eligibleDiagnoses.flatMap((item) => item.resultIds));
        const optimizableFailures = failed.filter((item) => eligibleResultIds.has(item.id));
        if (!optimizableFailures.length) {
          return reply.code(409).send({
            error: '当前失败原因需要先修正用例、运行环境或数据，暂不应修改知识包',
            diagnoses,
          });
        }
        const prevRound = m3repo.listRounds(c.p.id)
          .find((item) => item.status === 'done' && item.id !== round.id);
        if (prevRound) {
          const prevPass = new Set(m3repo.listResults(prevRound.id)
            .filter((item) => item.verdict === 'pass').map((item) => item.question));
          for (const failure of optimizableFailures) {
            if (prevPass.has(failure.question)) {
              failure.reason = `【回退：上一轮（${prevRound.packageVersion}）曾通过】${failure.reason ?? ''}`;
            }
          }
        }
        try {
          const ruleFindings = runRuleChecks(pv.spec, c.ctx.tables, packageIntentQuestions(pv.spec));
          const raw = await proposeOptimizations(
            c.p, pv.spec, optimizableFailures, undefined, ruleFindings, diagnoses,
          );
          const seen = new Set(m4repo.listSuggestions(c.p.id, round.id)
            .filter((item) => item.status !== 'rejected')
            .map((item) => `${(item.diagnosisIds ?? []).slice().sort().join(',')}|${item.kind}|${item.target ?? ''}|${item.relatedQuestion ?? item.title}`));
          const fresh = raw.filter((item) => {
            const key = `${(item.diagnosisIds ?? []).slice().sort().join(',')}|${item.kind}|${item.target ?? ''}|${item.relatedQuestion ?? item.title}`;
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
          });
          const suggestions = m4repo.insertSuggestions(
            c.p.id, round.id, round.packageVersion, pv.revisionOid, fresh,
          );
          return { suggestions };
        } catch (error) {
          return reply.code(500).send({ error: `优化建议生成失败：${error instanceof Error ? error.message : error}` });
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
        runningTask: inFlight.status(`${c.p.id}:optimize`),
      };
    });
    r.get('/api/projects/:id/optimize/apply-status', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return { task: latestOptimizationApplyTask(c.p.id) };
    });
    r.post('/api/projects/:id/optimize/apply', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'optimize-apply');
      if (blocked) return blocked;
      return withLock(`${c.p.id}:optimize`, reply, async () => {
        const { suggestionIds } = (req.body ?? {}) as any;
        if (!Array.isArray(suggestionIds) || !suggestionIds.length) {
          return reply.code(400).send({ error: '请选择建议' });
        }
        const uniqueSuggestionIds = [...new Set(suggestionIds.map((id: unknown) => String(id)))];
        let adopted = m4repo.getSuggestions(uniqueSuggestionIds)
          .filter((item) => item.projectId === c.p.id && item.status === 'pending');
        if (!adopted.length) return reply.code(400).send({ error: '没有可应用的建议（可能已应用过）' });
        const latest = await packageArtifacts.current(c.p.id);
        if (!latest) return reply.code(404).send({ error: '尚未生成知识包' });
        if (adopted.some((item) => item.basedRevisionOid !== latest.revision.oid)) {
          return reply.code(409).send({ error: '优化建议基于旧工件修订生成，请对当前修订重新评测并分析失败原因' });
        }
        try {
          adopted = normalizeOptimizationSuggestionTargets(adopted, latest.spec);
        } catch (error) {
          return reply.code(409).send({
            error: `优化建议的修复目标无法定位：${error instanceof Error ? error.message : error}。请重新生成建议`,
          });
        }
        const roundId = adopted[0].roundId;
        if (adopted.some((item) => item.roundId !== roundId)) {
          return reply.code(400).send({ error: '不能同时应用不同评测轮次的优化建议' });
        }
        const currentDiagnoses = diagnosisRepo.listFailureDiagnoses(c.p.id, roundId)
          .filter((item) => item.basedRevisionOid === latest.revision.oid);
        const currentDiagnosisIds = new Set(currentDiagnoses.map((item) => item.id));
        if (!currentDiagnoses.length || adopted.some((item) => !(item.diagnosisIds ?? []).length
          || item.diagnosisIds!.some((id) => !currentDiagnosisIds.has(id)))) {
          return reply.code(409).send({ error: '优化建议来自旧的失败分析，已过期；请重新分析失败原因并生成优化建议' });
        }
        const round = m3repo.getRound(roundId);
        if (!round || round.projectId !== c.p.id) return reply.code(404).send({ error: '评测轮次不存在' });
        const failed = m3repo.listResults(roundId).filter((item) => item.verdict !== 'pass');
        try {
          validateOptimizationSuggestions(adopted, currentDiagnoses, failed, latest.spec);
        } catch (error) {
          return reply.code(409).send({
            error: `优化建议已过期或未通过防过拟合校验：${error instanceof Error ? error.message : error}。请重新生成优化建议`,
          });
        }
        const resourceCount = latest.spec.objects.length + latest.spec.operations.length
          + (latest.spec.rules?.length ?? 0) + (latest.spec.metrics?.length ?? 0) + (latest.spec.scripts?.length ?? 0);
        const task = startOptimizationApplyTask(
          c.p.id,
          adopted.map((item) => item.id),
          resourceCount,
        );
        try {
          const revised = await applyOptimizations(
            c.p,
            latest.spec,
            adopted,
            undefined,
            (event) => { recordOptimizationApplyProgress(task.id, event); },
            failed,
            currentDiagnoses,
          );
          markOptimizationCandidateSaving(task.id);
          const spec = { ...latest.spec, ...revised, version: latest.spec.version };
          const intentQuestions = packageIntentQuestions(spec);
          validateSpecIntegrity(spec, intentQuestions);
          const coverage = m2.checkCoverage(spec, intentQuestions);
          const current = await packageArtifacts.readPackageFiles(c.p.id, latest.revision.oid);
          const candidate = await createOptimizationCandidate({
            store: packageArtifacts,
            projectId: c.p.id,
            current,
            spec,
            coverage,
            suggestionIds: adopted.map((item) => item.id),
            summary: `优化候选：${adopted.map((item) => item.title).join('；')}`.slice(0, 500),
            linkCandidate: (candidateId) => m4repo.markCandidateRevision(
              c.p.id, adopted.map((item) => item.id), candidateId,
            ),
          });
          try {
            getAuthoringState().apply({
              kind: 'publish_event',
              projectId: c.p.id,
              type: 'package.candidate.created',
              payload: {
                candidateId: candidate.id,
                cause: 'optimization',
                basedRevision: latest.revision.oid,
                suggestionCount: adopted.length,
              },
            });
          } catch { /* 候选本身已经持久化，事件失败不回滚候选 */ }
          setProjectStage(c.p.id, 'optimize');
          completeOptimizationCandidateTask(task.id, candidate.id, adopted.length);
          return optimizationCandidateApplyResponse(candidate, adopted.length, latest.revision.oid);
        } catch (error) {
          if (error instanceof OptimizationExpertReviewRequiredError) {
            waitOptimizationExpertReview(task.id, error.message, error.validationErrors);
            return reply.code(202).send({
              status: 'waiting_expert',
              taskId: task.id,
              validationErrors: error.validationErrors,
              suggestionIds: adopted.map((item) => item.id),
            });
          }
          failOptimizationApplyTask(task.id, error instanceof Error ? error.message : String(error));
          if (error instanceof ArtifactRepositoryError && error.code === 'revision_conflict') {
            return reply.code(409).send({ code: error.code, error: error.message });
          }
          return reply.code(500).send({ error: `优化候选生成失败：${error instanceof Error ? error.message : error}` });
        }
      }, 'apply');
    });
    r.post('/api/projects/:id/optimize/apply-review', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const { taskId, action, note } = (req.body ?? {}) as {
        taskId?: string;
        action?: 'retry' | 'skip';
        note?: string;
      };
      if (!taskId || !['retry', 'skip'].includes(String(action))) {
        return reply.code(400).send({ error: '请选择“带反馈重试”或“确认跳过”' });
      }
      const decision = action as 'retry' | 'skip';
      const task = latestOptimizationApplyTask(c.p.id);
      if (!task || task.node.id !== taskId) return reply.code(404).send({ error: '待处理的优化任务不存在' });
      const state = task.node.output as any;
      if (task.node.status !== 'waiting_user' || state?.phase !== 'expert_review') {
        return reply.code(409).send({ error: '当前任务不在等待专家决策状态' });
      }
      const pendingIds = (state.suggestionIds ?? []).filter((id: string) => (
        m4repo.getSuggestions([id]).some((item) => item.projectId === c.p.id && item.status === 'pending')
      ));
      if (!pendingIds.length) return reply.code(409).send({ error: '所选优化建议已不再待处理' });
      const expertNote = String(note ?? '').trim();
      if (decision === 'retry') {
        const feedback = [
          '【上次自动修复未通过的服务端校验】',
          ...(state.validationErrors ?? []).map((item: string) => `- ${item}`),
          ...(expertNote ? ['【专家补充】', expertNote] : []),
        ].join('\n');
        m4repo.appendPendingSuggestionFeedback(c.p.id, pendingIds, feedback);
      } else {
        m4repo.markRejected(pendingIds, c.p.id);
      }
      resolveOptimizationExpertReview(task.node.id, decision, expertNote);
      try {
        getAuthoringState().apply({
          kind: 'publish_event',
          projectId: c.p.id,
          type: 'optimization.expert_review.resolved',
          payload: { taskId, action: decision, note: expertNote, suggestionIds: pendingIds },
        });
      } catch { /* 兼容 PR5 事件面 */ }
      return { status: decision === 'retry' ? 'retry_ready' : 'skipped', suggestionIds: pendingIds };
    });
    r.post('/api/projects/:id/optimize/reject', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const { suggestionIds } = (req.body ?? {}) as any;
      if (!Array.isArray(suggestionIds) || !suggestionIds.length) return reply.code(400).send({ error: '请选择建议' });
      m4repo.markRejected(suggestionIds, c.p.id);
      return { suggestions: m4repo.listSuggestions(c.p.id) };
    });

    r.post('/api/projects/:id/release', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const blocked = blockForActivePackageDraft(reply, c.p.id, 'release');
      if (blocked) return blocked;
      return withLock(`${c.p.id}:release`, reply, async () => {
        try {
          const current = await packageArtifacts.current(c.p.id);
          if (!current) return reply.code(404).send({ error: '尚未生成知识包' });

          const latestRound = m3repo.listRounds(c.p.id)[0];
          const latestResults = latestRound ? m3repo.listResults(latestRound.id) : [];
          const requiredTestcases = m3repo.listTestcases(c.p.id)
            .filter((testcase) => testcase.source === 'manual');
          const constructionRequired = c.p.kind !== 'package';
          const constructionSnapshot = constructionRequired
            ? aiConstruction.snapshot(c.p.id)
            : null;
          const pendingRequiredTasks = constructionSnapshot?.tasks.filter((task) => (
            task.required && !['completed', 'skipped', 'cancelled'].includes(task.status)
          )).length ?? 0;
          const pendingCandidateChanges = (await packageArtifacts.candidateRevisionOverview(
            c.p.id, 'pending', 1,
          )).pendingCount;
          const warningReview = getPackageWarningReview(c.p.id, current.revision.oid);
          const readiness = buildReleaseReadiness({
            currentRevision: current.revision.oid,
            packageCheck: getCheck(c.p.id),
            warningReview,
            flowchartRequired: constructionRequired,
            flowchartConfirmed: constructionSnapshot?.milestones.some((milestone) => (
              milestone.id === 'process_confirmed' && milestone.status === 'confirmed'
            )),
            constructionRequired,
            constructionCompleted: constructionSnapshot?.status === 'completed'
              && constructionSnapshot.milestones.some((milestone) => (
                milestone.id === 'construction_completed' && milestone.status === 'confirmed'
              )),
            pendingRequiredTasks,
            pendingCandidateChanges,
            latestRound,
            latestResults,
            requiredTestcases,
          });
          if (!readiness.ready) {
            return reply.code(409).send({ error: '发布条件未满足', gates: readiness.gates });
          }

          const releaseCandidate = current.spec.status === 'review'
            && isReleaseCandidateVersion(current.packageVersion);
          if (!releaseCandidate) {
            // 出口有两种，取决于卡在哪一步。按错误码统一兜底会同时给出两个按钮，
            // 其中一个必然点不动——这里有上下文，直接给唯一正确的那个。
            const draft = await getActivePackageDraft(c.p.id);
            return reply.code(409).send({
              error: draft
                ? '还有未发布的人工编辑草稿，发布知识包前需要先处理它'
                : '当前知识包还不是可发布的 RC 候选，请先完成 AI 辅助构建的确认',
              code: 'RELEASE_CANDIDATE_REQUIRED',
              nextActions: draft
                ? [
                  { id: 'publish_package_draft', label: '发布草稿', style: 'primary',
                    description: '把人工编辑的内容并入知识包，然后再发布' },
                  { id: 'discard_package_draft', label: '放弃草稿', style: 'secondary',
                    description: '丢弃这些人工编辑，回到草稿之前的版本' },
                ]
                : [
                  { id: 'confirm_construction', label: '确认完成 AI 辅助构建', style: 'primary',
                    description: '确认后会创建验证候选版本，之后才能发布' },
                  { id: 'review_knowledge_files', label: '查看知识文件', style: 'secondary' },
                ],
            });
          }
          const version = promoteReleaseCandidate(current.packageVersion);
          const action = {
            cause: 'release',
            summary: `发布 ${version}`,
            actorKind: 'user' as const,
            actorName: req.user!.username,
          };
          const saved = await packageArtifacts.saveCurrentAsPackageVersion(c.p.id, {
            expectedRevision: current.revision.oid,
            version,
            status: 'released',
            note: '正式发布',
            action,
          });
          getAuthoringState().apply({
            kind: 'publish_event',
            projectId: c.p.id,
            type: 'package.revision.created',
            payload: {
              revisionId: saved.revision.oid,
              cause: 'release',
              packageVersion: version,
            },
          });
          saveReleaseKnownLimitations({
            projectId: c.p.id,
            version,
            revision: saved.revision.oid,
            knownLimitations: warningReview.knownLimitations,
            actor: req.user!.username,
          });
          return { version, revision: saved.revision.oid };
        } catch (error) {
          if (error instanceof Error && error.message === '知识包版本不正确') {
            return reply.code(400).send({ error: error.message });
          }
          return sendArtifactError(reply, error);
        }
      }, 'release');
    });

    // ---- 版本与评测汇总（评测中心 / 交付选版） ----
    r.get('/api/projects/:id/versions', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const current = await packageArtifacts.current(c.p.id);
      const currentRevision = current?.revision.oid ?? null;
      const rounds = m3repo.listRounds(c.p.id);
      const roundSummary = (revision: string | undefined) => revision
        ? rounds.filter((round) => round.artifactRevision === revision)
          .map((round) => ({
            id: round.id,
            status: round.status,
            passCount: round.passCount,
            progressTotal: round.progressTotal,
            createdAt: round.createdAt,
          }))
        : [];
      const packageVersions = m2repo.listPackageVersions(c.p.id);
      const versions = packageVersions.map((version) => ({
        ...version,
        revision: version.revision ?? null,
        current: version.revision === currentRevision,
        unversioned: false,
        rounds: roundSummary(version.revision),
      }));
      if (current && !packageVersions.some((version) => version.revision === currentRevision)) {
        const unversionedRevision = current.revision.oid;
        versions.push({
          id: `current:${unversionedRevision}`,
          version: current.packageVersion,
          revision: unversionedRevision,
          note: '当前未版本化修订',
          createdAt: current.revision.createdAt,
          current: true,
          unversioned: true,
          rounds: roundSummary(unversionedRevision),
        });
      }
      return { currentRevision, versions };
    });

    registerAuthoringRoutes(r, { packageChatApply: runPackageChatApply, jobs: inFlight });
    registerArtifactRoutes(r);
  });
}

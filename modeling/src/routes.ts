import type { FastifyInstance } from 'fastify';
import { requireAuth } from './auth/middleware.js';
import { getAuthClient } from './auth/client.js';
import { createProject, getProject, listProjects, setStage } from './projects/repo.js';
import { insertDocument, listDocuments, getDocument, deleteDocument } from './documents/repo.js';
import { kindOf, parseDocument } from './documents/parser.js';
import { listNodes, runAnalyze, upsertNode } from './workflow/engine.js';
import * as m2 from './workflow/m2.js';
import * as m2repo from './workflow/m2-repo.js';
import { renderPackage } from './workflow/package-render.js';
import { zipPackage } from './workflow/export.js';
import { advanceStage as setProjectStage } from './projects/repo.js';
import { getSetting, setSetting, deleteSetting, getConsumerApiUrl, getLlmSettings, getEvalDbUrl, getEvalBindingDefaults } from './settings/repo.js';
import { packageChatPlan, packageChatApply, getOverrides, setOverride, clearOverride } from './workflow/package-chat.js';
import { runRuleChecks, runSemanticCheck, saveCheck, getCheck } from './workflow/package-check.js';
import * as m3repo from './workflow/m3-repo.js';
import * as m4repo from './workflow/m4-repo.js';
import { startEvaluation } from './evaluation/engine.js';
import { evalSchemaName } from './evaluation/eval-db.js';
import { proposeOptimizations, applyOptimizations, findingsToSuggestions } from './evaluation/optimizer.js';
import { testConsumerConnectivity } from './consumer/client.js';
import { config } from './config.js';
import type { DataDictTable } from './types.js';

export async function registerRoutes(app: FastifyInstance) {
  app.get('/health', async () => ({ status: 'ok' }));

  // ---- 认证：登录转发给 issuer ----
  app.post('/auth/login', async (req, reply) => {
    const { username, password } = (req.body ?? {}) as { username?: string; password?: string };
    if (!username || !password) return reply.code(400).send({ error: '用户名和密码必填' });
    const result = await getAuthClient().login(username, password);
    if (!result) return reply.code(401).send({ error: '登录失败' });
    return result;
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
        consumerApiUrlSource: getSetting('consumer_api_url') ? 'settings' : (config.consumerApiUrl ? 'env' : 'issuer'),
        authIssuerUrl: config.auth.issuerUrl, // 只读展示：登录 issuer 由部署配置
        llm: {
          baseUrl: llm.baseUrl, model: llm.model, maxTokens: llm.maxTokens,
          apiKeySet: !!llm.apiKey, // key 不回显
          source: getSetting('llm_base_url') ? 'settings' : 'env',
        },
        evalDbUrl: getEvalDbUrl(),
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
      put('eval_db_url', b.evalDbUrl);
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
      };
    });

    // ---- 资料上传与解析（节点 2）----
    r.post('/api/projects/:id/documents', async (req, reply) => {
      const p = getProject((req.params as any).id);
      if (!p || p.owner !== req.user!.username) return reply.code(404).send({ error: '项目不存在' });
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
        setStage(p.id, 'analyze');
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

    // 长任务防重入（同项目同操作并发触发 → 409），配合前端从节点状态推导进行中
    const inFlight = new Set<string>();
    const withLock = async (key: string, reply: any, fn: () => Promise<unknown>) => {
      if (inFlight.has(key)) return reply.code(409).send({ error: '该操作正在进行中，请稍候（切换页面不会中断执行）' });
      inFlight.add(key);
      try { return await fn(); } finally { inFlight.delete(key); }
    };

    // 取项目 + 数据字典上下文的公共辅助
    const loadCtx = (id: string, username: string) => {
      const p = getProject(id);
      if (!p || p.owner !== username) return null;
      const docs = listDocuments(p.id);
      const tables: DataDictTable[] = docs.flatMap((d) => d.tables ?? []);
      const textDocs = docs.filter((d) => d.textContent).map((d) => ({ filename: d.filename, textContent: d.textContent }));
      // stale（资料变更后）也返回旧分析——结果仍可用，由前端提示重跑
      const analyzeNode = listNodes(p.id).find((n) => n.type === 'analyze' && ['done', 'stale'].includes(n.status));
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
      try {
        const res = await m2.startInterview(c.p, c.ctx, c.analysis);
        setProjectStage(c.p.id, 'interview');
        return res;
      } catch (e) { return reply.code(500).send({ error: e instanceof Error ? e.message : String(e) }); }
    });

    r.post('/api/projects/:id/interview/answer', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const { batchId, answers } = (req.body ?? {}) as any;
      if (!batchId || !Array.isArray(answers)) return reply.code(400).send({ error: 'batchId 与 answers 必填' });
      try {
        return await m2.answerInterview(c.p, c.ctx, batchId, answers);
      } catch (e) { return reply.code(500).send({ error: e instanceof Error ? e.message : String(e) }); }
    });

    // ---- 节点 5 胜任力 ----
    r.get('/api/projects/:id/competency', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return { questions: m2repo.listCompetency(c.p.id) };
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
      return withLock(`${c.p.id}:generate`, reply, async () => {
      const selected = m2repo.listCompetency(c.p.id).filter((q) => q.selected);
      if (selected.length < 5) return reply.code(400).send({ error: '请先在胜任力问题中至少勾选 5 条' });
      try {
        const { spec, coverage, version } = await m2.generatePackage(c.p, c.ctx);
        setProjectStage(c.p.id, 'generate');
        return { version, coverage, objectCount: spec.objects.length, operationCount: spec.operations.length };
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
      const overrides = getOverrides(c.p.id);
      return {
        version: pv.version, coverage: pv.coverage,
        files: Object.keys(files).sort(),
        overriddenPaths: Object.keys(overrides).filter((k) => k in files).sort(),
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
      const overrides = getOverrides(c.p.id);
      return { path, content: overrides[path] ?? files[path], overridden: path in overrides, rendered: files[path] };
    });

    // 人工修订包文件（覆盖渲染产物；对导出与后续版本渲染持续生效，直至清除）
    r.put('/api/projects/:id/package/file', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const { path, content } = (req.body ?? {}) as { path?: string; content?: string };
      const latest = m2repo.latestPackageVersion(c.p.id);
      if (!latest) return reply.code(404).send({ error: '尚未生成知识包' });
      const files = renderPackage(latest.spec);
      if (!path || !(path in files)) return reply.code(400).send({ error: 'path 不存在于当前包' });
      if (typeof content !== 'string') return reply.code(400).send({ error: 'content 必填' });
      setOverride(c.p.id, path, content);
      return { ok: true, path, overridden: true };
    });
    r.delete('/api/projects/:id/package/file', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const path = String((req.query as any).path || '');
      clearOverride(c.p.id, path);
      const latest = m2repo.latestPackageVersion(c.p.id);
      const files = latest ? renderPackage(latest.spec) : {};
      return { ok: true, path, content: (files as Record<string, string>)[path] ?? '' };
    });

    // 包修缮 agent 对话·两段式：① plan 影响分析（咨询直接回答）② 专家确认后 apply 落新版本
    r.post('/api/projects/:id/package/chat', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return withLock(`${c.p.id}:chat`, reply, async () => {
      const { message, history } = (req.body ?? {}) as { message?: string; history?: { role: 'user' | 'assistant'; text: string }[] };
      if (!message?.trim()) return reply.code(400).send({ error: 'message 必填' });
      try {
        return await packageChatPlan(c.p, history ?? [], message.trim());
      } catch (e) {
        return reply.code(500).send({ error: e instanceof Error ? e.message : String(e) });
      }
      });
    });
    r.post('/api/projects/:id/package/chat/apply', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return withLock(`${c.p.id}:chat`, reply, async () => {
      const { message, history, plan } = (req.body ?? {}) as any;
      if (!plan?.changes?.length) return reply.code(400).send({ error: 'plan 必填（先调用 chat 获取修缮计划）' });
      try {
        return await packageChatApply(c.p, history ?? [], String(message ?? ''), plan);
      } catch (e) {
        return reply.code(500).send({ error: e instanceof Error ? e.message : String(e) });
      }
      });
    });

    // 包体检：规则引擎（确定性）+ 语义检查（LLM）；结果落库供回看
    r.post('/api/projects/:id/package/check', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return withLock(`${c.p.id}:check`, reply, async () => {
      const latest = m2repo.latestPackageVersion(c.p.id);
      if (!latest) return reply.code(404).send({ error: '尚未生成知识包' });
      const selected = m2repo.listCompetency(c.p.id).filter((q) => q.selected).map((q) => q.question);
      try {
        const ruleFindings = runRuleChecks(latest.spec, c.ctx.tables, selected);
        const semanticFindings = await runSemanticCheck(c.p, latest.spec);
        const findings = [...ruleFindings, ...semanticFindings]
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
      const pv = pickVersion(c.p.id, req);
      if (!pv) return reply.code(404).send({ error: '尚未生成知识包或版本不存在' });
      const rendered = renderPackage(pv.spec);
      const overrides = getOverrides(c.p.id);
      for (const [op, oc] of Object.entries(overrides)) if (op in rendered) rendered[op] = oc;
      const zip = zipPackage(rendered);
      reply.header('Content-Type', 'application/zip');
      reply.header('Content-Disposition', `attachment; filename="${pv.spec.id}-${pv.version}.zip"`);
      return reply.send(Buffer.from(zip));
    });

    // ---- 节点 8 评测用例录入 ----
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
      return { testcase: m3repo.addTestcase(c.p.id, question, groundTruth ?? '') };
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
      return { testcases: m3repo.listTestcases(c.p.id) };
    });
    r.delete('/api/projects/:id/testcases/:tcId', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      m3repo.deleteTestcase((req.params as any).tcId);
      return { testcases: m3repo.listTestcases(c.p.id) };
    });

    // ---- 节点 9 评测运行 ----
    r.post('/api/projects/:id/evaluate', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const token = (req.headers.authorization ?? '').slice(7);
      const { binding } = (req.body ?? {}) as any;
      // scene 缺省 = 本项目评测库 schema（Excel 全量行自动落库的那个）
      if (binding?.config && !binding.config.scene) binding.config.scene = evalSchemaName(c.p.id);
      try {
        const { roundId } = startEvaluation({ projectId: c.p.id, token, tables: c.ctx.tables, binding });
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

    // ---- 节点 10 优化建议与应用 ----
    r.post('/api/projects/:id/optimize/propose', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      return withLock(`${c.p.id}:optimize`, reply, async () => {
      const { roundId } = (req.body ?? {}) as any;
      const round = roundId ? m3repo.getRound(roundId) : m3repo.listRounds(c.p.id).find((x) => x.status === 'done');
      if (!round || round.projectId !== c.p.id) return reply.code(404).send({ error: '评测轮次不存在' });
      if (round.status !== 'done') return reply.code(400).send({ error: '该轮评测尚未完成' });
      const pv = m2repo.getPackageVersion(c.p.id, round.packageVersion) ?? m2repo.latestPackageVersion(c.p.id);
      if (!pv) return reply.code(404).send({ error: '尚未生成知识包' });
      const failed = m3repo.listResults(round.id).filter((x) => x.verdict !== 'pass');
      if (!failed.length) return reply.code(400).send({ error: '该轮评测全部通过，无需优化' });
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
      });
    });
    r.get('/api/projects/:id/optimize/suggestions', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const roundId = (req.query as any).roundId ? String((req.query as any).roundId) : undefined;
      return { suggestions: m4repo.listSuggestions(c.p.id, roundId) };
    });
    r.post('/api/projects/:id/optimize/apply', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
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
      });
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

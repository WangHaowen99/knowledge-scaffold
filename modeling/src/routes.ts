import type { FastifyInstance } from 'fastify';
import { requireAuth } from './auth/middleware.js';
import { getAuthClient } from './auth/client.js';
import { createProject, getProject, listProjects, setStage } from './projects/repo.js';
import { insertDocument, listDocuments } from './documents/repo.js';
import { kindOf, parseDocument } from './documents/parser.js';
import { listNodes, runAnalyze } from './workflow/engine.js';
import * as m2 from './workflow/m2.js';
import * as m2repo from './workflow/m2-repo.js';
import { renderPackage } from './workflow/package-render.js';
import { zipPackage } from './workflow/export.js';
import { advanceStage as setProjectStage } from './projects/repo.js';
import { getSetting, setSetting, getConsumerApiUrl } from './settings/repo.js';
import * as m3repo from './workflow/m3-repo.js';
import { startEvaluation } from './evaluation/engine.js';
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
    r.get('/api/settings', async () => ({
      consumerApiUrl: getConsumerApiUrl(),
      consumerApiUrlSource: getSetting('consumer_api_url') ? 'settings' : (config.consumerApiUrl ? 'env' : 'issuer'),
      authIssuerUrl: config.auth.issuerUrl, // 只读展示：登录 issuer 由部署配置
    }));

    r.put('/api/settings', async (req, reply) => {
      const { consumerApiUrl } = (req.body ?? {}) as { consumerApiUrl?: string };
      if (!consumerApiUrl || !/^https?:\/\//.test(consumerApiUrl)) {
        return reply.code(400).send({ error: 'consumerApiUrl 必须是 http(s) 地址' });
      }
      setSetting('consumer_api_url', consumerApiUrl.trim());
      return { consumerApiUrl: getConsumerApiUrl() };
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
      return { project: p, documents: listDocuments(p.id), nodes: listNodes(p.id) };
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

    // ---- 初步分析（节点 3）----
    r.post('/api/projects/:id/analyze', async (req, reply) => {
      const p = getProject((req.params as any).id);
      if (!p || p.owner !== req.user!.username) return reply.code(404).send({ error: '项目不存在' });
      const docs = listDocuments(p.id);
      const tables: DataDictTable[] = docs.flatMap((d) => d.tables ?? []);
      const textDocs = docs.filter((d) => d.textContent).map((d) => ({ filename: d.filename, textContent: d.textContent }));
      if (!tables.length && !textDocs.length) return reply.code(400).send({ error: '尚无可分析的资料，请先上传文档' });
      try {
        const { node, analysis } = await runAnalyze(p.id, tables, textDocs);
        setStage(p.id, 'analyze');
        return { node, analysis };
      } catch (e) {
        return reply.code(500).send({ error: e instanceof Error ? e.message : String(e) });
      }
    });

    // 取项目 + 数据字典上下文的公共辅助
    const loadCtx = (id: string, username: string) => {
      const p = getProject(id);
      if (!p || p.owner !== username) return null;
      const docs = listDocuments(p.id);
      const tables: DataDictTable[] = docs.flatMap((d) => d.tables ?? []);
      const textDocs = docs.filter((d) => d.textContent).map((d) => ({ filename: d.filename, textContent: d.textContent }));
      const analyzeNode = listNodes(p.id).find((n) => n.type === 'analyze' && n.status === 'done');
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
      try {
        const questions = await m2.proposeCompetency(c.p, c.ctx, c.analysis);
        setProjectStage(c.p.id, 'competency');
        return { questions };
      } catch (e) { return reply.code(500).send({ error: e instanceof Error ? e.message : String(e) }); }
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
      const selected = m2repo.listCompetency(c.p.id).filter((q) => q.selected);
      if (selected.length < 5) return reply.code(400).send({ error: '请先在胜任力问题中至少勾选 5 条' });
      try {
        const { spec, coverage, version } = await m2.generatePackage(c.p, c.ctx);
        setProjectStage(c.p.id, 'generate');
        return { version, coverage, objectCount: spec.objects.length, operationCount: spec.operations.length };
      } catch (e) { return reply.code(500).send({ error: e instanceof Error ? e.message : String(e) }); }
    });

    // ---- 节点 7 包评审：读文件树 / 单文件 / 导出 ----
    r.get('/api/projects/:id/package', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const latest = m2repo.latestPackageVersion(c.p.id);
      if (!latest) return reply.code(404).send({ error: '尚未生成知识包' });
      const files = renderPackage(latest.spec);
      return { version: latest.version, coverage: latest.coverage, files: Object.keys(files).sort(), versions: m2repo.listPackageVersions(c.p.id) };
    });
    r.get('/api/projects/:id/package/file', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const latest = m2repo.latestPackageVersion(c.p.id);
      if (!latest) return reply.code(404).send({ error: '尚未生成知识包' });
      const files = renderPackage(latest.spec);
      const path = String((req.query as any).path || '');
      if (!(path in files)) return reply.code(404).send({ error: '文件不存在' });
      return { path, content: files[path] };
    });
    r.get('/api/projects/:id/package/export', async (req, reply) => {
      const c = loadCtx((req.params as any).id, req.user!.username);
      if (!c) return reply.code(404).send({ error: '项目不存在' });
      const latest = m2repo.latestPackageVersion(c.p.id);
      if (!latest) return reply.code(404).send({ error: '尚未生成知识包' });
      const zip = zipPackage(renderPackage(latest.spec));
      reply.header('Content-Type', 'application/zip');
      reply.header('Content-Disposition', `attachment; filename="${latest.spec.id}-${latest.version}.zip"`);
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
  });
}

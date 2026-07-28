/**
 * M3 冒烟：评测闭环端到端（stub consumer + faux judge）。
 * stub consumer 实现：workspaces / import-zip（首次 409 再覆盖）/ ontology data-source /
 * runs·sync（含一次 429 限流验证退避）。
 * 验证：隔离值工作空间创建/复用、包导入、本体数据源复用、逐用例新对话、judge 判定、轮次进度与日志。
 */
import { createServer } from 'node:http';
import { createProject } from '../src/projects/repo.js';
import { insertDocument } from '../src/documents/repo.js';
import { insertPackageVersion } from '../src/workflow/m2-repo.js';
import * as m3repo from '../src/workflow/m3-repo.js';
import { startEvaluation } from '../src/evaluation/engine.js';
import { configureProjectDataSource } from '../src/evaluation/data-source-service.js';
import { setSetting } from '../src/settings/repo.js';
import type { PackageSpec } from '../src/types.js';

const ok = (c: boolean, m: string) => { console.log(`  ${c ? '✓' : '✗ FAIL'}  ${m}`); if (!c) process.exitCode = 1; };

// ---- stub consumer ----
const seen = {
  wsCreated: false, workspace: null as any, imported: 0,
  dataSource: null as any, dataSourceReads: 0, runs: 0, rateLimited: false,
  workspaceHeaders: [] as string[], sessionIds: [] as string[],
};
function startStubConsumer(port: number) {
  const srv = createServer(async (req, res) => {
    const url = req.url ?? '';
    const body = await new Promise<string>((resolve) => {
      let b = ''; req.on('data', (c) => { b += c; }); req.on('end', () => resolve(b));
    });
    res.setHeader('Content-Type', 'application/json');
    const send = (code: number, obj: unknown) => { res.statusCode = code; res.end(JSON.stringify(obj)); };
    if (url.startsWith('/agentic/') && url !== '/agentic/workspaces') {
      seen.workspaceHeaders.push(String(req.headers['x-workspace-id'] ?? ''));
    }

    if (url === '/health') return send(200, { status: 'ok' });
    if (url === '/auth/me') return send(200, { user: { username: 'expert', role: 'user' } });
    if (url === '/agentic/workspaces' && req.method === 'GET') return send(200, { workspaces: seen.workspace ? [seen.workspace] : [], total: seen.workspace ? 1 : 0, default_workspace_id: null });
    if (url === '/agentic/workspaces' && req.method === 'POST') { seen.wsCreated = true; seen.workspace = JSON.parse(body); return send(201, seen.workspace); }
    if (url === '/agentic/ontologies/import-zip') {
      seen.imported++;
      if (seen.imported === 1) return send(409, { requires_confirmation: true, conflict_type: 'version_exists', ontology_id: 'x' });
      return send(201, { success: true, ontology_id: 'demo-agent', objects_count: 2, operations_count: 5 });
    }
    if (url === '/agentic/ontologies/demo-agent/data-source' && req.method === 'PUT') {
      seen.dataSource = JSON.parse(body);
      return send(200, { ontology_id: 'demo-agent', configured: true, source: seen.dataSource });
    }
    if (url === '/agentic/ontologies/demo-agent/data-source' && req.method === 'GET') {
      seen.dataSourceReads++;
      return send(200, { ontology_id: 'demo-agent', configured: true, source: seen.dataSource });
    }
    if (url === '/agentic/runs/sync') {
      seen.runs++;
      const runPayload = JSON.parse(body);
      seen.sessionIds.push(String(runPayload.session_id ?? ''));
      if (seen.runs === 1 && !seen.rateLimited) { seen.rateLimited = true; return send(429, { error: '限流' }); }
      const q = (runPayload.request ?? '') as string;
      // 命中「STRUCTURE_LIST」的问题给正确答案，其余答错——制造 pass/fail 混合
      const answer = q.includes('STRUCTURE_LIST') ? '共有 2 条 STRUCTURE_LIST 记录，父项 A001。' : '无法确定。';
      return send(200, { status: 'success', final_answer: answer, session_id: 'x', run_id: 'r' });
    }
    return send(404, { error: `stub 未实现 ${req.method} ${url}` });
  });
  srv.listen(port);
  return srv;
}

async function main() {
  console.log('\n=== M3 冒烟：评测闭环端到端 ===\n');
  const stub = startStubConsumer(5581);
  setSetting('consumer_api_url', 'http://127.0.0.1:5581');

  // 造项目 + 数据字典；评测数据写入已由“数据源配置”步骤负责。
  const project = createProject({ owner: 'smoke', name: 'M3 演示', domain: 'DOMAIN', background: 'x' });
  insertDocument({
    projectId: project.id, filename: 'd.xlsx', kind: 'excel', parseStatus: 'parsed',
    tables: [{ name: 'STRUCTURE_LIST', rowCount: 2, rows: [['1', 'A001'], ['2', 'A001']], fields: [
      { name: 'ID', inferredType: 'number', sampleValues: ['1'], distinctRatio: 1 },
      { name: 'ParentPart', inferredType: 'string', sampleValues: ['A001'], distinctRatio: 0.5 },
    ] }],
  });

  // 造一个包版本（跳过节点 4~6，直接给 spec）
  const spec: PackageSpec = {
    id: 'demo-agent', name: 'demo', description: 'd', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'domain',
    objects: [{ key: 'STRUCTURE_LIST', title: 'STRUCTURE_LIST', description: 'd', fields: [{ key: 'ID' }] }],
    operations: [{ key: 'op1', title: 't', description: 'd', inputs: [], outputs: [], flow: ['x'], relatedObjects: ['STRUCTURE_LIST'] }],
  };
  insertPackageVersion(project.id, '1.0.0', spec, { covered: [], uncovered: [] });

  // 用例：一条应 pass（答案含标准答案关键词）、一条应 fail
  m3repo.addTestcase(project.id, 'STRUCTURE_LIST 有几条记录，父项是什么', '2 条 STRUCTURE_LIST 记录，父项 A001');
  m3repo.addTestcase(project.id, 'CHANGE_ORDER 审批流程是什么', '需经工艺、质量双会签');

  // 走真实配置编排：Workspace 隔离值 test 同时作为 consumer 评测工作空间 ID。
  await configureProjectDataSource({
    projectId: project.id,
    token: 'stub-token',
    input: {
      mode: 'connection',
      provider: 'neo4j',
      config: {
        uri: 'bolt://neo4j:7687',
        username: 'neo4j',
        password: 'secret',
        database: 'neo4j',
        workspace_property: 'd2o_workspace_id',
        workspace_value: 'test',
        ontology_property: 'd2o_ontology_id',
        ontology_value: spec.id,
      },
    },
    testConnection: async () => {},
  });

  const { roundId } = startEvaluation({ projectId: project.id, token: 'stub-token' });
  ok(!!roundId, `轮次已启动：${roundId}`);

  // 轮询直到完成（引擎为后台任务）
  for (let i = 0; i < 60; i++) {
    const r = m3repo.getRound(roundId)!;
    if (r.status !== 'running') break;
    await new Promise((res) => setTimeout(res, 500));
  }
  const round = m3repo.getRound(roundId)!;
  const results = m3repo.listResults(roundId);

  ok(round.status === 'done', `轮次状态=${round.status}`);
  ok(seen.wsCreated && seen.workspace?.id === 'test' && seen.workspace?.name === 'test', '按 Workspace 隔离值创建 test 评测工作空间');
  ok(round.log.includes('复用评测工作空间：test'), '评测运行复用数据源配置的 test 工作空间');
  ok(seen.workspaceHeaders.length > 0 && seen.workspaceHeaders.every((value) => value === 'test'), '数据源保存、导包和评测请求均使用 test 工作空间');
  ok(seen.imported === 3, `配置导包遇到 409 后覆盖，评测再次导包（调用 ${seen.imported} 次）`);
  ok(seen.dataSource?.mode === 'direct_provider'
    && seen.dataSource?.provider === 'neo4j'
    && seen.dataSourceReads === 1, '数据源配置写入并在评测时复用本体级数据源');
  ok(seen.rateLimited && seen.runs === 3, `runs/sync 经历一次 429 退避重试（共 ${seen.runs} 次调用）`);
  ok(seen.sessionIds.length === 3
    && seen.sessionIds[0] === seen.sessionIds[1]
    && seen.sessionIds[1] !== seen.sessionIds[2], '同一用例重试复用对话，不同用例使用独立新对话');
  ok(results.length === 2, `产出 ${results.length} 条结果`);
  const passed = results.filter((r) => r.verdict === 'pass').length;
  const failed = results.filter((r) => r.verdict === 'fail').length;
  ok(passed === 1 && failed === 1, `judge 判定：pass=${passed} fail=${failed}（预期 1/1）`);
  ok(round.passCount === 1 && round.progressDone === 2, `轮次统计：通过 ${round.passCount}/${round.progressTotal}`);
  ok(!/(评测库就绪|评测库落库失败|跳过落库)/.test(round.log), '评测运行不再执行评测库写入');

  // 人工改判
  const failedResult = results.find((r) => r.verdict === 'fail')!;
  m3repo.overrideVerdict(failedResult.id, 'pass');
  ok(m3repo.getRound(roundId)!.passCount === 2, '人工改判后轮次通过数同步更新');

  stub.close();
  console.log(`\n=== M3 冒烟${process.exitCode ? '存在失败' : '通过'} ===\n`);
}

main().catch((e) => { console.error('\nM3 SMOKE ERROR:', e?.stack || e); process.exit(1); });

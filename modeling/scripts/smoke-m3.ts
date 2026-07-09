/**
 * M3 冒烟：评测闭环端到端（stub consumer + faux judge）。
 * stub consumer 实现：workspaces / import-zip（首次 409 再覆盖）/ data-connections /
 * data-binding / runs·sync（含一次 429 限流验证退避）。
 * 验证：影子工作区创建、包导入、绑定、逐用例出分、judge 判定、轮次进度与日志。
 */
import { createServer } from 'node:http';
import { createProject } from '../src/projects/repo.js';
import { insertDocument } from '../src/documents/repo.js';
import { insertPackageVersion } from '../src/workflow/m2-repo.js';
import * as m3repo from '../src/workflow/m3-repo.js';
import { startEvaluation, shadowWorkspaceId } from '../src/evaluation/engine.js';
import { setSetting } from '../src/settings/repo.js';
import type { PackageSpec } from '../src/types.js';

const ok = (c: boolean, m: string) => { console.log(`  ${c ? '✓' : '✗ FAIL'}  ${m}`); if (!c) process.exitCode = 1; };

// ---- stub consumer ----
const seen = { wsCreated: false, imported: 0, bound: false, connCreated: false, runs: 0, rateLimited: false };
function startStubConsumer(port: number) {
  const srv = createServer(async (req, res) => {
    const url = req.url ?? '';
    const body = await new Promise<string>((resolve) => {
      let b = ''; req.on('data', (c) => { b += c; }); req.on('end', () => resolve(b));
    });
    res.setHeader('Content-Type', 'application/json');
    const send = (code: number, obj: unknown) => { res.statusCode = code; res.end(JSON.stringify(obj)); };

    if (url === '/health') return send(200, { status: 'ok' });
    if (url === '/auth/me') return send(200, { user: { username: 'expert', role: 'user' } });
    if (url === '/agentic/workspaces' && req.method === 'GET') return send(200, { workspaces: [], total: 0, default_workspace_id: null });
    if (url === '/agentic/workspaces' && req.method === 'POST') { seen.wsCreated = true; return send(201, JSON.parse(body)); }
    if (url === '/agentic/ontologies/import-zip') {
      seen.imported++;
      if (seen.imported === 1) return send(409, { requires_confirmation: true, conflict_type: 'version_exists', ontology_id: 'x' });
      return send(201, { success: true, ontology_id: 'demo-agent', objects_count: 2, operations_count: 5 });
    }
    if (url === '/agentic/data-connections' && req.method === 'GET') return send(200, { connections: [], default_connection_id: '' });
    if (url === '/agentic/data-connections' && req.method === 'POST') { seen.connCreated = true; return send(201, { connection: JSON.parse(body) }); }
    if (url.includes('/data-binding') && req.method === 'PUT') { seen.bound = true; return send(200, { binding: {} }); }
    if (url === '/agentic/runs/sync') {
      seen.runs++;
      if (seen.runs === 1 && !seen.rateLimited) { seen.rateLimited = true; return send(429, { error: '限流' }); }
      const q = (JSON.parse(body).request ?? '') as string;
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

  // 造项目 + 数据字典（含 rows，验证评测库路径的“无 EVAL_DB_URL 跳过”日志）
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

  const { roundId } = startEvaluation({
    projectId: project.id, token: 'stub-token',
    tables: [], // 评测库 provisioning 走“跳过”分支
    binding: { provider: 'nl2sql_template', config: { url: 'http://nl2sql:8080', token: 't', scene: 'mw' } },
  });
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
  ok(seen.wsCreated, `影子工作区已创建（${shadowWorkspaceId(project.id)}）`);
  ok(seen.imported === 2, `import-zip 409 冲突后以 overwrite_active 重试（调用 ${seen.imported} 次）`);
  ok(seen.connCreated && seen.bound, '数据连接已创建并完成绑定');
  ok(seen.rateLimited && seen.runs === 3, `runs/sync 经历一次 429 退避重试（共 ${seen.runs} 次调用）`);
  ok(results.length === 2, `产出 ${results.length} 条结果`);
  const passed = results.filter((r) => r.verdict === 'pass').length;
  const failed = results.filter((r) => r.verdict === 'fail').length;
  ok(passed === 1 && failed === 1, `judge 判定：pass=${passed} fail=${failed}（预期 1/1）`);
  ok(round.passCount === 1 && round.progressDone === 2, `轮次统计：通过 ${round.passCount}/${round.progressTotal}`);
  ok(round.log.includes('跳过') || round.log.includes('评测库'), '评测库 best-effort 日志存在');

  // 人工改判
  const failedResult = results.find((r) => r.verdict === 'fail')!;
  m3repo.overrideVerdict(failedResult.id, 'pass');
  ok(m3repo.getRound(roundId)!.passCount === 2, '人工改判后轮次通过数同步更新');

  stub.close();
  console.log(`\n=== M3 冒烟${process.exitCode ? '存在失败' : '通过'} ===\n`);
}

main().catch((e) => { console.error('\nM3 SMOKE ERROR:', e?.stack || e); process.exit(1); });

/**
 * M4 冒烟：优化闭环端到端（评测 → 建议 → 采纳应用 → 版本++ → 再评测）。
 * stub consumer 是「内容感知」的：runs/sync 的回答质量取决于最近导入的知识包内容
 * （按问题片段命中包内文件），从而真实模拟「包变好 → 回答变好 → 通过率提升」。
 * 验收（design.md M4）：两轮优化后通过率可见提升 —— 1/3 → 2/3 → 3/3。
 */
import { createServer } from 'node:http';
import { unzipSync, strFromU8 } from 'fflate';
import { createProject, getProject } from '../src/projects/repo.js';
import { insertDocument } from '../src/documents/repo.js';
import { insertPackageVersion, listPackageVersions, getPackageVersion, latestPackageVersion } from '../src/workflow/m2-repo.js';
import { checkCoverage } from '../src/workflow/m2.js';
import * as m3repo from '../src/workflow/m3-repo.js';
import * as m4repo from '../src/workflow/m4-repo.js';
import { startEvaluation } from '../src/evaluation/engine.js';
import { proposeOptimizations, applyOptimizations } from '../src/evaluation/optimizer.js';
import { renderPackage } from '../src/workflow/package-render.js';
import { zipPackage } from '../src/workflow/export.js';
import { setSetting } from '../src/settings/repo.js';
import type { PackageSpec } from '../src/types.js';

const ok = (c: boolean, m: string) => { console.log(`  ${c ? '✓' : '✗ FAIL'}  ${m}`); if (!c) process.exitCode = 1; };
const frags = (s: string) => s.split(/[，。；、？?：:\s]+/).filter((f) => f.length > 1);

// ---- 内容感知 stub consumer ----
let importedPkgText: Record<string, string> = {};
function startStubConsumer(port: number) {
  const srv = createServer(async (req, res) => {
    const url = req.url ?? '';
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(c as Buffer);
    const body = Buffer.concat(chunks);
    res.setHeader('Content-Type', 'application/json');
    const send = (code: number, obj: unknown) => { res.statusCode = code; res.end(JSON.stringify(obj)); };

    if (url === '/health') return send(200, { status: 'ok' });
    if (url === '/auth/me') return send(200, { user: { username: 'expert', role: 'user' } });
    if (url === '/agentic/workspaces' && req.method === 'GET') return send(200, { workspaces: [] });
    if (url === '/agentic/workspaces' && req.method === 'POST') return send(201, {});
    if (url === '/agentic/ontologies/import-zip') {
      // 从 multipart 里裸提 zip（PK 头定位），解包缓存全文
      const pk = body.indexOf(Buffer.from('PK\x03\x04', 'binary'));
      if (pk >= 0) {
        // multipart 尾部边界从 zip 末尾裁掉：找最后一个 \r\n--
        let end = body.length;
        const tail = body.lastIndexOf(Buffer.from('\r\n--'));
        if (tail > pk) end = tail;
        try {
          const files = unzipSync(new Uint8Array(body.subarray(pk, end)));
          importedPkgText = Object.fromEntries(Object.entries(files)
            .filter(([p]) => /\.(md|ya?ml|json)$/.test(p))
            .map(([p, u8]) => [p, strFromU8(u8)]));
        } catch { /* 解包失败保持上次内容 */ }
      }
      return send(201, { success: true, ontology_id: 'demo-agent', objects_count: 1, operations_count: Object.keys(importedPkgText).length });
    }
    if (url === '/agentic/data-connections' && req.method === 'GET') return send(200, { connections: [] });
    if (url === '/agentic/data-connections' && req.method === 'POST') return send(201, {});
    if (url.includes('/data-binding')) return send(200, { binding: {} });
    if (url === '/agentic/runs/sync') {
      const q = (JSON.parse(body.toString()).request ?? '') as string;
      const qf = frags(q);
      // 内容感知：把命中问题片段的包内文件拼成回答（包里有知识才答得出）。
      // 只读详情文件——新格式的索引按规范「目录只放摘要」不再承载口径正文（objects 索引
      // 三列、operations 索引四列均无 description 全文），消费端要靠详情文件作答；
      // 不过滤的话索引/映射文件会以同分挤掉详情文件，与真实消费链路不符。
      const hits = Object.entries(importedPkgText)
        .filter(([p]) => !p.endsWith('/index.md') && !p.includes('/mappings/'))
        .map(([p, text]) => ({ p, text, score: qf.filter((f) => text.includes(f)).length }))
        .filter((x) => x.score > 0)
        .sort((a, b) => b.score - a.score);
      const answer = hits.length
        ? hits.slice(0, 3).map((h) => h.text).join('\n').slice(0, 4000)
        : '根据当前知识包无法回答该问题。';
      return send(200, { status: 'success', final_answer: answer, session_id: 'x', run_id: 'r' });
    }
    return send(404, { error: `stub 未实现 ${req.method} ${url}` });
  });
  srv.listen(port);
  return srv;
}

async function runRoundAndWait(projectId: string): Promise<{ passCount: number; total: number; version: string }> {
  const { roundId } = startEvaluation({ projectId, token: 'stub-token', tables: [] });
  for (let i = 0; i < 120; i++) {
    const r = m3repo.getRound(roundId)!;
    if (r.status !== 'running') break;
    await new Promise((res) => setTimeout(res, 300));
  }
  const r = m3repo.getRound(roundId)!;
  if (r.status !== 'done') throw new Error(`轮次未完成：${r.status}\n${r.log}`);
  return { passCount: r.passCount, total: r.progressTotal, version: r.packageVersion };
}

// 镜像 routes.ts 的 optimize/apply 逻辑（版本号递增 + 覆盖重算 + 状态落库）
async function optimizeOnce(projectId: string, adoptQuestions: string[]): Promise<string> {
  const round = m3repo.listRounds(projectId).find((x) => x.status === 'done')!;
  const pv = getPackageVersion(projectId, round.packageVersion) ?? latestPackageVersion(projectId)!;
  const failed = m3repo.listResults(round.id).filter((x) => x.verdict !== 'pass');
  const raw = await proposeOptimizations(getProject(projectId)!, pv.spec, failed);
  const suggestions = m4repo.insertSuggestions(projectId, round.id, round.packageVersion, raw);
  const adopted = suggestions.filter((s) => s.status === 'pending' && adoptQuestions.some((q) => s.relatedQuestion === q));
  if (!adopted.length) throw new Error(`没有匹配的可采纳建议（候选 ${suggestions.length} 条）`);
  const latest = latestPackageVersion(projectId)!;
  const revised = await applyOptimizations(getProject(projectId)!, latest.spec, adopted);
  const version = `1.${listPackageVersions(projectId).length}.0`;
  const spec: PackageSpec = { ...latest.spec, version, objects: revised.objects, operations: revised.operations };
  insertPackageVersion(projectId, version, spec, checkCoverage(spec, []), `优化迭代：基于 ${adopted[0].basedVersion} 应用 ${adopted.length} 条建议`);
  m4repo.markApplied(adopted.map((s) => s.id), version);
  return version;
}

async function main() {
  console.log('\n=== M4 冒烟：优化闭环（评测→建议→应用→版本++→再评测） ===\n');
  const stub = startStubConsumer(5582);
  setSetting('consumer_api_url', 'http://127.0.0.1:5582');

  const project = createProject({ owner: 'smoke', name: 'M4 演示', domain: 'DOMAIN', background: '示例制造' });
  insertDocument({ projectId: project.id, filename: 'structure-list.xlsx', kind: 'excel', parseStatus: 'parsed', tables: [] });

  // v1.0.0：只沉淀了 STRUCTURE_LIST 口径 —— 3 条用例只有 1 条答得出
  const spec: PackageSpec = {
    id: 'demo-agent', name: 'DOMAIN 演示', description: 'd', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'domain',
    objects: [{ key: 'STRUCTURE_LIST', title: 'STRUCTURE_LIST', description: '制造 BOM', fields: [{ key: 'ID' }, { key: 'ParentPart' }] }],
    operations: [{
      key: 'query_structure_list', title: 'STRUCTURE_LIST 查询', description: 'STRUCTURE_LIST 共有 2 条 STRUCTURE_LIST 记录，父项 A001。',
      inputs: [], outputs: [], flow: ['查询 STRUCTURE_LIST'], relatedObjects: ['STRUCTURE_LIST'],
    }],
  };
  insertPackageVersion(project.id, '1.0.0', spec, { covered: [], uncovered: [] });

  const Q2 = 'CHANGE_ORDER 审批流程是什么';
  const Q3 = '有效性字段的口径如何判定';
  m3repo.addTestcase(project.id, 'STRUCTURE_LIST 有几条记录，父项是什么', '2 条 STRUCTURE_LIST 记录，父项 A001');
  m3repo.addTestcase(project.id, Q2, '需经工艺、质量双会签');
  m3repo.addTestcase(project.id, Q3, '按批次区间 BATCH001-BATCH050 判定');

  // 轮次 1：基线
  const r1 = await runRoundAndWait(project.id);
  ok(r1.version === '1.0.0' && r1.passCount === 1 && r1.total === 3, `轮次1 基线：v${r1.version} 通过 ${r1.passCount}/${r1.total}（预期 1/3）`);

  // 优化迭代 1：只采纳 CHANGE_ORDER 建议（模拟专家选择性采纳）
  const v2 = await optimizeOnce(project.id, [Q2]);
  ok(v2 === '1.1.0', `优化迭代1 生成新版本：v${v2}`);
  const sugs1 = m4repo.listSuggestions(project.id);
  ok(sugs1.some((s) => s.status === 'applied' && s.appliedVersion === v2), '采纳的建议已标记 applied + 版本号');
  ok(sugs1.some((s) => s.status === 'pending' && s.relatedQuestion === Q3), '未采纳的建议保持 pending');

  const r2 = await runRoundAndWait(project.id);
  ok(r2.version === '1.1.0' && r2.passCount === 2, `轮次2：v${r2.version} 通过 ${r2.passCount}/${r2.total}（预期 2/3，较上轮提升）`);

  // 优化迭代 2：基于轮次 2 再提议，采纳剩余失败项
  const v3 = await optimizeOnce(project.id, [Q3]);
  ok(v3 === '1.2.0', `优化迭代2 生成新版本：v${v3}`);
  const r3 = await runRoundAndWait(project.id);
  ok(r3.version === '1.2.0' && r3.passCount === 3, `轮次3：v${r3.version} 通过 ${r3.passCount}/${r3.total}（预期 3/3）`);
  ok(r1.passCount < r2.passCount && r2.passCount < r3.passCount, `验收达成：两轮优化通过率持续提升 ${r1.passCount}/3 → ${r2.passCount}/3 → ${r3.passCount}/3`);

  // 版本与导出：v1.2.0 的包应包含优化新增操作，且可正常打 zip
  const versions = listPackageVersions(project.id);
  ok(versions.length === 3 && versions[2].note?.includes('优化迭代'), `版本历史 ${versions.map((v) => v.version).join(' → ')}，含变更说明`);
  const finalPv = getPackageVersion(project.id, '1.2.0')!;
  const files = renderPackage(finalPv.spec);
  const opFiles = Object.keys(files).filter((p) => p.startsWith('operations/') && !p.endsWith('index.md'));
  ok(finalPv.spec.operations.length === 3 && opFiles.length === 3, `v1.2.0 含 ${finalPv.spec.operations.length} 个操作（原 1 + 优化新增 2）`);
  const zip = zipPackage(files);
  ok(zip.length > 0 && !!unzipSync(zip)['ontology/manifest.yaml'], '最终版本可导出合法 zip（含 ontology/manifest.yaml）');

  // 回归：大规格（规格 JSON >12KB）应用建议不得丢失既有对象/操作（曾因 prompt 截断静默丢失）
  const bigSpec: PackageSpec = {
    ...spec, version: '9.0.0',
    objects: Array.from({ length: 10 }, (_, i) => ({
      key: `OBJ_${i}`, title: `业务对象${i}`, description: `（回归）第 ${i} 个对象，含较长的中文业务描述用于撑大规格体积。`,
      fields: Array.from({ length: 25 }, (_, j) => ({ key: `F_${i}_${j}`, cnName: `字段${j}`, type: 'string', note: `第 ${j} 个字段的业务口径说明` })),
    })),
    operations: spec.operations,
  };
  const bigJson = JSON.stringify({ objects: bigSpec.objects, operations: bigSpec.operations });
  const bigAdopted = m4repo.listSuggestions(project.id).filter((s) => s.status === 'applied').slice(0, 1);
  const bigRevised = await applyOptimizations(getProject(project.id)!, bigSpec, bigAdopted);
  ok(bigJson.length > 12000 && bigRevised.objects.length === 10 && bigRevised.operations.length >= spec.operations.length,
    `大规格（JSON ${bigJson.length} 字符）应用建议后既有内容完整保留（对象 ${bigRevised.objects.length}/10）`);

  stub.close();
  console.log(`\n=== M4 冒烟${process.exitCode ? '存在失败' : '通过'} ===\n`);
}

main().catch((e) => { console.error('\nM4 SMOKE ERROR:', e?.stack || e); process.exit(1); });

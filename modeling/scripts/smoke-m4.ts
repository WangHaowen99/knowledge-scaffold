/**
 * M4 冒烟：优化闭环端到端（评测 → 建议 → 采纳应用 → 版本++ → 再评测）。
 * stub consumer 是「内容感知」的：runs/sync 的回答质量取决于最近导入的知识包内容
 * （按问题片段命中包内文件），从而真实模拟「包变好 → 回答变好 → 通过率提升」。
 * 验收（design.md M4）：两轮优化后通过率可见提升 —— 1/3 → 2/3 → 3/3。
 */
import { createServer } from 'node:http';
import { createFauxCore, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';
import { unzipSync, strFromU8 } from 'fflate';
import { createProject, getProject } from '../src/projects/repo.js';
import { insertDocument } from '../src/documents/repo.js';
import { listPackageVersions } from '../src/workflow/m2-repo.js';
import { checkCoverage } from '../src/workflow/m2.js';
import * as m3repo from '../src/workflow/m3-repo.js';
import * as m4repo from '../src/workflow/m4-repo.js';
import { startCurrentEvaluation } from '../src/evaluation/engine.js';
import { configureProjectDataSource } from '../src/evaluation/data-source-service.js';
import { proposeOptimizations, applyOptimizations } from '../src/evaluation/optimizer.js';
import { createOptimizationCandidate } from '../src/evaluation/optimization-candidate.js';
import { getPackageArtifactStore } from '../src/artifacts/runtime.js';
import { nextValidationCandidateVersion } from '../src/workflow/package-version-lifecycle.js';
import { zipPackage } from '../src/workflow/export.js';
import { setSetting } from '../src/settings/repo.js';
import type { LlmDeps } from '../src/llm/provider.js';
import type { PackageSpec } from '../src/types.js';

const ok = (c: boolean, m: string) => { console.log(`  ${c ? '✓' : '✗ FAIL'}  ${m}`); if (!c) process.exitCode = 1; };
const frags = (s: string) => s.split(/[，。；、？?：:\s]+/).filter((f) => f.length > 1);

function faux(toolName: string, params: unknown): LlmDeps {
  const core = createFauxCore({ provider: 'smoke-m4', models: [{ id: 'smoke-m4' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall(toolName, params)])]);
  return { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };
}

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
    if (/^\/agentic\/ontologies\/[^/]+\/active-version$/.test(url) && req.method === 'PUT') {
      const version = String(JSON.parse(body.toString()).version ?? '');
      return send(200, { success: true, active_version: version });
    }
    if (/^\/agentic\/ontologies\/[^/]+\/data-source$/.test(url) && req.method === 'DELETE') {
      return send(200, { success: true });
    }
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
        ? hits.slice(0, 5).map((h) => h.text).join('\n').slice(0, 6000)
        : '根据当前知识包无法回答该问题。';
      return send(200, {
        status: 'success', final_answer: answer, session_id: 'x', run_id: 'r',
        execution_trace: { tool_calls: ['knowledge_search'] },
      });
    }
    return send(404, { error: `stub 未实现 ${req.method} ${url}` });
  });
  srv.listen(port);
  return srv;
}

async function runRoundAndWait(projectId: string): Promise<{ passCount: number; total: number; version: string }> {
  const { roundId } = await startCurrentEvaluation({ projectId, token: 'stub-token' });
  for (let i = 0; i < 120; i++) {
    const r = m3repo.getRound(roundId)!;
    if (r.status !== 'running') break;
    await new Promise((res) => setTimeout(res, 300));
  }
  const r = m3repo.getRound(roundId)!;
  if (r.status !== 'done') throw new Error(`轮次未完成：${r.status}\n${r.log}`);
  return { passCount: r.passCount, total: r.progressTotal, version: r.packageVersion };
}

// 镜像正式链路：当前 Git 修订 → 优化候选 → 专家接受 → RC 版本固化。
async function optimizeOnce(projectId: string, adoptQuestions: string[]): Promise<string> {
  const round = m3repo.listRounds(projectId).find((x) => x.status === 'done')!;
  if (!round.artifactRevision) throw new Error('评测轮次缺少工件修订');
  const artifacts = getPackageArtifactStore();
  const current = await artifacts.readPackageFiles(projectId);
  if (current.revision.oid !== round.artifactRevision) {
    throw new Error('评测轮次已经过期，请先对当前工件修订重新评测');
  }
  const failed = m3repo.listResults(round.id).filter((x) => x.verdict !== 'pass');
  const raw = await proposeOptimizations(getProject(projectId)!, current.spec, failed);
  const suggestions = m4repo.insertSuggestions(
    projectId, round.id, round.packageVersion, current.revision.oid, raw,
  );
  const adopted = suggestions.filter((s) => s.status === 'pending' && adoptQuestions.some((q) => s.relatedQuestion === q));
  if (!adopted.length) throw new Error(`没有匹配的可采纳建议（候选 ${suggestions.length} 条）`);
  const revised = await applyOptimizations(
    getProject(projectId)!,
    current.spec,
    adopted,
    faux('submit_revised_spec', {
      objects: current.spec.objects,
      operations: [
        ...current.spec.operations,
        ...adopted.map((suggestion, index) => ({
          key: `opt_${current.spec.operations.length + index + 1}`,
          title: suggestion.relatedQuestion ?? suggestion.title,
          description: `（优化新增）${suggestion.relatedQuestion ?? ''}：${suggestion.detail}`,
          inputs: [{ param: 'keyword', type: 'string', required: true, note: '查询关键字' }],
          outputs: [{ field: 'result', type: 'string', note: '结论' }],
          flow: ['按沉淀的业务规则直接作答', suggestion.detail],
          relatedObjects: current.spec.objects.slice(0, 1).map((object) => object.key),
          competencyQuestion: suggestion.relatedQuestion,
        })),
      ],
      rules: current.spec.rules ?? [],
      metrics: current.spec.metrics ?? [],
      scripts: current.spec.scripts ?? [],
    }),
  );
  const candidateSpec: PackageSpec = {
    ...current.spec,
    ...revised,
    version: current.spec.version,
  };
  const coverage = checkCoverage(candidateSpec, []);
  const candidate = await createOptimizationCandidate({
    store: artifacts,
    projectId,
    current,
    spec: candidateSpec,
    coverage,
    suggestionIds: adopted.map((suggestion) => suggestion.id),
    summary: `M4 冒烟优化：${adopted.map((suggestion) => suggestion.title).join('；')}`,
    linkCandidate: (candidateId) => m4repo.markCandidateRevision(
      projectId, adopted.map((suggestion) => suggestion.id), candidateId,
    ),
  });
  const accepted = await artifacts.acceptCandidateRevision(projectId, {
    candidateId: candidate.id,
    action: {
      cause: 'smoke_optimization_accepted',
      summary: `接受 ${adopted.length} 条 M4 冒烟优化建议`,
      actorKind: 'user',
      actorName: '冒烟专家',
    },
  });
  m4repo.markCandidateRevisionApplied(
    projectId, candidate.id, current.spec.version, accepted.revision.oid,
  );

  const acceptedSnapshot = await artifacts.readPackageFiles(projectId, accepted.revision.oid);
  const version = nextValidationCandidateVersion({
    currentVersion: acceptedSnapshot.spec.version,
    currentStatus: acceptedSnapshot.spec.status ?? 'draft',
    changeLevel: 'minor',
  });
  await artifacts.saveCurrentAsPackageVersion(projectId, {
    expectedRevision: accepted.revision.oid,
    version,
    status: 'review',
    note: `优化迭代：基于 ${adopted[0].basedVersion} 应用 ${adopted.length} 条建议`,
    action: {
      cause: 'smoke_optimization_versioned',
      summary: `固化 M4 冒烟优化版本 ${version}`,
      actorKind: 'user',
      actorName: '冒烟专家',
    },
  });
  return version;
}

async function main() {
  console.log('\n=== M4 冒烟：优化闭环（评测→建议→应用→版本++→再评测） ===\n');
  const stub = startStubConsumer(5582);
  setSetting('consumer_api_url', 'http://127.0.0.1:5582');

  const project = createProject({ owner: 'smoke', name: 'M4 演示', domain: 'DOMAIN', background: '示例制造' });
  const artifacts = getPackageArtifactStore();
  insertDocument({ projectId: project.id, filename: 'structure-list.xlsx', kind: 'excel', parseStatus: 'parsed', tables: [] });

  // v1.0.0：只沉淀了 STRUCTURE_LIST 口径 —— 3 条用例只有 1 条答得出
  const spec: PackageSpec = {
    id: 'demo-agent', name: 'DOMAIN 演示', description: 'd', version: '1.0.0', language: 'zh-CN',
    defaultProviderAlias: 'domain', status: 'released',
    objects: [{
      key: 'STRUCTURE_LIST', title: 'STRUCTURE_LIST', description: '制造 BOM', sourceTable: 'STRUCTURE_LIST',
      fields: [{ key: 'ID', cnName: '标识' }, { key: 'ParentPart', cnName: '父项' }],
    }],
    operations: [{
      key: 'query_structure_list', title: 'STRUCTURE_LIST 查询', description: 'STRUCTURE_LIST 共有 2 条 STRUCTURE_LIST 记录，父项 A001。',
      inputs: [], outputs: [], flow: ['查询 STRUCTURE_LIST'], relatedObjects: ['STRUCTURE_LIST'],
    }],
  };
  const initialRevision = await artifacts.ensureProject(project.id);
  await artifacts.savePackageVersion(project.id, {
    expectedRevision: initialRevision.oid,
    spec,
    coverage: { covered: [], uncovered: [] },
    note: 'M4 冒烟评测基线',
    action: {
      cause: 'smoke_m4_baseline',
      summary: '创建 M4 冒烟评测基线',
      actorKind: 'system',
      actorName: '冒烟脚本',
    },
  });

  const Q2 = 'CHANGE_ORDER 审批流程是什么';
  const Q3 = '有效性字段的口径如何判定';
  m3repo.addTestcase(project.id, 'STRUCTURE_LIST 有几条记录，父项是什么', '2 条 STRUCTURE_LIST 记录，父项 A001');
  m3repo.addTestcase(project.id, Q2, '需经工艺、质量双会签');
  m3repo.addTestcase(project.id, Q3, '按批次区间 BATCH001-BATCH050 判定');
  await configureProjectDataSource({
    projectId: project.id,
    token: 'stub-token',
    input: { mode: 'none' },
  });

  // 轮次 1：基线
  const r1 = await runRoundAndWait(project.id);
  ok(r1.version === '1.0.0' && r1.passCount === 1 && r1.total === 3, `轮次1 基线：v${r1.version} 通过 ${r1.passCount}/${r1.total}（预期 1/3）`);

  // 优化迭代 1：只采纳 CHANGE_ORDER 建议（模拟专家选择性采纳）
  const v2 = await optimizeOnce(project.id, [Q2]);
  ok(v2 === '1.1.0-rc.1', `优化迭代1 生成发布候选：v${v2}`);
  const sugs1 = m4repo.listSuggestions(project.id);
  ok(sugs1.some((s) => s.status === 'applied' && s.appliedRevision), '采纳的建议已绑定 accepted 工件修订');
  ok(sugs1.some((s) => s.status === 'pending' && s.relatedQuestion === Q3), '未采纳的建议保持 pending');

  const r2 = await runRoundAndWait(project.id);
  ok(r2.version === '1.1.0-rc.1' && r2.passCount === 2, `轮次2：v${r2.version} 通过 ${r2.passCount}/${r2.total}（预期 2/3，较上轮提升）`);

  // 优化迭代 2：基于轮次 2 再提议，采纳剩余失败项
  const v3 = await optimizeOnce(project.id, [Q3]);
  ok(v3 === '1.1.0-rc.2', `优化迭代2 生成发布候选：v${v3}`);
  const r3 = await runRoundAndWait(project.id);
  ok(r3.version === '1.1.0-rc.2' && r3.passCount === 3, `轮次3：v${r3.version} 通过 ${r3.passCount}/${r3.total}（预期 3/3）`);
  ok(r1.passCount < r2.passCount && r2.passCount < r3.passCount, `验收达成：两轮优化通过率持续提升 ${r1.passCount}/3 → ${r2.passCount}/3 → ${r3.passCount}/3`);

  // 版本与导出：当前 Git 工件应包含优化新增操作，且可正常打 zip
  const versions = listPackageVersions(project.id);
  ok(versions.length === 3 && versions[2].note?.includes('优化迭代'), `版本历史 ${versions.map((v) => v.version).join(' → ')}，含变更说明`);
  const finalSnapshot = await artifacts.readPackageFiles(project.id);
  const opFiles = Object.keys(finalSnapshot.files).filter((p) => p.startsWith('operations/') && !p.endsWith('index.md'));
  ok(finalSnapshot.spec.operations.length === 3 && opFiles.length === 3, `${finalSnapshot.spec.version} 含 ${finalSnapshot.spec.operations.length} 个操作（原 1 + 优化新增 2）`);
  const zip = zipPackage(finalSnapshot.files);
  ok(zip.length > 0 && !!unzipSync(zip)['ontology/manifest.yaml'], '最终版本可导出合法 zip（含 ontology/manifest.yaml）');

  // 回归：大规格（规格 JSON >12KB）应用建议不得丢失既有对象/操作（曾因 prompt 截断静默丢失）
  const bigSpec: PackageSpec = {
    ...spec, version: '9.0.0',
    objects: Array.from({ length: 10 }, (_, i) => ({
      key: `OBJ_${i}`, title: `业务对象${i}`, description: `（回归）第 ${i} 个对象，含较长的中文业务描述用于撑大规格体积。`,
      sourceTable: 'STRUCTURE_LIST',
      fields: Array.from({ length: 25 }, (_, j) => ({ key: `F_${i}_${j}`, cnName: `字段${j}`, type: 'string', note: `第 ${j} 个字段的业务口径说明` })),
    })),
    operations: spec.operations.map((operation) => ({ ...operation, relatedObjects: ['OBJ_0'] })),
  };
  const bigJson = JSON.stringify({ objects: bigSpec.objects, operations: bigSpec.operations });
  const bigAdopted = m4repo.listSuggestions(project.id).filter((s) => s.status === 'applied').slice(0, 1);
  const bigRevised = await applyOptimizations(
    getProject(project.id)!,
    bigSpec,
    bigAdopted,
    faux('submit_revised_spec', {
      objects: bigSpec.objects,
      operations: [...bigSpec.operations, {
        key: 'opt_big',
        title: bigAdopted[0].relatedQuestion ?? bigAdopted[0].title,
        description: `（优化新增）${bigAdopted[0].detail}`,
        inputs: [{ param: 'keyword', type: 'string', required: true, note: '查询关键字' }],
        outputs: [{ field: 'result', type: 'string', note: '结论' }],
        flow: ['按沉淀的业务规则直接作答', bigAdopted[0].detail],
        relatedObjects: [bigSpec.objects[0].key],
        competencyQuestion: bigAdopted[0].relatedQuestion,
      }],
      rules: bigSpec.rules ?? [],
      metrics: bigSpec.metrics ?? [],
      scripts: bigSpec.scripts ?? [],
    }),
  );
  ok(bigJson.length > 12000 && bigRevised.objects.length === 10 && bigRevised.operations.length >= spec.operations.length,
    `大规格（JSON ${bigJson.length} 字符）应用建议后既有内容完整保留（对象 ${bigRevised.objects.length}/10）`);

  stub.close();
  console.log(`\n=== M4 冒烟${process.exitCode ? '存在失败' : '通过'} ===\n`);
}

main().catch((e) => { console.error('\nM4 SMOKE ERROR:', e?.stack || e); process.exit(1); });

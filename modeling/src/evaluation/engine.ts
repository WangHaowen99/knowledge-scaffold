import { randomUUID } from 'node:crypto';
// consumerFetch 走 npm undici 的 fetch（放宽超时），multipart 必须用同一 undici 的 FormData，
// 混用内置全局 FormData 会被序列化成空体（consumer 报 No file uploaded）
import { FormData } from 'undici';
import { consumerFetch } from '../consumer/client.js';
import { latestPackageVersion } from '../workflow/m2-repo.js';
import { renderPackage } from '../workflow/package-render.js';
import { zipPackage } from '../workflow/export.js';
import { provisionEvalDb } from './eval-db.js';
import { judge } from './judge.js';
import * as repo from '../workflow/m3-repo.js';
import type { DataDictTable } from '../types.js';

// 评测引擎（M3 核心）：影子工作区 → 导入包 → （可选）配置数据源 →
// 逐用例 runs/sync（串行 + 退避）→ Judge → 落轮次结果。
// 后台执行：startEvaluation 立即返回 roundId，前端轮询 GET round。
export function shadowWorkspaceId(projectId: string): string {
  return `mw-shadow-${projectId.replace(/[^a-zA-Z0-9-]/g, '-')}`.slice(0, 60);
}

export interface BindingConfig {
  provider: string;
  config: Record<string, unknown>;
  name?: string;
}

interface RunOptions {
  projectId: string;
  token: string; // 专家 token 透传
  tables: DataDictTable[];
  binding?: BindingConfig; // 可选：影子工作区数据源配置（provider+config）
}

const active = new Set<string>(); // projectId 级互斥，防并发评测

export function startEvaluation(opts: RunOptions): { roundId: string } {
  const { projectId } = opts;
  if (active.has(projectId)) throw new Error('该项目已有评测在运行');
  const latest = latestPackageVersion(projectId);
  if (!latest) throw new Error('尚未生成知识包');
  const testcases = repo.listTestcases(projectId).filter((t) => t.groundTruth.trim());
  if (!testcases.length) throw new Error('没有已填写标准答案的评测用例');

  const round = repo.createRound(projectId, latest.version, testcases.length);
  active.add(projectId);
  // 后台执行（单进程内异步任务；进度与日志写库供轮询）
  void runRound(round.id, opts, latest, testcases)
    .catch((e) => {
      repo.roundLog(round.id, `轮次失败：${e instanceof Error ? e.message : e}`);
      repo.finishRound(round.id, 'failed');
    })
    .finally(() => active.delete(projectId));
  return { roundId: round.id };
}

async function runRound(
  roundId: string,
  opts: RunOptions,
  latest: { version: string; spec: any },
  testcases: { id: string; question: string; groundTruth: string }[],
): Promise<void> {
  const { projectId, token, tables, binding } = opts;
  const ws = shadowWorkspaceId(projectId);
  const log = (m: string) => repo.roundLog(roundId, m);

  // 1. 影子工作区
  const wsList = await cjson(consumerFetch(token, '/agentic/workspaces'));
  const exists = (wsList.workspaces ?? []).some((w: any) => w.id === ws);
  if (!exists) {
    await cjson(consumerFetch(token, '/agentic/workspaces', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: ws, name: `建模影子·${projectId}`, description: '知识包建模工具评测专用，自动管理' }),
    }), '创建影子工作区');
    log(`影子工作区已创建：${ws}`);
  } else {
    log(`复用影子工作区：${ws}`);
  }

  // 2. 评测库落库（best-effort）
  const db = await provisionEvalDb(projectId, tables);
  log(db.detail);

  // 3. 导入知识包（已存在则覆盖激活版本；人工修订的文件以修订稿为准）
  const files = renderPackage(latest.spec);
  const { getOverrides } = await import('../workflow/package-chat.js');
  for (const [op, oc] of Object.entries(getOverrides(projectId))) if (op in files) files[op] = oc;
  const zip = zipPackage(files);
  const importResult = await importZip(token, ws, zip, latest.spec.id);
  log(`知识包已导入影子工作区：${importResult.ontology_id}（对象 ${importResult.objects_count ?? '-'} / 操作 ${importResult.operations_count ?? '-'}）`);
  // 导入 ≠ 生效：consumer 可能仍激活旧版本（实测吃过亏）。显式激活本次版本并确认。
  try {
    const act = await consumerFetch(token, `/agentic/ontologies/${encodeURIComponent(importResult.ontology_id)}/active-version`, {
      method: 'PUT', workspaceId: ws, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version: latest.spec.version }),
    });
    const actData = (await act.json().catch(() => ({}))) as any;
    if (act.ok) log(`已激活版本：${latest.spec.version}`);
    else log(`⚠ 激活版本失败（${act.status}）：${actData.error ?? actData.detail ?? ''}——评测可能运行旧版本`);
  } catch (e) {
    log(`⚠ 激活版本请求异常：${e instanceof Error ? e.message : e}`);
  }

  // 4. 可选：为本次导入的知识包覆盖评测数据源
  if (binding?.provider) {
    await cjson(consumerFetch(token, `/agentic/ontologies/${encodeURIComponent(importResult.ontology_id)}/data-source`, {
      method: 'PUT', workspaceId: ws, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'direct_provider', provider: binding.provider, config: binding.config }),
    }), '配置知识包评测数据源');
    log(`已配置评测数据源：${binding.provider}`);
  } else {
    log('未提供数据源配置，跳过配置（判断类问题仍可评测）');
  }

  // 5. 逐用例串行运行 + judge
  let done = 0;
  let pass = 0;
  for (const tc of testcases) {
    let finalAnswer = '';
    let verdict: 'pass' | 'partial' | 'fail' | 'error' = 'error';
    let reason = '';
    try {
      finalAnswer = await runCase(token, ws, tc.question);
      const v = await judge(tc.question, tc.groundTruth, finalAnswer);
      verdict = v.verdict;
      reason = v.reason;
    } catch (e) {
      reason = `运行失败：${e instanceof Error ? e.message : e}`;
    }
    repo.addResult(roundId, { testcaseId: tc.id, question: tc.question, groundTruth: tc.groundTruth, finalAnswer, verdict, reason });
    done++;
    if (verdict === 'pass') pass++;
    repo.roundProgress(roundId, done, pass);
    log(`[${done}/${testcases.length}] ${verdict} · ${tc.question.slice(0, 40)}`);
  }

  repo.finishRound(roundId, 'done');
  log(`轮次完成：通过 ${pass}/${testcases.length}`);
}

// runs/sync：429/503 退避重试
async function runCase(token: string, ws: string, question: string): Promise<string> {
  const sessionId = `mw-eval-${randomUUID().slice(0, 8)}`;
  for (let attempt = 0; attempt < 4; attempt++) {
    const res = await consumerFetch(token, '/agentic/runs/sync', {
      method: 'POST', workspaceId: ws, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request: question, session_id: sessionId }),
    });
    if (res.status === 429 || res.status === 503) {
      await new Promise((r) => setTimeout(r, (attempt + 1) * 3000));
      continue;
    }
    const data = (await res.json().catch(() => ({}))) as any;
    if (!res.ok) throw new Error(data.error || data.detail || `runs/sync ${res.status}`);
    return String(data.final_answer ?? '');
  }
  throw new Error('runs/sync 重试后仍被限流');
}

async function importZip(token: string, ws: string, zip: Uint8Array, ontologyId: string): Promise<any> {
  const attempt = async (action?: string) => {
    const form = new FormData();
    form.append('file', new Blob([Buffer.from(zip)], { type: 'application/zip' }), `${ontologyId}.zip`);
    if (action) form.append('action', action);
    return consumerFetch(token, '/agentic/ontologies/import-zip', { method: 'POST', workspaceId: ws, body: form as any });
  };
  let res = await attempt();
  let data = (await res.json().catch(() => ({}))) as any;
  if (res.status === 409 && data.requires_confirmation) {
    // overwrite_version 同时覆盖两类冲突：包未带版本号（覆盖激活版）与包内版本已存在（覆盖同版本）
    res = await attempt('overwrite_version');
    data = (await res.json().catch(() => ({}))) as any;
  }
  if (!res.ok) throw new Error(`导入知识包失败：${data.error || data.detail || res.status}`);
  return data;
}

async function cjson(p: Promise<Response>, what = '请求'): Promise<any> {
  const res = await p;
  const data = (await res.json().catch(() => ({}))) as any;
  if (!res.ok) throw new Error(`${what}失败（${res.status}）：${data.error || data.detail || ''}`);
  return data;
}

import { randomUUID } from 'node:crypto';
import { consumerFetch } from '../consumer/client.js';
import { latestPackageVersion } from '../workflow/m2-repo.js';
import { renderPackage } from '../workflow/package-render.js';
import { zipPackage } from '../workflow/export.js';
import { judge } from './judge.js';
import * as repo from '../workflow/m3-repo.js';
import { getProjectDataSource, type ProjectDataSource } from './data-source-repo.js';
import { prepareProjectDataSourceForEvaluation } from './data-source-evaluation-service.js';
import { getRunningEvaluationDatabaseImportRun } from './eval-db-import-repo.js';
import { importPackageZip } from './data-source-consumer-sync.js';
import {
  ensureEvaluationWorkspace,
  evaluationWorkspaceId,
  shadowWorkspaceId,
} from './shadow-workspace.js';

// 评测引擎（M3 核心）：评测工作空间 → 导入包 → 复用本体级数据源 →
// 逐用例 runs/sync（串行 + 退避）→ Judge → 落轮次结果。
// 后台执行：startEvaluation 立即返回 roundId，前端轮询 GET round。
export { shadowWorkspaceId } from './shadow-workspace.js';

interface RunOptions {
  projectId: string;
  token: string; // 专家 token 透传
}

const activeProjects = new Set<string>();
const activeWorkspaces = new Set<string>();

export function isEvaluationActive(projectId: string): boolean {
  return activeProjects.has(projectId);
}

export function evaluationWorkspaceForRun(
  projectId: string,
  dataSource: ProjectDataSource,
  ontologyId: string,
): string {
  if (dataSource.mode !== 'connection') return dataSource.workspaceId;
  if (!dataSource.ontologyId) {
    throw new Error('数据源配置缺少可用的本体数据源，请返回“数据源配置”重新输入密钥并保存');
  }
  if (dataSource.ontologyId !== ontologyId) {
    throw new Error('本体数据源与当前知识包不匹配，请返回“数据源配置”重新保存');
  }
  const expectedWorkspaceId = evaluationWorkspaceId(projectId, dataSource);
  if (dataSource.workspaceId !== expectedWorkspaceId) {
    throw new Error('数据源配置缺少可用的本体数据源，请返回“数据源配置”重新输入密钥并保存');
  }
  return expectedWorkspaceId;
}

export function startEvaluation(opts: RunOptions): { roundId: string } {
  const { projectId } = opts;
  if (activeProjects.has(projectId)) throw new Error('该项目已有评测在运行');
  const latest = latestPackageVersion(projectId);
  if (!latest) throw new Error('尚未生成知识包');
  const testcases = repo.listTestcases(projectId).filter((t) => t.groundTruth.trim());
  if (!testcases.length) throw new Error('没有已填写标准答案的评测用例');
  const dataSource = getProjectDataSource(projectId);
  if (!dataSource) throw new Error('尚未配置数据源，请先完成“数据源配置”步骤');
  const runningDatabaseImport = getRunningEvaluationDatabaseImportRun(projectId);
  if (runningDatabaseImport) {
    throw new Error(`评测库数据仍在后台导入（${runningDatabaseImport.filename}），请等待导入完成后再运行评测`);
  }
  const workspaceId = evaluationWorkspaceForRun(projectId, dataSource, latest.spec.id);
  if (activeWorkspaces.has(workspaceId)) {
    throw new Error(`评测工作空间 ${workspaceId} 已有评测在运行`);
  }

  const round = repo.createRound(projectId, latest.version, testcases.length);
  activeProjects.add(projectId);
  activeWorkspaces.add(workspaceId);
  // 后台执行（单进程内异步任务；进度与日志写库供轮询）
  void runRound(round.id, opts, latest, testcases, dataSource, workspaceId)
    .catch((e) => {
      repo.roundLog(round.id, `轮次失败：${e instanceof Error ? e.message : e}`);
      repo.finishRound(round.id, 'failed');
    })
    .finally(() => {
      activeProjects.delete(projectId);
      activeWorkspaces.delete(workspaceId);
    });
  return { roundId: round.id };
}

async function runRound(
  roundId: string,
  opts: RunOptions,
  latest: { version: string; spec: any },
  testcases: { id: string; question: string; groundTruth: string }[],
  dataSource: ProjectDataSource,
  workspaceId: string,
): Promise<void> {
  const { projectId, token } = opts;
  const ws = workspaceId;
  const log = (m: string) => repo.roundLog(roundId, m);

  // 1. 评测工作空间
  const workspace = await ensureEvaluationWorkspace({ projectId, workspaceId: ws, token });
  if (workspace.created) {
    log(`评测工作空间已创建：${ws}`);
  } else {
    log(`复用评测工作空间：${ws}`);
  }

  // 2. 导入知识包（已存在则覆盖激活版本；人工修订的文件以修订稿为准）
  const files = renderPackage(latest.spec);
  const { getOverrides } = await import('../workflow/package-chat.js');
  for (const [op, oc] of Object.entries(getOverrides(projectId))) if (op in files) files[op] = oc;
  const zip = zipPackage(files);
  const importResult = await importPackageZip({
    token,
    workspaceId: ws,
    ontologyId: latest.spec.id,
    zip,
  });
  log(`知识包已导入评测工作空间：${importResult.ontology_id}（对象 ${importResult.objects_count ?? '-'} / 操作 ${importResult.operations_count ?? '-'}）`);
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

  // 3. 复用“数据源配置”步骤保存的本体级数据源，不在评测运行时传递密钥
  await prepareProjectDataSourceForEvaluation({ token, ontologyId: importResult.ontology_id, dataSource });
  if (dataSource.mode === 'connection') {
    log(`已复用本体数据源：${dataSource.ontologyId}（${dataSource.provider}）`);
  } else {
    log('已按数据源配置清除本体数据源（判断类问题仍可评测）');
  }

  // 4. 逐用例串行运行 + judge
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

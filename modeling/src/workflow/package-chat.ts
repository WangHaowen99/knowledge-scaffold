import { Type } from '@earendil-works/pi-ai';
import { getDb, nowIso } from '../db/index.js';
import { buildLlm } from '../llm/provider.js';
import { makeAgent, runAgent } from './agent.js';
import { assertObjectDataMappings } from './data-mapping.js';
import { checkCoverage } from './m2.js';
import * as repo from './m2-repo.js';
import type { DataDictTable, PackageSpec, Project } from '../types.js';

// 知识包完善右栏的修缮 agent —— 两段式（采用 update-impact → user-confirm → update-apply）：
// ① plan：只分析影响面（改哪些对象/操作、怎么改、风险），返回结构化计划等专家确认；咨询类直接文字回答；
// ② apply：确认后按计划修订全量规格 → 落新版本（保留内容丢失防护，计划中显式 remove 的除外）。

export interface ChatTurn { role: 'user' | 'assistant'; text: string; }
export interface ChangePlanItem {
  targetKind: 'object' | 'operation';
  targetKey: string;
  action: 'add' | 'modify' | 'remove';
  description: string;
}
export interface ChangePlan { summary: string; changes: ChangePlanItem[]; risks?: string[]; }
export interface ChatPlanResult { reply?: string; plan?: ChangePlan; }
export interface ChatApplyResult { reply: string; version?: string; }

const specBody = (spec: PackageSpec) => JSON.stringify({ objects: spec.objects, operations: spec.operations });
const historyText = (history: ChatTurn[]) =>
  history.slice(-6).map((t) => `${t.role === 'user' ? '专家' : '助手'}：${t.text}`).join('\n');

// ---- 阶段一：影响分析（不改包） ----
export async function packageChatPlan(project: Project, history: ChatTurn[], message: string): Promise<ChatPlanResult> {
  const latest = repo.latestPackageVersion(project.id);
  if (!latest) throw new Error('尚未生成知识包');

  const cap: { plan?: ChangePlan } = {};
  const submitPlan = {
    name: 'submit_change_plan',
    label: '提交修缮计划',
    description: '当专家要求修改知识包时调用：提交影响分析计划（改哪些对象/操作、怎么改、有什么风险），等专家确认后另行应用。仅咨询/解释类问题不要调用。',
    parameters: Type.Object({
      summary: Type.String(),
      changes: Type.Array(Type.Object({
        targetKind: Type.Union([Type.Literal('object'), Type.Literal('operation')]),
        targetKey: Type.String(),
        action: Type.Union([Type.Literal('add'), Type.Literal('modify'), Type.Literal('remove')]),
        description: Type.String(),
      })),
      risks: Type.Optional(Type.Array(Type.String())),
    }),
    async execute(_id: string, params: any) {
      cap.plan = params;
      return { content: [{ type: 'text' as const, text: '计划已提交，等待专家确认。' }], details: {}, terminate: true };
    },
  };

  const sys = [
    '你是知识包修缮助手（package_chat_plan）。专家在评审知识包，会提出修改或提问。',
    '规则：① 咨询/解释类问题直接文字回答，不调用工具；',
    '② 要求修改时【只做影响分析，不直接改】：用 submit_change_plan 提交计划——每处修改一条 change',
    '（目标对象/操作 key、add/modify/remove、具体怎么改），并列出级联风险（如：改口径会影响哪些其他操作、',
    '是否与既有典型问题路由冲突、remove 是否破坏领域问题覆盖）。',
    `项目背景：${project.background || project.domain || project.name}`,
  ].join('\n');

  const body = [
    '【当前规格】', specBody(latest.spec),
    historyText(history) ? `【此前对话】\n${historyText(history)}` : '',
    `【专家消息】\n${message}`,
  ].filter(Boolean).join('\n');

  const agent = makeAgent(buildLlm(), sys, [submitPlan] as any);
  const messages = await runAgent(agent, body);

  if (cap.plan?.changes?.length) return { plan: cap.plan };

  const lastText = [...messages].reverse().find((m: any) => m?.role === 'assistant');
  const text = lastText
    ? (typeof (lastText as any).content === 'string'
      ? (lastText as any).content
      : ((lastText as any).content ?? []).map((c: any) => c.text ?? '').join(''))
    : '';
  return { reply: text || '（agent 未给出答复）' };
}

// ---- 阶段二：确认后应用 ----
export async function packageChatApply(
  project: Project,
  history: ChatTurn[],
  message: string,
  plan: ChangePlan,
  tables: DataDictTable[],
): Promise<ChatApplyResult> {
  const latest = repo.latestPackageVersion(project.id);
  if (!latest) throw new Error('尚未生成知识包');

  const cap: { revised?: { objects: PackageSpec['objects']; operations: PackageSpec['operations'] } } = {};
  const submitRevised = {
    name: 'submit_revised_spec',
    label: '提交修订后的规格',
    description: '提交按已确认计划修订后的完整规格（全量 objects + operations，不是增量）',
    parameters: Type.Object({
      objects: Type.Array(Type.Any()),
      operations: Type.Array(Type.Any()),
    }),
    async execute(_id: string, params: any) {
      cap.revised = { objects: params.objects ?? [], operations: params.operations ?? [] };
      return { content: [{ type: 'text' as const, text: '修订规格已提交。' }], details: {}, terminate: true };
    },
  };

  const sys = [
    '你是知识包修缮助手（package_chat_apply）。专家已确认修缮计划，请把计划逐条落实到规格上，',
    '用 submit_revised_spec 提交完整规格：未涉及的对象与操作原样保留，不得丢失；',
    'add 新增、modify 就地修改、remove 移除计划中指名的条目。',
    '每个对象都必须声明 sourceTable，且只能逐字使用已上传 Excel 数据字典中的真实 Sheet 名；同一 Sheet 可以对应多个对象，每个对象字段必须通过 sourceColumn（缺省为字段 key）对应到该 Sheet 的真实列。',
    `项目背景：${project.background || project.domain || project.name}`,
  ].join('\n');

  const body = [
    '【当前规格】', specBody(latest.spec),
    '【已确认的修缮计划】', JSON.stringify(plan),
    `【专家原始诉求】\n${message}`,
    '请应用计划并提交修订后的完整规格。',
  ].join('\n');

  const agent = makeAgent(buildLlm(), sys, [submitRevised] as any);
  await runAgent(agent, body);

  if (!cap.revised || (!cap.revised.objects.length && !cap.revised.operations.length)) {
    throw new Error('agent 未产出修订规格，请重试');
  }
  const spec = latest.spec;
  const revisedSpec = cap.revised;
  // 落实校验：agent 声称"已修订"但目标未变是实测出现过的失败模式——确定性比对，防空转版本
  const opJson = (ops: any[], key: string) => JSON.stringify(ops.find((o: any) => o.key === key) ?? null);
  const objJson = (objs: any[], key: string) => JSON.stringify(objs.find((o: any) => o.key === key) ?? null);
  const modifyTargets = plan.changes.filter((c) => c.action === 'modify');
  const unexecuted = modifyTargets.filter((c) => c.targetKind === 'operation'
    ? opJson(spec.operations as any[], c.targetKey) === opJson(revisedSpec.operations as any[], c.targetKey)
    : objJson(spec.objects as any[], c.targetKey) === objJson(revisedSpec.objects as any[], c.targetKey));
  if (modifyTargets.length && unexecuted.length === modifyTargets.length) {
    throw new Error(`修订未落实：计划中的 ${modifyTargets.map((c) => c.targetKey).join('/')} 内容与原规格完全相同，已拒绝入库，请重试`);
  }
  if (unexecuted.length) {
    // 部分未落实：放行但在回执中明示，专家可追问补刀
    (cap as any).partialNote = `注意：${unexecuted.map((c) => c.targetKey).join('/')} 未发生实际变化，如需修改请再次明确要求`;
  }
  // 内容丢失防护：数量减少必须能被计划中的 remove 条目解释
  const removedObjs = plan.changes.filter((c) => c.action === 'remove' && c.targetKind === 'object').length;
  const removedOps = plan.changes.filter((c) => c.action === 'remove' && c.targetKind === 'operation').length;
  if (spec.objects.length - cap.revised.objects.length > removedObjs
    || spec.operations.length - cap.revised.operations.length > removedOps) {
    throw new Error(`修订丢失了计划外的内容（对象 ${spec.objects.length}→${cap.revised.objects.length}，操作 ${spec.operations.length}→${cap.revised.operations.length}，计划内删除 ${removedObjs}/${removedOps}），已拒绝入库`);
  }

  const version = `1.${repo.listPackageVersions(project.id).length}.0`;
  const newSpec: PackageSpec = { ...spec, version, objects: cap.revised.objects, operations: cap.revised.operations };
  assertObjectDataMappings(newSpec, tables);
  const selected = repo.listCompetency(project.id).filter((q) => q.selected).map((q) => q.question);
  repo.insertPackageVersion(project.id, version, newSpec, checkCoverage(newSpec, selected), `包修缮对话：${plan.summary.slice(0, 60)}`);
  const note = (cap as any).partialNote ? `\n${(cap as any).partialNote}` : '';
  return { reply: `已按计划修订并生成新版本 v${version}（对象 ${newSpec.objects.length} / 操作 ${newSpec.operations.length}）。左侧文件树已是最新内容。${note}`, version };
}

// ---- 包文件人工修订（覆盖渲染产物）----
export function getOverrides(projectId: string): Record<string, string> {
  const rows = getDb().prepare('SELECT path, content FROM package_overrides WHERE project_id = ?').all(projectId) as any[];
  return Object.fromEntries(rows.map((r) => [r.path, r.content]));
}
export function setOverride(projectId: string, path: string, content: string): void {
  getDb().prepare(`INSERT INTO package_overrides (project_id, path, content, updated_at) VALUES (?,?,?,?)
    ON CONFLICT(project_id, path) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at`)
    .run(projectId, path, content, nowIso());
}
export function clearOverride(projectId: string, path: string): void {
  getDb().prepare('DELETE FROM package_overrides WHERE project_id = ? AND path = ?').run(projectId, path);
}

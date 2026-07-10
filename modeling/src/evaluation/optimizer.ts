import { Type } from '@earendil-works/pi-ai';
import { buildLlm, type LlmDeps } from '../llm/provider.js';
import { makeAgent, runAgent } from '../workflow/agent.js';
import { specObjectSchema, specOperationSchema } from '../workflow/tools.js';
import type { CheckFinding } from '../workflow/package-check.js';
import type { PackageSpec, Project } from '../types.js';
import type { EvalResult } from '../workflow/m3-repo.js';
import type { OptSuggestion } from '../workflow/m4-repo.js';

// M4 优化器（design.md 决策 8）：
// ① proposeOptimizations —— 读未通过的评测结果 + 当前规格，产出结构化优化建议；
//    诊断维度含「执行痕迹」：从 finalAnswer 提取实际引用的操作 vs 认领该问题的操作，
//    区分三类根因——知识缺失（add）/ 知识在包里但没被走到（改路由与交叉引用）/ 知识写错（改内容）。
// ② applyOptimizations —— 把专家采纳的建议应用到规格上，产出修订版（版本号由调用方递增）。
// 另有 findingsToSuggestions —— 把包体检的确定性发现直接转成建议（不经 LLM）。

export interface RawSuggestion {
  title: string; detail: string; kind: string; target?: string; relatedQuestion?: string;
}

const KIND_LITERALS = ['add_operation', 'modify_operation', 'add_object', 'modify_object', 'clarify_description'] as const;

// 从回答文本里提取被引用的操作/对象 key（执行痕迹，确定性提取）
export function extractCitedKeys(text: string, spec: PackageSpec): string[] {
  const keys = [...spec.operations.map((o) => o.key), ...spec.objects.map((o) => o.key)];
  const hit = keys.filter((k) => k.length >= 3 && text.includes(k));
  return [...new Set(hit)];
}

// 认领该问题的操作（消费端路由的"应答入口"）
function claimingOps(question: string, spec: PackageSpec): string[] {
  return spec.operations
    .filter((op) => op.competencyQuestion &&
      (op.competencyQuestion === question || question.includes(op.competencyQuestion) || op.competencyQuestion.includes(question)))
    .map((op) => op.key);
}

// 包体检确定性发现 → 优化建议（错误/警告级、带修复建议的才转；提示级噪音大不转）
const FINDING_KIND: Record<string, string> = {
  典型问题重复认领: 'modify_operation', 悬空引用: 'modify_operation',
  领域问题未覆盖: 'add_operation', 操作不完整: 'modify_operation',
  数据映射失效: 'modify_object', 主键失配: 'modify_object',
};
export function findingsToSuggestions(findings: CheckFinding[]): RawSuggestion[] {
  return findings
    .filter((f) => f.severity !== '提示' && f.suggestion)
    .map((f) => ({
      title: `[体检] ${f.category}：${f.target}`,
      detail: `${f.message}。修复：${f.suggestion}`,
      kind: FINDING_KIND[f.category] ?? 'clarify_description',
      target: f.target,
    }));
}

export async function proposeOptimizations(
  project: Project,
  spec: PackageSpec,
  failedResults: EvalResult[],
  llmOverride?: LlmDeps,
  checkFindings: CheckFinding[] = [],
): Promise<RawSuggestion[]> {
  const cap: { suggestions?: RawSuggestion[] } = {};
  const submitSuggestions = {
    name: 'submit_suggestions',
    label: '提交优化建议',
    description: '针对未通过的评测用例，提交对知识包规格的结构化优化建议（每条建议对应一处可落地的修改）',
    parameters: Type.Object({
      suggestions: Type.Array(Type.Object({
        title: Type.String(),
        detail: Type.String(),
        kind: Type.Union(KIND_LITERALS.map((k) => Type.Literal(k)) as any),
        target: Type.Optional(Type.String()),
        relatedQuestion: Type.Optional(Type.String()),
      })),
    }),
    async execute(_id: string, params: { suggestions: RawSuggestion[] }) {
      cap.suggestions = params.suggestions;
      return { content: [{ type: 'text' as const, text: `已提交 ${params.suggestions.length} 条建议。` }], details: {}, terminate: true };
    },
  };

  const sys = [
    '你是知识包优化器。评测中部分用例未通过，请先按执行痕迹诊断根因，再给可落地的优化建议：',
    '每条用例给出了「应答操作」（认领该问题的操作）与「实际引用」（回答里真实用到的操作）。三类根因分别处理：',
    'A) 知识缺失——没有操作沉淀该口径 → add_operation，把标准答案里的关键规则写进 detail；',
    'B) 知识在包里但没被走到——应答操作含正确口径但实际引用的是别的操作 → modify_operation 修路由：',
    '   收紧被误走操作的认领/描述、加“该问题必须走 XX 操作”的交叉引用；不要重复新增已有规则；',
    'C) 知识写错——实际走了应答操作但结论仍错 → modify_operation 修正该操作的口径/流程措辞（写成不可绕过的强约束、',
    '   把追溯/遍历步骤写成逐行指令，避免执行时漏项）。若同一操作在不同用例中时对时错（执行随机性），',
    '   把口径下沉为可直接执行的查询条件写进 flow（如「查询条件必须为 状态 IN (在库,在制)，禁止只查单一状态」、',
    '   「组合型问题必须分别执行并列出各组成部分结果，任一部分为 0 时先复核再汇总」），杜绝转述丢失口径。',
    'kind 取值：add_operation / modify_operation / add_object / modify_object / clarify_description；',
    'relatedQuestion 填对应用例的问题原文；target 填要改的操作/对象 key。用 submit_suggestions 提交。',
    `项目背景：${project.background || project.domain || project.name}`,
    `当前规格概要：对象 [${spec.objects.map((o) => o.key).join(', ')}]；操作 [${spec.operations.map((o) => `${o.key}:${o.title}`).join(', ')}]`,
  ].join('\n');

  // 压成单行：faux 分支按行正则解析，多行的标准答案/回答会破坏对应关系
  const one = (s: string) => (s || '').replace(/\s*\n\s*/g, ' ').trim();
  const failLines = failedResults.map((r, i) => {
    const answer = one(r.finalAnswer || '（无）');
    const cited = extractCitedKeys(answer, spec);
    const claiming = claimingOps(r.question, spec);
    return `${i + 1}. 问题：${one(r.question)} ｜标准答案：${one(r.groundTruth)} ｜判定：${r.verdict}` +
      ` ｜应答操作：${claiming.join(',') || '（无认领）'} ｜实际引用：${cited.join(',') || '（未识别）'}` +
      ` ｜智能体回答：${answer.slice(0, 600)} ｜理由：${one(r.reason ?? '')}`;
  });
  const checkBlock = checkFindings.length
    ? `\n【包体检发现（确定性检查，可直接采信）】\n${checkFindings.map((f) => `- [${f.severity}/${f.category}] ${f.target}：${f.message}`).join('\n')}`
    : '';
  const agent = makeAgent(llmOverride ?? buildLlm(), sys, [submitSuggestions] as any);
  await runAgent(agent, `未通过的评测用例如下，请给出优化建议：\n${failLines.join('\n')}${checkBlock}`);
  if (!cap.suggestions?.length) throw new Error('优化器未产出建议，请重试');
  return cap.suggestions;
}

export async function applyOptimizations(
  project: Project,
  spec: PackageSpec,
  adopted: OptSuggestion[],
  llmOverride?: LlmDeps,
): Promise<{ objects: PackageSpec['objects']; operations: PackageSpec['operations'] }> {
  const cap: { revised?: { objects: PackageSpec['objects']; operations: PackageSpec['operations'] } } = {};
  const submitRevised = {
    name: 'submit_revised_spec',
    label: '提交修订后的规格',
    description: '提交应用优化建议后的完整知识包规格（全量 objects + operations，不是增量）',
    // 与 submit_package_spec 共用对象/操作 schema（tools.ts），修订时不丢新字段
    parameters: Type.Object({
      objects: Type.Array(specObjectSchema()),
      operations: Type.Array(specOperationSchema()),
    }),
    async execute(_id: string, params: any) {
      cap.revised = { objects: params.objects ?? [], operations: params.operations ?? [] };
      return { content: [{ type: 'text' as const, text: '修订规格已提交。' }], details: {}, terminate: true };
    },
  };

  const sys = [
    '你是知识包优化器。请把专家采纳的优化建议应用到当前知识包规格上，产出修订后的完整规格。',
    '要求：① 保留原有对象与操作（除非建议要求修改）；② 每条建议都要落实——新增操作时把 detail 中的业务规则写进 description/flow，',
    '并在 competencyQuestion 里填 relatedQuestion 原文；③ 用 submit_revised_spec 提交全量规格。',
    `项目背景：${project.background || project.domain || project.name}`,
  ].join('\n');

  // 规格 JSON 不截断：截断会让模型（或 faux 解析）看不到尾部对象/操作，
  // 提交的「全量规格」静默缺内容并被覆盖式入库
  const body = [
    '【当前规格】',
    JSON.stringify({ objects: spec.objects, operations: spec.operations }),
    '【采纳的建议】',
    JSON.stringify(adopted.map((s) => ({ title: s.title, detail: s.detail, kind: s.kind, target: s.target, relatedQuestion: s.relatedQuestion }))),
    '请应用以上建议并提交修订后的完整规格。',
  ].join('\n');

  const agent = makeAgent(llmOverride ?? buildLlm(), sys, [submitRevised] as any);
  await runAgent(agent, body);
  if (!cap.revised || (!cap.revised.objects.length && !cap.revised.operations.length)) {
    throw new Error('优化器未产出修订规格');
  }
  // 落实校验：修订与原规格完全相同 = agent 空转，拒绝产出无效版本
  if (JSON.stringify(cap.revised.objects) === JSON.stringify(spec.objects)
    && JSON.stringify(cap.revised.operations) === JSON.stringify(spec.operations)) {
    throw new Error('修订与原规格完全相同（建议未被落实），已拒绝入库，请重试');
  }
  // 现有建议类型只增改不删：修订版对象/操作数量变少 = 模型丢了内容，拒绝入库
  if (cap.revised.objects.length < spec.objects.length || cap.revised.operations.length < spec.operations.length) {
    throw new Error(`修订规格丢失既有内容（对象 ${spec.objects.length}→${cap.revised.objects.length}，操作 ${spec.operations.length}→${cap.revised.operations.length}），已拒绝入库，请重试`);
  }
  return cap.revised;
}

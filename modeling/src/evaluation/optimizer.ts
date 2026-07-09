import { Type } from '@earendil-works/pi-ai';
import { buildLlm, type LlmDeps } from '../llm/provider.js';
import { makeAgent, runAgent } from '../workflow/agent.js';
import type { PackageSpec, Project } from '../types.js';
import type { EvalResult } from '../workflow/m3-repo.js';
import type { OptSuggestion } from '../workflow/m4-repo.js';

// M4 优化器（design.md 决策 8）：
// ① proposeOptimizations —— 读未通过的评测结果 + 当前规格，产出结构化优化建议；
// ② applyOptimizations —— 把专家采纳的建议应用到规格上，产出修订版（版本号由调用方递增）。
// 两个 agent 用不同工具名（submit_suggestions / submit_revised_spec），faux 模式据此分流。

export interface RawSuggestion {
  title: string; detail: string; kind: string; target?: string; relatedQuestion?: string;
}

const KIND_LITERALS = ['add_operation', 'modify_operation', 'add_object', 'modify_object', 'clarify_description'] as const;

export async function proposeOptimizations(
  project: Project,
  spec: PackageSpec,
  failedResults: EvalResult[],
  llmOverride?: LlmDeps,
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
    '你是知识包优化器。评测中部分用例未通过，说明当前知识包规格缺少回答这些问题所需的操作或业务规则。',
    '请针对每条未通过用例给出可落地的优化建议：新增/修改哪个 operation 或 object、要沉淀什么业务口径（把标准答案中的关键规则写进 detail）。',
    'kind 取值：add_operation / modify_operation / add_object / modify_object / clarify_description；',
    'relatedQuestion 填对应用例的问题原文。用 submit_suggestions 提交。',
    `项目背景：${project.background || project.domain || project.name}`,
    `当前规格概要：对象 [${spec.objects.map((o) => o.key).join(', ')}]；操作 [${spec.operations.map((o) => `${o.key}:${o.title}`).join(', ')}]`,
  ].join('\n');

  // 压成单行：faux 分支按行正则解析，多行的标准答案/回答会破坏对应关系
  const one = (s: string) => (s || '').replace(/\s*\n\s*/g, ' ').trim();
  const failLines = failedResults.map((r, i) =>
    `${i + 1}. 问题：${one(r.question)} ｜标准答案：${one(r.groundTruth)} ｜判定：${r.verdict} ｜智能体回答：${one(r.finalAnswer || '（无）').slice(0, 300)} ｜理由：${one(r.reason ?? '')}`);
  const agent = makeAgent(llmOverride ?? buildLlm(), sys, [submitSuggestions] as any);
  await runAgent(agent, `未通过的评测用例如下，请给出优化建议：\n${failLines.join('\n')}`);
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
    parameters: Type.Object({
      objects: Type.Array(Type.Object({
        key: Type.String(), title: Type.String(), description: Type.String(),
        sourceTable: Type.Optional(Type.String()),
        primaryKey: Type.Optional(Type.Array(Type.String())),
        fields: Type.Array(Type.Object({
          key: Type.String(),
          cnName: Type.Optional(Type.String()),
          type: Type.Optional(Type.String()),
          required: Type.Optional(Type.Boolean()),
          note: Type.Optional(Type.String()),
        })),
      })),
      operations: Type.Array(Type.Object({
        key: Type.String(), title: Type.String(), description: Type.String(),
        inputs: Type.Array(Type.Object({ param: Type.String(), type: Type.String(), required: Type.Boolean(), note: Type.Optional(Type.String()) })),
        outputs: Type.Array(Type.Object({ field: Type.String(), type: Type.String(), note: Type.Optional(Type.String()) })),
        flow: Type.Array(Type.String()),
        relatedObjects: Type.Array(Type.String()),
        competencyQuestion: Type.Optional(Type.String()),
      })),
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
  // 现有建议类型只增改不删：修订版对象/操作数量变少 = 模型丢了内容，拒绝入库
  if (cap.revised.objects.length < spec.objects.length || cap.revised.operations.length < spec.operations.length) {
    throw new Error(`修订规格丢失既有内容（对象 ${spec.objects.length}→${cap.revised.objects.length}，操作 ${spec.operations.length}→${cap.revised.operations.length}），已拒绝入库，请重试`);
  }
  return cap.revised;
}

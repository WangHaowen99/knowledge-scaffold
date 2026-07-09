import type { DataDictTable, PackageSpec, CoverageResult, InterviewBatch, Project } from '../types.js';
import { buildLlm, type LlmDeps } from '../llm/provider.js';
import { buildInterviewTools, buildCompetencyTools, buildGenerateTools, type ToolContext, type InterviewCapture, type CompetencyCapture, type SpecCapture } from './tools.js';
import { makeAgent, runAgent } from './agent.js';
import { upsertNode, getNode, nextOrdinal, listNodes } from './engine.js';
import * as repo from './m2-repo.js';
import { renderPackage } from './package-render.js';

type Ctx = { tables: DataDictTable[]; documents: { filename: string; textContent?: string }[] };

function toolCtx(ctx: Ctx, captured: Record<string, unknown> = {}): ToolContext {
  return { tables: ctx.tables, documents: ctx.documents, captured };
}

function qid(batch: unknown[], i: number): unknown {
  const q = batch[i] as Record<string, unknown>;
  return { id: `q${i}`, ...q };
}

// ---- 节点 4 访谈：启动（产出第一批题，挂起） ----
export async function startInterview(project: Project, ctx: Ctx, analysis: unknown, llmOverride?: LlmDeps): Promise<{ nodeId: string; batch: InterviewBatch | null; finished: boolean }> {
  const llm = llmOverride ?? buildLlm();
  const cap: InterviewCapture = {};
  const tools = buildInterviewTools(toolCtx(ctx), cap);
  const sys = [
    '你是知识建模访谈助手。基于初步分析的开放问题、数据字典与文档，设计访谈澄清四类决策点：对象划分/字段含义/关系/口径。',
    '每次用 ask_user 提交一批（1~5 道）选择题（single/multi/text，均可含"其他"由前端补充）。专家答复后你再决定继续追问或 finish_interview。',
    '控制专家负担：整场访谈一般 2~3 批内结束；只有仍存在影响建模的关键分歧时才追问，勿为完备性重复确认。',
    `项目背景：${project.background || project.domain}`,
    `初步分析：${JSON.stringify(analysis).slice(0, 2000)}`,
  ].join('\n');

  const node = upsertNode({ projectId: project.id, type: 'interview', status: 'running', ordinal: nextOrdinal(project.id) });
  const agent = makeAgent(llm, sys, tools);
  const messages = await runAgent(agent, '请基于初步分析开始建模访谈，用 ask_user 提交第一批题。');

  return finalizeInterviewStep(project.id, node.id, cap, messages);
}

// ---- 节点 4 访谈：专家应答 → 恢复继续 ----
export async function answerInterview(project: Project, ctx: Ctx, batchId: string, answers: any[], llmOverride?: LlmDeps): Promise<{ nodeId: string; batch: InterviewBatch | null; finished: boolean }> {
  repo.saveAnswers(batchId, answers);
  const node = listNodes(project.id).find((n) => n.type === 'interview');
  if (!node) throw new Error('访谈节点不存在');

  const llm = llmOverride ?? buildLlm();
  const cap: InterviewCapture = {};
  const tools = buildInterviewTools(toolCtx(ctx), cap);
  const sys = [
    '你是知识建模访谈助手。继续澄清四类决策点。收到专家答复后，继续 ask_user 追问，或在澄清充分时 finish_interview。',
    '控制专家负担：整场访谈一般 2~3 批内结束；关键决策点已有答案就 finish_interview，勿为完备性重复确认。',
    `项目背景：${project.background || project.domain}`,
  ].join('\n');

  // 从快照恢复
  const agent = makeAgent(llm, sys, tools, (node.agentMessages as unknown[]) ?? []);
  const batch = repo.getBatch(batchId);
  const answerText = summarizeAnswers(batch, answers);
  const messages = await runAgent(agent, `专家对上一批题的答复：\n${answerText}`);

  return finalizeInterviewStep(project.id, node.id, cap, messages);
}

function finalizeInterviewStep(projectId: string, nodeId: string, cap: InterviewCapture, messages: unknown[]) {
  if (cap.finished) {
    upsertNode({ id: nodeId, projectId, type: 'interview', status: 'done', ordinal: getNode(nodeId)!.ordinal, agentMessages: messages });
    return { nodeId, batch: null, finished: true };
  }
  if (cap.pendingBatch) {
    const qs = cap.pendingBatch.questions.map((_, i) => qid(cap.pendingBatch!.questions, i));
    const batch = repo.insertBatch(projectId, nodeId, qs);
    upsertNode({ id: nodeId, projectId, type: 'interview', status: 'waiting_user', ordinal: getNode(nodeId)!.ordinal, agentMessages: messages });
    return { nodeId, batch, finished: false };
  }
  // agent 未出题也未结束——兜底视为结束
  upsertNode({ id: nodeId, projectId, type: 'interview', status: 'done', ordinal: getNode(nodeId)!.ordinal, agentMessages: messages });
  return { nodeId, batch: null, finished: true };
}

function summarizeAnswers(batch: InterviewBatch | null, answers: any[]): string {
  if (!batch) return JSON.stringify(answers);
  return batch.questions.map((q) => {
    const a = answers.find((x) => x.questionId === q.id);
    if (!a || a.skipped) return `- ${q.q}：（跳过）`;
    const parts = [...(a.selected ?? []), a.other].filter(Boolean);
    return `- ${q.q}：${parts.join('；')}`;
  }).join('\n');
}

// ---- 节点 5 胜任力：生成候选 ----
export async function proposeCompetency(project: Project, ctx: Ctx, analysis: unknown, llmOverride?: LlmDeps) {
  const llm = llmOverride ?? buildLlm();
  const cap: CompetencyCapture = {};
  const tools = buildCompetencyTools(toolCtx(ctx), cap);
  const sys = [
    '你是知识建模助手。基于数据字典、领域分析与已有访谈结论，提出 10~20 条"该知识包建成后必须能回答的业务问题"（领域问题）。',
    '问题应具体、可被数据回答、覆盖核心业务场景。用 propose_competency_questions 提交。',
    `初步分析：${JSON.stringify(analysis).slice(0, 1500)}`,
  ].join('\n');

  const node = upsertNode({ projectId: project.id, type: 'competency', status: 'running', ordinal: nextOrdinal(project.id) });
  const agent = makeAgent(llm, sys, tools);
  const messages = await runAgent(agent, '请提出候选胜任力问题并调用 propose_competency_questions。');

  const questions = cap.questions ?? [];
  repo.replaceCompetency(project.id, questions);
  upsertNode({ id: node.id, projectId: project.id, type: 'competency', status: questions.length ? 'done' : 'running', ordinal: node.ordinal, agentMessages: messages });
  return repo.listCompetency(project.id);
}

// ---- 节点 6 包生成：agent 产出 spec → 渲染 + 覆盖检查 ----
export async function generatePackage(project: Project, ctx: Ctx, llmOverride?: LlmDeps): Promise<{ spec: PackageSpec; coverage: CoverageResult; files: Record<string, string>; version: string }> {
  const selected = repo.listCompetency(project.id).filter((c) => c.selected).map((c) => c.question);
  const batches = repo.listBatches(project.id);
  const interviewSummary = batches.map((b) => summarizeAnswers(b, repo.getAnswers(b.id))).join('\n');

  const llm = llmOverride ?? buildLlm();
  const cap: SpecCapture = {};
  const tools = buildGenerateTools(toolCtx(ctx), cap);
  const sys = [
    '你是知识建模助手。基于数据字典、访谈结论与选中的领域问题，产出结构化知识包规格。',
    '要求：① 每个业务对象给出字段（含中文名/类型/业务说明），标注对应的数据表（sourceTable）与主键；',
    '② 每个选中的领域问题至少对应一个 operation（在 operation.competencyQuestion 里填该问题原文），operation 要有输入/输出/流程/关联对象；',
    '③ 用 submit_package_spec 提交。',
    `选中的领域问题（每条都要被 operation 覆盖）：\n${selected.map((q, i) => `${i + 1}. ${q}`).join('\n')}`,
    `访谈结论：\n${interviewSummary.slice(0, 2000)}`,
  ].join('\n');

  const node = upsertNode({ projectId: project.id, type: 'generate', status: 'running', ordinal: nextOrdinal(project.id) });
  const agent = makeAgent(llm, sys, tools);
  const messages = await runAgent(agent, '请产出知识包规格并调用 submit_package_spec。');

  const raw = (cap.spec as any) ?? { objects: [], operations: [] };
  const versions = repo.listPackageVersions(project.id);
  const version = `1.${versions.length}.0`;
  const spec: PackageSpec = {
    id: `${slug(project.name)}-agent`,
    name: project.name,
    description: project.background || project.domain || project.name,
    version,
    language: 'zh-CN',
    defaultProviderAlias: slug(project.domain || 'data'),
    objects: raw.objects ?? [],
    operations: raw.operations ?? [],
  };

  const coverage = checkCoverage(spec, selected);
  const files = renderPackage(spec);
  repo.insertPackageVersion(project.id, version, spec, coverage, node.id);
  upsertNode({ id: node.id, projectId: project.id, type: 'generate', status: 'done', ordinal: node.ordinal, agentMessages: messages, output: { version, coverage } });
  return { spec, coverage, files, version };
}

export function checkCoverage(spec: PackageSpec, selected: string[]): CoverageResult {
  const covered: { question: string; operation: string }[] = [];
  const uncovered: string[] = [];
  for (const q of selected) {
    const op = spec.operations.find((o) => o.competencyQuestion && (o.competencyQuestion === q || q.includes(o.competencyQuestion) || o.competencyQuestion.includes(q)));
    if (op) covered.push({ question: q, operation: op.key });
    else uncovered.push(q);
  }
  return { covered, uncovered };
}

function slug(s: string): string {
  // consumer 的 ontology id 仅允许字母数字下划线短横线且字母/数字开头——中文必须剔除
  return (s || 'pkg').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'pkg';
}

export { repo as m2repo };

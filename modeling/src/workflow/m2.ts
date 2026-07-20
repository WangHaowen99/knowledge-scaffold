import type { DataDictTable, PackageSpec, CoverageResult, InterviewBatch, Project } from '../types.js';
import { buildLlm, type LlmDeps } from '../llm/provider.js';
import { buildInterviewTools, buildCompetencyTools, buildGenerateTools, type ToolContext, type InterviewCapture, type CompetencyCapture, type SpecCapture } from './tools.js';
import { makeAgent, runAgent, runAgentCaptured } from './agent.js';
import { upsertNode, getNode, nextOrdinal, listNodes } from './engine.js';
import * as repo from './m2-repo.js';
import { assertObjectDataMappings } from './data-mapping.js';
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
    `初步分析：${JSON.stringify(stripQuality(analysis)).slice(0, 2000)}`,
    ...gapHints(analysis),
  ].join('\n');

  const node = upsertNode({ projectId: project.id, type: 'interview', status: 'running', ordinal: nextOrdinal(project.id) });
  const agent = makeAgent(llm, sys, tools);
  const messages = await runAgentCaptured(agent, '请基于初步分析开始建模访谈，用 ask_user 提交第一批题。',
    () => !!(cap.finished || cap.pendingBatch),
    '你刚才把题目写成了正文。请立即调用 ask_user 工具提交这批题（结构化），不要以正文形式输出；若确无需访谈请调用 finish_interview。');

  return finalizeInterviewStep(project.id, node.id, cap, messages);
}

// ---- 节点 4 访谈：专家应答 → 恢复继续 ----
export async function answerInterview(project: Project, ctx: Ctx, batchId: string, answers: any[], llmOverride?: LlmDeps): Promise<{ nodeId: string; batch: InterviewBatch | null; finished: boolean }> {
  repo.saveAnswers(batchId, answers);
  // 取最新访谈节点（访谈可能被重开，旧节点残留）
  const node = [...listNodes(project.id)].reverse().find((n) => n.type === 'interview');
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
  const messages = await runAgentCaptured(agent, `专家对上一批题的答复：\n${answerText}`,
    () => !!(cap.finished || cap.pendingBatch),
    '请调用 ask_user 工具提交下一批题，或调用 finish_interview 结束访谈——不要以正文形式输出题目。');

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
  // agent 未出题也未结束：只有已经问过至少一批时才宽容地视为结束；
  // 一批都没问过就"完成"是伪成功（实测：模型把题目写成正文），必须显式失败可重试
  if (repo.listBatches(projectId).length > 0) {
    upsertNode({ id: nodeId, projectId, type: 'interview', status: 'done', ordinal: getNode(nodeId)!.ordinal, agentMessages: messages });
    return { nodeId, batch: null, finished: true };
  }
  upsertNode({ id: nodeId, projectId, type: 'interview', status: 'pending', ordinal: getNode(nodeId)!.ordinal, agentMessages: messages });
  throw new Error('访谈 agent 未按协议出题（已轻推仍失败），请重试');
}

// 资料质量评估的缺口 → 访谈优先澄清点（分析摘要剔除 quality 防挤占篇幅）
function stripQuality(analysis: unknown): unknown {
  if (!analysis || typeof analysis !== 'object') return analysis;
  const { quality, ...rest } = analysis as Record<string, unknown>;
  return rest;
}
function gapHints(analysis: unknown): string[] {
  const gaps = (analysis as any)?.quality?.gaps;
  if (!Array.isArray(gaps) || !gaps.length) return [];
  const lines = gaps.slice(0, 6).map((g: any) => `${g.kind}·${g.target}：${g.message}`).join('；');
  return [`资料缺口（访谈应优先澄清）：${lines}`.slice(0, 900)];
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
  const messages = await runAgentCaptured(agent, '请提出候选胜任力问题并调用 propose_competency_questions。',
    () => !!cap.questions, '请调用 propose_competency_questions 工具提交候选问题（不要以正文形式输出）。');

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
    '要求：① 每个业务对象都必须给出字段（含中文名/类型/业务说明）、sourceTable 和主键；sourceTable 只能逐字填写数据字典中真实存在的 Excel Sheet 名，不得生成没有 Excel 来源的对象；',
    '同一个 Excel Sheet 可以拆分、投影为多个业务对象，不要求 Sheet 与对象一一对应；每个对象字段都必须存在于该对象的 sourceTable 中，字段 key 用业务命名，sourceColumn 填真实列名（二者一致时可省略）；',
    '尽量补充对象的查询边界（queryBoundary）、别名（aliases）、字段别名（alias）与对象间关系（relations）；',
    '② 每个选中的领域问题至少对应一个 operation（在 operation.competencyQuestion 里填该问题原文），operation 要有输入/输出（含中文名 cnName）/流程/关联对象；',
    '流程步骤中引用对象统一写 object:<key>、引用脚本统一写 script:<key>；',
    '③ 可复用的业务判断条件沉淀为 rule（规则内容/判断条件/例外/适用对象），可复用的计算口径沉淀为 metric（业务口径公式/计算边界/空值策略/维度），不要把它们长期埋在 operation 正文里；operation 通过 usesRules/usesMetrics 引用；',
    '④ 用 submit_package_spec 提交。',
    `选中的领域问题（每条都要被 operation 覆盖）：\n${selected.map((q, i) => `${i + 1}. ${q}`).join('\n')}`,
    `访谈结论：\n${interviewSummary.slice(0, 2000)}`,
  ].join('\n');

  const node = upsertNode({ projectId: project.id, type: 'generate', status: 'running', ordinal: nextOrdinal(project.id) });
  const agent = makeAgent(llm, sys, tools);
  const messages = await runAgentCaptured(agent, '请产出知识包规格并调用 submit_package_spec。',
    () => !!cap.spec, '请调用 submit_package_spec 工具提交完整规格 JSON（不要以正文形式输出）。');

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
    rules: raw.rules ?? [],
    metrics: raw.metrics ?? [],
  };

  assertObjectDataMappings(spec, ctx.tables);
  const coverage = checkCoverage(spec, selected);
  const files = renderPackage(spec);
  repo.insertPackageVersion(project.id, version, spec, coverage, node.id);
  upsertNode({ id: node.id, projectId: project.id, type: 'generate', status: 'done', ordinal: node.ordinal, agentMessages: messages, output: { version, coverage, objectCount: spec.objects.length, operationCount: spec.operations.length } });
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

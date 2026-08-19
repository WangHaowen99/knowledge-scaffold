import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import type { PackageArtifactStore } from '../artifacts/package-store.js';
import { getPackageArtifactStore } from '../artifacts/runtime.js';
import { PiAuthoringAdapter } from '../authoring/pi-adapter.js';
import { AuthoringState, getAuthoringState } from '../authoring/state.js';
import type { AuthoringInteraction, JsonObject } from '../authoring/types.js';
import { getDb, nowIso } from '../db/index.js';
import type { PackageSpec, Project } from '../types.js';
import { getCheck, type CheckFinding } from '../workflow/package-check.js';
import { renderPackage, type PackageFiles } from '../workflow/package-render.js';
import {
  validateGeneratedKnowledge,
  type KnowledgeGenerationContext,
  type KnowledgeStructureValidation,
  type KnowledgeStructureValidator,
} from './knowledge-structure-runner.js';
import { AiConstructionOrchestrator } from './orchestrator.js';
import { throwIfCancelled } from '../cancellation.js';
import { reportLongTaskProgress } from './task-progress.js';

export const DEFAULT_AUTOMATIC_REPAIR_LIMIT = 2;

export type RepairAuthority = 'automatic' | 'approval_required' | 'expert_decision';
export type RepairAttemptStatus = 'running' | 'completed' | 'waiting_user' | 'stopped' | 'failed';

export interface ClassifiedRepairFinding {
  finding: CheckFinding;
  authority: RepairAuthority;
  reason: string;
}

export interface AutomaticRepairAction {
  findingIds: string[];
  path: string;
  mode: 'restore_generated_file' | 'repair_frontmatter';
  summary: string;
}

export interface AutomaticRepairPlan {
  files: Record<string, string>;
  actions: AutomaticRepairAction[];
  unresolved: ClassifiedRepairFinding[];
}

export interface PackageRepairAttempt {
  id: string;
  projectId: string;
  rootRevision: string;
  inputRevision: string;
  outputRevision?: string;
  authority: RepairAuthority;
  status: RepairAttemptStatus;
  findingsBefore: CheckFinding[];
  findingsAfter: CheckFinding[];
  actions: JsonObject[];
  error?: string;
  createdAt: string;
  completedAt?: string;
}

export interface ControlledRepairResult {
  status: 'valid' | 'automatic_repaired' | 'waiting_approval' | 'waiting_expert' | 'stopped';
  revision: string;
  validation: KnowledgeStructureValidation;
  attempts: PackageRepairAttempt[];
  interaction?: AuthoringInteraction;
  aiConstruction: ReturnType<AiConstructionOrchestrator['startKnowledgeRepair']>['aiConstruction'];
  message: ReturnType<AiConstructionOrchestrator['startKnowledgeRepair']>['message'];
}

export interface ControlledRepairRunnerOptions {
  orchestrator: AiConstructionOrchestrator;
  packages?: PackageArtifactStore;
  adapter?: PiAuthoringAdapter;
  state?: AuthoringState;
  attempts?: ControlledRepairAttemptStore;
  validator?: KnowledgeStructureValidator;
  checkReader?: typeof getCheck;
  maxAutomaticAttempts?: number;
}

const SAFE_FORMAT_CATEGORIES = new Set([
  '必选文件缺失',
  'manifest 非法',
  'frontmatter 缺失',
  'frontmatter 不完整',
  'resource 声明不符',
  'key 非法',
  '索引与详情不一致',
  '对象索引非法',
  'mapping 非法',
]);

const FRONTMATTER_CATEGORIES = new Set([
  'frontmatter 缺失',
  'frontmatter 不完整',
  'resource 声明不符',
  'key 非法',
]);

const EXPERT_DECISION_PATTERN = /(阈值|门槛|判断条件|流程分支|分支条件|异常策略|计算口径|统计口径|口径冲突|公式|冲突裁决|知识冲突|空值语义|无证据|证据与假设|业务边界)/;
const SAFE_MAPPING_STRUCTURE_PATTERN = /(缺少 schema_version|缺少 default_provider_alias|缺少 objects 映射)/;

function cloneFinding(finding: CheckFinding): CheckFinding {
  return JSON.parse(JSON.stringify(finding)) as CheckFinding;
}

function findingCounts(findings: CheckFinding[]): Omit<KnowledgeStructureValidation, 'findings'> {
  return {
    errors: findings.filter((item) => item.severity === '错误').length,
    warnings: findings.filter((item) => item.severity === '警告').length,
    hints: findings.filter((item) => item.severity === '提示').length,
  };
}

function repairMessageKey(kind: string, ...tokens: Array<string | number>): string {
  const suffix = tokens.map((token) => String(token)
    .replace(/[^A-Za-z0-9_-]/g, '_')
    .slice(0, 12)).filter(Boolean).join('.');
  return `knowledge.repair.${kind}${suffix ? `.${suffix}` : ''}`.slice(0, 64);
}

export function classifyRepairFinding(finding: CheckFinding): ClassifiedRepairFinding {
  const safeFormat = finding.source === 'format'
    && SAFE_FORMAT_CATEGORIES.has(finding.category)
    && (finding.category !== 'mapping 非法' || SAFE_MAPPING_STRUCTURE_PATTERN.test(finding.message));
  if (safeFormat) {
    return {
      finding: cloneFinding(finding),
      authority: 'automatic',
      reason: '该问题只涉及可由当前规格唯一重新生成的知识包结构',
    };
  }
  const businessText = `${finding.category}\n${finding.message}\n${finding.suggestion ?? ''}`;
  if (EXPERT_DECISION_PATTERN.test(businessText)) {
    return {
      finding: cloneFinding(finding),
      authority: 'expert_decision',
      reason: '该问题涉及业务判断，系统不能替领域专家选择口径或分支',
    };
  }
  return {
    finding: cloneFinding(finding),
    authority: 'approval_required',
    reason: '修复会改变知识语义，必须先展示方案并由用户确认',
  };
}

export function classifyRepairFindings(findings: CheckFinding[]): ClassifiedRepairFinding[] {
  return findings.map(classifyRepairFinding);
}

function frontmatterEnd(content: string): number {
  if (!content.startsWith('---\n')) return -1;
  return content.indexOf('\n---', 4);
}

function repairFrontmatter(
  current: string | undefined,
  generated: string,
  finding: CheckFinding,
): string {
  const generatedEnd = frontmatterEnd(generated);
  if (generatedEnd < 0) return generated;
  const canonical = generated.slice(0, generatedEnd + 4);
  if (current === undefined) return generated;
  const currentEnd = frontmatterEnd(current);
  if (currentEnd < 0) return `${canonical}\n${current}`;

  const canonicalLines = generated.slice(4, generatedEnd).split('\n');
  const currentLines = current.slice(4, currentEnd).split('\n');
  const fields = finding.category === 'resource 声明不符'
    ? ['resource']
    : finding.category === 'key 非法'
      ? ['key']
      : finding.category === 'frontmatter 不完整'
        ? (finding.message.match(/缺少 (resource|key|title)/)?.slice(1) ?? ['resource', 'key', 'title'])
        : ['resource', 'key', 'title'];
  for (const field of fields) {
    const canonicalLine = canonicalLines.find((line) => line.startsWith(`${field}:`));
    if (!canonicalLine) continue;
    const index = currentLines.findIndex((line) => line.startsWith(`${field}:`));
    if (index >= 0) currentLines[index] = canonicalLine;
    else currentLines.push(canonicalLine);
  }
  return `---\n${currentLines.join('\n')}\n---${current.slice(currentEnd + 4)}`;
}

export function buildAutomaticRepairPlan(
  spec: PackageSpec,
  files: PackageFiles,
  findings: ClassifiedRepairFinding[],
): AutomaticRepairPlan {
  const generated = renderPackage(spec);
  const actionsByPath = new Map<string, AutomaticRepairAction>();
  const targetFiles = new Map<string, string>();
  const unresolved: ClassifiedRepairFinding[] = [];

  for (const classified of findings) {
    if (classified.authority !== 'automatic') continue;
    const { finding } = classified;
    const canonical = generated[finding.target];
    if (canonical === undefined) {
      unresolved.push({
        ...classified,
        authority: 'approval_required',
        reason: '当前规格不能唯一生成该目标，自动修改可能覆盖人工内容',
      });
      continue;
    }
    const requestedMode = FRONTMATTER_CATEGORIES.has(finding.category)
      ? 'repair_frontmatter' as const
      : 'restore_generated_file' as const;
    const existing = actionsByPath.get(finding.target);
    const mode = existing?.mode === 'restore_generated_file'
      || requestedMode === 'restore_generated_file'
      ? 'restore_generated_file' as const
      : 'repair_frontmatter' as const;
    const base = targetFiles.get(finding.target) ?? files[finding.target];
    const repaired = mode === 'repair_frontmatter'
      ? repairFrontmatter(base, canonical, finding)
      : canonical;
    if (base === repaired) {
      if (existing) {
        existing.findingIds.push(finding.id);
        continue;
      }
      unresolved.push({
        ...classified,
        authority: 'approval_required',
        reason: '确定性修复不会产生内容变化，需要人工检查问题来源',
      });
      continue;
    }
    targetFiles.set(finding.target, repaired);
    if (existing) {
      existing.findingIds.push(finding.id);
      existing.mode = mode;
      existing.summary = mode === 'repair_frontmatter'
        ? `按当前规格修复 ${finding.target} 的结构化头部，保留正文`
        : `按当前规格重新生成 ${finding.target}`;
    } else {
      actionsByPath.set(finding.target, {
        findingIds: [finding.id],
        path: finding.target,
        mode,
        summary: mode === 'repair_frontmatter'
          ? `按当前规格修复 ${finding.target} 的结构化头部，保留正文`
          : `按当前规格重新生成 ${finding.target}`,
      });
    }
  }

  return {
    files: Object.fromEntries([...targetFiles.entries()].sort(([left], [right]) => left.localeCompare(right))),
    actions: [...actionsByPath.values()].sort((left, right) => left.path.localeCompare(right.path)),
    unresolved,
  };
}

type RepairTargetKind = 'object' | 'operation' | 'rule' | 'metric' | 'script';

export interface RepairTarget { targetKind: RepairTargetKind; targetKey: string }
const REPAIR_TARGET_KINDS = new Set<RepairTargetKind>([
  'object', 'operation', 'rule', 'metric', 'script',
]);

/**
 * 找出该校验发现可能涉及的知识资源。
 * 冲突类问题（例如两个对象共用同一个 candidateId）天然指向多个资源，
 * 这时不能替业务决定改哪一个——把候选交给专家选。
 */
export function repairTargetCandidates(spec: PackageSpec, finding: CheckFinding): RepairTarget[] {
  const target = finding.target.replace(/\.md$/, '').split('/').at(-1) ?? finding.target;
  const collections: Array<[RepairTargetKind, Array<{ key: string }>]> = [
    ['object', spec.objects],
    ['operation', spec.operations],
    ['rule', spec.rules ?? []],
    ['metric', spec.metrics ?? []],
    ['script', spec.scripts ?? []],
  ];
  for (const [targetKind, items] of collections) {
    const item = items.find((candidate) => candidate.key === finding.target || candidate.key === target);
    if (item) return [{ targetKind, targetKey: item.key }];
  }
  const findingText = `${finding.target}\n${finding.message}\n${finding.suggestion ?? ''}`;
  const mentioned = collections.flatMap(([targetKind, items]) => items
    .filter((item) => findingText.includes(item.key))
    .map((item) => ({ targetKind, targetKey: item.key })));
  return [...new Map(mentioned.map((item) => [
    `${item.targetKind}:${item.targetKey}`,
    item,
  ])).values()];
}

export function inferRepairTarget(
  spec: PackageSpec,
  finding: CheckFinding,
): RepairTarget | null {
  const candidates = repairTargetCandidates(spec, finding);
  return candidates.length === 1 ? candidates[0] : null;
}

/**
 * 定位不到唯一目标时抛这个而不是普通 Error：调用方据此把该发现转成
 * 「需人工处理」并给出可执行的下一步，而不是让整轮修复中断在一条红字上。
 */
export class RepairTargetUnresolvedError extends Error {
  constructor(
    readonly finding: CheckFinding,
    readonly candidates: RepairTarget[],
  ) {
    super(candidates.length
      ? `校验问题「${finding.category}」同时涉及 ${candidates.map((item) => item.targetKey).join('、')}，需要先确定要修改哪一个`
      : `校验问题「${finding.category}」无法定位到具体的知识资源，需要人工处理`);
    this.name = 'RepairTargetUnresolvedError';
  }
}

export function buildSemanticRepairPlan(
  spec: PackageSpec,
  finding: CheckFinding,
  expertDecision?: string,
  explicitTarget?: RepairTarget,
): {
  summary: string;
  message: string;
  changes: Array<{
    id: string;
    targetKind: RepairTargetKind;
    targetKey: string;
    action: 'modify';
    description: string;
  }>;
  risks: string[];
} {
  // 专家显式指定的目标优先：冲突类问题的候选不止一个，只有业务能决定改哪一个
  const target = explicitTarget ?? inferRepairTarget(spec, expertDecision ? {
    ...finding,
    suggestion: [finding.suggestion, expertDecision].filter(Boolean).join('\n'),
  } : finding);
  if (!target) {
    throw new RepairTargetUnresolvedError(finding, repairTargetCandidates(spec, finding));
  }
  const instruction = [
    finding.suggestion || `修复“${finding.category}”：${finding.message}`,
    expertDecision ? `专家明确决策：${expertDecision}` : '',
  ].filter(Boolean).join('\n');
  return {
    summary: `修复 ${target.targetKind}:${target.targetKey} 的“${finding.category}”问题`,
    message: [
      `当前校验发现：${finding.message}`,
      `请严格只修改 ${target.targetKind}:${target.targetKey}，落实以下修复要求：`,
      instruction,
      '不得顺带修改计划外资源，也不得把示例答案写成通用知识。',
    ].join('\n'),
    changes: [{
      id: '0',
      ...target,
      action: 'modify',
      description: instruction,
    }],
    risks: [
      '该修改会改变知识语义，应用后必须重新执行阶段校验。',
      ...(expertDecision ? ['方案必须忠实落实专家决策，不得自行扩展业务口径。'] : []),
    ],
  };
}

/** 从回答里取出专家选定的修改目标（多候选时才有） */
export function parseExpertRepairTarget(response: JsonObject): RepairTarget | undefined {
  const answers = Array.isArray(response.answers) ? response.answers : [];
  const picked = answers.find((item) => (
    item && typeof item === 'object' && !Array.isArray(item)
      && (item as Record<string, unknown>).questionId === 'repair_target'
  )) as Record<string, unknown> | undefined;
  const selected = Array.isArray(picked?.selected) ? picked!.selected.map(String) : [];
  const [value] = selected.map((item) => item.trim()).filter(Boolean);
  if (!value) return undefined;
  const separator = value.indexOf(':');
  if (separator <= 0) return undefined;
  const targetKind = value.slice(0, separator).trim() as RepairTargetKind;
  const targetKey = value.slice(separator + 1).trim();
  if (!REPAIR_TARGET_KINDS.has(targetKind) || !targetKey) return undefined;
  return { targetKind, targetKey };
}

export function parseExpertRepairDecision(response: JsonObject): string {
  const answers = Array.isArray(response.answers) ? response.answers : [];
  // 目标选择是另一道题，不能混进业务决策文本
  const first = answers.find((item) => item && typeof item === 'object' && !Array.isArray(item)
    && (item as Record<string, unknown>).questionId !== 'repair_target') ?? answers[0];
  if (!first || typeof first !== 'object' || Array.isArray(first)) {
    throw new Error('请明确填写业务决策');
  }
  const answer = first as Record<string, unknown>;
  const selected = Array.isArray(answer.selected)
    ? answer.selected.map(String).map((item) => item.trim()).filter(Boolean)
    : [];
  const other = typeof answer.other === 'string' ? answer.other.trim() : '';
  const decision = [...selected, ...(other ? [other] : [])].join('；');
  if (!decision) throw new Error('请明确填写业务决策');
  return decision.slice(0, 8_000);
}

function attemptFromRow(row: any): PackageRepairAttempt {
  return {
    id: row.id,
    projectId: row.project_id,
    rootRevision: row.root_revision_oid,
    inputRevision: row.input_revision_oid,
    outputRevision: row.output_revision_oid ?? undefined,
    authority: row.authority,
    status: row.status,
    findingsBefore: JSON.parse(row.findings_before_json),
    findingsAfter: JSON.parse(row.findings_after_json),
    actions: JSON.parse(row.actions_json),
    error: row.error ?? undefined,
    createdAt: row.created_at,
    completedAt: row.completed_at ?? undefined,
  };
}

export class ControlledRepairAttemptStore {
  constructor(private readonly db: DatabaseSync = getDb()) {}

  rootRevision(projectId: string, inputRevision: string): string {
    const previous = this.db.prepare(`SELECT root_revision_oid FROM package_repair_attempts
      WHERE project_id = ? AND output_revision_oid = ?
      ORDER BY COALESCE(completed_at, created_at) DESC, rowid DESC LIMIT 1`).get(projectId, inputRevision) as
      { root_revision_oid: string } | undefined;
    return previous?.root_revision_oid ?? inputRevision;
  }

  automaticAttemptCount(projectId: string, rootRevision: string): number {
    const row = this.db.prepare(`SELECT COUNT(*) AS count FROM package_repair_attempts
      WHERE project_id = ? AND root_revision_oid = ? AND authority = 'automatic'`)
      .get(projectId, rootRevision) as { count: number };
    return Number(row.count);
  }

  start(input: {
    projectId: string;
    rootRevision: string;
    inputRevision: string;
    authority: RepairAuthority;
    status?: Extract<RepairAttemptStatus, 'running' | 'waiting_user'>;
    findings: CheckFinding[];
    actions?: JsonObject[];
  }): PackageRepairAttempt {
    const attempt: PackageRepairAttempt = {
      id: `pra_${randomUUID().slice(0, 12)}`,
      projectId: input.projectId,
      rootRevision: input.rootRevision,
      inputRevision: input.inputRevision,
      authority: input.authority,
      status: input.status ?? 'running',
      findingsBefore: input.findings.map(cloneFinding),
      findingsAfter: [],
      actions: input.actions ?? [],
      createdAt: nowIso(),
    };
    this.db.prepare(`INSERT INTO package_repair_attempts (
      id, project_id, root_revision_oid, input_revision_oid, authority, status,
      findings_before_json, findings_after_json, actions_json, created_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?)`).run(
      attempt.id,
      attempt.projectId,
      attempt.rootRevision,
      attempt.inputRevision,
      attempt.authority,
      attempt.status,
      JSON.stringify(attempt.findingsBefore),
      '[]',
      JSON.stringify(attempt.actions),
      attempt.createdAt,
    );
    return attempt;
  }

  complete(
    attemptId: string,
    outputRevision: string,
    findingsAfter: CheckFinding[],
    actions?: JsonObject[],
  ): PackageRepairAttempt {
    const completedAt = nowIso();
    const result = this.db.prepare(`UPDATE package_repair_attempts SET
      status = 'completed', output_revision_oid = ?, findings_after_json = ?,
      actions_json = COALESCE(?, actions_json), completed_at = ?, error = NULL
      WHERE id = ? AND status IN ('running', 'waiting_user')`).run(
      outputRevision,
      JSON.stringify(findingsAfter),
      actions ? JSON.stringify(actions) : null,
      completedAt,
      attemptId,
    );
    if (Number(result.changes) !== 1) throw new Error('修复尝试已经处理或不存在');
    return this.get(attemptId);
  }

  addAction(attemptId: string, action: JsonObject): PackageRepairAttempt {
    const attempt = this.get(attemptId);
    const actions = [...attempt.actions, action];
    this.db.prepare('UPDATE package_repair_attempts SET actions_json = ? WHERE id = ?')
      .run(JSON.stringify(actions), attemptId);
    return this.get(attemptId);
  }

  recordRetryableFailure(attemptId: string, reason: string): PackageRepairAttempt {
    const attempt = this.get(attemptId);
    if (attempt.status !== 'waiting_user') throw new Error('修复尝试当前不能重试');
    const actions = [
      ...attempt.actions,
      { type: 'apply_failed', reason: reason.trim().slice(0, 3_000), at: nowIso() },
    ];
    this.db.prepare(`UPDATE package_repair_attempts SET error = ?, actions_json = ?
      WHERE id = ? AND status = 'waiting_user'`)
      .run(reason.trim().slice(0, 3_000), JSON.stringify(actions), attemptId);
    return this.get(attemptId);
  }

  stop(attemptId: string, reason: string): PackageRepairAttempt {
    return this.finishWithStatus(attemptId, 'stopped', reason);
  }

  fail(
    attemptId: string,
    reason: string,
    outputRevision?: string,
    findingsAfter: CheckFinding[] = [],
  ): PackageRepairAttempt {
    const result = this.db.prepare(`UPDATE package_repair_attempts SET
      status = 'failed', error = ?, output_revision_oid = COALESCE(?, output_revision_oid),
      findings_after_json = ?, completed_at = ?
      WHERE id = ? AND status IN ('running', 'waiting_user')`)
      .run(
        reason.trim().slice(0, 3_000),
        outputRevision ?? null,
        JSON.stringify(findingsAfter),
        nowIso(),
        attemptId,
      );
    if (Number(result.changes) !== 1) throw new Error('修复尝试已经处理或不存在');
    return this.get(attemptId);
  }

  get(attemptId: string): PackageRepairAttempt {
    const row = this.db.prepare('SELECT * FROM package_repair_attempts WHERE id = ?')
      .get(attemptId);
    if (!row) throw new Error('修复尝试不存在');
    return attemptFromRow(row);
  }

  listChain(projectId: string, rootRevision: string): PackageRepairAttempt[] {
    return (this.db.prepare(`SELECT * FROM package_repair_attempts
      WHERE project_id = ? AND root_revision_oid = ? ORDER BY created_at, rowid`)
      .all(projectId, rootRevision) as any[]).map(attemptFromRow);
  }

  private finishWithStatus(
    attemptId: string,
    status: Extract<RepairAttemptStatus, 'stopped' | 'failed'>,
    reason: string,
  ): PackageRepairAttempt {
    const result = this.db.prepare(`UPDATE package_repair_attempts SET
      status = ?, error = ?, completed_at = ?
      WHERE id = ? AND status IN ('running', 'waiting_user')`)
      .run(status, reason.trim().slice(0, 3_000), nowIso(), attemptId);
    if (Number(result.changes) !== 1) throw new Error('修复尝试已经处理或不存在');
    return this.get(attemptId);
  }
}

function findingForPayload(finding: CheckFinding): JsonObject {
  return JSON.parse(JSON.stringify(finding)) as JsonObject;
}

export function repairFindingFromPayload(value: unknown): CheckFinding {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('修复问题上下文已失效');
  }
  const finding = value as Record<string, unknown>;
  const source = String(finding.source ?? '');
  const severity = String(finding.severity ?? '');
  const id = String(finding.id ?? '').trim();
  const category = String(finding.category ?? '').trim();
  const target = String(finding.target ?? '').trim();
  const message = String(finding.message ?? '').trim();
  if (!id || !category || !target || !message
    || !['rule', 'semantic', 'format'].includes(source)
    || !['错误', '警告', '提示'].includes(severity)) {
    throw new Error('修复问题上下文已失效');
  }
  return {
    id,
    source: source as CheckFinding['source'],
    severity: severity as CheckFinding['severity'],
    category,
    target,
    message,
    ...(typeof finding.suggestion === 'string' && finding.suggestion.trim()
      ? { suggestion: finding.suggestion.trim() }
      : {}),
    ...(typeof finding.releaseBlocking === 'boolean'
      ? { releaseBlocking: finding.releaseBlocking }
      : {}),
  };
}

/**
 * 分级校验修复的唯一入口。它只暴露“运行、回答专家问题、完成/取消方案”四个动作，
 * 把分类、自动修复修订、重试链和人工交互审计封装在模块内部。
 */
export class ControlledRepairRunner {
  private readonly orchestrator: AiConstructionOrchestrator;
  private readonly packages: PackageArtifactStore;
  private readonly adapter: PiAuthoringAdapter;
  private readonly state: AuthoringState;
  private readonly attempts: ControlledRepairAttemptStore;
  private readonly validator: KnowledgeStructureValidator;
  private readonly checkReader: typeof getCheck;
  private readonly maxAutomaticAttempts: number;

  constructor(options: ControlledRepairRunnerOptions) {
    this.orchestrator = options.orchestrator;
    this.packages = options.packages ?? getPackageArtifactStore();
    this.state = options.state ?? getAuthoringState();
    this.adapter = options.adapter ?? new PiAuthoringAdapter(this.state);
    this.attempts = options.attempts ?? new ControlledRepairAttemptStore();
    this.validator = options.validator ?? validateGeneratedKnowledge;
    this.checkReader = options.checkReader ?? getCheck;
    this.maxAutomaticAttempts = Math.max(
      1,
      Math.min(10, Math.trunc(options.maxAutomaticAttempts ?? DEFAULT_AUTOMATIC_REPAIR_LIMIT)),
    );
  }

  async run(project: Project, context: KnowledgeGenerationContext): Promise<ControlledRepairResult> {
    let current = await this.requireCurrent(project.id);
    let packageFiles = await this.packages.readPackageFiles(project.id, current.revision.oid);
    let validation = await this.currentValidation(project, current.packageVersion, packageFiles, context);
    const rootRevision = this.attempts.rootRevision(project.id, current.revision.oid);
    const controlledPending = this.state.snapshot(project.id).pendingInteractions.filter((interaction) => (
      interaction.payload.purpose === 'controlled_repair'
      || interaction.payload.purpose === 'controlled_repair_expert'
    ));
    const stalePending = controlledPending.filter((interaction) => (
      interaction.baseRevision !== current.revision.oid
    ));
    for (const interaction of stalePending) {
      const attemptId = String(interaction.payload.repairAttemptId ?? '').trim();
      if (attemptId) {
        try {
          this.attempts.addAction(attemptId, {
            type: 'superseded_by_revision',
            revision: current.revision.oid,
          });
          this.attempts.stop(attemptId, '知识包内容已经变化，旧修复待办失效');
        } catch { /* 旧审计已经结束时只关闭待办 */ }
      }
      try {
        this.adapter.complete(project.id, interaction.runId, {
          purpose: 'controlled_repair',
          superseded: true,
          revision: current.revision.oid,
        });
      } catch { /* 已结束运行无需重复处理 */ }
    }
    if (stalePending.length > 0) {
      try {
        this.orchestrator.supersedeKnowledgeRepair(project.id, {
          messageKey: repairMessageKey('superseded', current.revision.oid),
          reason: '知识包在等待确认期间产生了新修订，旧方案和专家问题已取消。',
        });
      } catch { /* 没有活动修复任务时继续基于新修订处理 */ }
    }
    const pending = this.state.snapshot(project.id).pendingInteractions.find((interaction) => (
      interaction.baseRevision === current.revision.oid
      && (interaction.payload.purpose === 'controlled_repair'
        || interaction.payload.purpose === 'controlled_repair_expert')
    ));
    if (pending) {
      const authority = pending.payload.purpose === 'controlled_repair_expert'
        ? 'expert_decision' as const
        : 'approval_required' as const;
      const waiting = this.orchestrator.waitForKnowledgeRepair(project.id, {
        messageKey: repairMessageKey('resume_waiting', pending.id),
        authority,
        finding: repairFindingFromPayload(pending.payload.finding),
      });
      return {
        status: authority === 'expert_decision' ? 'waiting_expert' : 'waiting_approval',
        revision: current.revision.oid,
        validation,
        attempts: this.attempts.listChain(project.id, rootRevision),
        interaction: pending,
        ...waiting,
      };
    }
    let latestUpdate: ReturnType<AiConstructionOrchestrator['startKnowledgeRepair']> | undefined;
    let repaired = false;
    // 修复可能跑几十分钟，用户只看得到一个转圈——按轮次上报还剩几个错误
    const repairStartedAt = Date.now();
    const initialErrors = validation.errors;
    let round = 0;

    while (validation.errors > 0) {
      // 检查点：上一轮修复已经落盘并重新校验过，这里停下来知识包是自洽的
      throwIfCancelled(project.id, 'knowledgeRepair');
      const fixedSoFar = Math.max(0, initialErrors - validation.errors);
      reportLongTaskProgress(project.id, {
        taskKinds: ['package_repair', 'evaluation_repair'],
        title: `正在修复知识包校验错误（第 ${round + 1} 轮）`,
        produced: `已修复 ${fixedSoFar} 个，剩余 ${validation.errors} 个`,
        done: fixedSoFar,
        total: Math.max(initialErrors, 1),
        elapsedMs: Date.now() - repairStartedAt,
      });
      round += 1;
      const blocking = validation.findings.filter((finding) => finding.severity === '错误');
      const classified = classifyRepairFindings(blocking);
      const automaticPlan = buildAutomaticRepairPlan(current.spec, packageFiles.files, classified);
      const automaticFindings = classified.filter((item) => item.authority === 'automatic');

      if (Object.keys(automaticPlan.files).length > 0) {
        const attemptNumber = this.attempts.automaticAttemptCount(project.id, rootRevision);
        if (attemptNumber >= this.maxAutomaticAttempts) {
          this.orchestrator.startKnowledgeRepair(project.id, {
            inputRevisionOid: current.revision.oid,
            messageKey: repairMessageKey('limit', rootRevision),
          });
          const attempts = this.attempts.listChain(project.id, rootRevision);
          const update = this.orchestrator.stopKnowledgeRepair(project.id, {
            messageKey: repairMessageKey('stopped', rootRevision),
            reason: `自动修复已连续尝试 ${attemptNumber} 次，仍有 ${validation.errors} 个必须修复的错误。`,
            attempts: attempts.flatMap((item) => item.actions.map((action) => JSON.stringify(action))),
          });
          return {
            status: 'stopped',
            revision: current.revision.oid,
            validation,
            attempts,
            ...update,
          };
        }

        latestUpdate = this.orchestrator.startKnowledgeRepair(project.id, {
          inputRevisionOid: current.revision.oid,
          messageKey: repairMessageKey('auto', rootRevision, attemptNumber + 1),
        });
        const actedFindingIds = new Set(automaticPlan.actions.flatMap((action) => action.findingIds));
        const attempt = this.attempts.start({
          projectId: project.id,
          rootRevision,
          inputRevision: current.revision.oid,
          authority: 'automatic',
          findings: automaticFindings
            .filter((item) => actedFindingIds.has(item.finding.id))
            .map((item) => item.finding),
          actions: automaticPlan.actions as unknown as JsonObject[],
        });
        let committedRevision: string | undefined;
        let attemptCompleted = false;
        try {
          const saved = await this.packages.saveFiles(project.id, {
            expectedRevision: current.revision.oid,
            files: Object.fromEntries(Object.entries(automaticPlan.files).map(([path, content]) => [
              `package/${path}`,
              content,
            ])),
            action: {
              cause: 'automatic_repair',
              summary: `自动修复知识包结构（${automaticPlan.actions.length} 个文件）`,
              actorKind: 'system',
              actorName: '知识包修复助手',
            },
          });
          committedRevision = saved.revision.oid;
          current = saved;
          packageFiles = await this.packages.readPackageFiles(project.id, saved.revision.oid);
          validation = await this.validator(project, {
            spec: current.spec,
            coverage: current.coverage,
            files: packageFiles.files,
            version: current.packageVersion,
            revisionId: current.revision.oid,
          }, context);
          this.attempts.complete(
            attempt.id,
            current.revision.oid,
            validation.findings,
            automaticPlan.actions as unknown as JsonObject[],
          );
          attemptCompleted = true;
          latestUpdate = this.orchestrator.completeKnowledgeRepair(project.id, {
            packageRevisionOid: current.revision.oid,
            messageKey: repairMessageKey('auto_completed', attempt.id),
            version: current.packageVersion,
            validation,
            summary: automaticPlan.actions.map((action) => action.summary).join('；'),
          });
          repaired = true;
          continue;
        } catch (error) {
          const reason = error instanceof Error ? error.message : String(error);
          try {
            if (attemptCompleted) {
              this.attempts.addAction(attempt.id, {
                type: 'orchestration_failed',
                reason: reason.slice(0, 3_000),
                at: nowIso(),
              });
            } else {
              this.attempts.fail(attempt.id, reason, committedRevision);
            }
          } catch { /* 保留原始错误 */ }
          try { this.orchestrator.failKnowledgeRepair(project.id, reason); } catch { /* 保留原始错误 */ }
          throw error;
        }
      }

      const validationFailure = blocking.find((finding) => finding.category === '语义校验执行失败');
      if (validationFailure) {
        this.orchestrator.startKnowledgeRepair(project.id, {
          inputRevisionOid: current.revision.oid,
          messageKey: repairMessageKey('validator_failed', rootRevision),
        });
        const attempt = this.attempts.start({
          projectId: project.id,
          rootRevision,
          inputRevision: current.revision.oid,
          authority: 'approval_required',
          findings: [validationFailure],
          actions: [{ type: 'validation_retry_required' }],
        });
        this.attempts.stop(attempt.id, validationFailure.message);
        const attempts = this.attempts.listChain(project.id, rootRevision);
        const update = this.orchestrator.stopKnowledgeRepair(project.id, {
          messageKey: repairMessageKey('validator_stopped', rootRevision),
          reason: `语义校验执行失败，系统没有修改知识包：${validationFailure.message}`,
          attempts: ['重新执行语义校验', '检查任务模型配置和服务连通性'],
          retryable: true,
        });
        return {
          status: 'stopped',
          revision: current.revision.oid,
          validation,
          attempts,
          ...update,
        };
      }

      const human = [
        ...classified.filter((item) => item.authority === 'expert_decision'),
        ...automaticPlan.unresolved.filter((item) => item.authority === 'expert_decision'),
        ...classified.filter((item) => item.authority === 'approval_required'),
        ...automaticPlan.unresolved.filter((item) => item.authority === 'approval_required'),
      ][0];
      if (!human) throw new Error('知识包仍有必须修复的错误，但没有可执行的修复路径');

      const target = inferRepairTarget(current.spec, human.finding);
      const authority: Exclude<RepairAuthority, 'automatic'> = human.authority === 'expert_decision'
        || (human.authority === 'approval_required' && !target)
        ? 'expert_decision'
        : 'approval_required';
      latestUpdate = this.orchestrator.startKnowledgeRepair(project.id, {
        inputRevisionOid: current.revision.oid,
        messageKey: repairMessageKey('human', rootRevision, human.finding.id),
      });
      const taskId = latestUpdate.aiConstruction.tasks.find((item) => (
        item.kind === 'package_repair' && item.status === 'running'
      ))?.id;
      if (!taskId) throw new Error('知识包修复任务启动失败');
      const attempt = this.attempts.start({
        projectId: project.id,
        rootRevision,
        inputRevision: current.revision.oid,
        authority,
        status: 'waiting_user',
        findings: [human.finding],
      });
      const run = this.adapter.start(project.id, '逐项修复知识包', current.revision.oid, {
        purpose: 'controlled_repair',
        repairAttemptId: attempt.id,
        taskId,
        authority,
        finding: findingForPayload(human.finding),
      }).run;

      let requested;
      if (authority === 'expert_decision') {
        requested = this.adapter.request(project.id, run.id, 'question_batch', {
          purpose: 'controlled_repair_expert',
          repairAttemptId: attempt.id,
          taskId,
          requiredRole: 'domain_expert',
          finding: findingForPayload(human.finding),
          title: `请明确“${human.finding.category}”的业务决策`,
          description: `${human.finding.message}\n系统不会自行选择业务阈值、流程分支、计算口径或冲突结论。`,
          ...(target ? {} : {
            repairCandidates: repairTargetCandidates(current.spec, human.finding)
              .map((item) => ({ targetKind: item.targetKind, targetKey: item.targetKey })),
          }),
          questions: [
            {
              id: 'expert_decision',
              kind: 'text',
              q: human.finding.suggestion
                ? `请给出明确决策，以便落实：${human.finding.suggestion}`
                : '请给出应采用的业务规则、口径或分支结论。',
              required: true,
            },
            // 冲突类问题涉及多个资源，系统不能替业务挑；早先只在文案里要求
            // 「回答中明确资源 key」，但界面根本没有填 key 的地方，必然走不下去。
            ...(target ? [] : (() => {
              const candidates = repairTargetCandidates(current.spec, human.finding);
              return candidates.length >= 2 ? [{
                id: 'repair_target',
                kind: 'single' as const,
                q: '这个问题涉及多个知识资源，请选择本次要修改哪一个：',
                required: true,
                options: candidates.map((item) => ({
                  value: `${item.targetKind}:${item.targetKey}`,
                  label: item.targetKey,
                  description: `${item.targetKind}`,
                })),
              }] : [];
            })()),
          ],
        }, current.revision.oid);
      } else {
        const plan = buildSemanticRepairPlan(current.spec, human.finding);
        requested = this.adapter.request(project.id, run.id, 'change_plan', {
          purpose: 'controlled_repair',
          repairAttemptId: attempt.id,
          taskId,
          finding: findingForPayload(human.finding),
          title: '确认知识语义修复方案',
          summary: plan.summary,
          changes: plan.changes,
          risks: plan.risks,
          message: plan.message,
          history: [],
        }, current.revision.oid);
      }
      this.attempts.addAction(attempt.id, {
        type: 'interaction_opened',
        interactionId: requested.interaction.id,
        interactionType: requested.interaction.type,
      });
      const waiting = this.orchestrator.waitForKnowledgeRepair(project.id, {
        messageKey: repairMessageKey('waiting', attempt.id),
        authority,
        finding: human.finding,
      });
      return {
        status: authority === 'expert_decision' ? 'waiting_expert' : 'waiting_approval',
        revision: current.revision.oid,
        validation,
        attempts: this.attempts.listChain(project.id, rootRevision),
        interaction: requested.interaction,
        ...waiting,
      };
    }

    if (!latestUpdate) {
      this.orchestrator.startKnowledgeRepair(project.id, {
        inputRevisionOid: current.revision.oid,
        messageKey: repairMessageKey('revalidate', rootRevision),
      });
      latestUpdate = this.orchestrator.completeKnowledgeRepair(project.id, {
        packageRevisionOid: current.revision.oid,
        messageKey: repairMessageKey('valid', rootRevision),
        version: current.packageVersion,
        validation,
        summary: '重新校验后未发现必须修复的错误',
      });
    }
    return {
      status: repaired ? 'automatic_repaired' : 'valid',
      revision: current.revision.oid,
      validation,
      attempts: this.attempts.listChain(project.id, rootRevision),
      ...latestUpdate,
    };
  }

  async answerExpertDecision(
    project: Project,
    pending: AuthoringInteraction,
    requestId: string,
    response: JsonObject,
    actor: { username: string; role: 'domain_expert' | 'project_owner' } = {
      username: project.owner,
      role: 'project_owner',
    },
  ) {
    const decision = parseExpertRepairDecision(response);
    const attemptId = String(pending.payload.repairAttemptId ?? '').trim();
    const finding = repairFindingFromPayload(pending.payload.finding);
    const current = await this.requireCurrent(project.id);
    if (pending.baseRevision && pending.baseRevision !== current.revision.oid) {
      throw new Error('知识包内容已经变化，请基于最新校验结果重新处理');
    }
    const candidates = repairTargetCandidates(current.spec, finding);
    const targetAnswer = Array.isArray(response.answers)
      ? response.answers.find((item) => item && typeof item === 'object' && !Array.isArray(item)
        && (item as Record<string, unknown>).questionId === 'repair_target')
      : undefined;
    const explicitTarget = parseExpertRepairTarget(response);
    if (targetAnswer && (!explicitTarget || !candidates.some((candidate) => (
      candidate.targetKind === explicitTarget.targetKind
        && candidate.targetKey === explicitTarget.targetKey
    )))) {
      throw new RepairTargetUnresolvedError(finding, candidates);
    }
    const plan = buildSemanticRepairPlan(current.spec, finding, decision, explicitTarget);
    const answered = this.adapter.respond(project.id, pending.id, requestId, response);
    const existing = this.state.snapshot(project.id).pendingInteractions.find((item) => (
      item.type === 'change_plan' && item.payload.parentInteractionId === pending.id
    ));
    if (answered.replayed && existing) {
      return { interaction: existing, run: answered.run, replayed: true };
    }
    this.adapter.complete(project.id, answered.run.id, {
      purpose: 'controlled_repair_expert',
      repairAttemptId: attemptId,
      decision,
    });
    const run = this.adapter.start(project.id, '落实专家修复决策', current.revision.oid, {
      purpose: 'controlled_repair',
      repairAttemptId: attemptId,
      taskId: String(pending.payload.taskId ?? ''),
      expertDecision: decision,
      finding: findingForPayload(finding),
    }).run;
    const requested = this.adapter.request(project.id, run.id, 'change_plan', {
      purpose: 'controlled_repair',
      parentInteractionId: pending.id,
      repairAttemptId: attemptId,
      taskId: String(pending.payload.taskId ?? ''),
      expertDecision: decision,
      finding: findingForPayload(finding),
      requiredRole: 'domain_expert',
      title: '确认专家决策的落实方案',
      summary: plan.summary,
      changes: plan.changes,
      risks: plan.risks,
      message: plan.message,
      history: [],
    }, current.revision.oid);
    this.attempts.addAction(attemptId, {
      type: 'expert_decision',
      interactionId: pending.id,
      decision,
      actor: actor.username,
      role: actor.role,
    });
    this.attempts.addAction(attemptId, {
      type: 'interaction_opened',
      interactionId: requested.interaction.id,
      interactionType: 'change_plan',
    });
    return { interaction: requested.interaction, run: requested.run, replayed: false };
  }

  async completeApprovedPlan(
    project: Project,
    context: KnowledgeGenerationContext,
    attemptId: string,
  ) {
    const attempt = this.attempts.get(attemptId);
    const current = await this.requireCurrent(project.id);
    const packageFiles = await this.packages.readPackageFiles(project.id, current.revision.oid);
    const validation = await this.validator(project, {
      spec: current.spec,
      coverage: current.coverage,
      files: packageFiles.files,
      version: current.packageVersion,
      revisionId: current.revision.oid,
    }, context);
    this.attempts.complete(attempt.id, current.revision.oid, validation.findings, [
      ...attempt.actions,
      { type: 'plan_approved', outputRevision: current.revision.oid },
    ]);
    const update = this.orchestrator.completeKnowledgeRepair(project.id, {
      packageRevisionOid: current.revision.oid,
      messageKey: repairMessageKey('plan_completed', attempt.id),
      version: current.packageVersion,
      validation,
      summary: '已按确认方案修改知识包并重新校验',
    });
    return { current, validation, ...update };
  }

  cancelPlan(projectId: string, attemptId: string, reason: string) {
    this.attempts.stop(attemptId, reason);
    return this.orchestrator.stopKnowledgeRepair(projectId, {
      messageKey: repairMessageKey('plan_cancelled', attemptId),
      reason,
      attempts: this.attempts.get(attemptId).actions.map((action) => JSON.stringify(action)),
    });
  }

  failPlan(projectId: string, attemptId: string, reason: string) {
    try {
      const attempt = this.attempts.recordRetryableFailure(attemptId, reason);
      const finding = attempt.findingsBefore[0];
      return this.orchestrator.waitForKnowledgeRepair(projectId, {
        messageKey: repairMessageKey('plan_retry', attemptId),
        authority: attempt.authority === 'expert_decision' ? 'expert_decision' : 'approval_required',
        finding: finding ?? {
          category: '方案应用失败',
          message: reason,
        },
      });
    } catch {
      try { return this.orchestrator.failKnowledgeRepair(projectId, reason); } catch { return undefined; }
    }
  }

  failAppliedPlan(
    projectId: string,
    attemptId: string,
    reason: string,
    outputRevision: string,
  ) {
    try {
      const attempt = this.attempts.get(attemptId);
      if (attempt.status === 'waiting_user' || attempt.status === 'running') {
        this.attempts.fail(attemptId, reason, outputRevision);
      } else {
        this.attempts.addAction(attemptId, {
          type: 'post_apply_failure',
          reason: reason.slice(0, 3_000),
          outputRevision,
        });
      }
    } catch { /* 编排状态仍需收敛 */ }
    try { return this.orchestrator.failKnowledgeRepair(projectId, reason); } catch { return undefined; }
  }

  private async currentValidation(
    project: Project,
    version: string,
    packageFiles: Awaited<ReturnType<PackageArtifactStore['readPackageFiles']>>,
    context: KnowledgeGenerationContext,
  ): Promise<KnowledgeStructureValidation> {
    const existing = this.checkReader(project.id);
    if (existing?.revision === packageFiles.revision.oid) {
      return { ...findingCounts(existing.findings), findings: existing.findings.map(cloneFinding) };
    }
    return this.validator(project, {
      spec: packageFiles.spec,
      coverage: packageFiles.coverage,
      files: packageFiles.files,
      version,
      revisionId: packageFiles.revision.oid,
    }, context);
  }

  private async requireCurrent(projectId: string) {
    const current = await this.packages.current(projectId);
    if (!current) throw new Error('当前项目没有知识包草稿');
    return current;
  }
}

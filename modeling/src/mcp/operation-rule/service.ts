import type { DatabaseSync } from 'node:sqlite';

import { getPackageArtifactStore } from '../../artifacts/runtime.js';
import { PackageArtifactStoreError } from '../../artifacts/package-store.js';
import { ArtifactRepositoryError } from '../../artifacts/repository.js';
import { getDb } from '../../db/index.js';
import { listDocuments } from '../../documents/repo.js';
import type { CoverageResult, PackageSpec, Project, SpecOperation, SpecRule } from '../../types.js';
import { validateKey } from '../../workflow/keys.js';
import { introducedSpecIntegrityProblems, listSpecIntegrityProblems } from '../../workflow/ontology-model-validation.js';
import { runFormatChecks, runRuleChecks, type CheckFinding } from '../../workflow/package-check.js';
import { getActivePackageDraft } from '../../workflow/package-draft.js';
import { parseRenderedPackageFiles } from '../../workflow/package-import.js';
import { renderPackage } from '../../workflow/package-render.js';
import { coverageFor } from '../../workflow/spec-validation.js';
import {
  ModelingMcpToolError,
  type ModelingMcpToolDetails,
  type ModelingMcpToolIssue,
  type ModelingMcpToolIssueLevel,
  type ModelingMcpToolResourceRef,
} from '../tool-error.js';

type ResourceType = 'operation' | 'rule';
type MutationAction = 'create' | 'update' | 'delete';
type ImpactLevel = ModelingMcpToolIssueLevel;
interface MutationCommand {
  resourceType: ResourceType;
  action: MutationAction;
  key: string;
  expectedRevision: string;
  payload?: Record<string, unknown>;
}

type Impact = ModelingMcpToolIssue;

interface AnalysisResult {
  ok: boolean;
  revision: string;
  resource: ModelingMcpToolResourceRef;
  changed_paths: string[];
  affected_resources: Array<{ type: string; key: string; relation?: string }>;
  historical_problem_count: number;
  field_errors: Record<string, string>;
  impacts: Impact[];
  nextSpec?: PackageSpec;
  coverage?: CoverageResult;
  resourceValue?: SpecOperation | SpecRule;
}

const MAX_LIST_LIMIT = 200;
const DEFAULT_LIST_LIMIT = 50;
const MAX_TEXT_LENGTH = 50_000;
const MAX_LIST_ITEMS = 500;
const OPERATION_READ_ONLY_FIELDS = new Set([
  'key', 'script', 'competencyQuestionId', 'competencyQuestion', 'competency_question_id',
  'competency_question', 'evidence',
]);
const RULE_READ_ONLY_FIELDS = new Set(['key', 'evidence']);

export class OperationRuleMcpService {
  constructor(
    private readonly db: DatabaseSync = getDb(),
    private readonly artifacts = getPackageArtifactStore(),
  ) {}

  async execute(
    toolName: string,
    project: Project,
    username: string,
    args: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    try {
      if (toolName === 'list_ontology_operations') return this.list(project, args, 'operation');
      if (toolName === 'get_ontology_operation') return this.get(project, args, 'operation');
      if (toolName === 'list_ontology_rules') return this.list(project, args, 'rule');
      if (toolName === 'get_ontology_rule') return this.get(project, args, 'rule');
      if (toolName === 'validate_ontology_operation_rule_change') {
        return this.publicAnalysis(await this.analyze(project, commandFromAnalyze(args)));
      }
      const command = commandFromWriteTool(toolName, args);
      if (command) return this.submit(project, username, command);
      throw new ModelingMcpToolError(
        'MCP_TOOL_NOT_FOUND',
        `未知 Operation/Rule 工具：${toolName}`,
        404,
        [],
        { phase: 'input', reason_code: 'MCP_TOOL_NOT_FOUND', retryable: false },
      );
    } catch (error) {
      throw mapArtifactError(error, { phase: 'load' });
    }
  }

  private async list(project: Project, args: Record<string, unknown>, type: ResourceType) {
    const requestedRevision = optionalText(args.revision);
    const current = await this.artifacts.readPackageFiles(project.id, requestedRevision || undefined);
    const query = optionalText(args.query).toLocaleLowerCase('zh-CN');
    const limit = integer(args.limit, DEFAULT_LIST_LIMIT, 1, MAX_LIST_LIMIT);
    const offset = decodeCursor(args.cursor, current.revision.oid, type);
    const resources = type === 'operation' ? current.spec.operations : current.spec.rules ?? [];
    const items = resources
      .filter((item) => !query || resourceSearchText(item, type).includes(query))
      .sort((left, right) => compareText(left.key, right.key));
    const page = items.slice(offset, offset + limit);
    const nextOffset = offset + page.length;
    return {
      revision: current.revision.oid,
      items: page.map((item) => type === 'operation'
        ? operationSummary(item as SpecOperation)
        : ruleSummary(item as SpecRule)),
      total: items.length,
      next_cursor: nextOffset < items.length ? encodeCursor(current.revision.oid, type, nextOffset) : null,
      display: { text: `已返回 ${page.length}/${items.length} 个${type === 'operation' ? '操作' : '规则'}` },
    };
  }

  private async get(project: Project, args: Record<string, unknown>, type: ResourceType) {
    const requestedRevision = optionalText(args.revision);
    const current = await this.artifacts.readPackageFiles(project.id, requestedRevision || undefined);
    const key = resourceKey(type, args[type === 'operation' ? 'operation_id' : 'rule_id'] ?? args.key);
    const item = type === 'operation'
      ? current.spec.operations.find((candidate) => candidate.key === key)
      : (current.spec.rules ?? []).find((candidate) => candidate.key === key);
    if (!item) throw resourceNotFound(type, key);
    return {
      revision: current.revision.oid,
      [`${type}_id`]: `${type}:${key}`,
      [type]: type === 'operation' ? publicOperation(item as SpecOperation) : publicRule(item as SpecRule),
      display: { text: `已返回${type === 'operation' ? '操作' : '规则'}《${item.title}》` },
    };
  }

  private async submit(project: Project, username: string, command: MutationCommand) {
    const analysis = await this.analyze(project, command);
    if (!analysis.ok || !analysis.nextSpec || !analysis.coverage || !analysis.resourceValue) {
      throw new ModelingMcpToolError(
        'SUBMISSION_BLOCKED',
        '修改未通过确定性检查',
        422,
        ['根据 issues 和 field_errors 修正后重新提交'],
        {
          phase: 'validate',
          resource: analysis.resource,
          reason_code: 'SUBMISSION_BLOCKED',
          field_errors: analysis.field_errors,
          issues: analysis.impacts,
          changed_paths: analysis.changed_paths,
          affected_resources: analysis.affected_resources,
          historical_problem_count: analysis.historical_problem_count,
          retryable: false,
        } satisfies ModelingMcpToolDetails,
      );
    }
    try {
      const saved = await this.artifacts.savePackageVersion(project.id, {
        expectedRevision: command.expectedRevision,
        spec: analysis.nextSpec,
        coverage: analysis.coverage,
        note: actionSummary(command),
        action: {
          cause: `modeling_mcp_${command.resourceType}_${command.action}`,
          summary: actionSummary(command),
          actorKind: 'user',
          actorName: username,
        },
      });
      return {
        resource: analysis.resource,
        [command.resourceType]: command.resourceType === 'operation'
          ? publicOperation(analysis.resourceValue as SpecOperation)
          : publicRule(analysis.resourceValue as SpecRule),
        previous_revision: command.expectedRevision,
        revision: saved.revision.oid,
        changed_paths: analysis.changed_paths,
        affected_resources: analysis.affected_resources,
        warnings: analysis.impacts.filter((impact) => impact.level !== 'blocking'),
        historical_problem_count: analysis.historical_problem_count,
        display: { text: `${actionSummary(command)}，已生成正式修订` },
      };
    } catch (error) {
      throw mapArtifactError(error, { phase: 'persist', resource: analysis.resource });
    }
  }

  private async analyze(project: Project, command: MutationCommand): Promise<AnalysisResult> {
    const current = await this.artifacts.readPackageFiles(project.id);
    const resource = resourceRef(command);
    if (current.revision.oid !== command.expectedRevision) {
      throw new ModelingMcpToolError(
        'REVISION_CONFLICT',
        '知识包内容已更新，请刷新后重试',
        409,
        ['重新读取资源和 revision 后再次分析'],
        {
          phase: 'load',
          resource,
          reason_code: 'REVISION_CONFLICT',
          expected_revision: command.expectedRevision,
          actual_revision: current.revision.oid,
          retryable: true,
        } satisfies ModelingMcpToolDetails,
      );
    }
    const impacts: Impact[] = [];
    const fieldErrors: Record<string, string> = {};
    if (!['draft', 'review'].includes(current.spec.status ?? 'draft')) {
      impacts.push(impact(
        'PACKAGE_NOT_EDITABLE',
        'blocking',
        'package.status',
        `当前知识包状态 ${current.spec.status} 不允许直接编辑`,
        '在 Modeling 中创建可编辑草稿或切换到 review/draft 状态后重试',
      ));
    }
    const editingIssue = await this.editingStateImpact(project.id);
    if (editingIssue) impacts.push(editingIssue);

    let nextSpec: PackageSpec | undefined;
    let resourceValue: SpecOperation | SpecRule | undefined;
    try {
      const proposal = buildProposal(current.spec, command);
      nextSpec = proposal.spec;
      resourceValue = proposal.resource;
      impacts.push(...proposal.impacts);
    } catch (error) {
      const currentImpact = normalizeInputError(command, error);
      impacts.push(currentImpact);
      fieldErrors[currentImpact.target] = currentImpact.message;
    }

    let coverage: CoverageResult | undefined;
    let changedPaths: string[] = [];
    if (nextSpec) {
      const baselineProblems = listSpecIntegrityProblems(current.spec);
      for (const problem of introducedSpecIntegrityProblems(nextSpec, baselineProblems)) {
        impacts.push(impact(
          'PACKAGE_INTEGRITY_FAILED',
          'blocking',
          resourceTarget(command),
          problem,
          suggestionForIntegrityProblem(problem),
        ));
      }
      try {
        const beforeFiles = renderPackage(current.spec);
        const afterFiles = renderPackage(nextSpec);
        parseRenderedPackageFiles(afterFiles);
        const selectedQuestions = coverageQuestions(current.coverage);
        coverage = coverageFor(nextSpec, selectedQuestions);
        changedPaths = changedRenderedPaths(beforeFiles, afterFiles, current.coverage, coverage);
        impacts.push(...this.findingImpacts(project.id, current.spec, beforeFiles, nextSpec, afterFiles, selectedQuestions));
      } catch (error) {
        impacts.push(impact(
          'PACKAGE_RENDER_FAILED',
          'blocking',
          resourceTarget(command),
          errorMessage(error),
          '按字段约束修正提交内容，确保知识包可以规范渲染和解析',
        ));
      }
    }

    const normalizedImpacts = dedupeImpacts(impacts);
    for (const currentImpact of normalizedImpacts) {
      if (currentImpact.level !== 'blocking' || fieldErrors[currentImpact.target]) continue;
      fieldErrors[currentImpact.target] = currentImpact.message;
    }
    const affectedResources = affectedResourcesFor(current.spec, command);
    return {
      ok: !normalizedImpacts.some((currentImpact) => currentImpact.level === 'blocking'),
      revision: current.revision.oid,
      resource,
      changed_paths: changedPaths,
      affected_resources: affectedResources,
      historical_problem_count: Math.max(0, listSpecIntegrityProblems(current.spec).length),
      field_errors: fieldErrors,
      impacts: normalizedImpacts,
      nextSpec,
      coverage,
      resourceValue,
    };
  }

  private async editingStateImpact(projectId: string): Promise<Impact | null> {
    if (await getActivePackageDraft(projectId)) {
      return impact(
        'PROJECT_EDITING_BUSY', 'blocking', 'package_draft',
        '项目存在未发布的人工编辑草稿', '先在 Modeling 页面发布或放弃草稿后重试',
      );
    }
    const candidateChanges = Number((this.db.prepare(`SELECT COUNT(*) AS count
      FROM agent_candidate_changes WHERE project_id = ?`).get(projectId) as { count?: number } | undefined)?.count ?? 0);
    const pendingCandidates = Number((this.db.prepare(`SELECT COUNT(*) AS count
      FROM artifact_candidate_revisions WHERE project_id = ? AND status = 'pending'`)
      .get(projectId) as { count?: number } | undefined)?.count ?? 0);
    if (candidateChanges || pendingCandidates) {
      return impact(
        'PROJECT_EDITING_BUSY', 'blocking', 'candidate',
        '项目存在未完成的 candidate/review 修改', '先完成或退回现有候选，再基于新的正式 revision 重试',
      );
    }
    return null;
  }

  private findingImpacts(
    projectId: string,
    baselineSpec: PackageSpec,
    baselineFiles: Record<string, string>,
    proposedSpec: PackageSpec,
    proposedFiles: Record<string, string>,
    selectedQuestions: string[],
  ): Impact[] {
    const tables = listDocuments(projectId).flatMap((document) => document.tables ?? []);
    const baseline = [
      ...runRuleChecks(baselineSpec, tables, selectedQuestions),
      ...runFormatChecks(baselineSpec, baselineFiles),
    ];
    const existing = new Set(baseline.map(findingIdentity));
    return [
      ...runRuleChecks(proposedSpec, tables, selectedQuestions),
      ...runFormatChecks(proposedSpec, proposedFiles),
    ]
      .filter((finding) => !existing.has(findingIdentity(finding)))
      .map((finding) => impact(
        finding.code || 'PACKAGE_CHECK_FAILED',
        finding.severity === '错误' ? 'blocking' : finding.severity === '警告' ? 'warning' : 'info',
        finding.path || finding.target,
        finding.userMessage || finding.message,
        finding.userSuggestion || finding.suggestion || '根据提示检查并修正相关资源',
      ));
  }

  private publicAnalysis(analysis: AnalysisResult): Record<string, unknown> {
    return {
      ok: analysis.ok,
      revision: analysis.revision,
      resource: analysis.resource,
      changed_paths: analysis.changed_paths,
      affected_resources: analysis.affected_resources,
      historical_problem_count: analysis.historical_problem_count,
      field_errors: analysis.field_errors,
      issues: analysis.impacts,
      display: { text: analysis.ok ? '修改通过确定性检查，可以提交' : '修改存在阻塞项，请修正后重试' },
    };
  }
}

function resourceSearchText(item: SpecOperation | SpecRule, type: ResourceType): string {
  const content = type === 'rule' ? (item as SpecRule).content : '';
  return [item.key, item.title, item.description ?? '', content].join('\n').toLocaleLowerCase('zh-CN');
}

function buildProposal(spec: PackageSpec, command: MutationCommand): {
  spec: PackageSpec;
  resource: SpecOperation | SpecRule;
  impacts: Impact[];
} {
  const next = structuredClone(spec);
  if (command.resourceType === 'operation') {
    const index = next.operations.findIndex((item) => item.key === command.key);
    if (command.action === 'create') {
      if (index >= 0) throw new ModelingMcpToolError('RESOURCE_ALREADY_EXISTS', 'Operation key 已存在', 409);
      const resource = normalizeOperation(command.key, command.payload ?? {}, undefined);
      next.operations.push(resource);
      return { spec: next, resource, impacts: [] };
    }
    if (index < 0) throw resourceNotFound('operation', command.key);
    const existing = next.operations[index];
    if (command.action === 'update') {
      const resource = normalizeOperation(command.key, command.payload ?? {}, existing);
      next.operations[index] = resource;
      return { spec: next, resource, impacts: [] };
    }
    const blockers = operationDeleteImpacts(next, command.key);
    next.operations.splice(index, 1);
    return { spec: next, resource: existing, impacts: blockers };
  }

  const rules = next.rules ?? (next.rules = []);
  const index = rules.findIndex((item) => item.key === command.key);
  if (command.action === 'create') {
    if (index >= 0) throw new ModelingMcpToolError('RESOURCE_ALREADY_EXISTS', 'Rule key 已存在', 409);
    const resource = normalizeRule(command.key, command.payload ?? {}, undefined);
    rules.push(resource);
    return { spec: next, resource, impacts: [] };
  }
  if (index < 0) throw resourceNotFound('rule', command.key);
  const existing = rules[index];
  if (command.action === 'update') {
    const resource = normalizeRule(command.key, command.payload ?? {}, existing);
    rules[index] = resource;
    return { spec: next, resource, impacts: [] };
  }
  const blockers = ruleDeleteImpacts(next, command.key);
  rules.splice(index, 1);
  return { spec: next, resource: existing, impacts: blockers };
}

function normalizeOperation(keyValue: string, payload: Record<string, unknown>, existing?: SpecOperation): SpecOperation {
  rejectUnknown(payload, [
    'title', 'description', 'inputs', 'outputs', 'flow', 'related_objects', 'uses_rules', 'uses_metrics', 'notes',
    ...OPERATION_READ_ONLY_FIELDS,
  ]);
  rejectReadOnly(payload, OPERATION_READ_ONLY_FIELDS, Boolean(existing), 'operation');
  const key = validateKey(keyValue, '操作');
  const title = patchText(payload, 'title', existing?.title, true);
  const description = patchText(payload, 'description', existing?.description, true);
  const inputs = Object.hasOwn(payload, 'inputs')
    ? normalizeInputs(payload.inputs)
    : existing?.inputs ?? requiredArray('inputs');
  const outputs = Object.hasOwn(payload, 'outputs')
    ? normalizeOutputs(payload.outputs)
    : existing?.outputs ?? requiredArray('outputs');
  const flow = Object.hasOwn(payload, 'flow')
    ? stringArray(payload.flow, 'flow', false)
    : existing?.flow ?? requiredArray('flow');
  const relatedObjects = Object.hasOwn(payload, 'related_objects')
    ? stringArray(payload.related_objects, 'related_objects', true)
    : existing?.relatedObjects ?? requiredArray('related_objects');
  const usesRules = Object.hasOwn(payload, 'uses_rules')
    ? stringArray(payload.uses_rules, 'uses_rules', true)
    : existing?.usesRules;
  const usesMetrics = Object.hasOwn(payload, 'uses_metrics')
    ? stringArray(payload.uses_metrics, 'uses_metrics', true)
    : existing?.usesMetrics;
  const notes = Object.hasOwn(payload, 'notes')
    ? stringArray(payload.notes, 'notes', true)
    : existing?.notes;
  return {
    key, title, description, inputs, outputs, flow, relatedObjects,
    ...(usesRules?.length ? { usesRules } : {}),
    ...(usesMetrics?.length ? { usesMetrics } : {}),
    ...(notes?.length ? { notes } : {}),
    ...(existing?.script ? { script: structuredClone(existing.script) } : {}),
    ...(existing?.competencyQuestionId ? { competencyQuestionId: existing.competencyQuestionId } : {}),
    ...(existing?.competencyQuestion ? { competencyQuestion: existing.competencyQuestion } : {}),
    ...(existing?.evidence ? { evidence: [...existing.evidence] } : {}),
  };
}

function normalizeRule(keyValue: string, payload: Record<string, unknown>, existing?: SpecRule): SpecRule {
  rejectUnknown(payload, [
    'title', 'description', 'applies_to_objects', 'applies_to_operations', 'content', 'conditions', 'exceptions',
    ...RULE_READ_ONLY_FIELDS,
  ]);
  rejectReadOnly(payload, RULE_READ_ONLY_FIELDS, Boolean(existing), 'rule');
  const key = validateKey(keyValue, '规则');
  const title = patchText(payload, 'title', existing?.title, true);
  const description = patchText(payload, 'description', existing?.description, false);
  const content = patchText(payload, 'content', existing?.content, true);
  const appliesToObjects = Object.hasOwn(payload, 'applies_to_objects')
    ? stringArray(payload.applies_to_objects, 'applies_to_objects', true)
    : existing?.appliesToObjects;
  const appliesToOperations = Object.hasOwn(payload, 'applies_to_operations')
    ? stringArray(payload.applies_to_operations, 'applies_to_operations', true)
    : existing?.appliesToOperations;
  const conditions = Object.hasOwn(payload, 'conditions')
    ? normalizeConditions(payload.conditions)
    : existing?.conditions;
  const exceptions = Object.hasOwn(payload, 'exceptions')
    ? stringArray(payload.exceptions, 'exceptions', true)
    : existing?.exceptions;
  return {
    key, title, ...(description ? { description } : {}), content,
    ...(appliesToObjects?.length ? { appliesToObjects } : {}),
    ...(appliesToOperations?.length ? { appliesToOperations } : {}),
    ...(conditions?.length ? { conditions } : {}),
    ...(exceptions?.length ? { exceptions } : {}),
    ...(existing?.evidence ? { evidence: [...existing.evidence] } : {}),
  };
}

function commandFromAnalyze(args: Record<string, unknown>): MutationCommand {
  const resourceType = args.resource_type === 'operation' ? 'operation' : args.resource_type === 'rule' ? 'rule' : null;
  const action = ['create', 'update', 'delete'].includes(String(args.action)) ? args.action as MutationAction : null;
  if (!resourceType) throw new ModelingMcpToolError('INVALID_INPUT', 'resource_type 必须为 operation 或 rule', 400);
  if (!action) throw new ModelingMcpToolError('INVALID_INPUT', 'action 必须为 create、update 或 delete', 400);
  const rawKey = args.key ?? args.resource_id;
  return {
    resourceType,
    action,
    key: resourceKey(resourceType, rawKey),
    expectedRevision: requiredText(args.expected_revision, 'expected_revision'),
    ...(action !== 'delete' ? { payload: record(args.payload, 'payload') } : {}),
  };
}

function commandFromWriteTool(toolName: string, args: Record<string, unknown>): MutationCommand | null {
  const match = toolName.match(/^(create|update|delete)_ontology_(operation|rule)$/u);
  if (!match) return null;
  const action = match[1] as MutationAction;
  const resourceType = match[2] as ResourceType;
  const idField = resourceType === 'operation' ? 'operation_id' : 'rule_id';
  const rawKey = action === 'create' ? args.key : args[idField] ?? args.key;
  return {
    resourceType,
    action,
    key: resourceKey(resourceType, rawKey),
    expectedRevision: requiredText(args.expected_revision, 'expected_revision'),
    ...(action === 'create' ? { payload: withoutEnvelope(args, ['expected_revision', 'key']) } : {}),
    ...(action === 'update' ? { payload: record(args.changes, 'changes') } : {}),
  };
}

function operationDeleteImpacts(spec: PackageSpec, key: string): Impact[] {
  const impacts: Impact[] = [];
  for (const rule of spec.rules ?? []) {
    if (!(rule.appliesToOperations ?? []).includes(key)) continue;
    impacts.push(impact(
      'OPERATION_DELETE_BLOCKED_BY_RULE', 'blocking', `rule:${rule.key}.applies_to_operations`,
      `Rule ${rule.key} 仍引用 Operation ${key}`,
      `先更新 Rule ${rule.key}，移除或替换该 Operation 引用`,
    ));
  }
  for (const script of spec.scripts ?? []) {
    if (script.boundOperation !== key) continue;
    impacts.push(impact(
      'OPERATION_DELETE_BLOCKED_BY_SCRIPT', 'blocking', `script:${script.key}.bound_operation`,
      `Script ${script.key} 仍绑定 Operation ${key}`,
      `先通过现有 Modeling 脚本入口解除 Script ${script.key} 的绑定`,
    ));
  }
  return impacts;
}

function ruleDeleteImpacts(spec: PackageSpec, key: string): Impact[] {
  return spec.operations
    .filter((operation) => (operation.usesRules ?? []).includes(key))
    .map((operation) => impact(
      'RULE_DELETE_BLOCKED_BY_OPERATION', 'blocking', `operation:${operation.key}.uses_rules`,
      `Operation ${operation.key} 仍引用 Rule ${key}`,
      `先更新 Operation ${operation.key}，移除或替换该 Rule 引用`,
    ));
}

function affectedResourcesFor(spec: PackageSpec, command: MutationCommand) {
  const result = new Map<string, { type: string; key: string; relation?: string }>();
  const add = (type: string, key: string, relation?: string) => result.set(`${type}:${key}:${relation ?? ''}`, { type, key, ...(relation ? { relation } : {}) });
  add(command.resourceType, command.key);
  if (command.resourceType === 'rule') {
    for (const operation of spec.operations) if ((operation.usesRules ?? []).includes(command.key)) add('operation', operation.key, 'uses_rule');
  } else {
    for (const rule of spec.rules ?? []) if ((rule.appliesToOperations ?? []).includes(command.key)) add('rule', rule.key, 'applies_to_operation');
    for (const script of spec.scripts ?? []) if (script.boundOperation === command.key) add('script', script.key, 'bound_operation');
  }
  return [...result.values()].sort((left, right) => compareText(`${left.type}:${left.key}`, `${right.type}:${right.key}`));
}

function changedRenderedPaths(
  before: Record<string, string>,
  after: Record<string, string>,
  beforeCoverage: CoverageResult,
  afterCoverage: CoverageResult,
): string[] {
  const paths = [...new Set([...Object.keys(before), ...Object.keys(after)])]
    .filter((path) => before[path] !== after[path]);
  paths.push('model/package-spec.json');
  if (JSON.stringify(beforeCoverage) !== JSON.stringify(afterCoverage)) paths.push('model/coverage.json');
  return [...new Set(paths)].sort(compareText);
}

function publicOperation(operation: SpecOperation) {
  return {
    key: operation.key,
    title: operation.title,
    description: operation.description,
    inputs: operation.inputs.map((item) => ({
      param: item.param, cn_name: item.cnName, type: item.type, required: item.required, note: item.note,
    })),
    outputs: operation.outputs.map((item) => ({
      field: item.field, cn_name: item.cnName, type: item.type, note: item.note,
    })),
    flow: [...operation.flow],
    related_objects: [...operation.relatedObjects],
    uses_rules: [...(operation.usesRules ?? [])],
    uses_metrics: [...(operation.usesMetrics ?? [])],
    notes: [...(operation.notes ?? [])],
    script: operation.script ? structuredClone(operation.script) : null,
    evidence: [...(operation.evidence ?? [])],
  };
}

function publicRule(rule: SpecRule) {
  return {
    key: rule.key,
    title: rule.title,
    description: rule.description ?? '',
    applies_to_objects: [...(rule.appliesToObjects ?? [])],
    applies_to_operations: [...(rule.appliesToOperations ?? [])],
    content: rule.content,
    conditions: structuredClone(rule.conditions ?? []),
    exceptions: [...(rule.exceptions ?? [])],
    evidence: [...(rule.evidence ?? [])],
  };
}

function operationSummary(operation: SpecOperation) {
  return { operation_id: `operation:${operation.key}`, key: operation.key, title: operation.title, description: operation.description };
}

function ruleSummary(rule: SpecRule) {
  return { rule_id: `rule:${rule.key}`, key: rule.key, title: rule.title, description: rule.description ?? '' };
}

function normalizeInputs(value: unknown): SpecOperation['inputs'] {
  const rows = recordArray(value, 'inputs');
  const result = rows.map((row, index) => ({
    param: requiredText(row.param, `inputs[${index}].param`),
    ...(optionalText(row.cn_name ?? row.cnName) ? { cnName: optionalText(row.cn_name ?? row.cnName) } : {}),
    type: requiredText(row.type, `inputs[${index}].type`),
    required: Boolean(row.required),
    ...(optionalText(row.note) ? { note: optionalText(row.note) } : {}),
  }));
  assertUnique(result.map((item) => item.param), 'inputs param');
  return result;
}

function normalizeOutputs(value: unknown): SpecOperation['outputs'] {
  const rows = recordArray(value, 'outputs');
  const result = rows.map((row, index) => ({
    field: requiredText(row.field, `outputs[${index}].field`),
    ...(optionalText(row.cn_name ?? row.cnName) ? { cnName: optionalText(row.cn_name ?? row.cnName) } : {}),
    type: requiredText(row.type, `outputs[${index}].type`),
    ...(optionalText(row.note) ? { note: optionalText(row.note) } : {}),
  }));
  assertUnique(result.map((item) => item.field), 'outputs field');
  return result;
}

function normalizeConditions(value: unknown): NonNullable<SpecRule['conditions']> {
  return recordArray(value, 'conditions').map((row, index) => ({
    condition: requiredText(row.condition, `conditions[${index}].condition`),
    conclusion: requiredText(row.conclusion, `conditions[${index}].conclusion`),
  }));
}

function patchText(payload: Record<string, unknown>, field: string, existing: string | undefined, required: boolean): string {
  if (!Object.hasOwn(payload, field)) {
    if (existing !== undefined) return existing;
    if (required) throw new ModelingMcpToolError('INVALID_INPUT', `${field} 必填`, 400);
    return '';
  }
  const value = optionalText(payload[field]);
  if (required && !value) throw new ModelingMcpToolError('INVALID_INPUT', `${field} 不能为空`, 400);
  if (value.length > MAX_TEXT_LENGTH) throw new ModelingMcpToolError('INVALID_INPUT', `${field} 不能超过 ${MAX_TEXT_LENGTH} 字符`, 400);
  return value;
}

function stringArray(value: unknown, field: string, allowEmpty: boolean): string[] {
  if (!Array.isArray(value)) throw new ModelingMcpToolError('INVALID_INPUT', `${field} 必须是数组`, 400);
  if (value.length > MAX_LIST_ITEMS) throw new ModelingMcpToolError('INVALID_INPUT', `${field} 不能超过 ${MAX_LIST_ITEMS} 项`, 400);
  const result = value.map((item, index) => requiredText(item, `${field}[${index}]`));
  assertUnique(result, field);
  if (!allowEmpty && !result.length) throw new ModelingMcpToolError('INVALID_INPUT', `${field} 至少包含一项`, 400);
  return result;
}

function recordArray(value: unknown, field: string): Record<string, unknown>[] {
  if (!Array.isArray(value)) throw new ModelingMcpToolError('INVALID_INPUT', `${field} 必须是数组`, 400);
  if (value.length > MAX_LIST_ITEMS) throw new ModelingMcpToolError('INVALID_INPUT', `${field} 不能超过 ${MAX_LIST_ITEMS} 项`, 400);
  return value.map((item, index) => record(item, `${field}[${index}]`));
}

function requiredArray(field: string): never {
  throw new ModelingMcpToolError('INVALID_INPUT', `${field} 必填`, 400);
}

function rejectUnknown(payload: Record<string, unknown>, allowed: Iterable<string>): void {
  const known = new Set(allowed);
  const unknown = Object.keys(payload).filter((key) => !known.has(key));
  if (unknown.length) throw new ModelingMcpToolError('INVALID_INPUT', `不支持的字段：${unknown.join('、')}`, 400);
}

function rejectReadOnly(
  payload: Record<string, unknown>,
  fields: Set<string>,
  update: boolean,
  resourceType: ResourceType,
): void {
  const present = Object.keys(payload).filter((key) => fields.has(key));
  if (!present.length) return;
  throw new ModelingMcpToolError(
    'FIELD_READ_ONLY',
    `${present.join('、')} ${update ? '不可修改' : '由服务端维护，不能在创建时指定'}`,
    422,
    ['从 payload 中移除只读字段'],
    {
      phase: 'validate',
      resource: { type: resourceType, id: `${resourceType}:${payload.key ?? ''}`.replace(/:$/u, ''), key: String(payload.key ?? '').trim() },
      reason_code: resourceType === 'operation' ? 'OPERATION_FIELD_READ_ONLY' : 'RULE_FIELD_READ_ONLY',
      fields: present,
      retryable: false,
    } satisfies ModelingMcpToolDetails,
  );
}

function resourceKey(type: ResourceType, value: unknown): string {
  const raw = requiredText(value, `${type}_id/key`);
  const prefix = `${type}:`;
  try {
    return validateKey(raw.startsWith(prefix) ? raw.slice(prefix.length) : raw, type === 'operation' ? '操作' : '规则');
  } catch (error) {
    throw new ModelingMcpToolError(
      'INVALID_INPUT',
      error instanceof Error ? error.message : `${type}_id/key 不正确`,
      400,
      ['使用 list 工具返回且符合知识包 key 规范的资源 ID'],
    );
  }
}

function record(value: unknown, field: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new ModelingMcpToolError('INVALID_INPUT', `${field} 必须是对象`, 400);
  }
  return value as Record<string, unknown>;
}

function requiredText(value: unknown, field: string): string {
  const text = optionalText(value);
  if (!text) throw new ModelingMcpToolError('INVALID_INPUT', `${field} 必填`, 400);
  if (text.length > MAX_TEXT_LENGTH) throw new ModelingMcpToolError('INVALID_INPUT', `${field} 不能超过 ${MAX_TEXT_LENGTH} 字符`, 400);
  return text;
}

function optionalText(value: unknown): string {
  return String(value ?? '').normalize('NFC').trim();
}

function integer(value: unknown, fallback: number, min: number, max: number): number {
  if (value === undefined || value === null || value === '') return fallback;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < min || parsed > max) {
    throw new ModelingMcpToolError('INVALID_INPUT', `数值必须在 ${min}-${max} 之间`, 400);
  }
  return parsed;
}

function withoutEnvelope(args: Record<string, unknown>, fields: string[]): Record<string, unknown> {
  return Object.fromEntries(Object.entries(args).filter(([key]) => !fields.includes(key)));
}

function encodeCursor(revision: string, type: ResourceType, offset: number): string {
  return Buffer.from(JSON.stringify({ revision, type, offset }), 'utf8').toString('base64url');
}

function decodeCursor(value: unknown, revision: string, type: ResourceType): number {
  const cursor = optionalText(value);
  if (!cursor) return 0;
  try {
    const parsed = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8')) as Record<string, unknown>;
    if (parsed.revision !== revision || parsed.type !== type) throw new Error('stale');
    return integer(parsed.offset, 0, 0, Number.MAX_SAFE_INTEGER);
  } catch {
    throw new ModelingMcpToolError(
      'REVISION_CONFLICT',
      '分页游标对应的 revision 已变化，请从第一页重新读取',
      409,
      [],
      { phase: 'load', reason_code: 'REVISION_CONFLICT', retryable: true },
    );
  }
}

function coverageQuestions(coverage: CoverageResult): string[] {
  return [...new Set([
    ...(coverage.covered ?? []).map((item) => item.question),
    ...(coverage.uncovered ?? []),
  ].map((item) => item.trim()).filter(Boolean))];
}

function findingIdentity(finding: CheckFinding): string {
  return finding.fingerprint || [finding.source, finding.category, finding.target, finding.code ?? '', finding.message].join('|');
}

function impact(
  code: string,
  level: ImpactLevel,
  target: string,
  message: string,
  suggestion: string,
  phase: Impact['phase'] = 'validate',
): Impact {
  return { code, phase, level, target, message, suggestion };
}

function dedupeImpacts(values: Impact[]): Impact[] {
  const ranked: Record<ImpactLevel, number> = { blocking: 0, warning: 1, info: 2 };
  return [...new Map(values.map((item) => [[item.code, item.level, item.target, item.message].join('|'), item])).values()]
    .sort((left, right) => ranked[left.level] - ranked[right.level]
      || compareText(left.code, right.code)
      || compareText(left.target, right.target)
      || compareText(left.message, right.message));
}

function normalizeInputError(command: MutationCommand, error: unknown): Impact {
  if (error instanceof ModelingMcpToolError) {
    return impact(reasonCodeForToolError(command, error), 'blocking', resourceTarget(command), error.message, error.next[0] || '修正输入后重试', error.details.phase === 'input' ? 'input' : 'validate');
  }
  return impact('INVALID_INPUT', 'blocking', resourceTarget(command), errorMessage(error), '修正输入后重试', 'input');
}

function resourceTarget(command: MutationCommand): string {
  return `${command.resourceType}:${command.key}`;
}

function resourceRef(command: MutationCommand): ModelingMcpToolResourceRef {
  return {
    type: command.resourceType,
    id: `${command.resourceType}:${command.key}`,
    key: command.key,
  };
}

function suggestionForIntegrityProblem(problem: string): string {
  if (problem.includes('引用了不存在')) return '改为当前 revision 中已存在的资源 key，或先创建依赖资源后重试';
  if (problem.includes('绑定')) return '先解除或修正相关绑定，再重新提交';
  return '根据完整性错误定位相关字段并修正后重试';
}

function actionSummary(command: MutationCommand): string {
  const action = command.action === 'create' ? '新增' : command.action === 'update' ? '修改' : '删除';
  return `${action}${command.resourceType === 'operation' ? '操作' : '规则'} ${command.key}`;
}

function resourceNotFound(type: ResourceType, key: string): ModelingMcpToolError {
  return new ModelingMcpToolError(
    'RESOURCE_NOT_FOUND',
    `${type === 'operation' ? 'Operation' : 'Rule'} ${key} 不存在`,
    404,
    [],
    {
      phase: 'load',
      resource: { type, id: `${type}:${key}`, key },
      reason_code: type === 'operation' ? 'OPERATION_NOT_FOUND' : 'RULE_NOT_FOUND',
      retryable: false,
    } satisfies ModelingMcpToolDetails,
  );
}

function mapArtifactError(
  error: unknown,
  context: { phase: 'input' | 'load' | 'validate' | 'persist'; resource?: ModelingMcpToolResourceRef },
): ModelingMcpToolError {
  if (error instanceof ArtifactRepositoryError && error.code === 'revision_conflict') {
    return new ModelingMcpToolError(
      'REVISION_CONFLICT',
      '知识包内容已更新，请刷新后重试',
      409,
      ['重新读取资源和 revision 后再次分析'],
      {
        phase: context.phase,
        ...(context.resource ? { resource: context.resource } : {}),
        reason_code: 'REVISION_CONFLICT',
        expected_revision: error.details?.expectedRevision,
        actual_revision: error.details?.actualRevision,
        retryable: true,
      } satisfies ModelingMcpToolDetails,
    );
  }
  if (error instanceof ArtifactRepositoryError && error.code === 'revision_not_found') {
    return new ModelingMcpToolError(
      'RESOURCE_NOT_FOUND',
      '指定的 revision 不存在',
      404,
      ['使用最新 revision 或从第一页重新读取'],
      {
        phase: context.phase,
        ...(context.resource ? { resource: context.resource } : {}),
        reason_code: 'REVISION_NOT_FOUND',
        retryable: false,
      } satisfies ModelingMcpToolDetails,
    );
  }
  if (error instanceof PackageArtifactStoreError && error.code === 'commit_in_progress') {
    return new ModelingMcpToolError(
      'PROJECT_WRITE_BUSY',
      '知识包正在保存，请稍后重试',
      409,
      ['短暂退避后重试；若 revision 已变化则重新分析'],
      {
        phase: context.phase,
        ...(context.resource ? { resource: context.resource } : {}),
        reason_code: 'PROJECT_WRITE_BUSY',
        retryable: true,
      } satisfies ModelingMcpToolDetails,
    );
  }
  if (error instanceof PackageArtifactStoreError && ['package_not_found', 'revision_has_no_package'].includes(error.code)) {
    return new ModelingMcpToolError(
      'RESOURCE_NOT_FOUND',
      '当前项目还没有可用的知识包版本',
      404,
      ['先生成或导入知识包后再调用 MCP 工具'],
      {
        phase: context.phase,
        ...(context.resource ? { resource: context.resource } : {}),
        reason_code: 'PACKAGE_NOT_FOUND',
        retryable: false,
      } satisfies ModelingMcpToolDetails,
    );
  }
  if (error instanceof ModelingMcpToolError) {
    if (error.details.phase) return error;
    return new ModelingMcpToolError(error.code, error.message, error.statusCode, error.next, {
      phase: context.phase,
      ...(context.resource ? { resource: context.resource } : {}),
      reason_code: error.code,
      retryable: error.code === 'REVISION_CONFLICT' || error.code === 'PROJECT_WRITE_BUSY',
      ...error.details,
    });
  }
  throw error;
}

function reasonCodeForToolError(command: MutationCommand, error: ModelingMcpToolError): string {
  if (error.code === 'RESOURCE_ALREADY_EXISTS') {
    return command.resourceType === 'operation' ? 'OPERATION_ALREADY_EXISTS' : 'RULE_ALREADY_EXISTS';
  }
  if (error.code === 'RESOURCE_NOT_FOUND') {
    return command.resourceType === 'operation' ? 'OPERATION_NOT_FOUND' : 'RULE_NOT_FOUND';
  }
  if (error.code === 'FIELD_READ_ONLY') {
    return command.resourceType === 'operation' ? 'OPERATION_FIELD_READ_ONLY' : 'RULE_FIELD_READ_ONLY';
  }
  return error.code;
}

function assertUnique(values: string[], label: string): void {
  const seen = new Set<string>();
  for (const value of values) {
    if (seen.has(value)) throw new ModelingMcpToolError('INVALID_INPUT', `${label} 重复：${value}`, 400);
    seen.add(value);
  }
}

function compareText(left: string, right: string): number {
  return Buffer.compare(Buffer.from(left), Buffer.from(right));
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

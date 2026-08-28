import { createHash } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { PackageArtifactStoreError, type PackageArtifactFiles } from '../../artifacts/package-store.js';
import { getPackageArtifactStore } from '../../artifacts/runtime.js';
import { ArtifactRepositoryError } from '../../artifacts/repository.js';
import { getDb } from '../../db/index.js';
import { listDocuments } from '../../documents/repo.js';
import type {
  CoverageResult,
  PackageSpec,
  Project,
  SourceRef,
  SpecField,
  SpecObject,
} from '../../types.js';
import { validateKey } from '../../workflow/keys.js';
import { listSpecIntegrityProblems, type OntologyValidationIssue } from '../../workflow/ontology-model-validation.js';
import { previewOntologyObjectMutation } from '../../workflow/ontology-object-model-mutation.js';
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

const MAX_LIST_LIMIT = 200;
const DEFAULT_LIST_LIMIT = 50;
const MAX_TEXT_LENGTH = 50_000;

interface ObjectMutationCommand {
  key: string;
  expectedRevision: string;
  metadata?: unknown;
  fieldOps?: unknown;
  relationOps?: unknown;
}

interface ObjectAnalysisResult {
  ok: boolean;
  revision: string;
  resource: ModelingMcpToolResourceRef;
  changed_paths: string[];
  affected_resources: Array<{ type: string; key: string; relation?: string }>;
  historical_problem_count: number;
  field_errors: Record<string, string>;
  issues: ModelingMcpToolIssue[];
  warnings: string[];
  impact_summary: unknown;
  normalized_mutation: unknown;
  next_object: Record<string, unknown>;
  nextSpec?: PackageSpec;
  coverage?: CoverageResult;
  objectValue?: SpecObject;
}

interface ObjectSummaryItem {
  object_id: string;
  key: string;
  title: string;
  description: string;
  source_path: string;
  relation_count: number;
}

export class ObjectMcpService {
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
      if (toolName === 'list_ontology_objects') return this.list(project, args);
      if (toolName === 'get_ontology_object') return this.get(project, args);
      if (toolName === 'validate_ontology_object_change') {
        return this.publicAnalysis(await this.analyze(project, commandFromObjectChange(args)));
      }
      if (toolName === 'update_ontology_object') {
        return this.submit(project, username, commandFromObjectChange(args));
      }
      throw new ModelingMcpToolError(
        'MCP_TOOL_NOT_FOUND',
        `未知 Object 工具：${toolName}`,
        404,
        [],
        { phase: 'input', reason_code: 'MCP_TOOL_NOT_FOUND', retryable: false },
      );
    } catch (error) {
      throw mapObjectMcpError(error, { phase: 'load' });
    }
  }

  private async list(project: Project, args: Record<string, unknown>) {
    const snapshot = await this.readSnapshot(project.id, args.revision);
    const query = optionalText(args.query).toLocaleLowerCase('zh-CN');
    const limit = integer(args.limit, DEFAULT_LIST_LIMIT, 1, MAX_LIST_LIMIT);
    const offset = decodeCursor(args.cursor, snapshot.revision.oid);
    const { sort, direction } = parseSort(args.sort, args.order);
    const items = snapshot.spec.objects
      .map(objectSummary)
      .filter((item) => !query || searchableObjectText(item).includes(query))
      .sort((left, right) => compareObjectSummary(left, right, sort, direction));
    const page = items.slice(offset, offset + limit);
    const nextOffset = offset + page.length;
    return {
      revision: snapshot.revision.oid,
      items: page,
      total: items.length,
      next_cursor: nextOffset < items.length ? encodeCursor(snapshot.revision.oid, nextOffset) : null,
      display: {
        text: items.length ? `已返回 ${page.length}/${items.length} 个对象` : '当前知识包还没有对象',
      },
    };
  }

  private async get(project: Project, args: Record<string, unknown>) {
    const snapshot = await this.readSnapshot(project.id, args.revision);
    const key = objectKey(args.object_id);
    const item = snapshot.spec.objects.find((candidate) => candidate.key === key);
    if (!item) throw objectNotFound(key);
    return {
      revision: snapshot.revision.oid,
      object: publicObject(item, snapshot.spec),
      display: { text: `已返回对象《${item.title}》的当前模型` },
    };
  }

  private async submit(project: Project, username: string, command: ObjectMutationCommand) {
    const analysis = await this.analyze(project, command);
    if (!analysis.ok || !analysis.nextSpec || !analysis.coverage || !analysis.objectValue) {
      throw new ModelingMcpToolError(
        'SUBMISSION_BLOCKED',
        '对象修改未通过确定性检查',
        422,
        ['根据 issues 和 field_errors 修正后重新提交'],
        {
          phase: 'validate',
          resource: analysis.resource,
          reason_code: 'SUBMISSION_BLOCKED',
          field_errors: analysis.field_errors,
          issues: analysis.issues,
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
        note: objectActionSummary(command),
        action: {
          cause: 'modeling_mcp_object_update',
          summary: objectActionSummary(command),
          actorKind: 'user',
          actorName: username,
        },
      });
      return {
        resource: analysis.resource,
        object: publicObject(analysis.objectValue, analysis.nextSpec),
        previous_revision: command.expectedRevision,
        revision: saved.revision.oid,
        changed_paths: analysis.changed_paths,
        affected_resources: analysis.affected_resources,
        warnings: analysis.issues.filter((issue) => issue.level !== 'blocking'),
        impact_summary: analysis.impact_summary,
        normalized_mutation: analysis.normalized_mutation,
        historical_problem_count: analysis.historical_problem_count,
        display: { text: `${objectActionSummary(command)}，已生成正式修订` },
      };
    } catch (error) {
      throw mapObjectMcpError(error, { phase: 'persist', resource: analysis.resource });
    }
  }

  private async analyze(project: Project, command: ObjectMutationCommand): Promise<ObjectAnalysisResult> {
    const current = await this.readSnapshot(project.id);
    const resource = objectResource(command.key);
    if (current.revision.oid !== command.expectedRevision) {
      throw new ModelingMcpToolError(
        'REVISION_CONFLICT',
        '知识包内容已更新，请刷新后重试',
        409,
        ['重新读取对象和 revision 后再次分析'],
        {
          phase: 'load',
          resource,
          reason_code: 'REVISION_CONFLICT',
          retryable: true,
          expected_revision: command.expectedRevision,
          actual_revision: current.revision.oid,
        } satisfies ModelingMcpToolDetails,
      );
    }

    const issues: ModelingMcpToolIssue[] = [];
    const fieldErrors: Record<string, string> = {};
    if (!['draft', 'review'].includes(current.spec.status ?? 'draft')) {
      issues.push(toolIssue(
        'PACKAGE_NOT_EDITABLE',
        'validate',
        'blocking',
        'package.status',
        `当前知识包状态 ${current.spec.status} 不允许直接编辑`,
        '在 Modeling 中创建可编辑草稿或切换到 review/draft 状态后重试',
      ));
    }
    const editingIssue = await this.editingStateIssue(project.id);
    if (editingIssue) issues.push(editingIssue);

    const preview = previewOntologyObjectMutation(
      current.spec,
      command.key,
      {
        metadata: command.metadata,
        field_ops: command.fieldOps,
        relation_ops: command.relationOps,
      },
      { tables: this.projectTables(project.id) },
    );
    issues.push(...preview.issues.map((issue) => normalizeObjectValidationIssue(issue, resource)));
    Object.assign(fieldErrors, preview.fieldErrors);

    let coverage: CoverageResult | undefined;
    let changedPaths: string[] = [];
    if (preview.nextSpec && preview.nextSpecObject) {
      try {
        const beforeFiles = renderPackage(current.spec);
        const afterFiles = renderPackage(preview.nextSpec);
        parseRenderedPackageFiles(afterFiles);
        const selectedQuestions = coverageQuestions(current.coverage);
        coverage = coverageFor(preview.nextSpec, selectedQuestions);
        changedPaths = changedRenderedPaths(beforeFiles, afterFiles, current.coverage, coverage);
        issues.push(...this.findingIssues(
          project.id,
          resource,
          current.spec,
          beforeFiles,
          preview.nextSpec,
          afterFiles,
          selectedQuestions,
        ));
      } catch (error) {
        issues.push(toolIssue(
          'PACKAGE_RENDER_FAILED',
          'validate',
          'blocking',
          resource.id,
          errorMessage(error),
          '按字段约束修正提交内容，确保知识包可以规范渲染和解析',
        ));
      }
    }

    const normalizedIssues = dedupeIssues(issues);
    for (const issue of normalizedIssues) {
      if (issue.level !== 'blocking' || issue.target === resource.id || fieldErrors[issue.target]) continue;
      fieldErrors[issue.target] = issue.message;
    }
    return {
      ok: !normalizedIssues.some((issue) => issue.level === 'blocking'),
      revision: current.revision.oid,
      resource,
      changed_paths: changedPaths,
      affected_resources: affectedResourcesForObject(preview, command.key),
      historical_problem_count: Math.max(0, listSpecIntegrityProblems(current.spec).length),
      field_errors: fieldErrors,
      issues: normalizedIssues,
      warnings: preview.warnings,
      impact_summary: structuredClone(preview.impactSummary),
      normalized_mutation: structuredClone(preview.normalizedMutation),
      next_object: preview.nextSpec && preview.nextSpecObject
        ? publicObject(preview.nextSpecObject, preview.nextSpec)
        : structuredClone(preview.nextObject),
      nextSpec: preview.nextSpec,
      coverage,
      objectValue: preview.nextSpecObject,
    };
  }

  private async readSnapshot(projectId: string, revision?: unknown): Promise<PackageArtifactFiles> {
    try {
      const requestedRevision = optionalText(revision);
      return await this.artifacts.readPackageFiles(projectId, requestedRevision || undefined);
    } catch (error) {
      throw mapObjectMcpError(error, { phase: 'load' });
    }
  }

  private projectTables(projectId: string) {
    return listDocuments(projectId).flatMap((document) => document.tables ?? []);
  }

  private async editingStateIssue(projectId: string): Promise<ModelingMcpToolIssue | null> {
    if (await getActivePackageDraft(projectId)) {
      return toolIssue(
        'PROJECT_EDITING_BUSY',
        'validate',
        'blocking',
        'package_draft',
        '项目存在未发布的人工编辑草稿',
        '先在 Modeling 页面发布或放弃草稿后重试',
      );
    }
    const candidateChanges = Number((this.db.prepare(`SELECT COUNT(*) AS count
      FROM agent_candidate_changes WHERE project_id = ?`).get(projectId) as { count?: number } | undefined)?.count ?? 0);
    const pendingCandidates = Number((this.db.prepare(`SELECT COUNT(*) AS count
      FROM artifact_candidate_revisions WHERE project_id = ? AND status = 'pending'`)
      .get(projectId) as { count?: number } | undefined)?.count ?? 0);
    if (!candidateChanges && !pendingCandidates) return null;
    return toolIssue(
      'PROJECT_EDITING_BUSY',
      'validate',
      'blocking',
      'candidate',
      '项目存在未完成的 candidate/review 修改',
      '先完成或退回现有候选，再基于新的正式 revision 重试',
    );
  }

  private findingIssues(
    projectId: string,
    resource: ModelingMcpToolResourceRef,
    baselineSpec: PackageSpec,
    baselineFiles: Record<string, string>,
    proposedSpec: PackageSpec,
    proposedFiles: Record<string, string>,
    selectedQuestions: string[],
  ): ModelingMcpToolIssue[] {
    const tables = this.projectTables(projectId);
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
      .map((finding) => toolIssue(
        findingReasonCode(finding, resource),
        'validate',
        finding.severity === '错误' ? 'blocking' : finding.severity === '警告' ? 'warning' : 'info',
        finding.path || finding.target || resource.id,
        finding.userMessage || finding.message,
        finding.userSuggestion || finding.suggestion || '根据提示检查并修正相关资源',
      ));
  }

  private publicAnalysis(analysis: ObjectAnalysisResult): Record<string, unknown> {
    return {
      ok: analysis.ok,
      revision: analysis.revision,
      resource: analysis.resource,
      changed_paths: analysis.changed_paths,
      affected_resources: analysis.affected_resources,
      historical_problem_count: analysis.historical_problem_count,
      field_errors: analysis.field_errors,
      issues: analysis.issues,
      warnings: analysis.warnings,
      impact_summary: analysis.impact_summary,
      normalized_mutation: analysis.normalized_mutation,
      next_object: analysis.next_object,
      display: {
        text: analysis.ok ? '对象修改通过确定性检查，可以提交' : '对象修改存在阻塞项，请修正后重试',
      },
    };
  }
}

function commandFromObjectChange(args: Record<string, unknown>): ObjectMutationCommand {
  return {
    key: objectKey(args.object_id),
    expectedRevision: requiredText(args.expected_revision, 'expected_revision'),
    ...(Object.hasOwn(args, 'metadata') ? { metadata: args.metadata } : {}),
    ...(Object.hasOwn(args, 'field_ops') ? { fieldOps: args.field_ops } : {}),
    ...(Object.hasOwn(args, 'relation_ops') ? { relationOps: args.relation_ops } : {}),
  };
}

function objectSummary(object: SpecObject): ObjectSummaryItem {
  return {
    object_id: `object:${object.key}`,
    key: object.key,
    title: object.title,
    description: object.description,
    source_path: `objects/${object.key}.md`,
    relation_count: object.relations?.length ?? 0,
  };
}

function searchableObjectText(item: ObjectSummaryItem): string {
  return [item.key, item.title, item.description].join('\n').toLocaleLowerCase('zh-CN');
}

function compareObjectSummary(
  left: ObjectSummaryItem,
  right: ObjectSummaryItem,
  sort: 'key' | 'title' | 'description' | 'relation_count',
  direction: 1 | -1,
): number {
  const sorted = sort === 'relation_count'
    ? (left.relation_count - right.relation_count) || compareText(left.key, right.key)
    : compareText(String(left[sort]), String(right[sort])) || compareText(left.key, right.key);
  return sorted * direction;
}

function parseSort(sortValue: unknown, orderValue: unknown): {
  sort: 'key' | 'title' | 'description' | 'relation_count';
  direction: 1 | -1;
} {
  const sort = String(sortValue ?? '').trim();
  const order = String(orderValue ?? '').trim().toLocaleLowerCase('zh-CN');
  return {
    sort: sort === 'title' || sort === 'description' || sort === 'relation_count' ? sort : 'key',
    direction: order === 'desc' ? -1 : 1,
  };
}

function encodeCursor(revision: string, offset: number): string {
  return Buffer.from(JSON.stringify({ revision, offset }), 'utf8').toString('base64url');
}

function decodeCursor(value: unknown, revision: string): number {
  const cursor = optionalText(value);
  if (!cursor) return 0;
  try {
    const parsed = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8')) as Record<string, unknown>;
    if (parsed.revision !== revision) throw new Error('stale');
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

function publicObject(object: SpecObject, spec: PackageSpec): Record<string, unknown> {
  return {
    id: `object:${object.key}`,
    key: object.key,
    title: object.title,
    description: object.description,
    source_path: `objects/${object.key}.md`,
    relation_count: object.relations?.length ?? 0,
    metadata: {
      title: object.title,
      description: object.description,
      aliases: structuredClone(object.aliases ?? []),
      queryBoundary: object.queryBoundary ?? '',
    },
    fields: structuredClone(object.fields ?? []),
    sources: structuredClone((object.sourceRefs ?? []) as SourceRef[]),
    relations: currentObjectRelations(object).map((relation) => ({
      id: relation.id,
      kind: relation.kind,
      label: relation.label,
      target_id: relation.targetId,
      target_kind: 'object',
      target_key: relation.targetKey,
      target_exists: spec.objects.some((item) => item.key === relation.targetKey),
      ...(relation.description ? { description: relation.description } : {}),
    })),
  };
}

function normalizeObjectValidationIssue(
  issue: OntologyValidationIssue,
  resource: ModelingMcpToolResourceRef,
): ModelingMcpToolIssue {
  return {
    code: objectReasonCode(issue),
    phase: 'validate',
    level: issue.level === 'error' ? 'blocking' : 'warning',
    target: issue.path && issue.path !== '_batch' ? issue.path : resource.id,
    message: issue.message,
    ...(issue.suggestion ? { suggestion: issue.suggestion } : {}),
  };
}

function objectReasonCode(issue: OntologyValidationIssue): string {
  if (issue.code === 'OBJECT_NOT_FOUND') return 'OBJECT_NOT_FOUND';
  if (issue.code === 'NO_CHANGES' || issue.code === 'EMPTY_CHANGES') return 'NO_CHANGES';
  if (issue.code === 'PRIMARY_KEY_REMOVED') return 'PRIMARY_KEY_REMOVED';
  if (issue.code === 'OBJECT_MAPPING_INVALID') return 'OBJECT_MAPPING_INVALID';
  if (issue.code === 'SPEC_INTEGRITY_INVALID') return 'PACKAGE_INTEGRITY_FAILED';
  if (issue.code.startsWith('OBJECT_METADATA_')) return 'OBJECT_METADATA_INVALID';
  if (issue.code === 'FIELD_UPDATE_NOT_FOUND') return 'FIELD_UPDATE_NOT_FOUND';
  if (issue.code === 'FIELD_REMOVE_NOT_FOUND') return 'FIELD_REMOVE_NOT_FOUND';
  if (issue.code === 'FIELD_OPS_CONFLICT') return 'FIELD_OPS_CONFLICT';
  if (issue.code.startsWith('FIELD_ADD_')) return 'FIELD_ADD_INVALID';
  if (issue.code.startsWith('FIELD_') || issue.code.startsWith('FIELDS_') || issue.code === 'ARRAY_REQUIRED') {
    return issue.path.startsWith('relation_ops') ? 'RELATION_OPS_INVALID' : 'FIELD_OPS_INVALID';
  }
  if (issue.code === 'RELATION_UPDATE_NOT_FOUND') return 'RELATION_UPDATE_NOT_FOUND';
  if (issue.code === 'RELATION_REMOVE_NOT_FOUND') return 'RELATION_REMOVE_NOT_FOUND';
  if (issue.code === 'RELATION_OPS_CONFLICT') return 'RELATION_OPS_CONFLICT';
  if (issue.code.startsWith('RELATION_ADD_')) return 'RELATION_ADD_INVALID';
  if (issue.code.includes('TARGET')) return 'RELATION_TARGET_INVALID';
  if (issue.code.startsWith('RELATION_') || issue.code.startsWith('RELATIONS_')) return 'RELATION_OPS_INVALID';
  return 'OBJECT_METADATA_INVALID';
}

function affectedResourcesForObject(
  preview: Awaited<ReturnType<typeof previewOntologyObjectMutation>>,
  objectKey: string,
): Array<{ type: string; key: string; relation?: string }> {
  const result = new Map<string, { type: string; key: string; relation?: string }>();
  const add = (type: string, key: string, relation?: string) => {
    result.set(`${type}:${key}:${relation ?? ''}`, { type, key, ...(relation ? { relation } : {}) });
  };
  add('object', objectKey);
  for (const targetId of preview.impactSummary.affectedObjects ?? []) {
    const key = parseTargetKey(String(targetId));
    if (key) add('object', key, 'relation');
  }
  if ((preview.impactSummary.primaryKeyAffected ?? []).length) {
    add('object', objectKey, 'primary_key');
  }
  if (preview.impactSummary.mappingAffected) {
    add('mapping', 'object-data-mappings', 'field_mapping');
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

function coverageQuestions(coverage: CoverageResult): string[] {
  return [...new Set([
    ...(coverage.covered ?? []).map((item) => item.question),
    ...(coverage.uncovered ?? []),
  ].map((item) => item.trim()).filter(Boolean))];
}

function findingIdentity(finding: CheckFinding): string {
  return finding.fingerprint || [finding.source, finding.category, finding.target, finding.code ?? '', finding.message].join('|');
}

function findingReasonCode(finding: CheckFinding, resource: ModelingMcpToolResourceRef): string {
  if (finding.code) return finding.code;
  if ((finding.path || '').startsWith('objects/')) return 'PACKAGE_CHECK_FAILED';
  return resource.type === 'object' ? 'PACKAGE_CHECK_FAILED' : 'PACKAGE_CHECK_FAILED';
}

function mapObjectMcpError(
  error: unknown,
  context: { phase: 'input' | 'load' | 'validate' | 'persist'; resource?: ModelingMcpToolResourceRef },
): ModelingMcpToolError {
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
  if (error instanceof ArtifactRepositoryError && error.code === 'revision_conflict') {
    return new ModelingMcpToolError(
      'REVISION_CONFLICT',
      '知识包内容已更新，请刷新后重试',
      409,
      ['重新读取对象和 revision 后再次分析'],
      {
        phase: context.phase,
        ...(context.resource ? { resource: context.resource } : {}),
        reason_code: 'REVISION_CONFLICT',
        retryable: true,
        expected_revision: error.details?.expectedRevision,
        actual_revision: error.details?.actualRevision,
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
  if (error instanceof PackageArtifactStoreError && error.code === 'invalid_package') {
    return new ModelingMcpToolError(
      'SUBMISSION_BLOCKED',
      '知识包内容不完整，无法执行当前修改',
      422,
      ['先修复当前知识包存量结构问题后重试'],
      {
        phase: context.phase,
        ...(context.resource ? { resource: context.resource } : {}),
        reason_code: 'PACKAGE_INTEGRITY_FAILED',
        retryable: false,
      } satisfies ModelingMcpToolDetails,
    );
  }
  if (error instanceof PackageArtifactStoreError && error.code === 'invalid_input') {
    return new ModelingMcpToolError(
      'INVALID_INPUT',
      error.message,
      400,
      [],
      {
        phase: context.phase,
        ...(context.resource ? { resource: context.resource } : {}),
        reason_code: 'INVALID_INPUT',
        retryable: false,
      } satisfies ModelingMcpToolDetails,
    );
  }
  throw error;
}

function objectNotFound(key: string): ModelingMcpToolError {
  return new ModelingMcpToolError(
    'RESOURCE_NOT_FOUND',
    `Object ${key} 不存在`,
    404,
    [],
    {
      phase: 'load',
      resource: objectResource(key),
      reason_code: 'OBJECT_NOT_FOUND',
      retryable: false,
    } satisfies ModelingMcpToolDetails,
  );
}

function objectResource(key: string): ModelingMcpToolResourceRef {
  return { type: 'object', id: `object:${key}`, key };
}

function objectKey(value: unknown): string {
  const raw = requiredText(value, 'object_id');
  try {
    return validateKey(raw.startsWith('object:') ? raw.slice('object:'.length) : raw, '对象');
  } catch (error) {
    throw new ModelingMcpToolError(
      'INVALID_INPUT',
      error instanceof Error ? error.message : 'object_id 不正确',
      400,
      ['使用 list_ontology_objects 或 get_ontology_object 返回的 object_id'],
      {
        phase: 'input',
        reason_code: 'INVALID_INPUT',
        field_errors: { object_id: error instanceof Error ? error.message : 'object_id 不正确' },
        retryable: false,
      } satisfies ModelingMcpToolDetails,
    );
  }
}

function requiredText(value: unknown, field: string): string {
  const text = optionalText(value);
  if (!text) {
    throw new ModelingMcpToolError(
      'INVALID_INPUT',
      `${field} 必填`,
      400,
      [],
      {
        phase: 'input',
        reason_code: 'INVALID_INPUT',
        field_errors: { [field]: `${field} 必填` },
        retryable: false,
      } satisfies ModelingMcpToolDetails,
    );
  }
  if (text.length > MAX_TEXT_LENGTH) {
    throw new ModelingMcpToolError(
      'INVALID_INPUT',
      `${field} 不能超过 ${MAX_TEXT_LENGTH} 字符`,
      400,
      [],
      {
        phase: 'input',
        reason_code: 'INVALID_INPUT',
        field_errors: { [field]: `${field} 不能超过 ${MAX_TEXT_LENGTH} 字符` },
        retryable: false,
      } satisfies ModelingMcpToolDetails,
    );
  }
  return text;
}

function optionalText(value: unknown): string {
  return String(value ?? '').normalize('NFC').trim();
}

function integer(value: unknown, fallback: number, min: number, max: number): number {
  if (value === undefined || value === null || value === '') return fallback;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < min || parsed > max) {
    throw new ModelingMcpToolError(
      'INVALID_INPUT',
      `数值必须在 ${min}-${max} 之间`,
      400,
      [],
      {
        phase: 'input',
        reason_code: 'INVALID_INPUT',
        retryable: false,
      } satisfies ModelingMcpToolDetails,
    );
  }
  return parsed;
}

function objectActionSummary(command: ObjectMutationCommand): string {
  return `修改对象 ${command.key}`;
}

function toolIssue(
  code: string,
  phase: ModelingMcpToolIssue['phase'],
  level: ModelingMcpToolIssueLevel,
  target: string,
  message: string,
  suggestion?: string,
): ModelingMcpToolIssue {
  return {
    code,
    phase,
    level,
    target,
    message,
    ...(suggestion ? { suggestion } : {}),
  };
}

function dedupeIssues(values: ModelingMcpToolIssue[]): ModelingMcpToolIssue[] {
  const ranked: Record<ModelingMcpToolIssueLevel, number> = { blocking: 0, warning: 1, info: 2 };
  return [...new Map(values.map((item) => [[item.code, item.phase, item.level, item.target, item.message].join('|'), item])).values()]
    .sort((left, right) => ranked[left.level] - ranked[right.level]
      || compareText(left.code, right.code)
      || compareText(left.target, right.target)
      || compareText(left.message, right.message));
}

function currentObjectRelations(object: SpecObject): Array<{
  id: string;
  kind: string;
  targetId: string;
  targetKey: string;
  label: string;
  description?: string;
}> {
  const sourceId = `object:${object.key}`;
  return (object.relations ?? []).map((relation) => {
    const targetId = relation.target.includes(':') ? relation.target : `object:${relation.target}`;
    const identity = JSON.stringify([
      sourceId,
      'object_relation',
      targetId,
      relation.relation,
      relation.note,
      relation.evidence,
    ]);
    return {
      id: `edge:${createHash('sha256').update(identity).digest('hex').slice(0, 20)}`,
      kind: 'object_relation',
      targetId,
      targetKey: parseTargetKey(targetId),
      label: relation.relation,
      ...(relation.note ? { description: relation.note } : {}),
    };
  });
}

function parseTargetKey(targetId: string): string {
  const normalized = targetId.trim();
  const separator = normalized.indexOf(':');
  return separator > 0 ? normalized.slice(separator + 1) : normalized;
}

function compareText(left: string, right: string): number {
  return Buffer.compare(Buffer.from(left), Buffer.from(right));
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

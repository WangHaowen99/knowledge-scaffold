import { createHash, randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { getPackageArtifactStore } from '../artifacts/runtime.js';
import { PackageArtifactStoreError, type PackageArtifactStore } from '../artifacts/package-store.js';
import { ArtifactRepositoryError, type ArtifactChange } from '../artifacts/repository.js';
import { getDb, nowIso } from '../db/index.js';
import { normalizeFlowchartDocument } from '../flowchart/index.js';
import type { CoverageResult, PackageSpec } from '../types.js';
import { nextValidationCandidateVersion } from '../workflow/package-version-lifecycle.js';
import {
  fuseKnowledgePackages,
  packageFusionDigest,
  type PackageFusionConcept,
  type PackageFusionOptions,
  type PackageFusionResourceDisposition,
  type PackageFusionResourceKind,
} from '../workflow/package-fusion.js';
import { getPackageFusionStoredSource, type PackageFusionStoredSource } from './package-merge.js';

const RESOURCE_KINDS = ['object', 'operation', 'rule', 'metric', 'script', 'diagram'] as const;
const RESOURCE_KIND_ORDER = new Map(RESOURCE_KINDS.map((kind, index) => [kind, index]));
const FUSION_SCOPES = ['objects', 'operations', 'rules', 'metrics', 'scripts', 'diagrams'] as const;
export type FusionScope = typeof FUSION_SCOPES[number];
type FusionDecision = 'preserve' | 'canonicalize' | 'exclude';

export interface PackageFusionScopeDependency {
  sourceId: string;
  from: string;
  fromScope: FusionScope;
  requires: string;
  requiresScope: FusionScope;
}

export type PackageFusionPlanStatus =
  | 'awaiting_decisions'
  | 'ready'
  | 'compiling'
  | 'candidate'
  | 'failed';

export interface PackageFusionDisposition {
  sourceId: string;
  kind: PackageFusionResourceKind;
  key: string;
  action: FusionDecision;
  targetSourceId?: string;
  targetKey?: string;
  reason?: string;
}

export interface PackageFusionInventoryEntry {
  sourceId: string;
  sourceFingerprint: string;
  kind: PackageFusionResourceKind;
  sourceKey: string;
  decision: FusionDecision | 'exclude_scope' | null;
  included: boolean;
  targetSourceId?: string;
  targetKey?: string;
  reason?: string;
}

interface StoredSourcePlan {
  sourceId: string;
  compilerSourceId: string;
  fingerprint: string;
  filename: string;
  packageId: string;
  name: string;
  version: string;
  selectedScopes: FusionScope[];
}

interface StoredFusionPlan {
  schemaVersion: '0.1';
  output: PackageFusionOptions;
  sources: StoredSourcePlan[];
  concepts: PackageFusionConcept[];
  dispositions: PackageFusionDisposition[];
  digest: string;
}

interface FusionPlanRow {
  id: string;
  project_id: string;
  expected_revision_oid: string;
  source_ids_json: string;
  plan_json: string;
  ledger_json: string;
  quality_json: string | null;
  output_digest: string | null;
  status: PackageFusionPlanStatus;
  candidate_id: string | null;
  error: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface PackageFusionPlanView {
  id: string;
  projectId: string;
  expectedRevision: string;
  status: PackageFusionPlanStatus;
  sources: StoredSourcePlan[];
  output: PackageFusionOptions;
  concepts: PackageFusionConcept[];
  ledger: PackageFusionInventoryEntry[] | Record<string, unknown>;
  unresolvedCount: number;
  candidateId?: string;
  outputDigest?: string;
  quality?: Record<string, unknown>;
  error?: string;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
}

export class PackageFusionServiceError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly statusCode = 400,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = 'PackageFusionServiceError';
  }
}

export class PackageFusionService {
  constructor(
    private readonly db: DatabaseSync = getDb(),
    private readonly artifacts: PackageArtifactStore = getPackageArtifactStore(),
  ) {}

  async createPlan(projectId: string, username: string, raw: unknown): Promise<PackageFusionPlanView> {
    const input = asRecord(raw, '融合计划');
    const expectedRevision = requiredText(input.expectedRevision, '基线修订', 200);
    const sourceSelections = normalizeSourceSelections(input);
    const sourceIds = sourceSelections.map((selection) => selection.sourceId);
    const current = await this.artifacts.current(projectId);
    if (!current || current.revision.oid !== expectedRevision) {
      throw new PackageFusionServiceError(
        'FUSION_REVISION_CONFLICT',
        '当前知识包已经变化，请刷新后重新建立融合计划',
        409,
        { currentRevision: current?.revision.oid },
      );
    }

    const sources = sourceIds.map((sourceId) => this.source(projectId, sourceId));
    const fingerprints = new Set(sources.map((source) => source.fingerprint));
    if (fingerprints.size !== sources.length) {
      throw new PackageFusionServiceError(
        'FUSION_DUPLICATE_SOURCE',
        '同一份知识包内容不能在一次融合中重复加入',
        409,
      );
    }
    const scopesBySource = new Map(sourceSelections.map((selection) => [selection.sourceId, selection.scopes]));
    const sourcePlans = sources.map((source) => storedSourcePlan(
      source,
      scopesBySource.get(source.id) ?? [...FUSION_SCOPES],
    )).sort(compareStoredSources);
    assertScopeClosure(sources, sourcePlans);
    const sourceById = new Map(sourcePlans.map((source) => [source.sourceId, source]));
    const output = normalizeOutput(input.output);
    const nextVersion = nextValidationCandidateVersion({
      currentVersion: current.packageVersion,
      currentStatus: current.spec.status ?? 'draft',
    });
    if (output.version !== current.packageVersion && output.version !== nextVersion) {
      throw new PackageFusionServiceError(
        'FUSION_INVALID_VERSION',
        `融合候选版本必须沿用 ${current.packageVersion} 或使用下一评审版本 ${nextVersion}`,
        409,
        { currentVersion: current.packageVersion, nextVersion },
      );
    }
    const reusable = (this.db.prepare(`SELECT * FROM agent_package_fusion_plans
      WHERE project_id = ? AND expected_revision_oid = ?
        AND status IN ('awaiting_decisions', 'ready')
      ORDER BY created_at DESC, rowid DESC`).all(projectId, expectedRevision) as unknown as FusionPlanRow[])
      .find((row) => reusablePlanMatches(row, sourcePlans, output));
    if (reusable) return planView(reusable);
    const inventory = buildInventory(sources, sourcePlans);
    const concepts = normalizeConcepts(input.concepts, sourceById, inventory);
    const dispositions = normalizeDispositions(input.dispositions, inventory);
    validateDispositions(dispositions, inventory);
    const ledger = applyDispositions(inventory, dispositions);
    if (!ledger.some((entry) => entry.decision === null)) {
      assertDispositionClosure(sources, sourcePlans, dispositions);
    }
    const plan = withPlanDigest({
      schemaVersion: '0.1',
      output,
      sources: sourcePlans,
      concepts,
      dispositions,
    });
    const status: PackageFusionPlanStatus = ledger.some((entry) => entry.decision === null)
      ? 'awaiting_decisions'
      : 'ready';
    const id = `fusion_${randomUUID().slice(0, 12)}`;
    const createdAt = nowIso();
    this.db.prepare(`INSERT INTO agent_package_fusion_plans (
      id, project_id, expected_revision_oid, source_ids_json, plan_json, ledger_json,
      status, created_by, created_at, updated_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?)`).run(
      id,
      projectId,
      expectedRevision,
      JSON.stringify(sourcePlans.map((source) => source.sourceId)),
      JSON.stringify(plan),
      JSON.stringify(ledger),
      status,
      requiredText(username, '创建人', 100),
      createdAt,
      createdAt,
    );
    return this.getPlan(projectId, id);
  }

  listPlans(projectId: string): PackageFusionPlanView[] {
    return (this.db.prepare(`SELECT * FROM agent_package_fusion_plans
      WHERE project_id = ? ORDER BY created_at DESC, rowid DESC`).all(projectId) as unknown as FusionPlanRow[])
      .map(planView);
  }

  getPlan(projectId: string, planId: string): PackageFusionPlanView {
    return planView(this.planRow(projectId, planId));
  }

  updatePlan(projectId: string, planId: string, raw: unknown): PackageFusionPlanView {
    const row = this.planRow(projectId, planId);
    if (row.status !== 'awaiting_decisions' && row.status !== 'ready') {
      throw new PackageFusionServiceError(
        'FUSION_PLAN_IMMUTABLE',
        '融合计划已经开始编译，不能再修改裁决',
        409,
      );
    }
    const input = asRecord(raw, '融合裁决');
    const plan = parsePlan(row.plan_json);
    const inventory: PackageFusionInventoryEntry[] = parseInventory(row.ledger_json).map(({
      targetSourceId: _targetSourceId,
      targetKey: _targetKey,
      reason: _reason,
      ...entry
    }) => ({
      ...entry,
      decision: entry.included !== false ? null : 'exclude_scope' as const,
    }));
    if (input.dispositions === undefined && input.concepts === undefined) {
      throw new PackageFusionServiceError('FUSION_INVALID_PLAN', '请提供元素去向或跨包业务概念');
    }
    const sourceById = new Map(plan.sources.map((source) => [source.sourceId, source]));
    const updatedConcepts = normalizeConcepts(input.concepts, sourceById, inventory);
    const conceptMap = new Map(plan.concepts.map((concept) => [concept.id, concept]));
    for (const concept of updatedConcepts) conceptMap.set(concept.id, concept);
    const concepts = [...conceptMap.values()].sort((left, right) => left.id.localeCompare(right.id));
    const updatedDispositions = normalizeDispositions(input.dispositions, inventory);
    const dispositionMap = new Map(plan.dispositions.map((item) => [dispositionIdentity(item), item]));
    for (const item of updatedDispositions) dispositionMap.set(dispositionIdentity(item), item);
    const dispositions = [...dispositionMap.values()]
      .sort((left, right) => dispositionIdentity(left).localeCompare(dispositionIdentity(right)));
    validateDispositions(dispositions, inventory);
    const ledger = applyDispositions(inventory, dispositions);
    if (!ledger.some((entry) => entry.decision === null)) {
      assertDispositionClosure(
        plan.sources.map((source) => this.source(projectId, source.sourceId)),
        plan.sources,
        dispositions,
      );
    }
    const { digest: _previousDigest, ...planWithoutDigest } = plan;
    const nextPlan = withPlanDigest({ ...planWithoutDigest, concepts, dispositions });
    const status: PackageFusionPlanStatus = ledger.some((entry) => entry.decision === null)
      ? 'awaiting_decisions'
      : 'ready';
    const updatedAt = nowIso();
    this.db.prepare(`UPDATE agent_package_fusion_plans
      SET plan_json = ?, ledger_json = ?, status = ?, quality_json = NULL,
        output_digest = NULL, candidate_id = NULL, error = NULL, updated_at = ?
      WHERE id = ? AND project_id = ?`).run(
        JSON.stringify(nextPlan),
        JSON.stringify(ledger),
        status,
        updatedAt,
        planId,
        projectId,
      );
    return this.getPlan(projectId, planId);
  }

  async compilePlan(projectId: string, planId: string, username: string): Promise<PackageFusionPlanView> {
    const row = this.planRow(projectId, planId);
    if (row.status === 'candidate') return planView(row);
    if (row.status === 'awaiting_decisions') {
      const unresolved = parseInventory(row.ledger_json).filter((entry) => entry.decision === null);
      throw new PackageFusionServiceError(
        'FUSION_DECISIONS_REQUIRED',
        `还有 ${unresolved.length} 个元素没有明确去向，不能生成候选版本`,
        409,
        { unresolved },
      );
    }
    if (row.status !== 'ready') {
      throw new PackageFusionServiceError('FUSION_PLAN_NOT_READY', '融合计划当前不能编译', 409);
    }
    const claimed = this.db.prepare(`UPDATE agent_package_fusion_plans
      SET status = 'compiling', error = NULL, updated_at = ?
      WHERE id = ? AND project_id = ? AND status = 'ready'`).run(nowIso(), planId, projectId);
    if (Number(claimed.changes) !== 1) {
      throw new PackageFusionServiceError('FUSION_PLAN_NOT_READY', '融合计划已经被其他请求处理', 409);
    }

    try {
      const plan = parsePlan(row.plan_json);
      const sources = plan.sources.map((planned) => {
        const source = this.source(projectId, planned.sourceId);
        if (source.fingerprint !== planned.fingerprint) {
          throw new PackageFusionServiceError(
            'FUSION_SOURCE_CHANGED',
            `来源知识包 ${planned.name} 的只读归档与计划不一致`,
            409,
          );
        }
        return source;
      });
      const compilerIdBySource = new Map(plan.sources.map((source) => [source.sourceId, source.compilerSourceId]));
      const sourceIdByCompiler = new Map(plan.sources.map((source) => [source.compilerSourceId, source.sourceId]));
      const selectedSources = sources.map((source) => {
        const planned = plan.sources.find((item) => item.sourceId === source.id)!;
        return filterSourceArchive(source, planned.selectedScopes ?? [...FUSION_SCOPES]);
      });
      const compilerDispositions: PackageFusionResourceDisposition[] = plan.dispositions.map((item) => ({
        ...item,
        sourceId: compilerIdBySource.get(item.sourceId)!,
        ...(item.targetSourceId ? { targetSourceId: compilerIdBySource.get(item.targetSourceId)! } : {}),
      }));
      const result = fuseKnowledgePackages(selectedSources.map((source) => ({
        sourceId: compilerIdBySource.get(source.id)!,
        filename: source.filename,
        archive: source.archive,
      })), {
        ...plan.output,
        concepts: plan.concepts.map((concept) => ({
          ...concept,
          members: concept.members.map((member) => ({
            ...member,
            sourceId: compilerIdBySource.get(member.sourceId)!,
          })),
        })),
        dispositions: compilerDispositions,
      });
      if (!result.quality.passed) {
        throw new PackageFusionServiceError(
          'FUSION_QUALITY_FAILED',
          '批量融合没有通过质量门禁，未生成候选版本',
          409,
          { quality: result.quality },
        );
      }
      const decisions = new Map(plan.dispositions.map((item) => [dispositionIdentity(item), item.action]));
      const sourcePlanByCompiler = new Map(plan.sources.map((source) => [source.compilerSourceId, source]));
      const scopeExcluded = buildInventory(sources, plan.sources)
        .filter((entry) => !entry.included)
        .map((entry) => ({
          kind: entry.kind,
          sourceId: plan.sources.find((source) => source.sourceId === entry.sourceId)!.compilerSourceId,
          sourceRecordId: entry.sourceId,
          sourceKey: entry.sourceKey,
          fusedKey: '',
          disposition: 'excluded_scope',
          decision: 'exclude_scope',
          layer: 'scenario',
          conceptIds: [],
          provenance: `pkg:${plan.sources.find((source) => source.sourceId === entry.sourceId)!.name}@${plan.sources.find((source) => source.sourceId === entry.sourceId)!.version}`,
          decisionReason: '该类型未纳入本次融合范围',
        }));
      const finalLedger = {
        ...result.ledger,
        sources: result.ledger.sources.map((source) => ({
          ...source,
          sourceRecordId: sourceIdByCompiler.get(source.sourceId),
          fingerprint: sourcePlanByCompiler.get(source.sourceId)?.fingerprint ?? source.fingerprint,
          selectedScopes: sourcePlanByCompiler.get(source.sourceId)?.selectedScopes ?? [...FUSION_SCOPES],
        })),
        resources: [...result.ledger.resources.map((entry) => {
          const sourceId = sourceIdByCompiler.get(entry.sourceId)!;
          return {
            ...entry,
            sourceRecordId: sourceId,
            ...(entry.canonicalTarget ? {
              canonicalTarget: {
                ...entry.canonicalTarget,
                sourceRecordId: sourceIdByCompiler.get(entry.canonicalTarget.sourceId),
              },
            } : {}),
            decision: decisions.get(dispositionIdentity({
              sourceId,
              kind: entry.kind,
              key: entry.sourceKey,
              action: 'preserve',
            })),
          };
        }), ...scopeExcluded].sort((left, right) => (
          String(left.sourceRecordId).localeCompare(String(right.sourceRecordId))
            || RESOURCE_KINDS.indexOf(left.kind) - RESOURCE_KINDS.indexOf(right.kind)
            || left.sourceKey.localeCompare(right.sourceKey)
        )),
      };
      const packageLedger = JSON.parse(JSON.stringify(finalLedger, (key, value) => (
        key === 'sourceRecordId' ? undefined : value
      )));
      result.files['mappings/package-fusion.json'] = `${JSON.stringify({
        schema_version: finalLedger.schemaVersion,
        strategy: finalLedger.strategy,
        sources: packageLedger.sources,
        providers: finalLedger.providers,
        resources: packageLedger.resources,
        concepts: finalLedger.concepts,
        alias_renames: finalLedger.aliasRenames,
      }, null, 2)}\n`;
      const current = await this.artifacts.current(projectId);
      if (!current || current.revision.oid !== row.expected_revision_oid) {
        throw new PackageFusionServiceError(
          'FUSION_REVISION_CONFLICT',
          '当前知识包已经变化，融合候选未写入',
          409,
          { currentRevision: current?.revision.oid },
        );
      }
      const currentArchive = await this.artifacts.readPackageArchiveFiles(projectId, row.expected_revision_oid);
      const coverage = combineCoverage(sources, compilerIdBySource, result.ledger.resources);
      const changes = fusionChanges(currentArchive.files, current.spec, current.coverage, result.files, result.spec, coverage);
      if (!changes.length) {
        throw new PackageFusionServiceError('FUSION_NO_CHANGES', '融合结果与当前知识包相同', 409);
      }
      const candidate = await this.artifacts.createCandidateRevision(projectId, {
        expectedRevision: row.expected_revision_oid,
        groups: [{
          id: 'fusion-batch',
          summary: `一次性融合 ${sources.length} 个来源知识包`,
          changes,
          validation: { warnings: result.quality.warnings },
        }],
        action: {
          cause: 'package_fusion',
          summary: `生成 ${plan.output.name} 的批量融合候选`,
          actorKind: 'agent',
          actorName: requiredText(username, '操作人', 100),
        },
      });
      const outputDigest = packageFusionDigest(result.files);
      const saved = this.db.prepare(`UPDATE agent_package_fusion_plans
        SET status = 'candidate', candidate_id = ?, ledger_json = ?, quality_json = ?,
          output_digest = ?, error = NULL, updated_at = ?
        WHERE id = ? AND project_id = ? AND status = 'compiling'`).run(
          candidate.id,
          JSON.stringify(finalLedger),
          JSON.stringify(result.quality),
          outputDigest,
          nowIso(),
          planId,
          projectId,
        );
      if (Number(saved.changes) !== 1) {
        await this.artifacts.rejectCandidateRevision(projectId, {
          candidateId: candidate.id,
          action: {
            cause: 'fusion_plan_lost',
            summary: '融合计划状态写入失败，撤销孤立候选',
            actorKind: 'system',
            actorName: '建模系统',
          },
        });
        throw new PackageFusionServiceError('FUSION_PLAN_NOT_READY', '融合计划状态已经变化', 409);
      }
      return this.getPlan(projectId, planId);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.db.prepare(`UPDATE agent_package_fusion_plans
        SET status = 'failed', error = ?, updated_at = ?
        WHERE id = ? AND project_id = ? AND status = 'compiling'`).run(
          message,
          nowIso(),
          planId,
          projectId,
        );
      if (error instanceof PackageFusionServiceError || error instanceof PackageArtifactStoreError) {
        throw error;
      }
      if (error instanceof ArtifactRepositoryError && error.code === 'revision_conflict') {
        throw new PackageFusionServiceError(
          'FUSION_REVISION_CONFLICT',
          '当前知识包已经变化，融合候选未写入',
          409,
          error.details,
        );
      }
      // 编译阶段可能失败于数据库、文件系统或上游解析器。它不是用户的
      // 融合裁决冲突，不能把任意异常包装成 409；保留固定公开文案并交给
      // HTTP 层按 500/503 脱敏返回，详细原因只写入服务端日志/计划记录。
      throw new PackageFusionServiceError(
        'FUSION_COMPILE_FAILED',
        '批量融合编译暂时失败，请稍后重试',
        500,
        { internalError: message.slice(0, 500) },
      );
    }
  }

  private source(projectId: string, sourceId: string): PackageFusionStoredSource {
    const source = getPackageFusionStoredSource(this.db, projectId, sourceId);
    if (!source) {
      throw new PackageFusionServiceError('FUSION_SOURCE_NOT_FOUND', '来源知识包不存在', 404, { sourceId });
    }
    return source;
  }

  private planRow(projectId: string, planId: string): FusionPlanRow {
    const row = this.db.prepare(`SELECT * FROM agent_package_fusion_plans
      WHERE id = ? AND project_id = ?`).get(planId, projectId) as FusionPlanRow | undefined;
    if (!row) throw new PackageFusionServiceError('FUSION_PLAN_NOT_FOUND', '融合计划不存在', 404);
    return row;
  }
}

function normalizeSourceSelections(input: Record<string, any>): Array<{ sourceId: string; scopes: FusionScope[] }> {
  if (input.sourceSelections === undefined) {
    return uniqueStrings(input.sourceIds, '来源知识包', 2, 20).map((sourceId) => ({
      sourceId,
      scopes: [...FUSION_SCOPES],
    }));
  }
  if (!Array.isArray(input.sourceSelections) || input.sourceSelections.length < 2 || input.sourceSelections.length > 20) {
    throw new PackageFusionServiceError('FUSION_INVALID_PLAN', '来源知识包数量必须在 2 到 20 之间');
  }
  const selections = input.sourceSelections.map((raw: unknown) => {
    const selection = asRecord(raw, '来源范围');
    const sourceId = requiredText(selection.sourceId, '来源知识包', 100);
    if (!Array.isArray(selection.scopes) || selection.scopes.length < 1) {
      throw new PackageFusionServiceError('FUSION_INVALID_PLAN', '每份来源至少要纳入一种内容');
    }
    const scopes = selection.scopes.map((scope: unknown) => {
      if (!FUSION_SCOPES.includes(scope as FusionScope)) {
        throw new PackageFusionServiceError('FUSION_INVALID_PLAN', `来源范围不正确：${String(scope ?? '')}`);
      }
      return scope as FusionScope;
    });
    if (new Set(scopes).size !== scopes.length) {
      throw new PackageFusionServiceError('FUSION_INVALID_PLAN', '同一来源范围不能重复');
    }
    return { sourceId, scopes };
  });
  if (new Set(selections.map((selection: { sourceId: string }) => selection.sourceId)).size !== selections.length) {
    throw new PackageFusionServiceError('FUSION_INVALID_PLAN', '来源知识包不能重复');
  }
  return selections;
}

export function packageFusionScopeDependencyGraph(
  source: Pick<PackageFusionStoredSource, 'id' | 'spec'>,
): PackageFusionScopeDependency[] {
  const dependencies: PackageFusionScopeDependency[] = [];
  const need = (
    fromScope: FusionScope,
    from: string,
    requiresScope: FusionScope,
    keys: string[] | undefined,
  ) => {
    for (const key of keys ?? []) {
      dependencies.push({
        sourceId: source.id,
        from,
        fromScope,
        requires: `${requiresScope}/${key}`,
        requiresScope,
      });
    }
  };
  for (const object of source.spec.objects) {
    need('objects', `objects/${object.key}`, 'objects', object.relations?.map((item) => item.target));
  }
  for (const operation of source.spec.operations) {
    need('operations', `operations/${operation.key}`, 'objects', operation.relatedObjects);
    need('operations', `operations/${operation.key}`, 'rules', operation.usesRules);
    need('operations', `operations/${operation.key}`, 'metrics', operation.usesMetrics);
    need('operations', `operations/${operation.key}`, 'scripts', operation.script ? [operation.script.key] : []);
    if ((operation.evidence ?? []).some((item) => item.includes('flow-node:'))) {
      dependencies.push({
        sourceId: source.id,
        from: `operations/${operation.key}`,
        fromScope: 'operations',
        requires: '对应流程图',
        requiresScope: 'diagrams',
      });
    }
  }
  for (const rule of source.spec.rules ?? []) {
    need('rules', `rules/${rule.key}`, 'objects', rule.appliesToObjects);
    need('rules', `rules/${rule.key}`, 'operations', rule.appliesToOperations);
  }
  for (const metric of source.spec.metrics ?? []) {
    need('metrics', `metrics/${metric.key}`, 'objects', metric.usesObjects);
  }
  for (const script of source.spec.scripts ?? []) {
    need('scripts', `scripts/${script.key}`, 'operations', [script.boundOperation]);
  }
  return dependencies;
}

function assertScopeClosure(sources: PackageFusionStoredSource[], plans: StoredSourcePlan[]): void {
  const planById = new Map(plans.map((plan) => [plan.sourceId, plan]));
  const dependencies = sources.flatMap((source) => {
    const selected = new Set(planById.get(source.id)?.selectedScopes ?? FUSION_SCOPES);
    return packageFusionScopeDependencyGraph(source).filter((dependency) => (
      selected.has(dependency.fromScope) && !selected.has(dependency.requiresScope)
    ));
  });
  if (!dependencies.length) return;
  const preview = dependencies.slice(0, 5).map((item) => `${item.from} 需要 ${item.requires}`).join('；');
  throw new PackageFusionServiceError(
    'FUSION_SCOPE_NOT_CLOSED',
    `当前纳入范围会留下引用缺口：${preview}${dependencies.length > 5 ? `；另有 ${dependencies.length - 5} 项` : ''}`,
    409,
    { dependencies },
  );
}

function assertDispositionClosure(
  sources: PackageFusionStoredSource[],
  plans: StoredSourcePlan[],
  dispositions: PackageFusionDisposition[],
): void {
  assertScopeClosure(sources, plans);
  const decisions = new Map(dispositions.map((item) => [dispositionIdentity(item), item.action]));
  const dependencies: Array<{ sourceId: string; from: string; requires: string }> = [];
  const action = (sourceId: string, kind: PackageFusionResourceKind, key: string) => (
    decisions.get(`${sourceId}\0${kind}\0${key}`) ?? 'exclude'
  );
  const need = (
    source: PackageFusionStoredSource,
    fromKind: PackageFusionResourceKind,
    fromKey: string,
    targetKind: PackageFusionResourceKind,
    keys: string[] | undefined,
  ) => {
    for (const key of keys ?? []) {
      if (action(source.id, targetKind, key) !== 'exclude') continue;
      dependencies.push({ sourceId: source.id, from: `${fromKind}/${fromKey}`, requires: `${targetKind}/${key}` });
    }
  };
  for (const source of sources) {
    for (const object of source.spec.objects) {
      if (action(source.id, 'object', object.key) !== 'preserve') continue;
      need(source, 'object', object.key, 'object', object.relations?.map((item) => item.target));
    }
    for (const operation of source.spec.operations) {
      if (action(source.id, 'operation', operation.key) !== 'preserve') continue;
      need(source, 'operation', operation.key, 'object', operation.relatedObjects);
      need(source, 'operation', operation.key, 'rule', operation.usesRules);
      need(source, 'operation', operation.key, 'metric', operation.usesMetrics);
      need(source, 'operation', operation.key, 'script', operation.script ? [operation.script.key] : []);
      const raw = source.archive.files['diagrams/processes.json'];
      if (raw) {
        const diagramByNode = new Map(normalizeFlowchartDocument(JSON.parse(raw)).diagrams.flatMap((diagram) => (
          diagram.nodes.map((node) => [node.id, diagram.id] as const)
        )));
        const nodeIds = (operation.evidence ?? []).flatMap((evidence) => (
          [...evidence.matchAll(/flow-node:\s*([^\s,;]+)/gu)].map((match) => match[1])
        ));
        for (const nodeId of nodeIds) {
          const diagramId = diagramByNode.get(nodeId);
          if (diagramId && action(source.id, 'diagram', diagramId) === 'exclude') {
            dependencies.push({ sourceId: source.id, from: `operation/${operation.key}`, requires: `diagram/${diagramId}` });
          }
        }
      }
    }
    for (const rule of source.spec.rules ?? []) {
      if (action(source.id, 'rule', rule.key) === 'preserve') {
        need(source, 'rule', rule.key, 'object', rule.appliesToObjects);
        need(source, 'rule', rule.key, 'operation', rule.appliesToOperations);
      }
    }
    for (const metric of source.spec.metrics ?? []) {
      if (action(source.id, 'metric', metric.key) === 'preserve') {
        need(source, 'metric', metric.key, 'object', metric.usesObjects);
      }
    }
    for (const script of source.spec.scripts ?? []) {
      if (action(source.id, 'script', script.key) === 'preserve') {
        need(source, 'script', script.key, 'operation', [script.boundOperation]);
      }
    }
  }
  if (!dependencies.length) return;
  const preview = dependencies.slice(0, 5).map((item) => `${item.from} 需要 ${item.requires}`).join('；');
  throw new PackageFusionServiceError(
    'FUSION_REFERENCE_NOT_CLOSED',
    `这些去向会留下引用缺口：${preview}${dependencies.length > 5 ? `；另有 ${dependencies.length - 5} 项` : ''}`,
    409,
    { dependencies },
  );
}

function filterSourceArchive(source: PackageFusionStoredSource, selectedScopes: FusionScope[]): PackageFusionStoredSource {
  const selected = new Set(selectedScopes);
  const spec: PackageSpec = {
    ...source.archive.spec,
    objects: selected.has('objects') ? source.archive.spec.objects : [],
    operations: selected.has('operations') ? source.archive.spec.operations : [],
    rules: selected.has('rules') ? source.archive.spec.rules ?? [] : [],
    metrics: selected.has('metrics') ? source.archive.spec.metrics ?? [] : [],
    scripts: selected.has('scripts') ? source.archive.spec.scripts ?? [] : [],
  };
  const keepPath = (path: string) => {
    const first = path.split('/')[0] as FusionScope;
    return !FUSION_SCOPES.includes(first) || selected.has(first);
  };
  const files = Object.fromEntries(Object.entries(source.archive.files).filter(([path]) => keepPath(path)));
  const rawFiles = Object.fromEntries(Object.entries(source.archive.rawFiles).filter(([path]) => keepPath(path)));
  const operationKeys = new Set(spec.operations.map((operation) => operation.key));
  const coverage: CoverageResult = {
    covered: (source.archive.coverage.covered ?? []).filter((item) => operationKeys.has(item.operation)),
    uncovered: [
      ...(source.archive.coverage.uncovered ?? []),
      ...(source.archive.coverage.covered ?? []).filter((item) => !operationKeys.has(item.operation)).map((item) => item.question),
    ],
  };
  return {
    ...source,
    spec,
    archive: { ...source.archive, spec, coverage, files, rawFiles },
  };
}

function storedSourcePlan(source: PackageFusionStoredSource, selectedScopes: FusionScope[]): StoredSourcePlan {
  return {
    sourceId: source.id,
    compilerSourceId: `src-${source.fingerprint.slice(0, 20)}`,
    fingerprint: source.fingerprint,
    filename: source.filename,
    packageId: source.packageId,
    name: source.name,
    version: source.version,
    selectedScopes: [...selectedScopes].sort((left, right) => FUSION_SCOPES.indexOf(left) - FUSION_SCOPES.indexOf(right)),
  };
}

function compareStoredSources(left: StoredSourcePlan, right: StoredSourcePlan): number {
  return left.compilerSourceId.localeCompare(right.compilerSourceId);
}

function reusablePlanMatches(
  row: FusionPlanRow,
  sources: StoredSourcePlan[],
  output: PackageFusionOptions,
): boolean {
  const plan = parsePlan(row.plan_json);
  return JSON.stringify(plan.sources) === JSON.stringify(sources)
    && JSON.stringify(plan.output) === JSON.stringify(output);
}

function buildInventory(
  sources: PackageFusionStoredSource[],
  sourcePlans: StoredSourcePlan[],
): PackageFusionInventoryEntry[] {
  const planById = new Map(sourcePlans.map((source) => [source.sourceId, source]));
  const entries: PackageFusionInventoryEntry[] = [];
  for (const source of sources) {
    const fingerprint = planById.get(source.id)!.fingerprint;
    const selected = new Set(planById.get(source.id)!.selectedScopes);
    const collections: Array<[PackageFusionResourceKind, FusionScope, Array<{ key: string }>]> = [
      ['object', 'objects', source.spec.objects],
      ['operation', 'operations', source.spec.operations],
      ['rule', 'rules', source.spec.rules ?? []],
      ['metric', 'metrics', source.spec.metrics ?? []],
      ['script', 'scripts', source.spec.scripts ?? []],
    ];
    for (const [kind, scope, resources] of collections) {
      for (const resource of resources) {
        const included = selected.has(scope);
        entries.push({
          sourceId: source.id,
          sourceFingerprint: fingerprint,
          kind,
          sourceKey: resource.key,
          decision: included ? null : 'exclude_scope',
          included,
        });
      }
    }
    const diagrams = source.archive.files['diagrams/processes.json'];
    if (diagrams) {
      for (const diagram of normalizeFlowchartDocument(JSON.parse(diagrams)).diagrams) {
        const included = selected.has('diagrams');
        entries.push({
          sourceId: source.id,
          sourceFingerprint: fingerprint,
          kind: 'diagram',
          sourceKey: diagram.id,
          decision: included ? null : 'exclude_scope',
          included,
        });
      }
    }
  }
  return entries.sort((left, right) => (
    left.sourceFingerprint.localeCompare(right.sourceFingerprint)
      || (RESOURCE_KIND_ORDER.get(left.kind)! - RESOURCE_KIND_ORDER.get(right.kind)!)
      || left.sourceKey.localeCompare(right.sourceKey)
  ));
}

function normalizeDispositions(
  value: unknown,
  inventory: PackageFusionInventoryEntry[],
): PackageFusionDisposition[] {
  if (value === undefined) return [];
  if (!Array.isArray(value)) throw new PackageFusionServiceError('FUSION_INVALID_PLAN', '逐元素去向必须是数组');
  const known = new Map(inventory.filter((entry) => entry.included !== false).map((entry) => [inventoryIdentity(entry), entry]));
  const seen = new Set<string>();
  return value.map((raw) => {
    const item = asRecord(raw, '元素去向');
    const action = dispositionAction(item.action);
    const disposition: PackageFusionDisposition = {
      sourceId: requiredText(item.sourceId, '来源 ID', 100),
      kind: resourceKind(item.kind),
      key: requiredText(item.key, '元素 key', 200),
      action,
      ...(action === 'canonicalize' ? {
        targetSourceId: requiredText(item.targetSourceId, '归并目标来源', 100),
        targetKey: requiredText(item.targetKey, '归并目标元素', 200),
        reason: requiredText(item.reason, '归并依据', 500),
      } : action === 'exclude' ? {
        reason: requiredText(item.reason, '排除原因', 500),
      } : {}),
    };
    const identity = dispositionIdentity(disposition);
    if (!known.has(identity)) {
      throw new PackageFusionServiceError('FUSION_INVALID_PLAN', `逐元素去向没有命中来源元素：${disposition.kind}/${disposition.key}`);
    }
    if (seen.has(identity)) {
      throw new PackageFusionServiceError('FUSION_INVALID_PLAN', `逐元素去向重复：${disposition.kind}/${disposition.key}`);
    }
    seen.add(identity);
    return disposition;
  }).sort((left, right) => dispositionIdentity(left).localeCompare(dispositionIdentity(right)));
}

function dispositionAction(value: unknown): FusionDecision {
  if (!['preserve', 'canonicalize', 'exclude'].includes(String(value))) {
    throw new PackageFusionServiceError('FUSION_INVALID_PLAN', '元素去向必须是独立保留、归并或排除');
  }
  return value as FusionDecision;
}

function validateDispositions(
  dispositions: PackageFusionDisposition[],
  inventory: PackageFusionInventoryEntry[],
): void {
  const inventoryByIdentity = new Map(inventory.filter((entry) => entry.included !== false).map((entry) => [
    inventoryIdentity(entry),
    entry,
  ]));
  const decisions = new Map(dispositions.map((item) => [dispositionIdentity(item), item]));
  for (const item of dispositions) {
    if (item.action !== 'canonicalize') continue;
    const targetIdentity = `${item.targetSourceId}\0${item.kind}\0${item.targetKey}`;
    const target = inventoryByIdentity.get(targetIdentity);
    if (!target || item.targetSourceId === item.sourceId) {
      throw new PackageFusionServiceError(
        'FUSION_INVALID_PLAN',
        `归并目标必须来自另一份来源且类型一致：${item.kind}/${item.key}`,
      );
    }
    const targetDecision = decisions.get(targetIdentity);
    if (targetDecision && targetDecision.action !== 'preserve') {
      throw new PackageFusionServiceError(
        'FUSION_INVALID_PLAN',
        `归并目标必须独立保留，不能再次归并或排除：${item.kind}/${item.targetKey}`,
      );
    }
  }
}

function applyDispositions(
  inventory: PackageFusionInventoryEntry[],
  dispositions: PackageFusionDisposition[],
): PackageFusionInventoryEntry[] {
  const byIdentity = new Map(dispositions.map((item) => [dispositionIdentity(item), item]));
  return inventory.map((entry) => {
    const disposition = byIdentity.get(inventoryIdentity(entry));
    return {
      ...entry,
      decision: entry.included !== false ? disposition?.action ?? null : 'exclude_scope',
      ...(disposition?.targetSourceId ? { targetSourceId: disposition.targetSourceId } : {}),
      ...(disposition?.targetKey ? { targetKey: disposition.targetKey } : {}),
      ...(disposition?.reason ? { reason: disposition.reason } : {}),
    };
  });
}

function normalizeConcepts(
  value: unknown,
  sources: Map<string, StoredSourcePlan>,
  inventory: PackageFusionInventoryEntry[],
): PackageFusionConcept[] {
  if (value === undefined) return [];
  if (!Array.isArray(value)) throw new PackageFusionServiceError('FUSION_INVALID_PLAN', '跨包概念必须是数组');
  const knownResources = new Set(inventory.filter((entry) => entry.included).map(inventoryIdentity));
  const conceptIds = new Set<string>();
  return value.map((raw) => {
    const concept = asRecord(raw, '跨包概念');
    if (!Array.isArray(concept.members) || concept.members.length < 2) {
      throw new PackageFusionServiceError('FUSION_INVALID_PLAN', '跨包概念至少需要两个来源成员');
    }
    const memberIds = new Set<string>();
    const members = concept.members.map((rawMember) => {
      const member = asRecord(rawMember, '概念成员');
      const sourceId = requiredText(member.sourceId, '概念来源 ID', 100);
      if (!sources.has(sourceId)) {
        throw new PackageFusionServiceError('FUSION_INVALID_PLAN', `概念成员不在融合来源中：${sourceId}`);
      }
      const relation = member.relation;
      if (!['equivalent', 'broader', 'narrower', 'overlap'].includes(String(relation))) {
        throw new PackageFusionServiceError('FUSION_INVALID_PLAN', '概念成员关系不正确');
      }
      const normalized = {
        sourceId,
        kind: resourceKind(member.kind),
        key: requiredText(member.key, '概念成员 key', 200),
        relation: relation as 'equivalent' | 'broader' | 'narrower' | 'overlap',
      };
      const identity = `${normalized.sourceId}\0${normalized.kind}\0${normalized.key}`;
      if (!knownResources.has(identity)) {
        throw new PackageFusionServiceError(
          'FUSION_INVALID_PLAN',
          `概念成员不在本次纳入范围：${normalized.kind}/${normalized.key}`,
        );
      }
      if (memberIds.has(identity)) {
        throw new PackageFusionServiceError('FUSION_INVALID_PLAN', `跨包概念成员重复：${normalized.kind}/${normalized.key}`);
      }
      memberIds.add(identity);
      return normalized;
    });
    if (new Set(members.map((member) => member.sourceId)).size < 2) {
      throw new PackageFusionServiceError('FUSION_INVALID_PLAN', '跨包概念必须包含至少两个不同来源');
    }
    const layer = concept.layer;
    if (layer !== 'common' && layer !== 'parameterized') {
      throw new PackageFusionServiceError('FUSION_INVALID_PLAN', '跨包概念层级必须是 common 或 parameterized');
    }
    if (concept.decision !== 'preserve_views') {
      throw new PackageFusionServiceError('FUSION_INVALID_PLAN', '跨包概念当前只能保留来源视图');
    }
    const id = requiredText(concept.id, '概念 ID', 200);
    if (conceptIds.has(id)) throw new PackageFusionServiceError('FUSION_INVALID_PLAN', `跨包概念 ID 重复：${id}`);
    conceptIds.add(id);
    return {
      id,
      title: requiredText(concept.title, '概念标题', 200),
      layer,
      members,
      decision: 'preserve_views',
      rationale: requiredText(concept.rationale, '裁决依据', 2_000),
    };
  });
}

function normalizeOutput(value: unknown): PackageFusionOptions {
  const output = asRecord(value, '融合输出');
  return {
    id: requiredText(output.id, '输出包 ID', 200),
    name: requiredText(output.name, '输出包名称', 200),
    description: requiredText(output.description, '输出包说明', 2_000),
    version: requiredText(output.version, '输出包版本', 100),
    ...(output.language === undefined ? {} : { language: requiredText(output.language, '输出语言', 50) }),
  };
}

function combineCoverage(
  sources: PackageFusionStoredSource[],
  compilerIdBySource: Map<string, string>,
  ledger: Array<{ sourceId: string; kind: PackageFusionResourceKind; sourceKey: string; fusedKey: string }>,
): CoverageResult {
  const covered = new Map<string, { question: string; operation: string }>();
  const uncovered = new Set<string>();
  const fusedOperation = new Map(ledger.filter((entry) => entry.kind === 'operation' && entry.fusedKey).map((entry) => [
    `${entry.sourceId}\0${entry.sourceKey}`,
    entry.fusedKey,
  ]));
  for (const source of sources) {
    const compilerSourceId = compilerIdBySource.get(source.id)!;
    for (const item of source.archive.coverage.covered ?? []) {
      const operation = fusedOperation.get(`${compilerSourceId}\0${item.operation}`);
      if (!operation) {
        uncovered.add(item.question);
        continue;
      }
      const normalized = { ...item, operation };
      covered.set(`${normalized.question}\0${normalized.operation}`, normalized);
    }
    for (const item of source.archive.coverage.uncovered ?? []) uncovered.add(item);
  }
  return {
    covered: [...covered.values()].sort((left, right) => (
      left.question.localeCompare(right.question) || left.operation.localeCompare(right.operation)
    )),
    uncovered: [...uncovered].sort(),
  };
}

function fusionChanges(
  currentPackageFiles: Record<string, Uint8Array>,
  currentSpec: PackageSpec,
  currentCoverage: CoverageResult,
  outputFiles: Record<string, string | Uint8Array>,
  outputSpec: PackageSpec,
  outputCoverage: CoverageResult,
): ArtifactChange[] {
  const encoder = new TextEncoder();
  const current = new Map<string, Uint8Array>([
    ['model/package-spec.json', encoder.encode(`${JSON.stringify(currentSpec, null, 2)}\n`)],
    ['model/coverage.json', encoder.encode(`${JSON.stringify(currentCoverage, null, 2)}\n`)],
    ...Object.entries(currentPackageFiles).map(([path, content]) => [`package/${path}`, content] as const),
  ]);
  const target = new Map<string, string | Uint8Array>([
    ['model/package-spec.json', `${JSON.stringify(outputSpec, null, 2)}\n`],
    ['model/coverage.json', `${JSON.stringify(outputCoverage, null, 2)}\n`],
    ...Object.entries(outputFiles).map(([path, content]) => [`package/${path}`, content] as const),
  ]);
  const changes: ArtifactChange[] = [];
  for (const [path, content] of [...target].sort(([left], [right]) => left.localeCompare(right))) {
    const bytes = typeof content === 'string' ? encoder.encode(content) : content;
    if (!sameBytes(current.get(path), bytes)) changes.push({ kind: 'write', path, content });
  }
  for (const path of [...current.keys()].filter((path) => path.startsWith('package/')).sort()) {
    if (!target.has(path)) changes.push({ kind: 'delete', path });
  }
  return changes;
}

function sameBytes(left: Uint8Array | undefined, right: Uint8Array): boolean {
  if (!left || left.byteLength !== right.byteLength) return false;
  return left.every((value, index) => value === right[index]);
}

function planView(row: FusionPlanRow): PackageFusionPlanView {
  const plan = parsePlan(row.plan_json);
  const ledger = JSON.parse(row.ledger_json) as PackageFusionInventoryEntry[] | Record<string, unknown>;
  const unresolvedCount = Array.isArray(ledger)
    ? ledger.filter((entry) => entry.decision === null).length
    : 0;
  return {
    id: row.id,
    projectId: row.project_id,
    expectedRevision: row.expected_revision_oid,
    status: row.status,
    sources: plan.sources,
    output: plan.output,
    concepts: plan.concepts,
    ledger,
    unresolvedCount,
    ...(row.candidate_id ? { candidateId: row.candidate_id } : {}),
    ...(row.output_digest ? { outputDigest: row.output_digest } : {}),
    ...(row.quality_json ? { quality: JSON.parse(row.quality_json) } : {}),
    ...(row.error ? { error: row.error } : {}),
    createdBy: row.created_by,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

function parsePlan(raw: string): StoredFusionPlan {
  return JSON.parse(raw) as StoredFusionPlan;
}

function parseInventory(raw: string): PackageFusionInventoryEntry[] {
  const value = JSON.parse(raw);
  if (!Array.isArray(value)) {
    throw new PackageFusionServiceError('FUSION_PLAN_IMMUTABLE', '融合计划已经生成候选，不能再修改', 409);
  }
  return value as PackageFusionInventoryEntry[];
}

function withPlanDigest(plan: Omit<StoredFusionPlan, 'digest'>): StoredFusionPlan {
  const digest = createHash('sha256').update(JSON.stringify(plan)).digest('hex');
  return { ...plan, digest };
}

function dispositionIdentity(value: PackageFusionDisposition): string {
  return `${value.sourceId}\0${value.kind}\0${value.key}`;
}

function inventoryIdentity(value: PackageFusionInventoryEntry): string {
  return `${value.sourceId}\0${value.kind}\0${value.sourceKey}`;
}

function resourceKind(value: unknown): PackageFusionResourceKind {
  if (!RESOURCE_KINDS.includes(value as PackageFusionResourceKind)) {
    throw new PackageFusionServiceError('FUSION_INVALID_PLAN', `元素类型不正确：${String(value ?? '')}`);
  }
  return value as PackageFusionResourceKind;
}

function uniqueStrings(value: unknown, label: string, min: number, max: number): string[] {
  if (!Array.isArray(value) || value.length < min || value.length > max) {
    throw new PackageFusionServiceError('FUSION_INVALID_PLAN', `${label}数量必须在 ${min} 到 ${max} 之间`);
  }
  const values = value.map((item) => requiredText(item, label, 100));
  if (new Set(values).size !== values.length) {
    throw new PackageFusionServiceError('FUSION_INVALID_PLAN', `${label}不能重复`);
  }
  return values;
}

function requiredText(value: unknown, label: string, max: number): string {
  if (typeof value !== 'string' || !value.trim() || value.trim().length > max) {
    throw new PackageFusionServiceError('FUSION_INVALID_PLAN', `${label}不能为空且不能超过 ${max} 个字符`);
  }
  return value.trim();
}

function asRecord(value: unknown, label: string): Record<string, any> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new PackageFusionServiceError('FUSION_INVALID_PLAN', `${label}格式不正确`);
  }
  return value as Record<string, any>;
}

import { createHash, randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { getPackageArtifactStore } from '../artifacts/runtime.js';
import { runProjectWriteExclusive } from '../artifacts/project-write-lock.js';
import {
  backgroundAuthSessions,
  type BackgroundAuthSessionRegistry,
} from '../auth/background-session.js';
import type {
  PackageArtifactFiles,
  PackageCandidateRevision,
} from '../artifacts/package-store.js';
import type { ArtifactChange, ArtifactFileDiff } from '../artifacts/repository.js';
import { getDb, nowIso } from '../db/index.js';
import {
  applyMaterialInvalidation,
  resolveMaterialImpactScope,
  type AffectedKnowledgeKind,
  type AffectedKnowledgeResource,
} from '../documents/material-impact.js';
import { getDocumentRawContent, listDocuments, updateDocumentDirectives } from '../documents/repo.js';
import {
  findMaterialPurposeReviewProblems,
  materialPurposeReviewProblemFingerprint,
  type MaterialPurposeReviewProblem,
  MaterialPurposeReviewStore,
} from '../documents/material-purpose-review.js';
import { parseFullTableDocumentIsolated } from '../documents/parse-service.js';
import { validateTermTables } from '../workflow/term-table.js';
import { config } from '../config.js';
import { getProjectDataSource } from '../evaluation/data-source-repo.js';
import { EvaluationAuthenticationError } from '../evaluation/auth-session.js';
import {
  getProjectPackageDataSourceSelection,
  normalizePackageDataSourceSelection,
} from '../workflow/package-data-source-selection.js';
import { getRunningEvaluationDatabaseImportRun } from '../evaluation/eval-db-import-repo.js';
import { redactEvaluationDatabaseError, resolveEvaluationDatabaseTarget } from '../evaluation/evaluation-database-target.js';
import {
  cancelEvaluation,
  isEvaluationActive,
  startCurrentEvaluation,
  startEvaluation,
} from '../evaluation/engine.js';
import { projectEvaluationDatabaseName } from '../evaluation/project-evaluation-database.js';
import { advanceStage, getProject } from '../projects/repo.js';
import type {
  DataDictTable,
  PackageSpec,
  Project,
  SpecField,
  SpecObject,
  SpecOperation,
  SpecRule,
  SpecScript,
  TableSourceRef,
  MaterialPurpose,
} from '../types.js';
import {
  KNOWLEDGE_MARKDOWN_VALIDATOR_VERSION,
  validateKnowledgeMarkdownFile,
  validateKnowledgeMarkdownContract,
} from '../workflow/knowledge-markdown-contract.js';
import {
  PACKAGE_CONTRACT_VERSION,
  PACKAGE_FORMAT_VALIDATOR_VERSION,
  PACKAGE_REQUIRED_FILES,
  mergeAgentManifest,
  packageGenerationContract,
} from '../workflow/package-contract.js';
import { runFormatChecks, type CheckFinding } from '../workflow/package-check.js';
import { renderObjectProjection, renderPackage } from '../workflow/package-render.js';
import {
  expectedSchemaObjects,
  OBJECT_DATA_MAPPING_PATH,
  packageNeedsObjectDataMapping,
  schemaObjectPolicyIssues,
  schemaTables,
} from '../workflow/schema-object-policy.js';
import { preservePhysicalSchemaSemantics } from '../workflow/schema-manifest.js';
import {
  analyzeSchemaSemantics,
  type SchemaSemanticStatus,
} from '../workflow/schema-semantics.js';
import {
  collectPreviousObjectRelations,
  suggestObjectRelations as suggestObjectRelationsFromSpec,
  type PreviousObjectRelation,
} from '../workflow/object-relation-suggestions.js';
import { normalizeRelationName, relationNameProblem } from '../workflow/relation-name.js';
import { tableSourcesFor } from '../workflow/source-references.js';
import { validateKey, validateSpecKeys } from '../workflow/keys.js';
import { canonicalizePackageSpec } from '../workflow/package-spec.js';
import {
  MERMAID_SOURCE_VALIDATOR_VERSION,
  validateMermaidSource,
} from '../workflow/mermaid-source-contract.js';
import { previewOntologyObjectMutation } from '../workflow/ontology-object-model-mutation.js';
import {
  parseRenderedPackageFiles,
  parseRenderedScriptFile,
} from '../workflow/package-import.js';
import { buildScriptCliArguments } from '../workflow/script-cli.js';
import { scriptTestSourceHash, untestedScriptPaths } from '../workflow/script-test-receipts.js';
import {
  buildPythonScriptScaffold,
  isCoreManagedPythonScaffold,
} from '../workflow/python-script-scaffold.js';
import {
  applyPythonSourceEdits,
  selectPythonSourceSymbols,
  type PythonSourceEdit,
} from '../evaluation/python-source-patch.js';
import {
  isReleaseCandidateVersion,
  nextValidationCandidateVersion,
  promoteReleaseCandidate,
} from '../workflow/package-version-lifecycle.js';
import { ensureFullDocumentSummaries } from '../workflow/material-analysis-map-reduce.js';
import { buildTaskLlm } from '../llm/provider.js';
import { identifyScriptCandidates } from '../ai-construction/deterministic-capability.js';
import {
  getRound,
  latestEffectiveEvaluation,
  listResults,
  listResultTraces,
  listTestcases,
} from '../workflow/m3-repo.js';
import {
  validateProductLedger,
  type ProductLedger,
  type ProductLedgerValidation,
} from './product-ledger.js';
import { issueAgentRunCredential } from './run-credential.js';
import { runScriptCasesInJiuwenbox } from './sandbox.js';
import {
  StudioReadError,
  StudioReadService,
  resolveStudioWorkingSpec,
  type StudioElement,
} from './studio-read-service.js';
import { StudioReviewService } from './studio-review-service.js';
import {
  StudioWriteError,
  StudioWriteService,
  type StudioMutationResponse,
} from './studio-write-service.js';
import {
  packageFusionScopeDependencyGraph,
  PackageFusionService,
  PackageFusionServiceError,
} from './package-fusion-service.js';
import {
  analyzePackageMerge as analyzeMergePackage,
  getPackageFusionStoredSource,
  getPackageMergeSource,
  listPackageMergeSources,
  storePackageMergeSource,
  type PackageMergeSource,
} from './package-merge.js';
import type { ImportedPackageArchive } from '../workflow/package-import.js';
import { ValidationReportStore } from './validation-report-store.js';
import {
  compileDomainPatchActions,
  compileObjectPatchActions,
  domainResourceHash,
  domainResourcePayload,
  DomainPatchError,
  type DomainKind,
} from './domain-resource-mutation.js';
import {
  ChangeControlError,
  ChangeControlStore,
  CHANGE_PLAN_DIRECT_TOOLS,
  CHANGE_SET_DOMAIN_TOOLS,
  parseChangeSetActions,
  type ChangeSetActionInput,
} from './change-control.js';
import {
  COMPILED_RESOURCES_GROUP,
  isHiddenCandidateGroup,
  LEGACY_RUNTIME_GROUP,
  RUNTIME_GROUP,
} from './candidate-change-scope.js';
import { publicKnowledgePackageFiles } from '../workflow/package-file-scope.js';
import { SchemaProfileCache, SCHEMA_PROFILE_ANALYSIS_VERSION } from '../workflow/schema-profile-cache.js';

const WORKSPACE_LEASE_MS = 30 * 60 * 1000;
const MAX_FILE_CHARS = 1_000_000;
const INTERNAL_SPEC_CHANGE_PATH = '.modeling/__compiled-package-spec.json';
const PACKAGE_REQUIRED_FILE_SET: ReadonlySet<string> = new Set(PACKAGE_REQUIRED_FILES);
const AGENT_REQUIRED_FILES: ReadonlySet<string> = new Set([
  '.modeling/goal.md',
  ...PACKAGE_REQUIRED_FILES,
]);
const CHANGE_GROUP_ID_PATTERN = /^[a-z][a-z0-9_.-]{0,63}$/u;
const DOMAIN_TOOL_CONTRACT_VERSION = 'domain-patch-v2';
const DOMAIN_TOOL_NAMES = new Set([
  'list_domain_resources', 'get_domain_resource',
  'create_object', 'create_rule', 'create_operation', 'create_metric',
  'patch_object', 'patch_rule', 'patch_operation', 'patch_metric',
  'sync_schema_objects', 'stage_schema_semantic_changes', 'confirm_schema_semantics',
  'suggest_object_relations',
  'patch_object_source_binding',
  'preview_domain_delete', 'delete_domain_resource',
]);
const CHANGE_CONTROL_TOOLS = new Set([
  'create_change_plan', 'get_change_plan', 'revise_change_plan',
  'stage_change_set', 'get_change_set_receipt', 'validate_change_set', 'discard_change_set',
]);
const MANAGED_DOMAIN_MARKDOWN = /^(?:objects|operations|rules|metrics)\/(?:index|[^/]+)\.md$/u;
const MANAGED_SCRIPT_PATH = /^scripts\/(?:index\.md|[^/]+\.md|[A-Za-z0-9_.-]+\.py)$/u;
const MANAGED_SCRIPT_SOURCE_PATH = /^scripts\/[A-Za-z0-9_.-]+\.py$/u;
type GateResult = { pass: boolean; blockers: string[]; warnings?: string[] };
type GateResults = Record<string, GateResult>;

const GATE_LABELS: Record<string, string> = {
  materials_ready: '资料准备',
  goal_confirmed: '建模目标确认',
  process_confirmed: '业务流程确认',
  knowledge_confirmed: '知识内容确认',
  product_ledger_verified: '产品清单台账',
  deterministic_confirmed: '确定性计算范围确认',
  modeling_complete: '建模完成',
  ready_for_eval: '评测准备',
  evaluation_passed: '正式评测',
  releasable: '发布条件',
};

type Tier = 'caliber' | 'structural' | 'index' | 'script';

interface WorkspaceRow {
  project_id: string;
  run_id: string;
  owner: string;
  base_revision_oid: string;
  package_data_source_type: 'nl2sql' | 'neo4j';
  candidate_id: string | null;
  lease_expires_at: string;
}

interface CandidateValidationReceipt {
  candidateRevision: string;
  baseRevision: string;
  candidateHash: string;
  validatorVersion: string;
  validatedAt: string;
}

interface ChangeRow {
  path: string;
  kind: 'write' | 'delete';
  content: string | null;
  group_id: string;
  evidence_json: string;
  reason: string | null;
}

interface ChangeGroupInput {
  id: string;
  tier: Tier;
  title: string;
  impact?: string[];
  warning?: string;
}

interface MaterialPurposeReviewValidation {
  baseline: MaterialPurposeReviewProblem[];
  candidate: MaterialPurposeReviewProblem[];
  introduced: MaterialPurposeReviewProblem[];
}

interface DeterministicCapabilityCandidateView {
  operation_key: string;
  title: string;
  confidence: 'medium' | 'high';
  reason: string;
  suggested_contract?: string;
  composes_operation_keys: string[];
  already_covered: boolean;
  bound_script_key: string | null;
}

interface DeterministicCapabilityView {
  scope_hash: string;
  status: 'not_required' | 'covered' | 'decision_required' | 'implementation_required' | 'ready';
  candidates: DeterministicCapabilityCandidateView[];
  selected_operation_keys: string[];
  skipped_operation_keys: string[];
  missing_script_bindings: string[];
  missing_script_tests: string[];
  decision: null | {
    reason: string;
    decided_by: string;
    decided_at: string;
  };
  display: { text: string };
}

interface DomainFailureReceiptContext {
  mutationId: string;
  requestHash: string;
  workingRevision: string;
  dependencyRevision: string;
}

export class AgentCoreError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly next: string[] = [],
    public readonly statusCode = 409,
    public readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = 'AgentCoreError';
  }
}

export interface AgentToolContext {
  project: Project;
  runId: string;
  username: string;
}

export class AgentCoreService {
  private readonly db: DatabaseSync;
  private readonly artifacts = getPackageArtifactStore();
  private readonly packageFusion: PackageFusionService;
  private readonly validationReports: ValidationReportStore;
  private readonly studioWrites: StudioWriteService;
  private readonly studioReviews: StudioReviewService;
  private readonly changeControl: ChangeControlStore;
  private readonly materialPurposeReviews: MaterialPurposeReviewStore;
  private readonly schemaSemanticStatusCache = new Map<string, SchemaSemanticStatus>();
  private readonly schemaProfileCache: SchemaProfileCache;

  constructor(
    db: DatabaseSync = getDb(),
    private readonly authSessions: BackgroundAuthSessionRegistry = backgroundAuthSessions,
  ) {
    this.db = db;
    this.packageFusion = new PackageFusionService(db, this.artifacts);
    this.validationReports = new ValidationReportStore(db);
    this.studioWrites = new StudioWriteService(db, this.artifacts);
    this.studioReviews = new StudioReviewService(db, this.artifacts);
    this.changeControl = new ChangeControlStore(db);
    this.materialPurposeReviews = new MaterialPurposeReviewStore(db);
    this.schemaProfileCache = new SchemaProfileCache(db);
  }

  async workbenchSnapshot(project: Project, username: string) {
    await this.artifacts.ensureProject(project.id);
    let current = await this.artifacts.current(project.id);
    if (!current) {
      current = await this.artifacts.initializeDraftPackage(project.id, {
        id: `project-${project.id.replace(/^pj_/, '')}`,
        name: project.name,
        description: project.background || `${project.name}知识包草稿`,
      });
    }
    const readableWorkspace = this.readableWorkspace(project.id, username, current.revision.oid);
    const studioChanges = readableWorkspace ? this.changes(readableWorkspace) : [];
    const reviewableStudioChanges = studioChanges.filter((change) => !isHiddenCandidateGroup(change.group_id));
    const packageFiles = readableWorkspace
      ? await this.overlay(readableWorkspace)
      : await this.artifacts.readPackageFiles(project.id, current.revision.oid);
    const documents = listDocuments(project.id);
    const materials = this.listDocuments(project.id).documents.map((summary) => {
      const document = documents.find((item) => item.id === summary.id)!;
      return { ...summary, refs: referringFiles(packageFiles.files, document).length };
    });
    return {
      project,
      revision: packageFiles.revision.oid,
      packageVersion: packageFiles.spec.version,
      packageStatus: packageFiles.spec.status,
      candidateRevision: readableWorkspace?.candidate_id ?? null,
      studioPendingCount: reviewableStudioChanges.length ? 1 : 0,
      files: Object.keys(packageFiles.files).sort(),
      binaryFiles: packageFiles.binaryFiles,
      materials,
      confirmPolicy: this.workbenchConfirmPolicy(project.id),
      packageDataSource: getProjectPackageDataSourceSelection(project.id),
    };
  }

  /**
   * 阶段轨的门禁投影：只回答“每道闸过没过、卡在哪”。
   * 阶段顺序和标签由 modeling-agent 的 PHASE_LABELS 给，这里不复制第二份。
   * 用项目自己的候选工作区而不是当前用户的——门禁是项目级事实，
   * 换个人看不该得出不同结论。
   */
  async workbenchProgress(project: Project) {
    await this.artifacts.ensureProject(project.id);
    const workspace = this.workspace(project.id);
    if (!workspace) return { available: false, revision: '', gates: [] };
    const gates = await this.computeGates(workspace);
    return {
      available: true,
      revision: workspace.base_revision_oid,
      gates: Object.entries(gates).map(([id, gate]) => ({
        id,
        label: GATE_LABELS[id] || id,
        pass: gate.pass,
        blockers: gate.blockers,
        warnings: gate.warnings ?? [],
      })),
    };
  }

  async workbenchSchemaSemantics(
    project: Project,
    username: string,
    args: Record<string, unknown> = {},
  ) {
    await this.artifacts.ensureProject(project.id);
    return this.getSchemaSemantics({
      project,
      username,
      runId: this.workspace(project.id)?.run_id ?? 'schema-semantics-read',
    }, args);
  }

  async workbenchStudioFiles(project: Project, username: string) {
    await this.artifacts.ensureProject(project.id);
    let current = await this.artifacts.current(project.id);
    if (!current) {
      current = await this.artifacts.initializeDraftPackage(project.id, {
        id: `project-${project.id.replace(/^pj_/, '')}`,
        name: project.name,
        description: project.background || `${project.name}知识包草稿`,
      });
    }
    const readableWorkspace = this.readableWorkspace(project.id, username, current.revision.oid);
    const workspaceChanges = readableWorkspace ? this.changes(readableWorkspace) : [];
    const packageFiles = readableWorkspace
      ? await this.overlay(readableWorkspace, workspaceChanges)
      : await this.artifacts.readPackageFiles(project.id, current.revision.oid);
    const studioFiles = { ...packageFiles.files };
    const internalSpecChange = workspaceChanges.find((change) => change.path === INTERNAL_SPEC_CHANGE_PATH);
    if (internalSpecChange?.kind === 'write') {
      studioFiles[INTERNAL_SPEC_CHANGE_PATH] = internalSpecChange.content ?? '';
    } else {
      const effectiveSpec = resolveStudioWorkingSpec(packageFiles.spec, studioFiles);
      studioFiles[INTERNAL_SPEC_CHANGE_PATH] = `${JSON.stringify(effectiveSpec, null, 2)}\n`;
    }
    return {
      workingRevision: workspaceChanges.length
        ? workingContentRevision(current.revision.oid, studioFiles)
        : current.revision.oid,
      baseRevision: current.revision.oid,
      candidateRevision: readableWorkspace?.candidate_id ?? null,
      hasReviewableChanges: workspaceChanges.some((change) => !isHiddenCandidateGroup(change.group_id)),
      files: studioFiles,
    };
  }

  workbenchConfirmPolicy(projectId: string) {
    return [
      { tier: 'caliber', mode: 'ask', locked: true, label: '业务口径' },
      { tier: 'structural', mode: this.autoPolicy(projectId, 'structural') ? 'auto' : 'ask', locked: false, label: '结构变更' },
      { tier: 'index', mode: 'auto', locked: true, label: '索引同步' },
      { tier: 'script', mode: 'ask', locked: true, label: '脚本' },
    ];
  }

  respondChangePlan(project: Project, username: string, planId: string, approved: boolean) {
    try {
      const plan = this.changeControl.respondPlan({
        projectId: project.id,
        planId,
        username,
        approved,
      });
      return {
        plan,
        display: {
          text: approved
            ? '已批准这份建模计划，可以按计划开始生成'
            : '已拒绝这份建模计划，当前计划不会产生修改',
        },
      };
    } catch (error) {
      throw normalizeChangeControlError(error);
    }
  }

  safeTerminateChangePlan(project: Project, username: string, planId: string) {
    try {
      const terminated = this.changeControl.safeTerminatePlan({
        projectId: project.id,
        planId,
        actor: username,
      });
      return {
        ...terminated,
        display: {
          text: '已安全终止失效建模计划，候选内容与审计记录保留，可以重新规划',
        },
      };
    } catch (error) {
      throw normalizeChangeControlError(error);
    }
  }

  workbenchPackageSources(project: Project) {
    return {
      sources: listPackageMergeSources(this.db, project.id).map(packageSourceSummary),
    };
  }

  addWorkbenchPackageSource(
    project: Project,
    username: string,
    filename: string,
    archive: ImportedPackageArchive,
  ) {
    const source = storePackageMergeSource(this.db, {
      projectId: project.id,
      filename,
      uploadedBy: username,
      archive,
    });
    return {
      source: packageSourceSummary(source),
      display: { text: `来源包《${source.name}》${source.version} 已解析并作为只读来源保存，可继续融合` },
    };
  }

  setWorkbenchConfirmPolicy(projectId: string, rawTier: unknown, rawMode: unknown) {
    const tier = String(rawTier ?? '') as Tier;
    const mode = String(rawMode ?? '');
    if (tier !== 'structural' || !['ask', 'auto'].includes(mode)) {
      throw new AgentCoreError(
        'POLICY_LOCKED',
        '只有结构变更可以在“问我”和“不用问我”之间切换',
        ['保留业务口径和脚本为必须确认，索引同步保持自动'],
        400,
      );
    }
    this.db.prepare(`INSERT INTO agent_confirm_policy(project_id, tier, mode, updated_at)
      VALUES (?,?,?,?) ON CONFLICT(project_id, tier) DO UPDATE SET
      mode = excluded.mode, updated_at = excluded.updated_at`)
      .run(projectId, tier, mode, nowIso());
    return { policy: this.workbenchConfirmPolicy(projectId) };
  }

  async workbenchFile(project: Project, username: string, rawPath: string) {
    const path = packagePath(rawPath);
    const current = await this.artifacts.current(project.id);
    if (!current) throw new AgentCoreError('PACKAGE_NOT_FOUND', '当前没有知识包', [], 404);
    const workspace = this.readableWorkspace(project.id, username, current.revision.oid);
    if (workspace) return this.readFile(workspace, { path });
    const file = await this.artifacts.readFile(project.id, `package/${path}`, current.revision.oid);
    return {
      path,
      content: file.content,
      revision: file.revision.oid,
      candidate: false,
    };
  }

  async workbenchDiff(project: Project, username: string) {
    const workspace = this.workspace(project.id);
    if (!workspace) return { candidateRevision: null, files: [] };
    if (workspace.owner !== username) {
      return { candidateRevision: null, files: [] };
    }
    const current = await this.artifacts.current(project.id);
    if (!current || workspace.base_revision_oid !== current.revision.oid) {
      return { candidateRevision: null, files: [] };
    }
    let files: ArtifactFileDiff[];
    if (workspace.candidate_id) {
      files = (await this.artifacts.previewCandidateRevision(
        workspace.project_id,
        workspace.candidate_id,
      )).diff;
    } else {
      const base = await this.artifacts.readPackageFiles(
        workspace.project_id,
        workspace.base_revision_oid,
      );
      files = candidateDiff(base.files, this.changes(workspace));
    }
    return {
      candidateRevision: workspace.candidate_id,
      files: files.map(plainDiff),
    };
  }

  workbenchMaterial(project: Project, documentId: string) {
    const document = listDocuments(project.id).find((item) => item.id === documentId);
    if (!document) throw new AgentCoreError('DOCUMENT_NOT_FOUND', '资料不存在', [], 404);
    return {
      id: document.id,
      filename: document.filename,
      kind: document.kind,
      parseStatus: document.parseStatus,
      parseError: document.parseError ?? null,
      textContent: document.textContent ?? '',
      tables: document.tables ?? [],
      revisionNo: document.revisionNo ?? 1,
      purpose: document.purpose ?? 'auto',
      readingMode: document.readingMode ?? 'on_demand',
      directiveRevision: document.directiveRevision ?? 0,
      purposeReview: this.materialPurposeReviews.get(document.id),
    };
  }

  workbenchEvidenceLocate(project: Project, rawReference: string) {
    const reference = rawReference.normalize('NFKC').trim();
    const body = reference.startsWith('material:') ? reference.slice('material:'.length) : reference;
    const hash = body.indexOf('#');
    const source = (hash >= 0 ? body.slice(0, hash) : body).trim();
    const anchor = (hash >= 0 ? body.slice(hash + 1) : '').trim();
    const document = listDocuments(project.id).find((item) => (
      item.id === source || item.sourceKey === source || item.filename === source
    ));
    if (!document) throw new AgentCoreError('EVIDENCE_NOT_FOUND', '证据引用对应的资料不存在', [], 404);
    const content = document.textContent ?? '';
    const offset = locateAnchor(content, anchor);
    return {
      reference,
      documentId: document.id,
      filename: document.filename,
      anchor,
      offset,
      excerpt: content.slice(Math.max(0, offset - 180), Math.min(content.length, offset + 520)),
    };
  }

  async workbenchMaterialReferrers(
    project: Project,
    username: string,
    documentId: string,
    anchor = '',
  ) {
    const document = listDocuments(project.id).find((item) => item.id === documentId);
    if (!document) throw new AgentCoreError('DOCUMENT_NOT_FOUND', '资料不存在', [], 404);
    const current = await this.artifacts.current(project.id);
    const workspace = current
      ? this.readableWorkspace(project.id, username, current.revision.oid)
      : null;
    const packageFiles = workspace
      ? await this.overlay(workspace)
      : await this.artifacts.readPackageFiles(project.id, current?.revision.oid);
    const referrers = referringFiles(packageFiles.files, document, anchor);
    return { documentId, anchor, referrers, count: referrers.length };
  }

  async invoke(context: AgentToolContext, tool: string, args: Record<string, unknown>) {
    try {
      return await this.invokeRaw(context, tool, args);
    } catch (error) {
      if (!(error instanceof AgentCoreError)
        || error.details.retryable !== true
        || !Array.isArray(error.details.findings)) {
        throw error;
      }
      const stored = this.validationReports.record(
        context.project.id,
        context.runId,
        tool,
        {
          code: error.code,
          reason: error.message,
          next: error.next,
          details: error.details,
        },
      );
      throw new AgentCoreError(
        stored.code,
        stored.reason,
        stored.next,
        error.statusCode,
        stored.details,
      );
    }
  }

  private async invokeRaw(context: AgentToolContext, tool: string, args: Record<string, unknown>) {
    if (DOMAIN_TOOL_NAMES.has(tool) || CHANGE_CONTROL_TOOLS.has(tool)) this.adoptDomainToolContract(context);
    this.assertObjectSourceToolMode(context.project.id, tool, args);
    if (CHANGE_SET_DOMAIN_TOOLS.has(tool)
      && this.changeControl.activePlan(context.project.id, context.runId)) {
      throw new AgentCoreError(
        'CHANGE_PLAN_REQUIRES_CHANGE_SET',
        '当前运行已经采用受控建模计划，领域修改必须作为原子 Change Set 提交',
        ['读取当前计划，把对应任务和领域动作一次交给 stage_change_set'],
        409,
        { retryable: false, required_tool: 'stage_change_set' },
      );
    }
    if (tool === 'claim_candidate_workspace') return this.claimCandidateWorkspace(context);
    if (tool === 'get_package_contract') return packageGenerationContract();
    if (tool === 'project_data_source_status') return this.projectDataSourceStatus(context.project.id);
    if (tool === 'get_material_analysis_progress') return this.materialAnalysisProgress(context.project.id, args);
    if (tool === 'evaluation_status') return this.evaluationStatus(context.project.id);
    if (tool === 'cancel_evaluation') return this.cancelEvaluation(context.project.id, args);
    switch (tool) {
      case 'create_change_plan': return this.createChangePlan(context, args);
      case 'get_change_plan': return this.getChangePlan(context, args);
      case 'revise_change_plan': return this.reviseChangePlan(context, args);
      case 'get_change_set_receipt': return this.getChangeSetReceipt(context, args);
      case 'list_domain_resources': return this.listDomainResources(context, args);
      case 'get_domain_resource': return this.getDomainResource(context, args);
      case 'get_schema_semantics': return this.getSchemaSemantics(context, args);
      case 'preview_domain_delete': return this.previewDomainDelete(context, args);
      case 'get_script_contract': return this.getScriptContract(context, args);
      case 'get_script_edit_context': return this.getScriptEditContext(context, args);
      case 'list_ontology_objects': return this.listOntologyObjects(context, args);
      case 'get_ontology_object': return this.getOntologyObject(context, args);
      case 'get_ontology_object_review_status': return this.getOntologyObjectReviewStatus(context, args);
      case 'list_ontology_object_audit_records': return this.listOntologyObjectAuditRecords(context, args);
      case 'validate_ontology_object_patch': return this.validateOntologyObjectPatch(context, args);
      default: break;
    }
    const workspace = await this.ensureWorkspace(context);
    switch (tool) {
      case 'list_documents': return this.listDocuments(context.project.id);
      case 'get_product_ledger': return this.getProductLedger(workspace);
      case 'stage_product_ledger': return this.stageProductLedger(workspace, args);
      case 'read_document': return this.readDocument(context.project.id, args);
      case 'set_material_directive': return this.setMaterialDirective(context.project.id, args);
      case 'read_documents_fully': return this.readDocumentsFully(context, args);
      case 'search_documents': return this.searchDocuments(context.project.id, args);
      case 'list_tables': return this.listTables(context.project.id);
      case 'get_schema_overview': return this.getSchemaOverview(context.project.id);
      case 'describe_table': return this.describeTable(context.project.id, args);
      case 'read_table_rows': return this.readTableRows(context.project.id, args);
      case 'apply_ontology_object_patch': return this.applyOntologyObjectPatch(context, args);
      case 'create_object': return this.createDomainResource(context, args, 'object');
      case 'create_rule': return this.createDomainResource(context, args, 'rule');
      case 'create_operation': return this.createDomainResource(context, args, 'operation');
      case 'create_metric': return this.createDomainResource(context, args, 'metric');
      case 'patch_object': return this.patchObjectResource(context, args);
      case 'patch_rule': return this.patchDomainResource(context, args, 'rule');
      case 'patch_operation': return this.patchDomainResource(context, args, 'operation');
      case 'patch_metric': return this.patchDomainResource(context, args, 'metric');
      case 'patch_object_source_binding': return this.runPlannedDirectTask(
        context,
        tool,
        args,
        domainTargetKey(args.resource_id),
        () => this.patchObjectSourceBinding(workspace, context, args),
      );
      case 'sync_schema_objects': return this.runPlannedDirectTask(
        context,
        tool,
        args,
        '__schema__',
        () => this.syncSchemaObjects(workspace, context, args),
      );
      case 'stage_schema_semantic_changes': return this.runPlannedProgressTask(
        context,
        tool,
        args,
        '__schema__',
        () => this.stageSchemaSemanticChanges(workspace, context, args),
      );
      case 'confirm_schema_semantics': return this.runPlannedDirectTask(
        context,
        tool,
        args,
        '__schema__',
        () => this.confirmSchemaSemantics(context, args),
      );
      case 'suggest_object_relations': return this.suggestObjectRelations(workspace, context, args);
      case 'delete_domain_resource': return this.runPlannedDirectTask(
        context,
        tool,
        args,
        singleDomainTargetKey(args.resource_ids),
        () => this.deleteDomainResource(context, args),
      );
      case 'stage_change_set': return this.stageChangeSet(context, args);
      case 'validate_change_set': return this.validateChangeSet(workspace, context, args);
      case 'discard_change_set': return this.discardChangeSet(workspace, context, args);
      case 'analyze_deterministic_capabilities': return this.analyzeDeterministicCapabilities(workspace);
      case 'confirm_deterministic_scope': return this.confirmDeterministicScope(workspace, context, args);
      case 'define_script_contract': return this.defineScriptContract(context, args);
      case 'stage_script_source': return this.runPlannedDirectTask(
        context,
        tool,
        args,
        text(args.script_key, 'script_key'),
        () => this.stageScriptSource(workspace, context, args),
      );
      case 'stage_script_patch': return this.runPlannedDirectTask(
        context,
        tool,
        args,
        text(args.script_key, 'script_key'),
        () => this.stageScriptPatch(workspace, context, args),
      );
      case 'submit_ontology_object_review': return this.submitOntologyObjectReview(context, args);
      case 'stage_tabular_dataset': return this.stageTabularDataset(workspace, context, args);
      case 'list_dir': return this.listDir(workspace, args);
      case 'read_file': return this.readFile(workspace, args, context);
      case 'get_validation_summary': return this.validationSummary(context, args);
      case 'get_validation_findings': return this.validationFindings(context, args);
      case 'get_schema_mapping_plan': return this.schemaMappingPlan(workspace);
      case 'upsert_object': return this.upsertObject(workspace, context, args);
      case 'upsert_rule': return this.upsertRule(workspace, context, args);
      case 'upsert_operation': return this.upsertOperation(workspace, context, args);
      case 'bind_object_source': return this.bindObjectSource(workspace, context, args);
      case 'bind_object_sources': return this.bindObjectSources(workspace, context, args);
      case 'write_file': return this.writeFile(workspace, context, args);
      case 'delete_file': return this.deleteFile(workspace, context, args);
      case 'discard_rejected_candidate_changes': return this.discardRejectedCandidateChanges(workspace);
      case 'prepare_confirmation_groups': return this.prepareConfirmationGroups(workspace);
      case 'regroup_changes': return this.regroupChanges(workspace, context, args);
      case 'grep_package': return this.grepPackage(workspace, args);
      case 'gate_status': return this.gateStatus(workspace, args);
      case 'who_references': return this.whoReferences(workspace, args);
      case 'analyze_material_impact': return this.analyzeMaterialImpact(workspace, args);
      case 'list_package_sources': return this.listPackageSources(workspace);
      case 'list_package_fusion_plans': return this.invokePackageFusion(() => {
        const plans = this.packageFusion.listPlans(workspace.project_id);
        return {
          plans,
          display: { text: plans.length ? `已找到 ${plans.length} 个多来源融合计划` : '还没有建立多来源融合计划' },
        };
      });
      case 'get_package_fusion_plan': return this.invokePackageFusion(() => (
        this.packageFusion.getPlan(workspace.project_id, text(args.plan_id, 'plan_id'))
      ));
      case 'create_package_fusion_plan': return this.createPackageFusionPlan(workspace, context, args);
      case 'update_package_fusion_plan': return this.invokePackageFusion(() => (
        this.packageFusion.updatePlan(workspace.project_id, text(args.plan_id, 'plan_id'), {
          dispositions: args.dispositions,
        })
      ));
      case 'compile_package_fusion_plan': return this.compilePackageFusionPlan(workspace, context, args);
      case 'analyze_package_merge': return this.analyzePackageMerge(workspace, args);
      case 'read_package_source_file': return this.readPackageSourceFile(workspace, args);
      case 'stage_package_merge': return this.stagePackageMerge(workspace, context, args);
      case 'diff': return this.diff(workspace, args);
      case 'update_work_notes':
      case 'update_plan': return this.updateRuntimeFile(workspace, context, '.modeling/plan.md', args);
      case 'request_confirmation_preflight': return this.preflightWithChangePlan(workspace, context, args);
      case 'request_confirmation': return this.confirm(workspace, context, args);
      case 'request_confirmation_rejected': return this.rejectConfirmation(workspace, context, args);
      case 'release_preflight': return this.releasePreflight(workspace);
      case 'request_release': return this.release(workspace, context, args);
      case 'run_script_test': return this.runScriptTest(workspace, args);
      case 'run_evaluation': return this.runEvaluation(workspace, context, args);
      case 'get_evaluation_results': return this.evaluationResults(workspace.project_id, args);
      case 'query_eval_db': return this.queryEvaluationDatabase(context.project.id, args);
      default:
        throw new AgentCoreError('TOOL_NOT_FOUND', `未知 Agent 工具：${tool}`, [], 404);
    }
  }

  private projectDataSourceStatus(projectId: string) {
    const dataSource = getProjectDataSource(projectId);
    if (!dataSource) {
      return {
        configured: false,
        mode: 'unset',
        display: { text: '尚未配置评测数据源' },
      };
    }
    if (dataSource.mode === 'none') {
      return {
        configured: true,
        mode: 'none',
        provider: null,
        display: { text: '已配置为不使用外部数据源' },
      };
    }

    const sourceConfig = dataSource.config ?? {};
    const provider = dataSource.provider ?? null;
    const base = {
      configured: true,
      mode: 'connection',
      provider,
      providerMeaning: 'project_evaluation_data_source_type',
      mappingAliasNote: 'mappings/object-data-mappings.json 中的 default_provider_alias/provider_alias 是知识包内部逻辑数据源别名，不是项目评测数据源 provider',
      workspaceLinked: Boolean(dataSource.workspaceId),
      ontologyLinked: Boolean(dataSource.ontologyId),
    };
    if (provider === 'nl2sql_http' || provider === 'nl2sql_template') {
      const scene = String(sourceConfig.scene ?? '').trim();
      return {
        ...base,
        nl2sql: {
          sceneConfigured: Boolean(scene),
          evaluationWorkspaceConfigured: Boolean(String(sourceConfig.evaluation_workspace_id ?? '').trim()),
          usesProjectEvaluationDatabase: scene === projectEvaluationDatabaseName(projectId),
        },
        display: { text: `已配置 ${provider} 评测数据源` },
      };
    }
    if (provider === 'neo4j') {
      return {
        ...base,
        neo4j: {
          workspaceIsolationConfigured: Boolean(
            String(sourceConfig.workspace_property ?? '').trim()
            && String(sourceConfig.workspace_value ?? '').trim(),
          ),
          ontologyIsolationConfigured: Boolean(
            String(sourceConfig.ontology_property ?? '').trim()
            && String(sourceConfig.ontology_value ?? '').trim(),
          ),
          graphDatabaseConfigured: Boolean(String(sourceConfig.database ?? '').trim()),
        },
        display: { text: '已配置 Neo4j 评测数据源' },
      };
    }
    return {
      ...base,
      display: { text: '已配置评测数据源' },
    };
  }

  private validationSummary(context: AgentToolContext, args: Record<string, unknown>) {
    const reportId = text(args.report_id, 'report_id');
    try {
      return this.validationReports.summary(context.project.id, context.runId, reportId);
    } catch (error) {
      if (error instanceof Error && error.message === 'VALIDATION_REPORT_NOT_FOUND') {
        throw new AgentCoreError(
          'VALIDATION_REPORT_NOT_FOUND',
          '当前运行中不存在这份校验报告',
          ['使用校验失败返回的 validationReportId'],
          404,
        );
      }
      throw error;
    }
  }

  private validationFindings(context: AgentToolContext, args: Record<string, unknown>) {
    const reportId = text(args.report_id, 'report_id');
    const offset = Number(args.offset ?? 0);
    const limit = Number(args.limit ?? 20);
    try {
      return this.validationReports.findings(context.project.id, context.runId, reportId, offset, limit);
    } catch (error) {
      if (error instanceof Error && error.message === 'VALIDATION_REPORT_NOT_FOUND') {
        throw new AgentCoreError(
          'VALIDATION_REPORT_NOT_FOUND',
          '当前运行中不存在这份校验报告',
          ['使用校验失败返回的 validationReportId'],
          404,
        );
      }
      throw error;
    }
  }

  private materialAnalysisProgress(projectId: string, args: Record<string, unknown>) {
    const startedAfter = String(args.started_after ?? '').trim();
    const requested = new Set(stringArray(args.filenames));
    const candidates = this.db.prepare(`SELECT id, status, stage, plan_json, created_at, updated_at
      FROM material_analysis_runs
      WHERE project_id = ? AND status = 'running' AND (? = '' OR created_at >= ?)
      ORDER BY created_at DESC LIMIT 10`).all(projectId, startedAfter, startedAfter) as any[];
    const run = candidates.find((candidate) => {
      if (!requested.size) return true;
      const sources = this.db.prepare(`SELECT DISTINCT document_id, filename
        FROM material_analysis_chunks WHERE run_id = ?`).all(candidate.id) as any[];
      const identifiers = new Set(sources.flatMap((source) => [
        String(source.document_id || ''), String(source.filename || ''),
      ]).filter(Boolean));
      return [...requested].every((identifier) => identifiers.has(identifier));
    });
    if (!run) {
      return {
        status: 'preparing', stage: 'planning', title: '正在准备全文精读',
        detail: '正在建立完整阅读计划',
        counters: { current: 0, total: 0, unit: '个处理检查点' },
      };
    }
    const chunks = this.db.prepare(`SELECT document_key, filename, start_offset, end_offset, status
      FROM material_analysis_chunks WHERE run_id = ? ORDER BY document_key, chunk_index`).all(run.id) as any[];
    const summaries = this.db.prepare(`SELECT document_key, status FROM material_analysis_document_summaries
      WHERE run_id = ?`).all(run.id) as any[];
    const completed = chunks.filter((chunk) => chunk.status === 'completed');
    const documents = [...new Set(chunks.map((chunk) => String(chunk.document_key)))];
    const summarized = new Set(summaries.filter((item) => item.status === 'completed')
      .map((item) => String(item.document_key)));
    const activeFilename = String(chunks.find((chunk) => chunk.status === 'running')?.filename
      || chunks.find((chunk) => chunk.status === 'pending')?.filename || '');
    let plan: any = {};
    try { plan = JSON.parse(String(run.plan_json || '{}')); } catch { /* 数据库约束保证正常 JSON */ }
    const totalChars = Math.max(0, Number(plan.totalChars) || 0);
    const completedChars = coveredCharacters(completed);
    const coveragePercent = totalChars > 0
      ? Math.min(100, Math.floor((completedChars / totalChars) * 100))
      : (chunks.length && completed.length === chunks.length ? 100 : 0);
    const totalCheckpoints = chunks.length + documents.length + 1;
    const completedCheckpoints = completed.length + summarized.size;
    const taskPercent = totalCheckpoints > 0
      ? Math.min(99, Math.floor((completedCheckpoints / totalCheckpoints) * 100))
      : 0;
    const updatedAt = String(run.updated_at || '');
    const lastUpdateMs = Date.parse(updatedAt);
    const delayed = Number.isFinite(lastUpdateMs) && Date.now() - lastUpdateMs > 120_000;
    const title = run.stage === 'document_reduce'
      ? '正在汇总全文阅读结果'
      : run.stage === 'domain_reduce'
        ? '正在整理建模候选'
        : activeFilename ? `正在全文精读《${activeFilename}》` : '正在全文精读资料';
    const factualDetail = run.stage === 'document_reduce'
      ? `已完成 ${summarized.size}/${documents.length} 份文档汇总 · 全文覆盖 ${coveragePercent}%`
      : `${completed.length}/${chunks.length} 个分块完成 · 全文覆盖 ${coveragePercent}%`;
    const detail = delayed
      ? `${factualDetail} · 较久未收到新进度，你可以继续输入或停止任务`
      : factualDetail;
    return {
      status: run.status,
      stage: run.stage,
      title,
      detail,
      updatedAt,
      health: delayed ? 'delayed' : 'active',
      documents: { completed: summarized.size, total: documents.length },
      coverage: { coveredChars: completedChars, totalChars, percent: coveragePercent },
      counters: {
        progress: taskPercent,
        current: completedCheckpoints,
        total: totalCheckpoints,
        unit: '个处理检查点',
      },
    };
  }

  private async schemaSemanticStatus(context: AgentToolContext) {
    const workingPackage = await this.workbenchStudioFiles(context.project, context.username);
    const projectTables = this.tables(context.project.id);
    const tables = schemaTables(projectTables);
    const tableFingerprint = createHash('sha256').update(JSON.stringify(projectTables.map((table) => ({
      documentId: table.documentId ?? '',
      name: table.name,
      fields: table.fields.map((field) => ({
        name: field.name,
        sourceColumn: field.sourceColumn ?? '',
        note: field.note ?? '',
        required: field.required ?? null,
        nullable: field.nullable ?? null,
        primaryKey: field.primaryKey ?? null,
        inferredType: field.inferredType,
      })),
      constraintSource: table.constraintSource ?? null,
      primaryKey: table.primaryKey ?? [],
      rowsDigest: createHash('sha256').update(JSON.stringify(table.rows ?? [])).digest('hex'),
    })))).digest('hex');
    const cacheKey = `${context.project.id}:${workingPackage.workingRevision}:${tableFingerprint}`;
    let status = this.schemaSemanticStatusCache.get(cacheKey);
    if (!status) {
      status = analyzeSchemaSemantics(parseWorkingPackageSpec(workingPackage.files), projectTables);
      for (const key of this.schemaSemanticStatusCache.keys()) {
        if (key.startsWith(`${context.project.id}:`)) this.schemaSemanticStatusCache.delete(key);
      }
      this.schemaSemanticStatusCache.set(cacheKey, status);
      const cacheLimit = status.issues.length > 10_000 ? 2 : 8;
      while (this.schemaSemanticStatusCache.size > cacheLimit) {
        this.schemaSemanticStatusCache.delete(this.schemaSemanticStatusCache.keys().next().value!);
      }
    }
    return { workingPackage, status };
  }

  private async getSchemaSemantics(context: AgentToolContext, args: Record<string, unknown>) {
    const { workingPackage, status } = await this.schemaSemanticStatus(context);
    const level = String(args.level ?? '').trim();
    const code = String(args.code ?? '').trim();
    const objectKey = String(args.object_key ?? '').trim();
    if (level && !['error', 'warning'].includes(level)) {
      throw new AgentCoreError('INVALID_INPUT', `不支持的语义问题级别：${level}`, [], 400);
    }
    const offset = boundedInteger(args.offset, 0, 100_000, 0);
    const limit = boundedInteger(args.limit, 1, 100, 50);
    const filtered = status.issues.filter((item) => (
      (!level || item.level === level)
      && (!code || item.code === code)
      && (!objectKey || item.objectKey === objectKey)
    ));
    const page = filtered.slice(offset, offset + limit);
    const studio = new StudioReadService(workingPackage);
    const resourceCache = new Map<string, ReturnType<typeof domainResourcePayload>>();
    const items = page.map((item) => {
      const resourceId = `object:${item.objectKey}`;
      let resource = resourceCache.get(resourceId);
      if (!resource) {
        try {
          resource = domainResourcePayload(studio.detail(resourceId).element);
          resourceCache.set(resourceId, resource);
        } catch { /* Physical object validation reports missing resources separately. */ }
      }
      const field = item.fieldKey
        ? (resource?.editable_items.fields as Array<Record<string, unknown>> | undefined)
          ?.find((candidate) => candidate.key === item.fieldKey)
        : undefined;
      return {
        ...item,
        resource_id: resourceId,
        ...(resource?.resource_hash ? { resource_hash: resource.resource_hash } : {}),
        ...(field?.item_hash ? { field_hash: field.item_hash } : {}),
        current: item.fieldKey ? {
          cn_name: field?.cnName ?? null,
          note: field?.note ?? null,
          alias: field?.alias ?? null,
          required: field?.required === true,
        } : {
          title: resource?.title ?? '',
          description: resource?.description ?? '',
          aliases: resource?.values.aliases ?? [],
        },
      };
    });
    const nextOffset = offset + items.length;
    return {
      working_revision: workingPackage.workingRevision,
      summary: schemaSemanticSummary(status),
      items,
      total: filtered.length,
      offset,
      next_offset: nextOffset < filtered.length ? nextOffset : null,
      display: { text: status.available
        ? `Schema 业务语义${status.pass ? '已满足门禁' : `还有 ${status.errorCount} 项阻断问题`}`
        : '当前项目没有需要补全的物理 Schema 对象' },
    };
  }

  private adoptDomainToolContract(context: AgentToolContext): void {
    this.db.prepare(`INSERT INTO agent_domain_tool_adoptions (
      project_id, run_id, contract_version, adopted_at
    ) VALUES (?,?,?,?) ON CONFLICT(project_id, run_id) DO UPDATE SET
      contract_version = excluded.contract_version`)
      .run(context.project.id, context.runId, DOMAIN_TOOL_CONTRACT_VERSION, nowIso());
  }

  private usesDomainToolContract(context: AgentToolContext): boolean {
    const row = this.db.prepare(`SELECT contract_version FROM agent_domain_tool_adoptions
      WHERE project_id = ? AND run_id = ?`).get(context.project.id, context.runId) as
      | { contract_version: string }
      | undefined;
    return row?.contract_version === DOMAIN_TOOL_CONTRACT_VERSION;
  }

  private usesScriptContract(context: AgentToolContext): boolean {
    return Boolean(this.db.prepare(`SELECT 1 FROM agent_script_contract_drafts
      WHERE project_id = ? AND run_id = ? LIMIT 1`).get(context.project.id, context.runId));
  }

  private async createChangePlan(context: AgentToolContext, args: Record<string, unknown>) {
    try {
      const working = await this.workbenchStudioFiles(context.project, context.username);
      const workingSpec = parseWorkingPackageSpec(working.files);
      const sourcePolicy = this.normalizeObjectSourcePlan(
        context.project.id,
        args.mode,
        args.tasks,
        workingSpec,
        working.files,
      );
      const plan = this.changeControl.createPlan({
        projectId: context.project.id,
        runId: context.runId,
        goal: args.goal,
        mode: args.mode,
        expectedRevision: args.expected_revision,
        currentRevision: working.workingRevision,
        tasks: sourcePolicy.tasks,
      });
      return {
        plan,
        object_source_policy: {
          mode: sourcePolicy.mode,
          schema_table_count: sourcePolicy.schemaTableCount,
          object_generation: sourcePolicy.mode === 'schema'
            ? 'deterministic_schema_sync'
            : 'virtual_objects_only',
          schema_sync_required: sourcePolicy.schemaSyncRequired,
          schema_semantics_required: sourcePolicy.schemaSemanticsRequired,
          reused_schema_sync: sourcePolicy.mode === 'schema' && !sourcePolicy.schemaSyncRequired,
          reused_schema_semantics: sourcePolicy.mode === 'schema' && !sourcePolicy.schemaSemanticsRequired,
        },
        display: { text: plan.approval_status === 'pending'
          ? `已建立 ${plan.task_count} 项受控建模计划，等待你批准后再写入`
          : `已建立 ${plan.task_count} 项受控建模计划，写入前会逐项核对范围` },
      };
    } catch (error) {
      throw normalizeChangeControlError(error);
    }
  }

  private normalizeObjectSourcePlan(
    projectId: string,
    rawMode: unknown,
    rawTasks: unknown,
    workingSpec: PackageSpec,
    workingFiles: Record<string, string>,
  ) {
    const mode = String(rawMode ?? 'expert_edit');
    const projectTables = this.tables(projectId);
    const physicalTables = schemaTables(projectTables);
    const sourceMode = physicalTables.length ? 'schema' : 'virtual';
    const emptyRequirements = { schemaSyncRequired: false, schemaSemanticsRequired: false };
    if (!Array.isArray(rawTasks)) {
      return { tasks: rawTasks, mode: sourceMode, schemaTableCount: physicalTables.length, ...emptyRequirements };
    }
    const tasks = rawTasks.map((task) => (
      task && typeof task === 'object' && !Array.isArray(task)
        ? { ...(task as Record<string, unknown>) }
        : task
    ));
    const toolsFor = (task: unknown) => (
      task && typeof task === 'object' && !Array.isArray(task)
        && Array.isArray((task as Record<string, unknown>).allowed_tools)
        ? (task as Record<string, unknown>).allowed_tools as unknown[]
        : []
    ).map(String);

    if (!physicalTables.length) {
      const forbidden = tasks.find((task) => toolsFor(task).some((tool) => (
        tool === 'sync_schema_objects' || tool === 'patch_object_source_binding'
      )));
      if (forbidden) {
        throw new AgentCoreError(
          'PHYSICAL_SCHEMA_REQUIRED',
          '项目没有明确标记为数据 Schema 的资料，只能生成不绑定物理表的虚拟对象',
          ['移除 Schema 同步和物理绑定任务；虚拟对象不得生成 mapping'],
          409,
          { retryable: false, object_source_mode: 'virtual' },
        );
      }
      return { tasks, mode: sourceMode, schemaTableCount: 0, ...emptyRequirements };
    }

    const createsPhysicalObject = (task: unknown) => toolsFor(task).includes('create_object');
    if (mode !== 'generation' && tasks.some(createsPhysicalObject)) {
      throw new AgentCoreError(
        'DERIVED_OBJECT_FORBIDDEN_IN_SCHEMA_MODE',
        '项目已有确定数据 Schema，不能由模型另行创建对象',
        ['使用 sync_schema_objects 按全部 Sheet 确定性同步，或只修改现有对象的非物理元数据'],
        409,
        { retryable: false, object_source_mode: 'schema', schema_table_count: physicalTables.length },
      );
    }
    if (mode !== 'generation') {
      return { tasks, mode: sourceMode, schemaTableCount: physicalTables.length, ...emptyRequirements };
    }

    const expectedObjects = preservePhysicalSchemaSemantics(
      expectedSchemaObjects(projectTables),
      workingSpec.objects,
    ).map((item) => item.object);
    const expectedSpec = canonicalizePackageSpec({
      ...structuredClone(workingSpec),
      objects: expectedObjects,
    });
    const expectedFiles = renderObjectProjection(expectedSpec);
    const expectedObjectPaths = new Set(expectedObjects.map((object) => `objects/${object.key}.md`));
    const projectionPaths = [...expectedObjectPaths, 'objects/index.md', OBJECT_DATA_MAPPING_PATH];
    const projectionMatches = projectionPaths.every((path) => workingFiles[path] === expectedFiles[path])
      && !Object.keys(workingFiles).some((path) => (
        /^objects\/[^/]+\.md$/u.test(path)
        && path !== 'objects/index.md'
        && !expectedObjectPaths.has(path)
      ));
    const physicalSatisfied = schemaObjectPolicyIssues(workingSpec, workingFiles, projectTables).length === 0
      && projectionMatches;
    const semanticStatus = analyzeSchemaSemantics(workingSpec, projectTables);
    const semanticsSatisfied = physicalSatisfied
      && this.schemaSemanticConfirmationMatches(projectId, semanticStatus);
    const schemaSyncRequired = !physicalSatisfied;
    const schemaSemanticsRequired = !semanticsSatisfied;

    const replacedKeys = new Set<string>();
    const retained = tasks.filter((task) => {
      const tools = toolsFor(task);
      const replaced = tools.some((tool) => (
        tool === 'create_object'
        || tool === 'patch_object_source_binding'
        || tool === 'sync_schema_objects'
        || tool === 'stage_schema_semantic_changes'
        || tool === 'confirm_schema_semantics'
      ));
      if (replaced && task && typeof task === 'object' && !Array.isArray(task)) {
        replacedKeys.add(String((task as Record<string, unknown>).client_key ?? ''));
      }
      return !replaced;
    });
    const schemaTaskKey = 'schema_object_sync';
    const prerequisiteTaskKey = schemaSyncRequired || schemaSemanticsRequired ? schemaTaskKey : '';
    const normalized = retained.map((task) => {
      if (!task || typeof task !== 'object' || Array.isArray(task)) return task;
      const record = task as Record<string, unknown>;
      const dependencies = Array.isArray(record.depends_on) ? record.depends_on.map(String) : [];
      return {
        ...record,
        depends_on: [...new Set([
          ...(prerequisiteTaskKey ? [prerequisiteTaskKey] : []),
          ...dependencies.filter((dependency) => !replacedKeys.has(dependency)),
        ])],
      };
    });
    const prerequisiteTasks: Array<Record<string, unknown>> = [];
    if (schemaSyncRequired || schemaSemanticsRequired) prerequisiteTasks.push({
      client_key: schemaTaskKey,
      title: `按 ${physicalTables.length} 张 Schema Sheet 同步物理对象`,
      resource_kind: 'binding',
      target_key: '__schema__',
      allowed_tools: [
        ...(schemaSyncRequired ? ['sync_schema_objects'] : []),
        ...(schemaSemanticsRequired ? ['stage_schema_semantic_changes', 'confirm_schema_semantics'] : []),
      ],
      depends_on: [],
      acceptance: schemaSemanticsRequired
        ? '每张 Schema Sheet 恰好对应一个对象，并在同一计划任务末尾完成对象标题、说明、字段中文名和必填二态语义确认；字段业务说明允许为空'
        : '每张 Schema Sheet 恰好对应一个对象，字段、主键、物理来源和 mapping 全部由平台确定性生成',
      required: true,
    });
    return {
      mode: sourceMode,
      schemaTableCount: physicalTables.length,
      schemaSyncRequired,
      schemaSemanticsRequired,
      tasks: [...prerequisiteTasks, ...normalized],
    };
  }

  private assertObjectSourceToolMode(
    projectId: string,
    tool: string,
    args: Record<string, unknown>,
  ): void {
    const hasSchema = schemaTables(this.tables(projectId)).length > 0;
    if (hasSchema && (tool === 'create_object' || tool === 'upsert_object')) {
      throw new AgentCoreError(
        'DERIVED_OBJECT_FORBIDDEN_IN_SCHEMA_MODE',
        '项目已有确定数据 Schema，对象只能按物理 Sheet 由平台同步生成',
        ['使用 sync_schema_objects；不要创建、合并或省略 Sheet 对象'],
        409,
        { retryable: false, object_source_mode: 'schema' },
      );
    }
    const virtualBindingAttempt = ['sync_schema_objects', 'patch_object_source_binding', 'bind_object_source'].includes(tool)
      || (tool === 'upsert_object' && (args.schema_source !== undefined || args.source !== undefined));
    if (!hasSchema && virtualBindingAttempt) {
      throw new AgentCoreError(
        'PHYSICAL_SCHEMA_REQUIRED',
        '项目没有确定数据 Schema，虚拟对象不能绑定物理表或生成 mapping',
        ['保留虚拟对象，不设置 sourceTable、table source 或 mapping'],
        409,
        { retryable: false, object_source_mode: 'virtual' },
      );
    }
  }

  private getChangePlan(context: AgentToolContext, args: Record<string, unknown>) {
    try {
      const plan = this.changeControl.snapshot(
        context.project.id,
        context.runId,
        String(args.plan_id ?? '').trim(),
      );
      return {
        plan,
        display: {
          text: plan.remaining_count
            ? `建模计划还有 ${plan.remaining_count} 项必选任务待完成`
            : '建模计划的必选任务已经全部形成可校验结果',
        },
      };
    } catch (error) {
      throw normalizeChangeControlError(error);
    }
  }

  private reviseChangePlan(context: AgentToolContext, args: Record<string, unknown>) {
    try {
      const active = this.changeControl.activePlan(context.project.id, context.runId);
      const additions = Array.isArray(args.add_tasks) ? args.add_tasks : [];
      const addedTools = additions.flatMap((task) => (
        task && typeof task === 'object' && !Array.isArray(task)
          && Array.isArray((task as Record<string, unknown>).allowed_tools)
          ? ((task as Record<string, unknown>).allowed_tools as unknown[]).map(String)
          : []
      ));
      const hasSchema = schemaTables(this.tables(context.project.id)).length > 0;
      const forbidden = hasSchema
        ? addedTools.some((tool) => tool === 'create_object'
          || (active?.mode === 'generation'
            && ['sync_schema_objects', 'patch_object_source_binding'].includes(tool)))
        : addedTools.some((tool) => ['sync_schema_objects', 'patch_object_source_binding'].includes(tool));
      if (forbidden) {
        throw new AgentCoreError(
          hasSchema ? 'DERIVED_OBJECT_FORBIDDEN_IN_SCHEMA_MODE' : 'PHYSICAL_SCHEMA_REQUIRED',
          hasSchema
            ? '计划修订不能绕过已确定 Schema 的对象同步边界'
            : '没有确定数据 Schema 时不能追加物理绑定任务',
          [hasSchema ? '保留原计划中的 Schema 同步任务' : '只追加不绑定物理表的虚拟对象任务'],
          409,
          { retryable: false, object_source_mode: hasSchema ? 'schema' : 'virtual' },
        );
      }
      const plan = this.changeControl.revisePlan({
        projectId: context.project.id,
        runId: context.runId,
        planId: args.plan_id,
        expectedVersion: args.expected_version,
        reason: args.reason,
        addTasks: args.add_tasks,
        skipTaskIds: args.skip_task_ids,
        scopeApproved: args.scope_approved,
      });
      return {
        plan,
        display: { text: `建模计划已更新为第 ${plan.version} 版，共 ${plan.task_count} 项任务` },
      };
    } catch (error) {
      throw normalizeChangeControlError(error);
    }
  }

  private async stageChangeSet(context: AgentToolContext, args: Record<string, unknown>) {
    let changeSetId = '';
    try {
      const planId = text(args.plan_id, 'plan_id');
      const mutationId = text(args.mutation_id, 'mutation_id');
      const expectedRevision = text(args.expected_revision, 'expected_revision');
      const actions = parseChangeSetActions(args.actions);
      this.assertProductLedgerBeforeRuleBatch(context, actions);
      const requestHash = domainMutationRequestHash('change-set', args);
      const prepared = this.changeControl.prepareChangeSet({
        projectId: context.project.id,
        runId: context.runId,
        planId,
        mutationId,
        requestHash,
        expectedRevision,
        actions,
      });
      changeSetId = prepared.changeSetId;
      if (prepared.replay) return { ...prepared.replay, replayed: true };
      if (prepared.staging !== undefined) {
        const previous = this.changeControl.changeSetContext(context.project.id, context.runId, mutationId);
        return {
          ...this.changeControl.changeSetReceipt(context.project.id, context.runId, mutationId),
          result_unknown: previous.status === 'staging',
          display: { text: previous.status === 'staging'
            ? '上一批修改仍在结算，请先查询回执，不要重复提交'
            : '上一批修改没有生效，请根据失败原因生成新的操作标识' },
        };
      }
      const working = await this.workbenchStudioFiles(context.project, context.username);
      if (expectedRevision !== working.workingRevision) {
        throw new AgentCoreError(
          'REVISION_CONFLICT',
          '知识包内容已经变化，请重新读取计划目标后生成新的 Change Set',
          ['读取当前建模计划和资源快照，不要原样重试旧参数'],
          409,
          {
            expected_revision: expectedRevision,
            current_revision: working.workingRevision,
            retryable: false,
            recovery: {
              owner: 'agent',
              strategy: 'refresh_change_set',
              requiredTool: 'stage_change_set',
              maxAttempts: 2,
            },
          },
        );
      }
      const activePlan = this.changeControl.activePlan(context.project.id, context.runId);
      if (activePlan?.id === planId && activePlan.mode === 'generation'
        && actions.some((action) => action.tool !== 'sync_schema_objects')
        && this.schemaSemanticEnforcementEnabled(context.project.id)) {
        const planWorkspace = this.workspace(context.project.id);
        if (planWorkspace) this.assertSchemaSemantics(planWorkspace, await this.overlay(planWorkspace));
      }
      const batch = this.compileChangeSetBatch(context, working, actions);
      const response = await this.studioWrites.batchDomainModel(
        context.project,
        context.username,
        {
          expectedRevision,
          clientMutationId: mutationId,
          editSessionId: changeSetId,
          creates: batch.creates,
          patches: batch.patches,
        },
        requestHash,
      );
      const staged = {
        ...domainMutationResponse(response, batch.summary),
        action_count: batch.summary.action_count,
        operations: batch.summary.operations,
        change_set_id: changeSetId,
        plan_id: planId,
        mutation_id: mutationId,
        status: 'staged',
      };
      this.changeControl.completeChangeSet(changeSetId, staged);
      return staged;
    } catch (error) {
      const normalized = error instanceof ChangeControlError
        ? normalizeChangeControlError(error)
        : normalizeDomainAgentError(error);
      if (changeSetId) {
        this.changeControl.failChangeSet(changeSetId, {
          code: normalized.code,
          message: normalized.message,
          retryable: normalized.details.retryable === true,
          details: normalized.details,
        });
      }
      throw normalized;
    }
  }

  private assertProductLedgerBeforeRuleBatch(
    context: AgentToolContext,
    actions: ChangeSetActionInput[],
  ): void {
    if (config.productLedger.mode !== 'enforce') return;
    const touchesRuleBatch = actions.some((action) => (
      action.tool === 'create_rule' || action.tool === 'patch_rule'
    ));
    if (!touchesRuleBatch) return;
    const workspace = this.workspace(context.project.id);
    if (!workspace) return;
    const ledger = this.getProductLedger(workspace);
    const validation = ledger.validation;
    const ruleActionCount = actions.filter((action) => (
      action.tool === 'create_rule' || action.tool === 'patch_rule'
    )).length;
    if (ledger.required === true && ruleActionCount > 10) {
      throw new AgentCoreError(
        'PRODUCT_LEDGER_BATCH_TOO_LARGE',
        `产品清单规则批次包含 ${ruleActionCount} 项，单批最多 10 项`,
        ['将规则生成拆分为每批 5 到 10 项，并在每批完成后重新读取台账和回执'],
        409,
        {
          gate: 'product_ledger_verified',
          batchSize: ruleActionCount,
          maxBatchSize: 10,
        },
      );
    }
    const hasCurrentLedger = ledger.available === true;
    const ledgerMatchesWorkspace = ledger.sourceRevision === workspace.base_revision_oid;
    const ledgerPassed = validation?.pass === true;
    const ledgerGatePassed = hasCurrentLedger && ledgerMatchesWorkspace && ledgerPassed;
    if (ledger.required !== true || ledgerGatePassed) return;
    const blockers = ledger.available !== true
      ? ['先提交当前产品大清单的结构化台账']
      : ledger.sourceRevision !== workspace.base_revision_oid
        ? ['台账对应的资料修订已变化，请重新核对当前清单']
        : (validation?.errors ?? []).slice(0, 8);
    throw new AgentCoreError(
      'PRODUCT_LEDGER_GATE_BLOCKED',
      `产品清单台账未通过，不能开始正式规则批次生成：${blockers.join('；')}`,
      blockers,
      409,
      {
        gate: 'product_ledger_verified',
        blockers,
        validation,
        next: ['完成台账核对后，再按小批次提交规则生成任务'],
      },
    );
  }

  private async runPlannedDirectTask(
    context: AgentToolContext,
    tool: string,
    args: Record<string, unknown>,
    targetKey: string,
    action: () => Promise<Record<string, unknown>>,
  ) {
    if (!CHANGE_PLAN_DIRECT_TOOLS.has(tool)) return action();
    const active = this.changeControl.activePlan(context.project.id, context.runId);
    if (!active) return action();
    const taskId = text(args.plan_task_id, 'plan_task_id');
    try {
      this.changeControl.authorizeDirectTask({
        projectId: context.project.id,
        runId: context.runId,
        taskId,
        tool,
        targetKey,
      });
      const result = await action();
      if (result.replayed === true) {
        return {
          ...result,
          plan: this.changeControl.snapshot(context.project.id, context.runId, active.id),
        };
      }
      const plan = this.changeControl.completeDirectTask({
        projectId: context.project.id,
        runId: context.runId,
        taskId,
        tool,
        receiptId: text(args.mutation_id, 'mutation_id'),
        resultingRevision: String(result.working_revision ?? ''),
      });
      return { ...result, plan };
    } catch (error) {
      if (error instanceof ChangeControlError) throw normalizeChangeControlError(error);
      throw error;
    }
  }

  private async runPlannedProgressTask(
    context: AgentToolContext,
    tool: string,
    args: Record<string, unknown>,
    targetKey: string,
    action: () => Promise<Record<string, unknown>>,
  ) {
    const active = this.changeControl.activePlan(context.project.id, context.runId);
    if (!active) {
      throw new AgentCoreError(
        'CHANGE_PLAN_REQUIRED',
        'Schema 业务语义修改必须属于当前受控建模计划',
        ['先建立包含 Schema 同步及其末尾语义阶段的 generation 计划'],
        409,
        { retryable: false },
      );
    }
    const taskId = text(args.plan_task_id, 'plan_task_id');
    try {
      this.changeControl.authorizeDirectTask({
        projectId: context.project.id,
        runId: context.runId,
        taskId,
        tool,
        targetKey,
      });
      const result = await action();
      return {
        ...result,
        plan: this.changeControl.snapshot(context.project.id, context.runId, active.id),
      };
    } catch (error) {
      if (error instanceof ChangeControlError) throw normalizeChangeControlError(error);
      throw error;
    }
  }

  private compileChangeSetBatch(
    context: AgentToolContext,
    working: Awaited<ReturnType<AgentCoreService['workbenchStudioFiles']>>,
    actions: ChangeSetActionInput[],
  ) {
    const studio = new StudioReadService(working);
    const creates: Array<{ kind: DomainKind; key: string; values: Record<string, unknown> }> = [];
    const patches: Array<{ elementId: string; changes: Record<string, unknown> }> = [];
    const operations: string[] = [];
    let actionCount = 0;
    for (const action of actions) {
      const input = action.arguments;
      const kind = action.tool.split('_').at(-1) as DomainKind;
      if (action.tool.startsWith('create_')) {
        if (kind === 'object' && schemaTables(this.tables(context.project.id)).length) {
          throw new AgentCoreError(
            'DERIVED_OBJECT_FORBIDDEN_IN_SCHEMA_MODE',
            'Schema 模式不接受模型创建对象，必须先执行计划中的 Schema 同步任务',
            ['调用 get_change_plan 读取 sync_schema_objects 任务并执行'],
            409,
            { retryable: false, object_source_mode: 'schema' },
          );
        }
        creates.push({ kind, key: domainKey(input.key, domainKindLabel(kind)), values: domainCreateValues(kind, input) });
        operations.push(action.tool);
        actionCount += 1;
        continue;
      }
      const resourceId = domainResourceIdArg(input.resource_id);
      const element = studio.detail(resourceId).element;
      if (element.kind !== kind) {
        throw new AgentCoreError('DOMAIN_KIND_MISMATCH', 'Change Set 中的工具与资源类型不一致', [], 400, {
          resource_id: resourceId, expected_kind: kind, actual_kind: element.kind, retryable: false,
        });
      }
      assertDomainResourceHash(element, input.expected_resource_hash, working.workingRevision);
      if (kind === 'object') {
        const compiled = compileObjectPatchActions(element, input.actions);
        const preview = previewOntologyObjectMutation(
          parseWorkingPackageSpec(working.files),
          element.key,
          {
            metadata: compiled.metadata,
            field_ops: compiled.field_ops,
            relation_ops: compiled.relation_ops,
          },
          { tables: this.projectTables(context.project.id) },
        );
        if (!preview.ok) {
          throw new AgentCoreError('DOMAIN_PATCH_INVALID', '对象增量修改未通过确定性校验', [], 422, {
            fieldErrors: preview.fieldErrors,
            errors: preview.issues,
            warnings: preview.warnings,
            retryable: false,
          });
        }
        patches.push({ elementId: resourceId, changes: preview.patchChanges });
        actionCount += compiled.summary.actionCount;
        operations.push(...compiled.summary.operations);
      } else {
        const compiled = compileDomainPatchActions(element, input.actions);
        patches.push({ elementId: resourceId, changes: compiled.changes });
        actionCount += compiled.summary.actionCount;
        operations.push(...compiled.summary.operations);
      }
    }
    return { creates, patches, summary: { action_count: actionCount, operations } };
  }

  private async getChangeSetReceipt(context: AgentToolContext, args: Record<string, unknown>) {
    try {
      const mutationId = text(args.mutation_id, 'mutation_id');
      const current = this.changeControl.changeSetContext(context.project.id, context.runId, mutationId);
      if (current.status === 'staging') {
        const replay = this.studioWrites.replayDomainMutation(
          context.project.id,
          mutationId,
          current.requestHash,
        );
        if (replay) {
          const staged = {
            ...domainMutationResponse(replay, {
              action_count: replay.elements.length || 1,
              operations: ['reconciled_change_set'],
            }),
            change_set_id: current.id,
            mutation_id: mutationId,
            status: 'staged',
            action_count: replay.elements.length || 1,
            reconciled: true,
          };
          this.changeControl.completeChangeSet(current.id, staged);
        }
      }
      const receipt = this.changeControl.changeSetReceipt(context.project.id, context.runId, mutationId);
      return {
        change_set: receipt,
        display: { text: receipt.status === 'staging'
          ? '这批修改的结果仍未确定，当前禁止重复提交'
          : `这批修改当前为“${changeSetStatusLabel(String(receipt.status))}”` },
      };
    } catch (error) {
      throw normalizeChangeControlError(error);
    }
  }

  private async validateChangeSet(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    try {
      const planId = text(args.plan_id, 'plan_id');
      const current = await this.overlay(workspace);
      this.changeControl.readyForValidation(context.project.id, context.runId, planId);
      this.assertSchemaSemantics(workspace, current);
      const prepared = await this.prepareConfirmationGroups(workspace);
      const result = await this.preflight(workspace, context, {
        summary: String(args.summary ?? '核对完整建模计划'),
        change_groups: prepared.change_groups,
      });
      const plan = this.changeControl.markValidated(context.project.id, context.runId, planId, result);
      return {
        plan,
        validation: result,
        display: { text: `完整计划的 ${plan.task_count} 项任务已通过校验，可以进入人工审阅` },
      };
    } catch (error) {
      if (error instanceof AgentCoreError) throw error;
      throw normalizeChangeControlError(error);
    }
  }

  private async preflightWithChangePlan(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const active = this.changeControl.activePlan(context.project.id, context.runId);
    if (active) {
      const current = await this.overlay(workspace);
      this.changeControl.readyForValidation(context.project.id, context.runId, active.id);
      this.assertSchemaSemantics(workspace, current);
    }
    const result = await this.preflight(workspace, context, args);
    if (active) {
      this.changeControl.markWaitingUser(
        context.project.id,
        context.runId,
        String(result.candidate_revision ?? ''),
      );
    }
    return result;
  }

  private discardChangeSet(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    try {
      const planId = text(args.plan_id, 'plan_id');
      if (workspace.candidate_id) {
        throw new AgentCoreError('CHANGE_SET_FROZEN', '这批修改已经进入确认，请先在页面退回', [], 409);
      }
      const result = this.discardRejectedCandidateChanges(workspace);
      this.changeControl.discardPlan(context.project.id, context.runId, planId);
      return { ...result, plan_id: planId, display: { text: '已放弃这份计划的待确认修改，正式版本保持不变' } };
    } catch (error) {
      if (error instanceof AgentCoreError) throw error;
      throw normalizeChangeControlError(error);
    }
  }

  private async listDomainResources(context: AgentToolContext, args: Record<string, unknown>) {
    try {
      const workingPackage = await this.workbenchStudioFiles(context.project, context.username);
      const studio = new StudioReadService(workingPackage);
      const requestedRevision = String(
        args.working_revision ?? args.workingRevision ?? args.expected_revision ?? args.expectedRevision ?? '',
      ).trim();
      if (requestedRevision) studio.assertRevision(requestedRevision);
      const kind = domainKindArg(args.kind);
      const response = studio.list({
        kind,
        query: args.query,
        sort: args.sort,
        order: args.order,
        cursor: args.cursor,
        limit: args.limit,
      });
      return {
        working_revision: response.workingRevision,
        kind,
        items: response.items.map((item) => {
          const element = studio.detail(item.id).element;
          return { ...item, resource_id: item.id, resource_hash: domainResourceHash(element) };
        }),
        total: response.total,
        next_cursor: response.nextCursor,
        display: {
          text: response.total
            ? `已返回 ${response.items.length}/${response.total} 个${domainKindLabel(kind)}`
            : `当前知识包还没有${domainKindLabel(kind)}`,
        },
      };
    } catch (error) {
      throw normalizeDomainAgentError(error);
    }
  }

  private async getDomainResource(context: AgentToolContext, args: Record<string, unknown>) {
    try {
      const { workingPackage, element } = await this.loadDomainResourceContext(context, args, false);
      const resource = domainResourcePayload(element);
      return {
        working_revision: workingPackage.workingRevision,
        resource: element.kind === 'operation'
          ? { ...resource, operation_contract_hash: scriptOperationContractHash(element) }
          : resource,
        display: { text: `已返回${domainKindLabel(element.kind as DomainKind)}《${element.title}》的结构化快照` },
      };
    } catch (error) {
      throw normalizeDomainAgentError(error);
    }
  }

  private async loadDomainResourceContext(
    context: AgentToolContext,
    args: Record<string, unknown>,
    requireExpectedRevision: boolean,
  ): Promise<{
    workingPackage: Awaited<ReturnType<AgentCoreService['workbenchStudioFiles']>>;
    resourceId: string;
    element: StudioElement;
  }> {
    const workingPackage = await this.workbenchStudioFiles(context.project, context.username);
    const studio = new StudioReadService(workingPackage);
    const requestedRevision = requireExpectedRevision
      ? text(args.expected_revision ?? args.expectedRevision, 'expected_revision')
      : String(
        args.working_revision ?? args.workingRevision ?? args.expected_revision ?? args.expectedRevision ?? '',
      ).trim();
    if (requestedRevision) studio.assertRevision(requestedRevision);
    const resourceId = domainResourceIdArg(args.resource_id ?? args.element_id ?? args.resourceId ?? args.elementId);
    return { workingPackage, resourceId, element: studio.detail(resourceId).element };
  }

  private async prepareDomainFailureReceipt(
    context: AgentToolContext,
    mutationId: string,
    requestHash: string,
  ): Promise<DomainFailureReceiptContext> {
    const workingPackage = await this.workbenchStudioFiles(context.project, context.username);
    const dependencyRevision = domainDependencyRevision(context.project.id);
    const reused = this.db.prepare(`SELECT request_hash FROM agent_domain_mutation_failure_receipts
      WHERE project_id = ? AND mutation_id = ? ORDER BY created_at DESC LIMIT 1`)
      .get(context.project.id, mutationId) as { request_hash: string } | undefined;
    if (reused && reused.request_hash !== requestHash) {
      throw new AgentCoreError(
        'MUTATION_ID_REUSED',
        '这个操作标识已经用于另一项修改，请生成新的操作标识后重试',
        [],
        409,
        { mutation_id: mutationId, error_class: 'conflict', retryable: false, repairable: true },
      );
    }
    const receipt = this.db.prepare(`SELECT error_json FROM agent_domain_mutation_failure_receipts
      WHERE project_id = ? AND mutation_id = ? AND request_hash = ?
        AND working_revision = ? AND dependency_revision = ?`)
      .get(
        context.project.id,
        mutationId,
        requestHash,
        workingPackage.workingRevision,
        dependencyRevision,
      ) as { error_json: string } | undefined;
    if (receipt) {
      const stored = JSON.parse(receipt.error_json) as {
        code: string;
        message: string;
        next: string[];
        statusCode: number;
        details: Record<string, unknown>;
      };
      throw new AgentCoreError(
        stored.code,
        stored.message,
        stored.next,
        stored.statusCode,
        { ...stored.details, replayed: true, negative_receipt: true },
      );
    }
    return {
      mutationId,
      requestHash,
      workingRevision: workingPackage.workingRevision,
      dependencyRevision,
    };
  }

  private recordDomainFailure(
    context: AgentToolContext,
    receipt: DomainFailureReceiptContext | undefined,
    rawError: unknown,
  ): AgentCoreError {
    const error = normalizeDomainAgentError(rawError);
    if (!receipt || error.statusCode >= 500 || error.code === 'MUTATION_ID_REUSED') return error;
    this.db.prepare(`INSERT OR IGNORE INTO agent_domain_mutation_failure_receipts (
      project_id, mutation_id, request_hash, working_revision, dependency_revision, error_json, created_at
    ) VALUES (?,?,?,?,?,?,?)`).run(
      context.project.id,
      receipt.mutationId,
      receipt.requestHash,
      receipt.workingRevision,
      receipt.dependencyRevision,
      JSON.stringify({
        code: error.code,
        message: error.message,
        next: error.next,
        statusCode: error.statusCode,
        details: error.details,
      }),
      nowIso(),
    );
    return error;
  }

  private async createDomainResource(
    context: AgentToolContext,
    args: Record<string, unknown>,
    kind: DomainKind,
  ) {
    let failureReceipt: DomainFailureReceiptContext | undefined;
    try {
      const mutationId = text(args.mutation_id ?? args.client_mutation_id, 'mutation_id');
      const requestHash = domainMutationRequestHash(`create:${kind}`, args);
      const replay = this.studioWrites.replayDomainMutation(context.project.id, mutationId, requestHash);
      if (replay) {
        return domainMutationResponse(replay, { action_count: 1, operations: [`create_${kind}`] });
      }
      failureReceipt = await this.prepareDomainFailureReceipt(context, mutationId, requestHash);
      const workingPackage = await this.workbenchStudioFiles(context.project, context.username);
      const studio = new StudioReadService(workingPackage);
      const expectedRevision = text(args.expected_revision ?? args.expectedRevision, 'expected_revision');
      studio.assertRevision(expectedRevision);
      const editSessionId = String(args.edit_session_id ?? '').trim();
      const body = {
        expectedRevision,
        clientMutationId: mutationId,
        ...(editSessionId ? { editSessionId } : {}),
        kind,
        key: text(args.key, 'key'),
        values: domainCreateValues(kind, args),
      };
      const response = await this.studioWrites.createDomainModel(
        context.project,
        context.username,
        body,
        requestHash,
        { allowObjectFieldWrite: kind === 'object' },
      );
      return domainMutationResponse(response, {
        action_count: 1,
        operations: [`create_${kind}`],
      });
    } catch (error) {
      throw this.recordDomainFailure(context, failureReceipt, error);
    }
  }

  private async patchDomainResource(
    context: AgentToolContext,
    args: Record<string, unknown>,
    expectedKind: Exclude<DomainKind, 'object'>,
  ) {
    let failureReceipt: DomainFailureReceiptContext | undefined;
    try {
      const mutationId = text(args.mutation_id ?? args.client_mutation_id, 'mutation_id');
      const requestHash = domainMutationRequestHash(`patch:${expectedKind}`, args);
      const replay = this.studioWrites.replayDomainMutation(context.project.id, mutationId, requestHash);
      if (replay) return domainMutationResponse(replay, domainMutationSummary(args));
      failureReceipt = await this.prepareDomainFailureReceipt(context, mutationId, requestHash);
      const { workingPackage, resourceId, element } = await this.loadDomainResourceContext(context, args, true);
      if (element.kind !== expectedKind) {
        throw new AgentCoreError(
          'DOMAIN_KIND_MISMATCH',
          `当前工具只能修改${domainKindLabel(expectedKind)}`,
          [`改用与 ${element.kind} 类型对应的 Patch 工具`],
          400,
          { resource_id: resourceId, actual_kind: element.kind, expected_kind: expectedKind },
        );
      }
      assertDomainResourceHash(element, args.expected_resource_hash, workingPackage.workingRevision);
      const compiled = compileDomainPatchActions(element, args.actions);
      const editSessionId = String(args.edit_session_id ?? '').trim();
      const response = await this.studioWrites.patchDomainModel(
        context.project,
        context.username,
        resourceId,
        {
          expectedRevision: workingPackage.workingRevision,
          clientMutationId: mutationId,
          ...(editSessionId ? { editSessionId } : {}),
          changes: compiled.changes,
        },
        requestHash,
      );
      return domainMutationResponse(response, {
        action_count: compiled.summary.actionCount,
        operations: compiled.summary.operations,
      });
    } catch (error) {
      throw this.recordDomainFailure(context, failureReceipt, error);
    }
  }

  private async patchObjectResource(context: AgentToolContext, args: Record<string, unknown>) {
    let failureReceipt: DomainFailureReceiptContext | undefined;
    try {
      const mutationId = text(args.mutation_id ?? args.client_mutation_id, 'mutation_id');
      const requestHash = domainMutationRequestHash('patch:object', args);
      const replay = this.studioWrites.replayDomainMutation(context.project.id, mutationId, requestHash);
      if (replay) return domainMutationResponse(replay, domainMutationSummary(args));
      failureReceipt = await this.prepareDomainFailureReceipt(context, mutationId, requestHash);
      const { workingPackage, resourceId, element } = await this.loadDomainResourceContext(context, args, true);
      if (element.kind !== 'object') {
        throw new AgentCoreError(
          'DOMAIN_KIND_MISMATCH',
          '当前工具只能修改对象',
          ['改用与资源类型对应的 Patch 工具'],
          400,
          { resource_id: resourceId, actual_kind: element.kind, expected_kind: 'object' },
        );
      }
      assertDomainResourceHash(element, args.expected_resource_hash, workingPackage.workingRevision);
      const compiled = compileObjectPatchActions(element, args.actions);
      const preview = previewOntologyObjectMutation(
        parseWorkingPackageSpec(workingPackage.files),
        element.key,
        {
          metadata: compiled.metadata,
          field_ops: compiled.field_ops,
          relation_ops: compiled.relation_ops,
        },
        { tables: this.projectTables(context.project.id) },
      );
      if (!preview.ok) {
        throw new AgentCoreError(
          'DOMAIN_PATCH_INVALID',
          '对象增量修改未通过确定性校验',
          ['根据 fieldErrors 只修正对应动作后重新提交，不要完整重写对象'],
          422,
          {
            fieldErrors: preview.fieldErrors,
            errors: preview.issues,
            warnings: preview.warnings,
            impact_summary: preview.impactSummary,
          },
        );
      }
      const editSessionId = String(args.edit_session_id ?? '').trim();
      const response = await this.studioWrites.patchDomainModel(
        context.project,
        context.username,
        resourceId,
        {
          expectedRevision: workingPackage.workingRevision,
          clientMutationId: mutationId,
          ...(editSessionId ? { editSessionId } : {}),
          changes: preview.patchChanges,
        },
        requestHash,
        { allowObjectFieldWrite: true },
      );
      return {
        ...domainMutationResponse(response, {
          action_count: compiled.summary.actionCount,
          operations: compiled.summary.operations,
        }),
        impact_summary: preview.impactSummary,
        normalized_mutation: preview.normalizedMutation,
      };
    } catch (error) {
      throw this.recordDomainFailure(context, failureReceipt, error);
    }
  }

  private async patchObjectSourceBinding(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const tool = 'patch_object_source_binding';
    const mutationId = text(args.mutation_id, 'mutation_id');
    const requestHash = domainMutationRequestHash(tool, args);
    const replay = this.replayStructuredMutation(context, tool, mutationId, requestHash);
    if (replay) return replay;
    const { workingPackage, element } = await this.loadDomainResourceContext(context, args, true);
    if (element.kind !== 'object') {
      throw new AgentCoreError('DOMAIN_KIND_MISMATCH', '数据源只能绑定对象资源', [], 400);
    }
    assertDomainResourceHash(element, args.expected_resource_hash, workingPackage.workingRevision);
    const result = await this.bindObjectSource(workspace, context, {
      key: element.key,
      document_id: args.document_id,
      table: args.table,
      field_bindings: args.field_bindings,
      primary_key: args.primary_key,
      evidence: args.evidence,
      change_group: `binding-${createHash('sha256').update(mutationId).digest('hex').slice(0, 20)}`,
    });
    const nextWorking = await this.workbenchStudioFiles(context.project, context.username);
    const nextElement = new StudioReadService(nextWorking).detail(element.id).element;
    const response = {
      ...result,
      working_revision: nextWorking.workingRevision,
      mutation_id: mutationId,
      resource: domainResourcePayload(nextElement),
      candidate: true,
    };
    this.saveStructuredMutation(context, tool, mutationId, requestHash, response);
    return response;
  }

  private async syncSchemaObjects(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const tool = 'sync_schema_objects';
    const mutationId = text(args.mutation_id, 'mutation_id');
    const requestHash = domainMutationRequestHash(tool, args);
    const replay = this.replayStructuredMutation(context, tool, mutationId, requestHash);
    if (replay) return replay;

    const workingPackage = await this.workbenchStudioFiles(context.project, context.username);
    new StudioReadService(workingPackage).assertRevision(text(args.expected_revision, 'expected_revision'));
    const projectTables = this.tables(context.project.id);
    const tables = schemaTables(projectTables);
    if (!tables.length) {
      throw new AgentCoreError(
        'PHYSICAL_SCHEMA_REQUIRED',
        '没有找到明确标记为数据 Schema 的已解析 Sheet',
        ['先在资料设置中把确定的 Excel 或 CSV 标记为数据 Schema'],
        409,
        { retryable: false },
      );
    }

    const current = await this.overlay(workspace);
    const physical = preservePhysicalSchemaSemantics(
      expectedSchemaObjects(projectTables),
      current.spec.objects ?? [],
    );
    const previousRelations = collectPreviousObjectRelations(
      current.spec.objects ?? [],
      physical.map((item) => item.object),
    );
    const nextSpec = validateSpecKeys(canonicalizePackageSpec({
      ...current.spec,
      objects: physical.map((item) => item.object),
    }));
    const rendered = renderPackage(nextSpec);
    const mapping = rendered[OBJECT_DATA_MAPPING_PATH];
    if (!mapping) {
      throw new AgentCoreError(
        'SCHEMA_MAPPING_MISSING',
        '确定性编译没有生成 Schema 对象 mapping',
        [],
        500,
      );
    }
    const objectPaths = physical.map((item) => `objects/${item.object.key}.md`);
    const expectedObjectPaths = new Set(objectPaths);
    const staleObjectPaths = Object.keys(current.files).filter((path) => (
      /^objects\/[^/]+\.md$/u.test(path)
      && path !== 'objects/index.md'
      && !expectedObjectPaths.has(path)
    ));
    const detailPath = objectPaths[0];
    if (detailPath) await this.assertCandidateStageGate(workspace, detailPath);
    await this.discardFrozenCandidate(workspace, context, 'Schema 物理对象重新同步，旧确认请求已失效');

    this.db.exec('SAVEPOINT agent_schema_object_sync');
    try {
      for (const path of objectPaths) {
        await this.upsertChange(workspace, {
          path,
          kind: 'write',
          content: rendered[path],
          groupId: 'schema_objects',
          evidence: [],
          reason: '根据已确认数据 Schema 确定性生成物理对象',
        });
      }
      for (const path of staleObjectPaths) {
        await this.upsertChange(workspace, {
          path,
          kind: 'delete',
          content: null,
          groupId: 'schema_objects',
          evidence: [],
          reason: 'Schema 模式移除不对应物理 Sheet 的派生对象',
        });
      }
      for (const path of ['objects/index.md', OBJECT_DATA_MAPPING_PATH]) {
        await this.upsertChange(workspace, {
          path,
          kind: 'write',
          content: rendered[path],
          groupId: COMPILED_RESOURCES_GROUP,
          evidence: [],
          reason: 'Schema 对象确定性编译产物',
        });
      }
      await this.upsertChange(workspace, {
        path: INTERNAL_SPEC_CHANGE_PATH,
        kind: 'write',
        content: `${JSON.stringify(nextSpec, null, 2)}\n`,
        groupId: COMPILED_RESOURCES_GROUP,
        evidence: [],
        reason: 'Schema 对象确定性编译的语义规格',
      });
      this.db.exec('RELEASE SAVEPOINT agent_schema_object_sync');
    } catch (error) {
      this.db.exec('ROLLBACK TO SAVEPOINT agent_schema_object_sync');
      this.db.exec('RELEASE SAVEPOINT agent_schema_object_sync');
      throw error;
    }

    const nextWorking = await this.workbenchStudioFiles(context.project, context.username);
    const response = {
      ok: true,
      deterministic: true,
      mutation_id: mutationId,
      working_revision: nextWorking.workingRevision,
      schema_table_count: tables.length,
      object_count: physical.length,
      removed_derived_object_count: staleObjectPaths.length,
      previousRelations,
      objects: physical.map(({ table, object }) => ({
        resource_id: `object:${object.key}`,
        key: object.key,
        title: object.title,
        document_id: table.documentId,
        table: table.name,
        field_count: object.fields.length,
        primary_key: object.primaryKey ?? [],
      })),
      rendered_paths: [...objectPaths, 'objects/index.md', OBJECT_DATA_MAPPING_PATH],
      candidate: true,
      display: { text: `已按 ${tables.length} 张 Schema Sheet 确定性同步 ${physical.length} 个物理对象及 mapping` },
    };
    this.saveStructuredMutation(context, tool, mutationId, requestHash, response);
    return response;
  }

  private async stageSchemaSemanticChanges(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const tool = 'stage_schema_semantic_changes';
    const mutationId = text(args.mutation_id, 'mutation_id');
    const requestHash = domainMutationRequestHash(tool, args);
    const replay = this.replayStructuredMutation(context, tool, mutationId, requestHash);
    if (replay) return replay;
    try {
      const requested = requiredObjectArray(args.objects, 'objects');
      if (requested.length < 1 || requested.length > 20) {
        throw new AgentCoreError(
          'INVALID_DOMAIN_PAYLOAD',
          '单批 Schema 语义修改必须包含 1 到 20 个对象',
          [],
          400,
        );
      }
      const fieldActionCount = requested.reduce((count, item) => (
        count + (Array.isArray(item.actions)
          ? item.actions.filter((action) => object(action).op === 'update_field').length
          : 0)
      ), 0);
      if (fieldActionCount > 400) {
        throw new AgentCoreError(
          'INVALID_DOMAIN_PAYLOAD',
          '单批 Schema 语义修改最多处理 400 个字段',
          [],
          400,
        );
      }

      const workingPackage = await this.workbenchStudioFiles(context.project, context.username);
      const studio = new StudioReadService(workingPackage);
      studio.assertRevision(text(args.expected_revision, 'expected_revision'));
      const resourceIds = requested.map((item) => domainResourceIdArg(item.resource_id));
      if (new Set(resourceIds).size !== resourceIds.length) {
        throw new AgentCoreError('INVALID_DOMAIN_PAYLOAD', '同一批不能重复修改同一个 Schema 对象', [], 400);
      }
      let previewSpec = parseWorkingPackageSpec(workingPackage.files);
      const beforeMapping = renderPackage(previewSpec)[OBJECT_DATA_MAPPING_PATH] ?? '';
      const patches: Array<{ elementId: string; changes: Record<string, unknown> }> = [];
      const operations: string[] = [];
      let actionCount = 0;
      for (let index = 0; index < requested.length; index += 1) {
        const item = requested[index];
        const resourceId = resourceIds[index];
        const element = studio.detail(resourceId).element;
        if (element.kind !== 'object') {
          throw new AgentCoreError('DOMAIN_KIND_MISMATCH', 'Schema 语义修改只接受对象资源', [], 400, {
            resource_id: resourceId,
          });
        }
        assertDomainResourceHash(element, item.expected_resource_hash, workingPackage.workingRevision);
        assertSchemaSemanticPatchActions(item.actions, index);
        const compiled = compileObjectPatchActions(element, item.actions);
        const preview = previewOntologyObjectMutation(
          previewSpec,
          element.key,
          { metadata: compiled.metadata, field_ops: compiled.field_ops },
          { tables: this.projectTables(context.project.id) },
        );
        if (!preview.ok || !preview.nextSpec) {
          throw new AgentCoreError(
            'DOMAIN_PATCH_INVALID',
            `对象 ${element.key} 的语义修改未通过确定性校验`,
            ['只修正返回的字段问题，不要修改对象 key、字段 key、来源列、类型或主键'],
            422,
            { fieldErrors: preview.fieldErrors, errors: preview.issues, warnings: preview.warnings },
          );
        }
        previewSpec = preview.nextSpec;
        patches.push({ elementId: resourceId, changes: preview.patchChanges });
        actionCount += compiled.summary.actionCount;
        operations.push(...compiled.summary.operations);
      }
      const afterMapping = renderPackage(previewSpec)[OBJECT_DATA_MAPPING_PATH] ?? '';
      assertOnlyRequiredMappingChanged(beforeMapping, afterMapping);
      const response = await this.studioWrites.batchDomainModel(
        context.project,
        context.username,
        {
          expectedRevision: workingPackage.workingRevision,
          clientMutationId: mutationId,
          editSessionId: mutationId,
          creates: [],
          patches,
        },
        requestHash,
      );
      const nextStatus = await this.schemaSemanticStatus(context);
      const result = {
        ...domainMutationResponse(response, { action_count: actionCount, operations }),
        mutation_id: mutationId,
        semantic_status: schemaSemanticSummary(nextStatus.status),
        candidate: true,
        display: {
          text: `已形成 ${requested.length} 个对象的语义候选，仍有 ${nextStatus.status.errorCount} 项阻断问题`,
        },
      };
      this.saveStructuredMutation(context, tool, mutationId, requestHash, result);
      return result;
    } catch (error) {
      throw normalizeDomainAgentError(error);
    }
  }

  private async confirmSchemaSemantics(
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const tool = 'confirm_schema_semantics';
    if (!this.changeControl.activePlan(context.project.id, context.runId)) {
      throw new AgentCoreError(
        'CHANGE_PLAN_REQUIRED',
        'Schema 业务语义确认必须属于当前受控建模计划',
        ['先建立包含 Schema 同步及其末尾语义阶段的 generation 计划'],
        409,
        { retryable: false },
      );
    }
    const mutationId = text(args.mutation_id, 'mutation_id');
    const requestHash = domainMutationRequestHash(tool, args);
    const replay = this.replayStructuredMutation(context, tool, mutationId, requestHash);
    if (replay) return replay;
    const { workingPackage, status } = await this.schemaSemanticStatus(context);
    new StudioReadService(workingPackage).assertRevision(text(args.expected_revision, 'expected_revision'));
    if (!status.available) {
      throw new AgentCoreError(
        'PHYSICAL_SCHEMA_REQUIRED',
        '当前项目没有可确认的物理 Schema 对象',
        [],
        409,
        { retryable: false },
      );
    }
    if (!status.pass) {
      throw new AgentCoreError(
        'SCHEMA_SEMANTICS_INCOMPLETE',
        `Schema 对象业务语义仍有 ${status.errorCount} 项阻断问题`,
        ['调用 get_schema_semantics 分页读取问题；只处理对象标题、对象说明、字段中文名和 required 归一化问题'],
        409,
        {
          retryable: false,
          ...schemaSemanticSummary(status),
          findings: status.issues.filter((item) => item.level === 'error').slice(0, 100),
        },
      );
    }
    const result = {
      ok: true,
      mutation_id: mutationId,
      working_revision: workingPackage.workingRevision,
      semantic_status: schemaSemanticSummary(status),
      display: { text: `已确认 ${status.objectCount} 个 Schema 对象、${status.fieldCount} 个字段的业务语义` },
    };
    this.saveStructuredMutation(context, tool, mutationId, requestHash, result);
    return result;
  }

  private async suggestObjectRelations(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const workingPackage = await this.workbenchStudioFiles(context.project, context.username);
    const requestedRevision = String(args.working_revision ?? args.workingRevision ?? args.expected_revision ?? '').trim();
    if (requestedRevision) new StudioReadService(workingPackage).assertRevision(requestedRevision);
    const current = await this.overlay(workspace);
    const result = suggestObjectRelationsFromSpec(
      current.spec,
      schemaTables(this.tables(context.project.id)),
      {
        limit: integer(args.limit, 10, 1, 20),
        cursor: optionalInputText(args.cursor),
        includeLowConfidence: args.includeLowConfidence === true || args.include_low_confidence === true,
        previousRelations: optionalPreviousObjectRelations(args.previousRelations ?? args.previous_relations),
        rejectedFingerprints: optionalStringArray(args.rejectedFingerprints ?? args.rejected_fingerprints),
        scope: optionalSuggestionScope(args.scope),
      },
    );
    return {
      ok: true,
      deterministic: true,
      working_revision: workingPackage.workingRevision,
      ...result,
      candidate: false,
      display: {
        text: `Suggested ${result.returnedCount} object relation candidate(s); ${result.totalCandidateCount} candidate(s) after filters.`,
      },
    };
  }

  private async previewDomainDelete(context: AgentToolContext, args: Record<string, unknown>) {
    try {
      const expectedRevision = text(args.expected_revision ?? args.expectedRevision, 'expected_revision');
      const resourceIds = requiredStringArray(args.resource_ids ?? args.element_ids, 'resource_ids');
      const response = await this.studioWrites.previewDelete(context.project, context.username, {
        expectedRevision,
        elementIds: resourceIds,
      });
      return {
        working_revision: response.workingRevision,
        resource_ids: response.elementIds,
        references: response.references.map((item) => ({
          source_id: item.sourceId,
          source_title: item.sourceTitle,
          field: item.field,
          target_id: item.targetId,
        })),
        affected_resource_ids: response.affectedElementIds,
        preview_token: response.previewToken,
        cleanup_required: response.cleanupRequired,
        display: response.display,
      };
    } catch (error) {
      throw normalizeDomainAgentError(error);
    }
  }

  private async deleteDomainResource(context: AgentToolContext, args: Record<string, unknown>) {
    let failureReceipt: DomainFailureReceiptContext | undefined;
    try {
      const mutationId = text(args.mutation_id ?? args.client_mutation_id, 'mutation_id');
      const requestHash = domainMutationRequestHash('delete', args);
      const replay = this.studioWrites.replayDomainMutation(context.project.id, mutationId, requestHash);
      if (replay) return domainMutationResponse(replay, domainMutationSummary(args, 'delete_resource'));
      failureReceipt = await this.prepareDomainFailureReceipt(context, mutationId, requestHash);
      const expectedRevision = text(args.expected_revision ?? args.expectedRevision, 'expected_revision');
      const resourceIds = requiredStringArray(args.resource_ids ?? args.element_ids, 'resource_ids');
      const editSessionId = String(args.edit_session_id ?? '').trim();
      const response = await this.studioWrites.deleteDomainModel(context.project, context.username, {
        expectedRevision,
        clientMutationId: mutationId,
        ...(editSessionId ? { editSessionId } : {}),
        elementIds: resourceIds,
        previewToken: text(args.preview_token ?? args.previewToken, 'preview_token'),
        cleanupReferences: args.cleanup_references === true,
      }, requestHash);
      return domainMutationResponse(response, {
        action_count: resourceIds.length,
        operations: resourceIds.map(() => 'delete_resource'),
      });
    } catch (error) {
      throw this.recordDomainFailure(context, failureReceipt, error);
    }
  }

  private replayStructuredMutation(
    context: AgentToolContext,
    tool: string,
    mutationId: string,
    requestHash: string,
  ): Record<string, unknown> | null {
    const receipt = this.db.prepare(`SELECT tool, request_hash, response_json
      FROM agent_structured_mutation_receipts WHERE project_id = ? AND mutation_id = ?`)
      .get(context.project.id, mutationId) as
      | { tool: string; request_hash: string; response_json: string }
      | undefined;
    if (!receipt) return null;
    if (receipt.tool !== tool || receipt.request_hash !== requestHash) {
      throw new AgentCoreError(
        'MUTATION_ID_REUSED',
        '这个操作标识已经用于另一项修改，请生成新的操作标识后重试',
        [],
        409,
        { mutation_id: mutationId, error_class: 'conflict', retryable: false, repairable: true },
      );
    }
    return { ...(JSON.parse(receipt.response_json) as Record<string, unknown>), replayed: true };
  }

  private saveStructuredMutation(
    context: AgentToolContext,
    tool: string,
    mutationId: string,
    requestHash: string,
    response: Record<string, unknown>,
  ): void {
    this.db.prepare(`INSERT INTO agent_structured_mutation_receipts (
      project_id, mutation_id, tool, request_hash, response_json, created_at
    ) VALUES (?,?,?,?,?,?)`).run(
      context.project.id,
      mutationId,
      tool,
      requestHash,
      JSON.stringify(response),
      nowIso(),
    );
  }

  private deterministicCapabilityState(
    projectId: string,
    spec: PackageSpec,
  ): DeterministicCapabilityView {
    const scopeHash = hashStructuredValue('deterministic-capability-scope-v1', {
      objects: spec.objects ?? [],
      operations: (spec.operations ?? []).map((operation) => {
        const value = structuredClone(operation);
        delete value.script;
        return value;
      }),
      rules: spec.rules ?? [],
      metrics: spec.metrics ?? [],
      dataSourceType: spec.dataSourceType ?? null,
    });
    const operationsForAnalysis = (spec.operations ?? []).map((operation) => {
      const value = structuredClone(operation);
      delete value.script;
      return value;
    });
    const scripts = new Map((spec.scripts ?? []).map((script) => [script.key, script]));
    const operationByKey = new Map((spec.operations ?? []).map((operation) => [operation.key, operation]));
    const candidates: DeterministicCapabilityCandidateView[] = identifyScriptCandidates(
      operationsForAnalysis,
      spec.metrics ?? [],
    ).map((candidate) => {
      const operation = operationByKey.get(candidate.operationKey);
      const scriptKey = operation?.script?.key ?? '';
      const script = scriptKey ? scripts.get(scriptKey) : undefined;
      const covered = Boolean(script && script.boundOperation === candidate.operationKey);
      return {
        operation_key: candidate.operationKey,
        title: candidate.title,
        confidence: candidate.confidence,
        reason: candidate.reason,
        ...(candidate.suggestedContract ? { suggested_contract: candidate.suggestedContract } : {}),
        composes_operation_keys: candidate.invokesOperationKeys ?? [],
        already_covered: covered,
        bound_script_key: covered ? scriptKey : null,
      };
    });
    const uncovered = candidates.filter((candidate) => !candidate.already_covered);
    const row = this.db.prepare(`SELECT scope_hash, selected_operation_keys_json, reason, decided_by, decided_at
      FROM agent_deterministic_scope_decisions WHERE project_id = ?`).get(projectId) as
      | {
        scope_hash: string;
        selected_operation_keys_json: string;
        reason: string;
        decided_by: string;
        decided_at: string;
      }
      | undefined;
    const currentDecision = row?.scope_hash === scopeHash ? row : undefined;
    const staleDecision = Boolean(row && row.scope_hash !== scopeHash);
    const allowed = new Set(candidates.map((candidate) => candidate.operation_key));
    const selected = currentDecision
      ? parseArray(currentDecision.selected_operation_keys_json).filter((key) => allowed.has(key))
      : [];
    const selectedSet = new Set(selected);
    const missingBindings = selected.filter((key) => (
      !candidates.find((candidate) => candidate.operation_key === key)?.already_covered
    ));
    const missingTests = currentDecision ? selected.filter((key) => {
      const operation = operationByKey.get(key);
      const scriptKey = operation?.script?.key ?? '';
      const script = scriptKey ? scripts.get(scriptKey) : undefined;
      if (!script || script.boundOperation !== key) return false;
      return !this.db.prepare(`SELECT 1 FROM agent_script_test_receipts
        WHERE project_id = ? AND script_path = ? AND source_hash = ? AND created_at >= ? LIMIT 1`)
        .get(
          projectId,
          `scripts/${script.entrypoint}`,
          scriptSourceHash(script.sourceCode),
          currentDecision.decided_at,
        );
    }) : [];
    const skipped = currentDecision
      ? uncovered.map((candidate) => candidate.operation_key).filter((key) => !selectedSet.has(key))
      : [];
    let status: DeterministicCapabilityView['status'];
    let displayText: string;
    if (!candidates.length) {
      status = 'not_required';
      displayText = '未发现需要确定性脚本的业务操作';
    } else if (!uncovered.length && !row) {
      status = 'covered';
      displayText = `已存在 ${candidates.length} 个有效脚本绑定`;
    } else if (!currentDecision || staleDecision) {
      status = 'decision_required';
      displayText = staleDecision
        ? '业务知识已经变化，等待专家重新确认确定性脚本范围'
        : `发现 ${uncovered.length} 个可能需要确定性计算的业务操作，等待专家确认范围`;
    } else if (missingBindings.length || missingTests.length) {
      status = 'implementation_required';
      displayText = `已确认 ${selected.length} 个确定性脚本，仍需完成 ${missingBindings.length} 个绑定和 ${missingTests.length} 个沙箱验证`;
    } else {
      status = 'ready';
      displayText = selected.length
        ? `确定性脚本范围已确认，${selected.length} 个脚本均已完成绑定`
        : '专家已确认本轮不生成确定性脚本';
    }
    return {
      scope_hash: scopeHash,
      status,
      candidates,
      selected_operation_keys: selected,
      skipped_operation_keys: skipped,
      missing_script_bindings: missingBindings,
      missing_script_tests: missingTests,
      decision: currentDecision ? {
        reason: currentDecision.reason,
        decided_by: currentDecision.decided_by,
        decided_at: currentDecision.decided_at,
      } : null,
      display: { text: displayText },
    };
  }

  private async analyzeDeterministicCapabilities(
    workspace: WorkspaceRow,
  ): Promise<DeterministicCapabilityView> {
    // Scope analysis and script authoring must observe the exact same Studio view.
    // Rendered resources can normalize defaults that are absent from the stored spec.
    const changes = this.changes(workspace);
    const workingPackage = await this.overlay(workspace, changes);
    return this.deterministicCapabilityState(
      workspace.project_id,
      resolveAgentStudioSpec(workingPackage.spec, workingPackage.files, changes),
    );
  }

  private async confirmDeterministicScope(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ): Promise<DeterministicCapabilityView> {
    const expectedScopeHash = text(args.expected_scope_hash, 'expected_scope_hash');
    const current = await this.analyzeDeterministicCapabilities(workspace);
    if (expectedScopeHash !== current.scope_hash) {
      throw new AgentCoreError(
        'DETERMINISTIC_SCOPE_CHANGED',
        '业务知识已经变化，不能沿用刚才的脚本范围决定',
        ['重新分析当前确定性计算候选，并请专家按最新范围确认'],
        409,
        {
          expected_scope_hash: expectedScopeHash,
          actual_scope_hash: current.scope_hash,
          candidates: current.candidates,
          retryable: false,
        },
      );
    }
    if (!Array.isArray(args.selected_operation_keys)) {
      throw new AgentCoreError(
        'INVALID_INPUT',
        'selected_operation_keys 必须是数组；空数组表示明确不生成脚本',
        [],
        400,
      );
    }
    const selected = [...new Set(args.selected_operation_keys.map((item) => String(item).trim())
      .filter(Boolean))];
    const allowed = new Set(current.candidates.map((candidate) => candidate.operation_key));
    const unknown = selected.filter((key) => !allowed.has(key));
    if (unknown.length) {
      throw new AgentCoreError(
        'INVALID_INPUT',
        `所选业务操作不属于当前确定性计算候选：${unknown.join('、')}`,
        ['使用最新分析结果中的候选范围重新确认'],
        400,
      );
    }
    const reason = text(args.reason, 'reason');
    this.db.prepare(`INSERT INTO agent_deterministic_scope_decisions (
      project_id, scope_hash, candidates_json, selected_operation_keys_json,
      reason, decided_by, decided_at
    ) VALUES (?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
      scope_hash = excluded.scope_hash,
      candidates_json = excluded.candidates_json,
      selected_operation_keys_json = excluded.selected_operation_keys_json,
      reason = excluded.reason,
      decided_by = excluded.decided_by,
      decided_at = excluded.decided_at`).run(
      workspace.project_id,
      current.scope_hash,
      JSON.stringify(current.candidates),
      JSON.stringify(selected),
      reason,
      context.username,
      nowIso(),
    );
    return this.analyzeDeterministicCapabilities(workspace);
  }

  private async defineScriptContract(context: AgentToolContext, args: Record<string, unknown>) {
    const tool = 'define_script_contract';
    const mutationId = text(args.mutation_id, 'mutation_id');
    const requestHash = domainMutationRequestHash(tool, args);
    const replay = this.replayStructuredMutation(context, tool, mutationId, requestHash);
    if (replay) return replay;
    const workingPackage = await this.workbenchStudioFiles(context.project, context.username);
    const studio = new StudioReadService(workingPackage);
    const expectedRevision = text(args.expected_revision, 'expected_revision');
    studio.assertRevision(expectedRevision);
    const operationId = operationResourceIdArg(args.operation_id);
    const operation = studio.detail(operationId).element;
    const deterministic = this.deterministicCapabilityState(
      context.project.id,
      parseWorkingPackageSpec(workingPackage.files),
    );
    const deterministicCandidate = deterministic.candidates.find(
      (candidate) => candidate.operation_key === operation.key,
    );
    if (deterministicCandidate
      && (deterministic.status === 'decision_required'
        || (!deterministicCandidate.already_covered
          && !deterministic.selected_operation_keys.includes(operation.key)))) {
      throw new AgentCoreError(
        'SCRIPT_SCOPE_NOT_CONFIRMED',
        '这个确定性计算操作尚未被专家纳入脚本生成范围',
        ['先分析确定性计算候选并通过人工确认卡确定脚本范围'],
        409,
        {
          operation_key: operation.key,
          scope_hash: deterministic.scope_hash,
          deterministic_status: deterministic.status,
          retryable: false,
        },
      );
    }
    const expectedOperationHash = text(args.expected_operation_hash, 'expected_operation_hash');
    const actualOperationHash = scriptOperationContractHash(operation);
    if (expectedOperationHash !== actualOperationHash) {
      throw new AgentCoreError(
        'RESOURCE_HASH_CONFLICT',
        '绑定操作已经变化，请重新读取后定义脚本契约',
        ['重新调用 get_domain_resource 读取操作，并使用 operation_contract_hash'],
        409,
        {
          expected_operation_hash: expectedOperationHash,
          actual_operation_hash: actualOperationHash,
          actual_revision: workingPackage.workingRevision,
          retryable: false,
        },
      );
    }
    const contract = scriptContractFromArgs(args, operation.key);
    assertMaterialEvidenceReferences(context.project.id, contract.evidence ?? []);
    const contractHash = hashStructuredValue('script-contract-v1', contract);
    this.db.prepare(`INSERT INTO agent_script_contract_drafts (
      project_id, run_id, script_key, operation_hash, contract_hash, contract_json, updated_at
    ) VALUES (?,?,?,?,?,?,?) ON CONFLICT(project_id, run_id, script_key) DO UPDATE SET
      operation_hash = excluded.operation_hash,
      contract_hash = excluded.contract_hash,
      contract_json = excluded.contract_json,
      updated_at = excluded.updated_at`).run(
      context.project.id,
      context.runId,
      contract.key,
      actualOperationHash,
      contractHash,
      JSON.stringify(contract),
      nowIso(),
    );
    const response = {
      working_revision: workingPackage.workingRevision,
      contract_hash: contractHash,
      operation_contract_hash: actualOperationHash,
      contract: scriptContractView(contract),
      candidate: false,
      next: ['提交与契约一致的 Python 源码；源码通过确定性校验后才形成待确认候选'],
      display: { text: `已定义脚本《${contract.title}》的执行契约` },
    };
    this.saveStructuredMutation(context, tool, mutationId, requestHash, response);
    return response;
  }

  private async getScriptContract(context: AgentToolContext, args: Record<string, unknown>) {
    const key = domainKey(args.script_key, '脚本');
    const row = this.db.prepare(`SELECT operation_hash, contract_hash, contract_json, updated_at
      FROM agent_script_contract_drafts WHERE project_id = ? AND run_id = ? AND script_key = ?`)
      .get(context.project.id, context.runId, key) as
      | { operation_hash: string; contract_hash: string; contract_json: string; updated_at: string }
      | undefined;
    if (!row) {
      throw new AgentCoreError(
        'SCRIPT_CONTRACT_NOT_FOUND',
        `脚本契约不存在：${key}`,
        ['先读取目标操作并调用 define_script_contract'],
        404,
      );
    }
    const contract = JSON.parse(row.contract_json) as Omit<SpecScript, 'sourceCode'>;
    return {
      contract_hash: row.contract_hash,
      operation_contract_hash: row.operation_hash,
      updated_at: row.updated_at,
      contract: scriptContractView(contract),
      display: { text: `已返回脚本《${contract.title}》的执行契约` },
    };
  }

  private async scriptEditState(
    context: AgentToolContext,
    scriptKey: string,
    options: { expectedRevision?: string; expectedContractHash?: string } = {},
  ) {
    const workingPackage = await this.workbenchStudioFiles(context.project, context.username);
    const studio = new StudioReadService(workingPackage);
    if (options.expectedRevision) studio.assertRevision(options.expectedRevision);
    const row = this.db.prepare(`SELECT operation_hash, contract_hash, contract_json, updated_at
      FROM agent_script_contract_drafts WHERE project_id = ? AND run_id = ? AND script_key = ?`)
      .get(context.project.id, context.runId, scriptKey) as
      | {
        operation_hash: string;
        contract_hash: string;
        contract_json: string;
        updated_at: string;
      }
      | undefined;
    if (!row) {
      throw new AgentCoreError(
        'SCRIPT_CONTRACT_NOT_FOUND',
        `脚本契约不存在：${scriptKey}`,
        ['先调用 define_script_contract，确认输入、输出、超时和执行边界'],
        404,
      );
    }
    if (options.expectedContractHash && options.expectedContractHash !== row.contract_hash) {
      throw new AgentCoreError(
        'SCRIPT_CONTRACT_CONFLICT',
        '脚本契约已经变化，请重新读取脚本编辑上下文',
        ['调用 get_script_edit_context 获取当前 contract_hash 和源码哈希'],
        409,
        { actual_contract_hash: row.contract_hash, retryable: false },
      );
    }
    const contract = JSON.parse(row.contract_json) as Omit<SpecScript, 'sourceCode'>;
    const operationElement = studio.detail(`operation:${contract.boundOperation}`).element;
    const operationHash = scriptOperationContractHash(operationElement);
    if (operationHash !== row.operation_hash) {
      throw new AgentCoreError(
        'RESOURCE_HASH_CONFLICT',
        '绑定操作已经变化，旧脚本契约不能继续使用',
        ['重新定义脚本契约后再读取脚本编辑上下文'],
        409,
        { actual_operation_hash: operationHash, retryable: false },
      );
    }
    const spec = parseWorkingPackageSpec(workingPackage.files);
    const existing = (spec.scripts ?? []).find((item) => item.key === scriptKey);
    if (existing && existing.entrypoint !== contract.entrypoint) {
      throw new AgentCoreError(
        'SCRIPT_ENTRYPOINT_IMMUTABLE',
        '已有脚本的 entrypoint 与当前契约不一致',
        ['保持原 entrypoint；脚本文件重命名需要独立的引用预览和原子迁移'],
        409,
        { existing_entrypoint: existing.entrypoint, requested_entrypoint: contract.entrypoint },
      );
    }
    return {
      workingPackage,
      contract,
      contractHash: row.contract_hash,
      operationHash,
      spec,
      existing,
      scaffold: !existing || isCoreManagedPythonScaffold(existing.sourceCode),
      sourceCode: existing?.sourceCode ?? buildPythonScriptScaffold(contract),
    };
  }

  private async getScriptEditContext(
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const key = domainKey(args.script_key, '脚本');
    const requestedSymbols = [...new Set(stringArray(args.symbols).map((item) => item.trim()).filter(Boolean))];
    const state = await this.scriptEditState(context, key);
    let index: ReturnType<typeof selectPythonSourceSymbols>;
    try {
      index = selectPythonSourceSymbols(state.sourceCode, requestedSymbols);
    } catch (error) {
      throw new AgentCoreError(
        'INVALID_SCRIPT_SOURCE',
        error instanceof Error ? error.message : '无法解析 Python 脚本符号',
        ['修复当前脚本语法，或从最新符号索引重新选择目标'],
        409,
        { retryable: false },
      );
    }
    const operation = state.spec.operations.find((item) => item.key === state.contract.boundOperation);
    return {
      working_revision: state.workingPackage.workingRevision,
      script_key: key,
      contract_hash: state.contractHash,
      operation_contract_hash: state.operationHash,
      base_source_hash: index.baseSourceHash,
      canonical_source_hash: index.canonicalSourceHash,
      imports: index.imports,
      scaffold: state.scaffold,
      contract: scriptContractView(state.contract),
      operation: operation ? {
        key: operation.key,
        title: operation.title,
        description: operation.description,
        inputs: operation.inputs,
        outputs: operation.outputs,
        flow: operation.flow,
        related_objects: operation.relatedObjects,
      } : undefined,
      symbols: index.symbols.map((symbol) => ({
        kind: symbol.kind,
        qualified_name: symbol.qualifiedName,
        node_hash: symbol.nodeHash,
        dependencies: symbol.dependencies,
        ...(requestedSymbols.length ? { source: symbol.source } : {}),
      })),
      editable_kinds: ['replace_function', 'replace_assignment', 'insert_before_function'],
      next: requestedSymbols.length
        ? ['只提交目标符号所需的最小补丁，并携带当前源码与节点哈希']
        : ['指定需要修改的 symbols 再读取源码；新增脚本优先选择 run_business_logic'],
      display: {
        text: requestedSymbols.length
          ? `已返回脚本《${state.contract.title}》的 ${index.symbols.length} 个目标符号`
          : `已返回脚本《${state.contract.title}》的符号索引`,
      },
    };
  }

  private async stageScriptPatch(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const tool = 'stage_script_patch';
    const mutationId = text(args.mutation_id, 'mutation_id');
    const requestHash = domainMutationRequestHash(tool, args);
    const replay = this.replayStructuredMutation(context, tool, mutationId, requestHash);
    if (replay) return replay;
    const key = domainKey(args.script_key, '脚本');
    const expectedRevision = text(args.expected_revision, 'expected_revision');
    const expectedContractHash = text(args.expected_contract_hash, 'expected_contract_hash');
    const baseSourceHash = text(args.base_source_hash, 'base_source_hash');
    const state = await this.scriptEditState(context, key, {
      expectedRevision,
      expectedContractHash,
    });
    const rawEdits = requiredObjectArray(args.edits, 'edits');
    if (!rawEdits.length || rawEdits.length > 20) {
      throw new AgentCoreError('INVALID_SCRIPT_PATCH', 'edits 必须包含 1 到 20 个符号补丁', [], 400);
    }
    const edits: PythonSourceEdit[] = rawEdits.map((raw, index) => {
      const kind = text(raw.kind, `edits[${index}].kind`);
      if (!['replace_function', 'replace_assignment', 'insert_before_function'].includes(kind)) {
        throw new AgentCoreError(
          'INVALID_SCRIPT_PATCH',
          `edits[${index}].kind 不属于允许的脚本补丁类型`,
          [],
          400,
        );
      }
      const symbol = text(raw.symbol, `edits[${index}].symbol`);
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/u.test(symbol)) {
        throw new AgentCoreError('INVALID_SCRIPT_PATCH', `非法 Python 顶层符号：${symbol}`, [], 400);
      }
      if (state.scaffold && kind === 'replace_function' && symbol !== 'run_business_logic') {
        throw new AgentCoreError(
          'SCAFFOLD_NODE_IMMUTABLE',
          `新增脚本不能修改 Core 脚手架函数 ${symbol}`,
          ['只替换 run_business_logic；辅助函数使用 insert_before_function 新增'],
          409,
          { retryable: false },
        );
      }
      if (state.scaffold && kind === 'replace_assignment') {
        throw new AgentCoreError(
          'SCAFFOLD_NODE_IMMUTABLE',
          `新增脚本不能修改 Core 脚手架常量 ${symbol}`,
          ['把领域常量放入业务函数或新增辅助函数，不要修改输出契约常量'],
          409,
          { retryable: false },
        );
      }
      return {
        kind: kind as PythonSourceEdit['kind'],
        symbol,
        source: text(raw.source, `edits[${index}].source`, true),
        expectedNodeHash: text(raw.expected_node_hash, `edits[${index}].expected_node_hash`),
      } as PythonSourceEdit;
    });
    let sourceCode: string;
    try {
      sourceCode = applyPythonSourceEdits(state.sourceCode, edits, {
        expectedBaseSourceHash: baseSourceHash,
        requireNodeHashes: true,
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Python 符号补丁无法应用';
      const conflict = /基线源码已变化|目标节点已变化/u.test(message);
      throw new AgentCoreError(
        conflict ? 'SCRIPT_PATCH_CONFLICT' : 'INVALID_SCRIPT_PATCH',
        message,
        conflict
          ? ['重新调用 get_script_edit_context 获取当前源码与节点哈希后再生成补丁']
          : ['按当前符号索引修正补丁源码、目标或补丁类型'],
        conflict ? 409 : 400,
        { retryable: false },
      );
    }
    const staged = await this.stageScriptSource(workspace, context, {
      script_key: key,
      plan_task_id: args.plan_task_id,
      expected_revision: expectedRevision,
      expected_contract_hash: expectedContractHash,
      mutation_id: `patch-compile-${requestHash.replace(/^sha256:/u, '').slice(0, 32)}`,
      reason: text(args.reason, 'reason'),
      source_code: sourceCode,
    }, 'stage_script_patch');
    const response = {
      ...staged,
      base_source_hash: baseSourceHash,
      scaffold: state.scaffold,
      applied_edits: edits.map((edit) => ({ kind: edit.kind, symbol: edit.symbol })),
      display: { text: `脚本《${state.contract.title}》的 ${edits.length} 个符号补丁已形成待确认修改` },
    };
    this.saveStructuredMutation(context, tool, mutationId, requestHash, response);
    return response;
  }

  private async stageScriptSource(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
    plannedTool = 'stage_script_source',
  ) {
    const tool = 'stage_script_source';
    const mutationId = text(args.mutation_id, 'mutation_id');
    const requestHash = domainMutationRequestHash(tool, args);
    const replay = this.replayStructuredMutation(context, tool, mutationId, requestHash);
    if (replay) return replay;
    const expectedRevision = text(args.expected_revision, 'expected_revision');
    const workingPackage = await this.workbenchStudioFiles(context.project, context.username);
    const studio = new StudioReadService(workingPackage);
    studio.assertRevision(expectedRevision);
    const key = domainKey(args.script_key, '脚本');
    const row = this.db.prepare(`SELECT operation_hash, contract_hash, contract_json
      FROM agent_script_contract_drafts WHERE project_id = ? AND run_id = ? AND script_key = ?`)
      .get(context.project.id, context.runId, key) as
      | { operation_hash: string; contract_hash: string; contract_json: string }
      | undefined;
    if (!row) {
      throw new AgentCoreError(
        'SCRIPT_CONTRACT_NOT_FOUND',
        `脚本契约不存在：${key}`,
        ['先调用 define_script_contract，确认输入、输出、超时和执行边界'],
        404,
      );
    }
    if (text(args.expected_contract_hash, 'expected_contract_hash') !== row.contract_hash) {
      throw new AgentCoreError(
        'SCRIPT_CONTRACT_CONFLICT',
        '脚本契约已经变化，请重新读取后再提交源码',
        ['调用 get_script_contract 获取当前 contract_hash'],
        409,
        { actual_contract_hash: row.contract_hash, retryable: false },
      );
    }
    const contract = JSON.parse(row.contract_json) as Omit<SpecScript, 'sourceCode'>;
    const operationElement = studio.detail(`operation:${contract.boundOperation}`).element;
    const operationHash = scriptOperationContractHash(operationElement);
    if (operationHash !== row.operation_hash) {
      throw new AgentCoreError(
        'RESOURCE_HASH_CONFLICT',
        '绑定操作已经变化，旧脚本契约不能继续使用',
        ['重新定义脚本契约后再提交源码'],
        409,
        { actual_operation_hash: operationHash, retryable: false },
      );
    }
    const sourceCode = text(args.source_code, 'source_code', true);
    const script: SpecScript = { ...contract, sourceCode };
    const spec = parseWorkingPackageSpec(workingPackage.files);
    const existing = (spec.scripts ?? []).find((item) => item.key === key);
    if (existing && existing.entrypoint !== contract.entrypoint) {
      throw new AgentCoreError(
        'SCRIPT_ENTRYPOINT_IMMUTABLE',
        '已有脚本的 entrypoint 不能通过源码提交隐式重命名',
        ['保持原 entrypoint；脚本文件重命名需要独立的引用预览和原子迁移'],
        409,
        { existing_entrypoint: existing.entrypoint, requested_entrypoint: contract.entrypoint },
      );
    }
    const conflicting = (spec.scripts ?? []).find((item) => (
      item.key !== key && item.boundOperation === contract.boundOperation
    ));
    if (conflicting) {
      throw new AgentCoreError(
        'SCRIPT_OPERATION_ALREADY_BOUND',
        `操作 ${contract.boundOperation} 已绑定脚本 ${conflicting.key}`,
        ['先由专家决定保留哪一个脚本契约，不要静默覆盖绑定'],
        409,
      );
    }
    const scripts = [...(spec.scripts ?? [])];
    const scriptIndex = scripts.findIndex((item) => item.key === key);
    if (scriptIndex >= 0) scripts[scriptIndex] = script;
    else scripts.push(script);
    const operations = spec.operations.map((operation) => {
      if (existing && operation.key === existing.boundOperation && operation.key !== contract.boundOperation) {
        return { ...operation, script: undefined };
      }
      if (operation.key === contract.boundOperation) {
        return { ...operation, script: { key, required: true } };
      }
      return operation;
    });
    const renderedPaths = [
      `scripts/${key}.md`,
      `scripts/${contract.entrypoint}`,
      'scripts/index.md',
      `operations/${contract.boundOperation}.md`,
      ...(existing && existing.boundOperation !== contract.boundOperation
        ? [`operations/${existing.boundOperation}.md`]
        : []),
      'operations/index.md',
      'index.md',
    ];
    const sourceHash = hashStructuredValue('script-source-v1', sourceCode);
    await this.stageCompiledSpec(
      workspace,
      context,
      { ...spec, operations, scripts },
      renderedPaths,
      `script-${sourceHash.slice('sha256:'.length, 'sha256:'.length + 20)}`,
      contract.evidence ?? [],
      `脚本 ${key} 的契约、源码和操作绑定`,
      {
        runId: context.runId,
        taskId: String(args.plan_task_id ?? '').trim(),
        tool: plannedTool,
        targetKey: key,
      },
    );
    const response = {
      working_revision: (await this.workbenchStudioFiles(context.project, context.username)).workingRevision,
      script_key: key,
      contract_hash: row.contract_hash,
      source_hash: sourceHash,
      bound_operation: contract.boundOperation,
      rendered_paths: renderedPaths,
      deterministic: true,
      candidate: true,
      next: ['使用当前源码对应的真实用例运行脚本沙箱测试，通过后再发起候选确认'],
      display: { text: `脚本《${contract.title}》已通过本地契约检查并形成待确认修改` },
    };
    this.saveStructuredMutation(context, tool, mutationId, requestHash, response);
    return response;
  }

  private async listOntologyObjects(context: AgentToolContext, args: Record<string, unknown>) {
    try {
      const workingPackage = await this.workbenchStudioFiles(context.project, context.username);
      const studio = new StudioReadService(workingPackage);
      const requestedRevision = String(
        args.working_revision ?? args.workingRevision ?? args.expected_revision ?? args.expectedRevision ?? '',
      ).trim();
      if (requestedRevision) studio.assertRevision(requestedRevision);
      const response = studio.list({
        kind: 'object',
        query: args.query,
        sort: args.sort,
        order: args.order,
        cursor: args.cursor,
        limit: args.limit,
      });
      return {
        working_revision: response.workingRevision,
        items: response.items.map((item) => ({ ...item, object_id: item.id })),
        total: response.total,
        next_cursor: response.nextCursor,
        display: {
          text: response.total
            ? `已返回 ${response.items.length}/${response.total} 个对象`
            : '当前知识包还没有对象',
        },
      };
    } catch (error) {
      throw normalizeStudioAgentError(error);
    }
  }

  private async getOntologyObject(context: AgentToolContext, args: Record<string, unknown>) {
    try {
      const { workingPackage, element } = await this.loadOntologyObjectContext(context, args, false);
      return {
        working_revision: workingPackage.workingRevision,
        object: ontologyObjectPayload(element),
        display: { text: `已返回对象《${element.title}》的当前模型` },
      };
    } catch (error) {
      throw normalizeStudioAgentError(error);
    }
  }

  private async validateOntologyObjectPatch(context: AgentToolContext, args: Record<string, unknown>) {
    try {
      const { workingPackage, element, objectId } = await this.loadOntologyObjectContext(context, args, true);
      const preview = previewOntologyObjectMutation(
        parseWorkingPackageSpec(workingPackage.files),
        element.key,
        {
          metadata: args.metadata,
          field_ops: args.field_ops,
          relation_ops: args.relation_ops,
        },
        { tables: this.projectTables(context.project.id) },
      );
      return {
        working_revision: workingPackage.workingRevision,
        object_id: objectId,
        ok: preview.ok,
        errors: preview.issues,
        fieldErrors: preview.fieldErrors,
        warnings: preview.warnings,
        impact_summary: preview.impactSummary,
        normalized_mutation: preview.normalizedMutation,
        patch_changes: preview.patchChanges,
        next_object: preview.nextObject,
        review_recommendation: preview.reviewRecommendation,
        display: { text: preview.ok ? '对象模型修改校验通过' : '对象模型修改未通过校验' },
      };
    } catch (error) {
      throw normalizeStudioAgentError(error);
    }
  }

  private async applyOntologyObjectPatch(context: AgentToolContext, args: Record<string, unknown>) {
    try {
      const { workingPackage, objectId, element } = await this.loadOntologyObjectContext(context, args, true);
      const preview = previewOntologyObjectMutation(
        parseWorkingPackageSpec(workingPackage.files),
        element.key,
        {
          metadata: args.metadata,
          field_ops: args.field_ops,
          relation_ops: args.relation_ops,
        },
        { tables: this.projectTables(context.project.id) },
      );
      if (!preview.ok) {
        throw new AgentCoreError(
          'ONTOLOGY_OBJECT_INVALID',
          '对象模型修改未通过校验',
          ['根据 fieldErrors 修正后，重新调用 validate_ontology_object_patch 或 apply_ontology_object_patch'],
          422,
          {
            fieldErrors: preview.fieldErrors,
            errors: preview.issues,
            warnings: preview.warnings,
            impact_summary: preview.impactSummary,
            normalized_mutation: preview.normalizedMutation,
            next_object: preview.nextObject,
          },
        );
      }
      const clientMutationId = text(args.client_mutation_id ?? args.clientMutationId, 'client_mutation_id');
      const editSessionId = String(args.edit_session_id ?? args.editSessionId ?? '').trim();
      const response = await this.studioWrites.patchObjectModel(
        context.project,
        context.username,
        objectId,
        {
          expectedRevision: workingPackage.workingRevision,
          clientMutationId,
          ...(editSessionId ? { editSessionId } : {}),
          changes: preview.patchChanges,
        },
      );
      return {
        working_revision: response.workingRevision,
        client_mutation_id: response.clientMutationId,
        ...(response.editSessionId ? { edit_session_id: response.editSessionId } : {}),
        candidate: response.candidate,
        ...(response.replayed ? { replayed: true } : {}),
        change_group: response.changeGroup,
        changed_paths: response.changedPaths,
        object: response.element ? ontologyObjectPayload(response.element) : null,
        elements: response.elements.map((item) => ontologyObjectPayload(item)),
        warnings: response.warnings,
        historical_problem_count: response.historicalProblemCount,
        impact_summary: preview.impactSummary,
        normalized_mutation: preview.normalizedMutation,
        review_recommendation: preview.reviewRecommendation,
        display: response.display,
      };
    } catch (error) {
      throw normalizeStudioAgentError(error);
    }
  }

  private async submitOntologyObjectReview(context: AgentToolContext, args: Record<string, unknown>) {
    try {
      const expectedRevision = text(args.expected_revision ?? args.expectedRevision, 'expected_revision');
      const clientMutationId = text(args.client_mutation_id ?? args.clientMutationId, 'client_mutation_id');
      const response = await this.studioReviews.requestReview(context.project, context.username, {
        expectedRevision,
        clientMutationId,
      });
      return {
        working_revision: response.workingRevision,
        client_mutation_id: response.clientMutationId,
        review_requested: response.reviewRequested,
        ...(response.replayed ? { replayed: true } : {}),
        changed_paths: response.changedPaths,
        element_ids: response.elementIds,
        next: [
          '复核完成后，如需人工确认生效，继续调用 prepare_confirmation_groups、request_confirmation_preflight、request_confirmation',
        ],
        display: response.display,
      };
    } catch (error) {
      throw normalizeStudioAgentError(error);
    }
  }

  private async getOntologyObjectReviewStatus(context: AgentToolContext, args: Record<string, unknown>) {
    try {
      const { objectId, element } = await this.loadOntologyObjectContext(context, args, false);
      const objectPath = ontologyObjectPath(element.key);
      const workspace = this.workspace(context.project.id);
      const current = await this.currentOntologyObjectReviewState(context, objectId, objectPath, workspace);
      const latest = this.readOntologyObjectAuditPage(context.project.id, objectId, objectPath, {
        limit: 1,
      }).items[0] ?? null;
      const currentStage = String(current?.stage ?? '');
      const reviewRequestedAt = Date.parse(String(
        (current?.review_request as Record<string, unknown> | undefined)?.requested_at ?? '',
      ));
      const latestDecidedAt = Date.parse(String(latest?.decided_at ?? ''));
      const workspaceChange = workspace ? this.db.prepare(`SELECT MAX(updated_at) AS updated_at
        FROM agent_candidate_changes WHERE project_id = ? AND run_id = ? AND path = ?`)
        .get(context.project.id, workspace.run_id, objectPath) as { updated_at?: string | null } | undefined : undefined;
      const workspaceChangeAt = Date.parse(String(workspaceChange?.updated_at ?? ''));
      const latestStatus = String(latest?.status ?? '');
      const latestDecisionWins = ['draft', 'draft_locked', 'review_requested'].includes(currentStage)
        && ['accepted', 'rejected', 'revision_requested', 'superseded'].includes(latestStatus)
        && Number.isFinite(latestDecidedAt)
        && (!Number.isFinite(workspaceChangeAt) || latestDecidedAt >= workspaceChangeAt)
        && (!Number.isFinite(reviewRequestedAt) || latestDecidedAt >= reviewRequestedAt);
      const stage = latestDecisionWins ? latestStatus : String(current?.stage ?? (latestStatus || 'idle'));
      const projectPendingCandidateCount = Number((this.db.prepare(`SELECT COUNT(*) AS count
        FROM artifact_candidate_revisions WHERE project_id = ? AND status = 'pending'`)
        .get(context.project.id) as any)?.count ?? 0);
      const objectPendingCandidateCount = this.objectPendingCandidateCount(context.project.id, objectPath);
      const objectScopeExclusive = current?.object_scope_exclusive !== false;
      return {
        object_id: objectId,
        object_key: element.key,
        stage,
        current: current ? { ...current, stage } : {
          stage,
          base_revision: latest?.base_revision ?? '',
          candidate_revision: latest?.candidate_revision ?? null,
          accepted_revision: latest?.accepted_revision ?? null,
        },
        latest_record: latest,
        pending_candidate_count: objectPendingCandidateCount,
        object_pending_candidate_count: objectPendingCandidateCount,
        project_pending_candidate_count: projectPendingCandidateCount,
        blocked_by_other_candidate: !objectScopeExclusive
          || (objectPendingCandidateCount === 0 && projectPendingCandidateCount > 0),
        display: {
          text: stage === 'idle'
            ? `对象《${element.title}》当前没有待处理的复核记录`
            : `对象《${element.title}》当前状态：${ontologyReviewStageLabel(stage)}`,
        },
      };
    } catch (error) {
      throw normalizeStudioAgentError(error);
    }
  }

  private objectPendingCandidateCount(projectId: string, objectPath: string): number {
    const rows = this.db.prepare(`SELECT groups_json FROM artifact_candidate_revisions
      WHERE project_id = ? AND status = 'pending'`).all(projectId) as Array<{ groups_json: string }>;
    return rows.filter((row) => parseObjects(row.groups_json)
      .some((group) => candidateGroupTouchesPath(group, objectPath))).length;
  }

  private async listOntologyObjectAuditRecords(context: AgentToolContext, args: Record<string, unknown>) {
    try {
      const { objectId, element } = await this.loadOntologyObjectContext(context, args, false);
      const objectPath = ontologyObjectPath(element.key);
      const page = this.readOntologyObjectAuditPage(context.project.id, objectId, objectPath, {
        status: candidateStatusArg(args.status),
        cursor: auditCursorArg(args.cursor),
        limit: integer(args.limit, 20, 1, 100),
      });
      return {
        object_id: objectId,
        object_key: element.key,
        items: page.items,
        next_cursor: page.nextCursor,
        display: {
          text: page.items.length
            ? `已返回对象《${element.title}》的 ${page.items.length} 条审计记录`
            : `对象《${element.title}》还没有审计记录`,
        },
      };
    } catch (error) {
      throw normalizeStudioAgentError(error);
    }
  }

  private async currentOntologyObjectReviewState(
    context: AgentToolContext,
    objectId: string,
    objectPath: string,
    workspace: WorkspaceRow | null,
  ): Promise<Record<string, unknown> | null> {
    if (!workspace) return null;
    if (workspace.candidate_id) {
      try {
        const candidate = await this.artifacts.getCandidateRevision(context.project.id, workspace.candidate_id);
        const candidateRow = this.db.prepare(`SELECT id, project_id, base_revision_oid, status, cause, summary,
            actor_kind, actor_name, groups_json, selected_group_ids_json, accepted_revision_oid,
            decision_cause, decision_summary, decision_actor_kind, decision_actor_name, decision_note,
            created_at, decided_at
          FROM artifact_candidate_revisions WHERE id = ? AND project_id = ?`).get(
          candidate.id,
          context.project.id,
        ) as Record<string, unknown> | undefined;
        const record = this.auditRecordFromCandidateRow(
          context.project.id,
          objectId,
          objectPath,
          candidateRow,
        );
        if (record) {
          const conflictingPaths = candidateOtherBusinessPaths(candidateRow?.groups_json, objectPath);
          return {
            stage: conflictingPaths.length
              ? 'scope_conflict'
              : record.status === 'pending' ? 'awaiting_confirmation' : record.status,
            workspace_owner: workspace.owner,
            base_revision: candidate.baseRevision,
            candidate_revision: candidate.id,
            change_groups: record.groups,
            changed_paths: record.changed_paths,
            review: record.review,
            created_at: record.created_at,
            validation_receipt: this.candidateValidationReceiptView(candidate.id),
            object_scope_exclusive: conflictingPaths.length === 0,
            conflicting_paths: conflictingPaths,
          };
        }
      } catch {
        // 候选被并发处理后按最新历史记录回落。
      }
    }

    const allChanges = this.changes(workspace);
    const changeGroups = workspaceObjectChangeGroups(allChanges, objectPath);
    if (!changeGroups.length) return null;
    const workingPackage = await this.overlay(workspace, allChanges);
    const workingRevision = allChanges.length
      ? workingContentRevision(workspace.base_revision_oid, workingPackage.files)
      : workspace.base_revision_oid;
    const reviewEvent = this.latestOntologyObjectReviewEvent(
      context.project.id,
      objectPath,
      workingRevision,
    );
    const stage = reviewEvent
      ? 'review_requested'
      : workspace.owner === context.username ? 'draft' : 'draft_locked';
    const conflictingPaths = otherWorkspaceBusinessPaths(allChanges, objectPath);
    return {
      stage: conflictingPaths.length ? 'scope_conflict' : stage,
      workspace_owner: workspace.owner,
      base_revision: workspace.base_revision_oid,
      working_revision: workingRevision,
      changed_paths: [...new Set(changeGroups.flatMap((group) => group.impact))].sort(),
      change_groups: changeGroups,
      object_scope_exclusive: conflictingPaths.length === 0,
      conflicting_paths: conflictingPaths,
      ...(reviewEvent ? {
        review_request: {
          event_id: reviewEvent.eventId,
          client_mutation_id: reviewEvent.clientMutationId,
          delivery_status: reviewEvent.deliveredAt ? 'delivered' : 'pending',
          requested_at: reviewEvent.createdAt,
          delivered_at: reviewEvent.deliveredAt,
        },
      } : {}),
    };
  }

  private latestOntologyObjectReviewEvent(projectId: string, objectPath: string, workingRevision: string): {
    eventId: string;
    clientMutationId: string;
    createdAt: string;
    deliveredAt: string | null;
  } | null {
    const rows = this.db.prepare(`SELECT id, payload_json, created_at, delivered_at
      FROM agent_project_event_outbox
      WHERE project_id = ? AND event_type = 'studio_review'
      ORDER BY created_at DESC, rowid DESC LIMIT 50`).all(projectId) as Array<{
      id: string;
      payload_json: string;
      created_at: string;
      delivered_at: string | null;
    }>;
    for (const row of rows) {
      const payload = object(safeJsonParse(row.payload_json));
      const changedPaths = stringArray(payload.changedPaths);
      if (!changedPaths.includes(objectPath)) continue;
      if (workingRevision && String(payload.workingRevision ?? '').trim() !== workingRevision) continue;
      return {
        eventId: row.id,
        clientMutationId: String(payload.clientMutationId ?? '').trim(),
        createdAt: row.created_at,
        deliveredAt: row.delivered_at ?? null,
      };
    }
    return null;
  }

  private readOntologyObjectAuditPage(
    projectId: string,
    objectId: string,
    objectPath: string,
    options: {
      status?: string;
      cursor?: { createdAt: string; candidateId: string } | null;
      limit: number;
    },
  ): {
    items: Array<Record<string, unknown>>;
    nextCursor: string;
  } {
    const items: Array<Record<string, unknown>> = [];
    let cursor = options.cursor ?? null;
    let lastReturnedCursor: { createdAt: string; candidateId: string } | null = null;
    const batchSize = Math.max(20, Math.min(100, options.limit * 4));

    while (items.length < options.limit + 1) {
      const where = ['project_id = ?'];
      const params: Array<string | number> = [projectId];
      if (options.status) {
        where.push('status = ?');
        params.push(options.status);
      }
      if (cursor) {
        where.push('(created_at < ? OR (created_at = ? AND id < ?))');
        params.push(cursor.createdAt, cursor.createdAt, cursor.candidateId);
      }
      params.push(batchSize);
      const rows = this.db.prepare(`SELECT id, project_id, base_revision_oid, status, cause, summary, actor_kind,
          actor_name, groups_json, selected_group_ids_json, accepted_revision_oid,
          decision_cause, decision_summary, decision_actor_kind, decision_actor_name, decision_note,
          created_at, decided_at
        FROM artifact_candidate_revisions
        WHERE ${where.join(' AND ')}
        ORDER BY created_at DESC, id DESC LIMIT ?`).all(...params) as Array<Record<string, unknown>>;
      if (!rows.length) break;

      for (const row of rows) {
        const record = this.auditRecordFromCandidateRow(projectId, objectId, objectPath, row);
        cursor = {
          createdAt: String(row.created_at ?? ''),
          candidateId: String(row.id ?? ''),
        };
        if (!record) continue;
        items.push(record);
        if (items.length === options.limit) lastReturnedCursor = cursor;
        if (items.length >= options.limit + 1) break;
      }
      if (rows.length < batchSize) break;
    }

    return {
      items: items.slice(0, options.limit),
      nextCursor: items.length > options.limit && lastReturnedCursor ? encodeAuditCursor(lastReturnedCursor) : '',
    };
  }

  private auditRecordFromCandidateRow(
    projectId: string,
    objectId: string,
    objectPath: string,
    row: Record<string, unknown> | undefined,
  ): Record<string, unknown> | null {
    if (!row) return null;
    const groups = parseObjects(row.groups_json);
    const matching = groups.filter((group) => candidateGroupTouchesPath(group, objectPath));
    if (!matching.length) return null;

    const selected = new Set(parseArray(row.selected_group_ids_json));
    const metadataById = new Map(this.candidateGroups(String(row.id ?? '')).map((group) => [group.id, group]));
    const confirmation = this.db.prepare(`SELECT actor_username, review_mode, selected_groups_json,
        sampled_groups_json, created_at
      FROM agent_confirmations WHERE project_id = ? AND candidate_id = ?
      ORDER BY created_at DESC, rowid DESC LIMIT 1`)
      .get(projectId, String(row.id ?? '')) as
      | { actor_username: string; review_mode: string; selected_groups_json: string; sampled_groups_json: string; created_at: string }
      | undefined;

    const changedPaths = [...new Set(matching.flatMap(candidateGroupChangedPaths))].sort();
    return {
      record_id: `candidate:${String(row.id ?? '')}`,
      object_id: objectId,
      candidate_revision: String(row.id ?? ''),
      base_revision: String(row.base_revision_oid ?? ''),
      status: String(row.status ?? ''),
      created_at: String(row.created_at ?? ''),
      decided_at: row.decided_at ? String(row.decided_at) : null,
      accepted_revision: row.accepted_revision_oid ? String(row.accepted_revision_oid) : null,
      summary: String(row.summary ?? ''),
      actor: {
        kind: String(row.actor_kind ?? ''),
        name: String(row.actor_name ?? ''),
      },
      decision: row.decided_at
        ? {
          cause: String(row.decision_cause ?? ''),
          summary: String(row.decision_summary ?? ''),
          actor_kind: String(row.decision_actor_kind ?? ''),
          actor_name: String(row.decision_actor_name ?? ''),
          note: row.decision_note ? String(row.decision_note) : '',
          decided_at: String(row.decided_at ?? ''),
        }
        : null,
      review: confirmation
        ? {
          actor_username: confirmation.actor_username,
          review_mode: confirmation.review_mode,
          selected_groups: parseArray(confirmation.selected_groups_json),
          sampled_groups: parseArray(confirmation.sampled_groups_json),
          confirmed_at: confirmation.created_at,
        }
        : null,
      groups: matching.map((group) => {
        const groupId = String(group.id ?? '');
        const metadata = metadataById.get(groupId);
        return {
          id: groupId,
          tier: metadata?.tier ?? 'structural',
          title: metadata?.title ?? String(group.summary ?? ''),
          impact: metadata?.impact ?? candidateGroupChangedPaths(group),
          warning: metadata?.warning ?? '',
          selected: selected.has(groupId),
          applied: String(row.status ?? '') === 'accepted' ? selected.has(groupId) : null,
        };
      }),
      changed_paths: changedPaths,
      display: {
        text: `对象 ${objectId} 在候选 ${String(row.id ?? '')} 中状态为 ${ontologyReviewStageLabel(String(row.status ?? ''))}`,
      },
    };
  }

  private candidateValidationReceiptView(candidateId: string): Record<string, unknown> | null {
    const row = this.db.prepare(`SELECT base_revision_oid, candidate_hash, validator_version, validated_at
      FROM agent_candidate_validation_receipts WHERE candidate_id = ?`).get(candidateId) as
      | { base_revision_oid: string; candidate_hash: string; validator_version: string; validated_at: string }
      | undefined;
    if (!row) return null;
    return {
      base_revision: row.base_revision_oid,
      candidate_hash: row.candidate_hash,
      validator_version: row.validator_version,
      validated_at: row.validated_at,
    };
  }

  private async loadOntologyObjectContext(
    context: AgentToolContext,
    args: Record<string, unknown>,
    requireExpectedRevision: boolean,
  ): Promise<{
    workingPackage: Awaited<ReturnType<AgentCoreService['workbenchStudioFiles']>>;
    objectId: string;
    element: StudioElement;
  }> {
    const workingPackage = await this.workbenchStudioFiles(context.project, context.username);
    const studio = new StudioReadService(workingPackage);
    const requestedRevision = requireExpectedRevision
      ? text(args.expected_revision ?? args.expectedRevision, 'expected_revision')
      : String(
        args.working_revision ?? args.workingRevision ?? args.expected_revision ?? args.expectedRevision ?? '',
      ).trim();
    if (requestedRevision) studio.assertRevision(requestedRevision);
    const objectId = objectIdArg(args.object_id ?? args.objectId ?? args.element_id ?? args.elementId);
    const detail = studio.detail(objectId);
    if (detail.element.kind !== 'object') {
      throw new AgentCoreError(
        'INVALID_INPUT',
        '当前接口只支持 object 类型',
        ['传入 list_ontology_objects 或 get_ontology_object 返回的 object_id'],
        400,
        { object_id: objectId },
      );
    }
    return {
      workingPackage,
      objectId,
      element: detail.element,
    };
  }

  private projectTables(projectId: string): DataDictTable[] {
    return listDocuments(projectId).flatMap((document) => document.tables ?? []);
  }

  private async claimCandidateWorkspace(context: AgentToolContext) {
    const existing = this.workspace(context.project.id);
    const current = await this.artifacts.current(context.project.id);
    if (existing && current && existing.base_revision_oid !== current.revision.oid) {
      if (existing.owner !== context.username) {
        throw new AgentCoreError(
          'LEASE_LOST',
          '这个项目的建模上下文属于另一位用户',
          ['由原用户继续，或等待租约到期后由项目管理员处理'],
          409,
        );
      }
      const supersededChanges = this.changes(existing).length;
      const workspace = this.resetWorkspaceToCurrent(context, current.revision.oid, current.spec);
      return {
        projectId: workspace.project_id,
        runId: workspace.run_id,
        previousRunId: existing.run_id,
        resumed: false,
        supersededChanges,
        runCredential: issueAgentRunCredential(workspace.project_id, workspace.run_id, workspace.owner),
        packageContract: packageGenerationContract(),
        packageDataSource: { dataSourceType: workspace.package_data_source_type },
        display: { text: '知识包版本已变化，已按当前内容重新准备修改' },
      };
    }
    if (!existing || existing.run_id === context.runId) {
      const workspace = await this.ensureWorkspace(context);
      return {
        projectId: workspace.project_id,
        runId: workspace.run_id,
        resumed: Boolean(existing),
        runCredential: issueAgentRunCredential(workspace.project_id, workspace.run_id, workspace.owner),
        packageContract: packageGenerationContract(),
        packageDataSource: { dataSourceType: workspace.package_data_source_type },
        display: { text: existing ? '已继续上次未完成的修改' : '已准备本次修改' },
      };
    }
    if (existing.owner !== context.username) {
      throw new AgentCoreError(
        'LEASE_LOST',
        '这个项目的建模上下文属于另一位用户',
        ['由原用户继续，或等待租约到期后由项目管理员处理'],
        409,
      );
    }

    const expiresAt = new Date(Date.now() + WORKSPACE_LEASE_MS).toISOString();
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.prepare(`UPDATE agent_candidate_changes SET run_id = ?
        WHERE project_id = ? AND run_id = ?`)
        .run(context.runId, context.project.id, existing.run_id);
      this.db.prepare(`UPDATE agent_candidate_workspaces
        SET run_id = ?, lease_expires_at = ?, updated_at = ?
        WHERE project_id = ? AND run_id = ? AND owner = ?`)
        .run(
          context.runId,
          expiresAt,
          nowIso(),
          context.project.id,
          existing.run_id,
          context.username,
        );
      this.changeControl.transferActiveRun(context.project.id, existing.run_id, context.runId);
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
    return {
      projectId: context.project.id,
      runId: context.runId,
      previousRunId: existing.run_id,
      resumed: true,
      preservedChanges: this.changes(this.workspace(context.project.id)!).length,
      runCredential: issueAgentRunCredential(context.project.id, context.runId, context.username),
      packageContract: packageGenerationContract(),
      packageDataSource: { dataSourceType: this.workspace(context.project.id)?.package_data_source_type ?? 'neo4j' },
      display: { text: '已继续上次未完成的修改' },
    };
  }

  private async ensureWorkspace(context: AgentToolContext): Promise<WorkspaceRow> {
    await this.artifacts.ensureProject(context.project.id);
    let current = await this.artifacts.current(context.project.id);
    if (!current) {
      current = await this.artifacts.initializeDraftPackage(context.project.id, {
        id: `project-${context.project.id.replace(/^pj_/, '')}`,
        name: context.project.name,
        description: context.project.background || `${context.project.name}知识包草稿`,
      });
    }

    const existing = this.workspace(context.project.id);
    const now = new Date();
    const expiresAt = new Date(now.getTime() + WORKSPACE_LEASE_MS).toISOString();
    if (existing && existing.run_id !== context.runId
      && Date.parse(existing.lease_expires_at) > now.getTime()) {
      throw new AgentCoreError(
        'LEASE_LOST',
        '这个项目已有另一个建模会话在写入',
        ['回到已有会话继续，或等待 30 分钟租约到期后接管'],
      );
    }

    if (existing && existing.base_revision_oid !== current.revision.oid) {
      if (existing.owner !== context.username) {
        throw new AgentCoreError('FORBIDDEN', '无法使用其他用户的建模工作区', [], 403);
      }
      return this.resetWorkspaceToCurrent(context, current.revision.oid, current.spec);
    }

    if (!existing || existing.run_id !== context.runId) {
      const packageDataSourceType = workspacePackageDataSourceType(context.project.id, current.spec);
      this.db.exec('BEGIN IMMEDIATE');
      try {
        if (existing && existing.run_id !== context.runId) {
          // 新 run 接管工作区时把仍在进行中的受控计划一并转移，
          // 避免计划仍挂在旧 run 上导致执行端找不到（CHANGE_PLAN_NOT_FOUND）。
          this.changeControl.transferActiveRun(context.project.id, existing.run_id, context.runId);
        }
        this.db.prepare('DELETE FROM agent_candidate_changes WHERE project_id = ?').run(context.project.id);
        this.db.prepare(`INSERT INTO agent_candidate_workspaces (
          project_id, run_id, owner, base_revision_oid, package_data_source_type, candidate_id,
          lease_expires_at, created_at, updated_at
        ) VALUES (?,?,?,?,?,NULL,?,?,?)
        ON CONFLICT(project_id) DO UPDATE SET
          run_id = excluded.run_id,
          owner = excluded.owner,
          base_revision_oid = excluded.base_revision_oid,
          package_data_source_type = excluded.package_data_source_type,
          candidate_id = NULL,
          lease_expires_at = excluded.lease_expires_at,
          created_at = excluded.created_at,
          updated_at = excluded.updated_at`)
          .run(
            context.project.id,
            context.runId,
            context.username,
            current.revision.oid,
            packageDataSourceType,
            expiresAt,
            now.toISOString(),
            now.toISOString(),
          );
        this.db.exec('COMMIT');
      } catch (error) {
        if (this.db.isTransaction) this.db.exec('ROLLBACK');
        throw error;
      }
    } else {
      if (existing.owner !== context.username) {
        throw new AgentCoreError('FORBIDDEN', '无法使用其他用户的建模工作区', [], 403);
      }
      this.db.prepare(`UPDATE agent_candidate_workspaces
        SET lease_expires_at = ?, updated_at = ? WHERE project_id = ? AND run_id = ?`)
        .run(expiresAt, now.toISOString(), context.project.id, context.runId);
    }
    return this.workspace(context.project.id)!;
  }

  private resetWorkspaceToCurrent(
    context: AgentToolContext,
    currentRevision: string,
    currentSpec: PackageSpec,
  ): WorkspaceRow {
    const now = new Date();
    const expiresAt = new Date(now.getTime() + WORKSPACE_LEASE_MS).toISOString();
    const previous = this.workspace(context.project.id);
    this.db.exec('BEGIN IMMEDIATE');
    try {
      if (previous && previous.run_id !== context.runId) {
        // 知识包修订变化触发重建工作区时同样接续旧计划，
        // 避免计划悬挂在旧 run 上形成“幽灵计划”。
        this.changeControl.transferActiveRun(context.project.id, previous.run_id, context.runId);
      }
      this.db.prepare('DELETE FROM agent_candidate_changes WHERE project_id = ?').run(context.project.id);
      this.db.prepare(`UPDATE agent_candidate_workspaces SET
        run_id = ?, owner = ?, base_revision_oid = ?, package_data_source_type = ?, candidate_id = NULL,
        lease_expires_at = ?, created_at = ?, updated_at = ?
        WHERE project_id = ?`)
        .run(
          context.runId,
          context.username,
          currentRevision,
          workspacePackageDataSourceType(context.project.id, currentSpec),
          expiresAt,
          now.toISOString(),
          now.toISOString(),
          context.project.id,
        );
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
    return this.workspace(context.project.id)!;
  }

  private workspace(projectId: string): WorkspaceRow | null {
    return (this.db.prepare(`SELECT project_id, run_id, owner, base_revision_oid,
      package_data_source_type, candidate_id, lease_expires_at FROM agent_candidate_workspaces WHERE project_id = ?`)
      .get(projectId) as WorkspaceRow | undefined) ?? null;
  }

  private readableWorkspace(projectId: string, username: string, currentRevision: string): WorkspaceRow | null {
    const workspace = this.workspace(projectId);
    return workspace?.owner === username && workspace.base_revision_oid === currentRevision
      ? workspace
      : null;
  }

  private changes(workspace: WorkspaceRow): ChangeRow[] {
    return this.db.prepare(`SELECT path, kind, content, group_id, evidence_json, reason
      FROM agent_candidate_changes WHERE project_id = ? AND run_id = ? ORDER BY path`)
      .all(workspace.project_id, workspace.run_id) as unknown as ChangeRow[];
  }

  private async overlay(
    workspace: WorkspaceRow,
    changes = this.changes(workspace),
  ): Promise<PackageArtifactFiles & { files: Record<string, string> }> {
    const base = await this.artifacts.readPackageFiles(workspace.project_id, workspace.base_revision_oid);
    const files = { ...base.files };
    let spec = base.spec;
    for (const change of changes) {
      if (change.path === INTERNAL_SPEC_CHANGE_PATH) {
        if (change.kind === 'write') {
          try {
            spec = JSON.parse(change.content ?? '') as PackageSpec;
          } catch {
            throw new AgentCoreError(
              'COMPILED_SPEC_INVALID',
              '候选中的内部语义规格无法解析',
              ['放弃当前候选后重新使用领域工具生成'],
              409,
            );
          }
        }
        continue;
      }
      if (change.kind === 'delete') delete files[change.path];
      else files[change.path] = change.content ?? '';
    }
    return { ...base, spec, files };
  }

  private listDocuments(projectId: string) {
    const documents = listDocuments(projectId).map((document) => {
      const text = document.textContent ?? '';
      return {
        id: document.id,
        filename: document.filename,
        kind: document.kind,
        parseStatus: document.parseStatus,
        parseError: document.parseError ?? null,
        revisionNo: document.revisionNo ?? 1,
        purpose: document.purpose ?? 'auto',
        readingMode: document.readingMode ?? 'on_demand',
        directiveRevision: document.directiveRevision ?? 0,
        purposeReview: this.materialPurposeReviews.get(document.id),
        chars: text.length,
        tableCount: document.tables?.length ?? 0,
        outline: text.split('\n').map((line) => line.trim())
          .filter((line) => /^(#{1,4}\s|第?[一二三四五六七八九十百0-9]{1,4}[章节条、．.)）])/.test(line))
          .slice(0, 40),
      };
    });
    const parsed = documents.filter((item) => item.parseStatus === 'parsed').length;
    const failed = documents.filter((item) => item.parseStatus === 'failed').length;
    return {
      documents,
      counts: { total: documents.length, parsed, failed, pending: documents.length - parsed - failed },
      display: { text: `已查看 ${documents.length} 份资料，${parsed} 份可读取${failed ? `，${failed} 份解析失败` : ''}` },
    };
  }

  private readDocument(projectId: string, args: Record<string, unknown>) {
    const filename = text(args.filename, 'filename');
    const document = listDocuments(projectId).find((item) => item.filename === filename || item.id === filename);
    if (!document?.textContent) {
      throw new AgentCoreError(
        'DOCUMENT_UNREADABLE',
        `未找到可读取正文的资料：${filename}`,
        ['调用 list_documents 核对文件名和解析状态'],
        404,
      );
    }
    const offset = integer(args.offset, 0, 0, document.textContent.length);
    const limit = integer(args.limit, 6_000, 1, 20_000);
    const endOffset = Math.min(document.textContent.length, offset + limit);
    return {
      filename: document.filename,
      content: document.textContent.slice(offset, endOffset),
      offset,
      endOffset,
      nextOffset: endOffset < document.textContent.length ? endOffset : null,
      totalChars: document.textContent.length,
      eof: endOffset >= document.textContent.length,
      display: { text: `正在读《${document.filename}》` },
    };
  }

  private setMaterialDirective(projectId: string, args: Record<string, unknown>) {
    const filename = text(args.filename, 'filename');
    const document = listDocuments(projectId).find((item) => item.filename === filename || item.id === filename);
    if (!document) {
      throw new AgentCoreError('DOCUMENT_NOT_FOUND', `资料不存在：${filename}`, ['先查看当前资料清单'], 404);
    }
    const purpose = String(args.purpose ?? document.purpose ?? 'auto');
    const readingMode = String(args.reading_mode ?? document.readingMode ?? 'on_demand');
    if (!['auto', 'goal', 'schema', 'term', 'evidence'].includes(purpose)) {
      throw new AgentCoreError('INVALID_MATERIAL_PURPOSE', '资料用途不正确', ['选择由助手判断、建模目标依据、数据 Schema、术语表或通用业务依据'], 400);
    }
    if (purpose === 'auto') {
      throw new AgentCoreError(
        'MATERIAL_PURPOSE_REQUIRED',
        '资料用途预判必须给出明确用途，不能继续保留为“由助手判断”',
        ['根据资料类型、目录、开头片段或 Sheet 结构选择明确用途'],
        400,
      );
    }
    if (!['on_demand', 'full'].includes(readingMode)) {
      throw new AgentCoreError('INVALID_READING_MODE', '阅读方式不正确', ['选择按需读取或全文精读'], 400);
    }
    try {
      if (purpose === 'schema' && !(document.tables ?? []).length) {
        throw new Error('只有包含可识别数据表的资料可以指定为数据 Schema');
      }
      if (purpose === 'term') validateTermTables(document.tables ?? [], {
        dataSourceType: getProjectPackageDataSourceSelection(projectId).dataSourceType,
      });
      const currentReview = this.materialPurposeReviews.get(document.id);
      const effectivePurpose = currentReview?.status === 'confirmed'
        ? currentReview.confirmedPurpose ?? document.purpose ?? 'auto'
        : purpose;
      const updated = updateDocumentDirectives(document.id, {
        purpose: effectivePurpose as NonNullable<typeof document.purpose>,
        readingMode: readingMode as NonNullable<typeof document.readingMode>,
      });
      const purposeReview = updated.parseStatus === 'parsed'
        ? this.materialPurposeReviews.suggest(updated, {
          purpose: purpose as MaterialPurpose,
          sheets: Array.isArray(args.sheet_purposes) ? args.sheet_purposes : [],
          suggestedBy: 'agent',
        })
        : null;
      const sheetPurposes = purposeReview?.status === 'confirmed'
        ? purposeReview.sheetDecisions
        : purposeReview?.sheetSuggestions ?? [];
      const purposeBySheet = new Map(sheetPurposes.map((item) => [item.sheet, item]));
      const reviewedSchemaTables = (updated.tables ?? []).filter((table) => (
        purposeBySheet.get(table.name)?.semanticRole === 'schema'
      ));
      const objectTables = reviewedSchemaTables.filter((table) => (
        purposeBySheet.get(table.name)?.importDisposition === 'object'
      ));
      const currentSchemaTables = schemaTables(this.tables(projectId));
      return {
        document: {
          id: updated.id, filename: updated.filename, kind: updated.kind,
          purpose: updated.purpose, readingMode: updated.readingMode,
          directiveRevision: updated.directiveRevision,
        },
        ...(purposeReview ? {
          purposeReview,
          requiresUserConfirmation: purposeReview.status !== 'confirmed',
        } : {}),
        ...(updated.purpose === 'schema' ? {
          schema: reviewedSchemaTables.map((table) => ({
            ...schemaTableFact(updated, table),
            importDisposition: purposeBySheet.get(table.name)?.importDisposition,
          })),
          objects: objectTables.map((table) => ({
            name: table.name,
            source: { documentId: updated.id, filename: updated.filename, sheet: table.name },
            attributes: table.fields.map((field) => ({
              name: field.name,
              sourceColumn: field.sourceColumn,
              status: field.name ? 'source_fact' : 'needs_clarification',
              inferredType: field.inferredType,
              inferred: true,
            })),
          })),
        } : {}),
        objectSourcePolicy: currentSchemaTables.length ? {
          mode: 'schema',
          schemaTableCount: currentSchemaTables.length,
          rule: '一张 Schema Sheet 恰好对应一个物理对象；禁止派生对象，字段、binding 和 mapping 由平台同步',
          requiredTool: 'sync_schema_objects',
        } : {
          mode: 'virtual',
          schemaTableCount: 0,
          rule: '只允许不绑定物理表的虚拟对象，不生成 mapping',
        },
        recommendedPhase: updated.purpose === 'goal'
          ? 'goal'
          : ['schema', 'term'].includes(updated.purpose ?? '') ? 'knowledge' : null,
        display: {
          text: purposeReview?.status === 'pending'
            ? `已整理《${updated.filename}》的逐 Sheet 用途建议，等待用户确认`
            : `已将《${updated.filename}》设为${materialPurposeLabel(updated.purpose)}，${updated.readingMode === 'full' ? '需要全文精读' : '默认按需读取'}`,
        },
      };
    } catch (error) {
      throw new AgentCoreError(
        'INVALID_MATERIAL_DIRECTIVE',
        error instanceof Error ? error.message : '资料设置保存失败',
        ['核对资料格式与用途后重试'],
        400,
      );
    }
  }

  private async readDocumentsFully(context: AgentToolContext, args: Record<string, unknown>) {
    const requested = stringArray(args.filenames);
    const all = listDocuments(context.project.id);
    const selected = (requested.length
      ? requested.map((filename) => all.find((item) => item.filename === filename || item.id === filename))
      : all.filter((item) => item.readingMode === 'full'))
      .filter((item): item is NonNullable<typeof item> => Boolean(item));
    if (requested.length && selected.length !== new Set(requested).size) {
      const found = new Set(selected.flatMap((item) => [item.id, item.filename]));
      const missing = requested.filter((item) => !found.has(item));
      throw new AgentCoreError('DOCUMENT_NOT_FOUND', `没有找到需要全文精读的资料：${missing.join('、')}`, ['先查看当前资料清单'], 404);
    }
    const unreadable = selected.filter((item) => (
      item.parseStatus !== 'parsed' || (!item.textContent && !(item.tables?.length))
    ));
    if (unreadable.length) {
      throw new AgentCoreError(
        'DOCUMENT_UNREADABLE',
        `以下资料暂时没有可全文读取的正文：${unreadable.map((item) => item.filename).join('、')}`,
        ['等待解析完成；扫描件可上传文字版或由用户口述'],
        409,
      );
    }
    if (!selected.length) {
      throw new AgentCoreError('NO_FULL_READ_DOCUMENTS', '当前没有指定需要全文精读的资料', ['根据用户要求先设置资料的阅读方式'], 400);
    }
    for (const document of selected) {
      if (document.readingMode !== 'full') {
        updateDocumentDirectives(document.id, {
          purpose: document.purpose ?? 'auto',
          readingMode: 'full',
        });
      }
    }
    const fullTables = new Map<string, DataDictTable[]>();
    const readableDocuments = await parallelMap(selected, 5, async (document) => {
      if (document.textContent) {
        return {
          id: document.id,
          filename: document.filename,
          textContent: document.textContent,
          revisionFingerprint: documentRevisionFingerprint(document),
        };
      }
      const rawContent = getDocumentRawContent(document.id);
      if (!rawContent) {
        throw new AgentCoreError(
          'DOCUMENT_SOURCE_MISSING',
          `无法读取《${document.filename}》的原文件`,
          ['重新上传原文件后继续全文精读'],
          409,
        );
      }
      const expanded = await parseFullTableDocumentIsolated(
        document.kind as 'excel' | 'csv', rawContent, document.filename,
      ).catch((error) => {
        throw new AgentCoreError(
          'DOCUMENT_INCOMPLETE',
          `《${document.filename}》的表格内容没有完整展开：${error instanceof Error ? error.message : String(error)}`,
          ['检查工作表首行字段名后重新上传'],
          409,
        );
      });
      fullTables.set(document.id, expanded.tables);
      return {
        id: document.id,
        filename: document.filename,
        textContent: expanded.textContent,
        revisionFingerprint: documentRevisionFingerprint(document),
      };
    });
    const textResult = await ensureFullDocumentSummaries({
      projectId: context.project.id,
      documents: readableDocuments,
      llm: buildTaskLlm(),
    });
    const textCoverage = textResult.coverage.map((item) => ({
      documentId: item.documentId,
      filename: item.filename,
      unit: 'characters',
      coveredChars: item.contiguousEnd,
      totalChars: item.totalChars,
      complete: item.complete && item.eofSeen && item.contiguousEnd === item.totalChars,
      ...(item.documentId && fullTables.has(item.documentId) ? {
        coveredRows: fullTables.get(item.documentId)!.reduce((sum, table) => sum + table.rowCount, 0),
        totalRows: fullTables.get(item.documentId)!.reduce((sum, table) => sum + table.rowCount, 0),
      } : {}),
    }));
    const coverage = textCoverage;
    if (coverage.some((item) => !item.complete) || coverage.length !== selected.length) {
      throw new AgentCoreError('FULL_READ_INCOMPLETE', '全文精读没有覆盖全部内容，已保留进度', ['从已完成分段继续处理缺失范围'], 409, { coverage });
    }
    return {
      documents: textResult.summaries.map((item) => {
        const document = selected.find((candidate) => candidate.filename === item.filename);
        const tables = document ? fullTables.get(document.id) : undefined;
        return {
        filename: item.filename,
        purpose: document?.purpose ?? 'auto',
        summary: item.payload.summary,
        suitableFor: item.payload.suitableFor,
        candidates: item.payload.candidates,
        openQuestions: item.payload.openQuestions,
        ...(tables ? {
          schema: tables.map((table) => ({
            table: table.name,
            rowCount: table.rowCount,
            fields: table.fields,
          })),
        } : {}),
        };
      }),
      coverage,
      totalChars: textCoverage.reduce((sum, item) => sum + item.totalChars, 0),
      display: {
        text: `已并行全文精读 ${coverage.length} 份资料，全部内容完整度 100%`,
      },
    };
  }

  private searchDocuments(projectId: string, args: Record<string, unknown>) {
    const query = text(args.query, 'query');
    const terms = query.split(/\s+/).map((item) => item.trim()).filter((item) => item.length >= 2);
    if (!terms.length) {
      throw new AgentCoreError('INVALID_INPUT', '检索词至少包含 2 个字符', ['换一个更具体的业务词后重试'], 400);
    }
    const hits: Array<{ filename: string; offset: number; excerpt: string; terms: string[] }> = [];
    for (const document of listDocuments(projectId)) {
      const body = document.textContent ?? '';
      const lower = body.toLocaleLowerCase('zh-CN');
      for (const term of terms) {
        let from = 0;
        const needle = term.toLocaleLowerCase('zh-CN');
        while (hits.length < 30) {
          const offset = lower.indexOf(needle, from);
          if (offset < 0) break;
          const start = Math.max(0, offset - 160);
          hits.push({
            filename: document.filename,
            offset,
            excerpt: body.slice(start, Math.min(body.length, offset + needle.length + 320)),
            terms: [term],
          });
          from = offset + needle.length;
        }
        if (hits.length >= 30) break;
      }
      if (hits.length >= 30) break;
    }
    return {
      query,
      hits,
      truncated: hits.length >= 30,
      display: { text: hits.length ? `找到 ${hits.length} 处相关原文` : `没有找到“${query}”` },
    };
  }

  private tables(projectId: string): DataDictTable[] {
    return listDocuments(projectId).flatMap((document) => {
      const review = this.materialPurposeReviews.get(document.id);
      const confirmedDecisions = review?.status === 'confirmed'
        ? new Map(review.sheetDecisions.map((decision) => [decision.sheet, decision]))
        : null;
      return (document.tables ?? []).map((table) => {
        const decision = confirmedDecisions?.get(table.name);
        const materialPurpose = review?.status === 'confirmed' && review.confirmedPurpose === 'schema'
          ? decision?.importDisposition === 'object' ? 'schema' : 'evidence'
          : review?.status === 'confirmed'
            ? review.confirmedPurpose ?? document.purpose ?? 'auto'
            : document.purpose ?? 'auto';
        return {
          ...table,
          documentId: document.id,
          materialPurpose,
        };
      });
    });
  }

  private listTables(projectId: string) {
    const documents = listDocuments(projectId);
    const tables = this.tables(projectId).map((table) => {
      const review = table.documentId ? this.materialPurposeReviews.get(table.documentId) : null;
      const purposeDecision = (review?.status === 'confirmed'
        ? review.sheetDecisions
        : review?.sheetSuggestions ?? []).find((item) => item.sheet === table.name);
      return {
        name: table.name,
        documentId: table.documentId,
        filename: documents.find((document) => document.id === table.documentId)?.filename,
        purpose: table.materialPurpose ?? 'auto',
        rowCount: table.rowCount,
        fields: table.fields.length,
        purposeReviewStatus: review?.status ?? null,
        purposeDecision: purposeDecision ?? null,
      };
    });
    return { tables, display: { text: `已查看 ${tables.length} 张数据表` } };
  }

  private getSchemaOverview(projectId: string) {
    const documents = new Map(listDocuments(projectId).map((document) => [document.id, document]));
    let cacheHits = 0;
    const tables = this.tables(projectId).map((table) => {
      const document = table.documentId ? documents.get(table.documentId) : undefined;
      const key = {
        projectId,
        documentId: table.documentId ?? `table:${table.name}`,
        contentHash: document?.contentHash ?? '',
        sheetName: table.name,
        purposeRevision: `${document?.directiveRevision ?? 0}:${table.materialPurpose ?? document?.purpose ?? 'auto'}`,
        analysisVersion: SCHEMA_PROFILE_ANALYSIS_VERSION,
      };
      const cached = this.schemaProfileCache.get(key);
      if (cached) {
        cacheHits += 1;
        return { ...cached, cacheHit: true };
      }
      return { ...this.schemaProfileCache.save(key, table), cacheHit: false };
    });
    return {
      tables,
      tableCount: tables.length,
      cacheHits,
      analysisVersion: SCHEMA_PROFILE_ANALYSIS_VERSION,
      display: { text: `已生成 ${tables.length} 张表的结构摘要${cacheHits ? `，复用 ${cacheHits} 张缓存` : ''}` },
    };
  }

  private findTable(projectId: string, args: Record<string, unknown>) {
    const name = text(args.table, 'table');
    const documentId = String(args.document_id ?? '').trim();
    const matches = this.tables(projectId).filter((item) => item.name === name
      && (!documentId || item.documentId === documentId));
    if (!documentId && matches.length > 1) {
      throw new AgentCoreError(
        'TABLE_AMBIGUOUS',
        `多个资料中都有数据表《${name}》`,
        ['根据 list_tables 返回的资料 ID 指定 document_id'],
        409,
      );
    }
    const table = matches[0];
    if (!table) {
      throw new AgentCoreError('TABLE_NOT_FOUND', `数据表不存在：${name}`, ['调用 list_tables 核对表名'], 404);
    }
    return table;
  }

  private describeTable(projectId: string, args: Record<string, unknown>) {
    const table = this.findTable(projectId, args);
    return {
      table: { name: table.name, documentId: table.documentId, rowCount: table.rowCount, fields: table.fields },
      display: { text: `正在分析数据表《${table.name}》` },
    };
  }

  private readTableRows(projectId: string, args: Record<string, unknown>) {
    const table = this.findTable(projectId, args);
    const offset = integer(args.offset, 0, 0, table.rowCount);
    const requestedLimit = integer(args.limit, 20, 1, 50);
    const limit = requestedLimit;
    const rows = (table.rows ?? []).slice(offset, offset + limit);
    const returned = rows.length;
    const scanned = Math.min(table.rowCount - offset, limit);
    return {
      table: table.name,
      columns: table.fields.map((field) => field.name),
      rows,
      offset,
      requestedLimit,
      effectiveLimit: limit,
      returned,
      scanned,
      truncated: offset + returned < table.rowCount,
      nextOffset: offset + rows.length < table.rowCount ? offset + rows.length : null,
      totalRows: table.rowCount,
      display: { text: `正在读取数据表《${table.name}》样例` },
    };
  }

  private async stageTabularDataset(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const source = text(args.source, 'source');
    const document = listDocuments(workspace.project_id).find((item) => (
      item.id === source || item.sourceKey === source || item.filename === source
    ));
    if (!document) {
      throw new AgentCoreError(
        'DOCUMENT_NOT_FOUND',
        `资料不存在：${source}`,
        ['先查看资料清单，再使用资料的完整文件名'],
        404,
      );
    }
    if (!['excel', 'csv'].includes(document.kind) || document.parseStatus !== 'parsed') {
      throw new AgentCoreError(
        'TABULAR_MATERIAL_REQUIRED',
        `《${document.filename}》不是已解析的数据表资料`,
        ['等待 Excel 或 CSV 解析成功后再生成数据集候选'],
        409,
      );
    }
    const tables = document.tables ?? [];
    if (!tables.length) {
      throw new AgentCoreError('TABLE_NOT_FOUND', `《${document.filename}》没有可同步的数据表`, [], 404);
    }
    const incomplete = tables.filter((table) => (table.rows?.length ?? 0) !== table.rowCount);
    if (incomplete.length) {
      throw new AgentCoreError(
        'TABLE_ROWS_INCOMPLETE',
        `《${document.filename}》有 ${incomplete.length} 张表没有完整展开，不能生成不完整候选`,
        ['先将超大数据导入项目数据源；知识包只保留结构和可追溯映射'],
        409,
        { tables: incomplete.map((table) => ({ name: table.name, rows: table.rows?.length ?? 0, total: table.rowCount })) },
      );
    }
    const ambiguous = tables.flatMap((table) => {
      const fields = table.fields.map((field) => field.name.trim());
      const duplicates = fields.filter((field, index) => !field || fields.indexOf(field) !== index);
      return duplicates.length ? [{ table: table.name, fields: [...new Set(duplicates)] }] : [];
    });
    if (ambiguous.length) {
      throw new AgentCoreError(
        'TABLE_FIELDS_AMBIGUOUS',
        `《${document.filename}》存在空字段或重名字段，不能可靠生成数据集候选`,
        ['请专家先确认空表头或重名列的业务含义'],
        409,
        { tables: ambiguous },
      );
    }
    const path = packagePath(String(args.path ?? '').trim() || 'datasets/data.csv');
    if (!/^datasets\/.+\.csv$/iu.test(path)) {
      throw new AgentCoreError(
        'INVALID_DATASET_PATH',
        '表格资料只能同步到 datasets/ 目录下的 CSV 文件',
        ['使用 datasets/data.csv 或另一个 datasets/*.csv 路径'],
        400,
      );
    }
    const content = tabularDatasetCsv(tables);
    if (content.length > MAX_FILE_CHARS) {
      throw new AgentCoreError(
        'DATASET_TOO_LARGE',
        '数据集候选超过知识包单文件上限，不能通过模型或候选文件搬运整表',
        ['将完整数据导入项目数据源；候选中只更新对象字段和映射'],
        409,
      );
    }
    const evidence = [`material:${document.id}`];
    assertMaterialEvidenceReferences(workspace.project_id, evidence);
    const { files } = await this.overlay(workspace);
    if (files[path] === content) {
      return {
        ok: true,
        changed: false,
        path,
        tables: tables.length,
        rows: tables.reduce((sum, table) => sum + table.rowCount, 0),
        display: { text: `《${document.filename}》与当前数据集一致，无需生成候选` },
      };
    }
    await this.discardFrozenCandidate(workspace, context, '表格资料继续同步，旧确认请求已失效');
    await this.upsertChange(workspace, {
      path,
      kind: 'write',
      content,
      groupId: 'increment_data',
      evidence,
      reason: `完整同步《${document.filename}》的数据表`,
    });
    const rows = tables.reduce((sum, table) => sum + table.rowCount, 0);
    return {
      ok: true,
      changed: true,
      path,
      tables: tables.length,
      rows,
      evidence,
      candidate: true,
      display: { text: `已从《${document.filename}》完整同步 ${tables.length} 张表、${rows} 行到候选数据集` },
    };
  }

  private async listDir(workspace: WorkspaceRow, args: Record<string, unknown>) {
    const raw = String(args.path ?? '').trim().replace(/^\/+|\/+$/g, '');
    const path = raw ? packagePath(raw) : '';
    const { files } = await this.overlay(workspace);
    const prefix = path ? `${path}/` : '';
    const entries = new Map<string, 'file' | 'directory'>();
    for (const candidate of Object.keys(files)) {
      if (!candidate.startsWith(prefix) || candidate === path) continue;
      const rest = candidate.slice(prefix.length);
      const [name, ...tail] = rest.split('/');
      if (name) entries.set(name, tail.length ? 'directory' : 'file');
    }
    return {
      path,
      entries: [...entries].map(([name, kind]) => ({ name, kind, path: prefix + name })),
      display: { text: path ? `正在查看《${path}》目录` : '正在查看知识包目录' },
    };
  }

  private async readFile(
    workspace: WorkspaceRow,
    args: Record<string, unknown>,
    context?: AgentToolContext,
  ) {
    const path = packagePath(text(args.path, 'path'));
    if (context && MANAGED_SCRIPT_SOURCE_PATH.test(path) && this.usesScriptContract(context)) {
      throw new AgentCoreError(
        'MANAGED_FILE_REQUIRES_DOMAIN_TOOL',
        `${path} 是脚本契约受管源码，不能通过通用文件读取进入模型上下文`,
        ['使用 get_script_edit_context 读取符号索引，再只请求目标符号及直接依赖'],
        409,
        {
          managedPath: path,
          requiredAction: 'redirect_tool',
          requiredTool: 'get_script_edit_context',
          targetKind: 'script_source',
          issueId: stableAgentIssueId('managed-script-read', path),
          contractVersion: 'script-contract-v1',
          retryable: false,
        },
      );
    }
    const { files } = await this.overlay(workspace);
    if (!(path in files)) {
      throw new AgentCoreError('FILE_NOT_FOUND', `知识包文件不存在：${path}`, ['调用 list_dir 核对路径'], 404);
    }
    return { path, content: files[path], revision: workspace.base_revision_oid, candidate: true,
      display: { text: `正在读取《${path}》` } };
  }

  private async upsertObject(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const current = await this.overlay(workspace);
    const key = domainKey(args.key, '对象');
    const title = text(args.title, 'title');
    const aliases = stringArray(args.aliases);
    const fields: SpecField[] = requiredObjectArray(args.fields, 'fields').map((raw, index) => {
      if (typeof raw.required !== 'boolean') {
        throw new AgentCoreError('INVALID_DOMAIN_PAYLOAD', `fields[${index}].required 必须是布尔值`, [], 400);
      }
      return {
        key: domainKey(raw.key, `对象字段 fields[${index}]`),
        ...(optionalInputText(raw.cn_name) ? { cnName: optionalInputText(raw.cn_name) } : {}),
        type: text(raw.type, `fields[${index}].type`),
        required: raw.required,
        ...(optionalInputText(raw.note) ? { note: optionalInputText(raw.note) } : {}),
        ...(optionalInputText(raw.alias) ? { alias: optionalInputText(raw.alias) } : {}),
      };
    });
    if (!fields.length) throw new AgentCoreError('INVALID_DOMAIN_PAYLOAD', '对象至少要包含一个字段', [], 400);
    assertUniqueDomainKeys(fields.map((field) => field.key), '对象字段');
    const existing = current.spec.objects.find((item) => item.key === key);
    const relations = requiredObjectArray(args.relations ?? [], 'relations').map((raw, index) => {
      const relation = normalizeRelationName(text(raw.relation, `relations[${index}].relation`));
      const target = domainKey(raw.target, `关系目标 relations[${index}]`);
      const targetObject = target === key
        ? { key, title, aliases }
        : current.spec.objects.find((item) => item.key === target);
      const problem = relationNameProblem(relation, {
        source: { key, title, aliases },
        target: targetObject,
      });
      if (problem) {
        throw new AgentCoreError(
          'INVALID_DOMAIN_PAYLOAD',
          `relations[${index}].relation：${problem}`,
          ['只填写关系谓词，并检查“源对象 -> 关系名称 -> 目标对象”连读符合业务逻辑'],
          400,
        );
      }
      return {
        relation,
        target,
        ...(optionalInputText(raw.note) ? { note: optionalInputText(raw.note) } : {}),
        ...(stringArray(raw.evidence).length ? { evidence: stringArray(raw.evidence) } : {}),
      };
    });
    assertReferences(
      '关系目标对象',
      relations.map((item) => item.target),
      [...current.spec.objects.map((item) => item.key), key],
    );
    const evidence = requiredStringArray(args.evidence, 'evidence');
    const compiledEvidence = [...new Set([
      ...evidence,
      ...relations.flatMap((relation) => relation.evidence ?? []),
    ])];
    if (args.remove_source !== undefined && typeof args.remove_source !== 'boolean') {
      throw new AgentCoreError('INVALID_DOMAIN_PAYLOAD', 'remove_source 必须是布尔值', [], 400);
    }
    const removeSource = args.remove_source === true;
    const source = args.source === undefined ? undefined : object(args.source);
    const sourcePlan = args.source_plan === undefined ? undefined : object(args.source_plan);
    if (source && sourcePlan) {
      throw new AgentCoreError(
        'INVALID_OBJECT_SOURCE_BINDING',
        'source 与 source_plan 不能同时提交',
        ['单表绑定使用 source；跨 Sheet 对象使用 source_plan'],
        400,
      );
    }
    if ((source || sourcePlan) && removeSource) {
      throw new AgentCoreError(
        'INVALID_OBJECT_SOURCE_BINDING',
        '数据来源绑定与 remove_source 不能同时提交',
        ['保留数据来源完成绑定，或只使用 remove_source=true 显式移除绑定'],
        400,
      );
    }
    let sourceRefs: SpecObject['sourceRefs'];
    let sourceTable: string | undefined;
    let primaryKey: string[] | undefined;
    if (sourcePlan) {
      if (workspace.package_data_source_type !== 'neo4j') {
        throw new AgentCoreError(
          'MULTI_SOURCE_OBJECT_UNSUPPORTED',
          '只有 Neo4j 图导入支持主表与扩展表组成的跨 Sheet 对象',
          ['拆分为逐来源对象，或将知识包数据源类型切换为 Neo4j 后重新建模'],
          400,
        );
      }
      primaryKey = requiredStringArray(sourcePlan.primary_key, 'source_plan.primary_key');
      assertReferences('对象字段', primaryKey, fields.map((field) => field.key));
      const parsed = this.parseObjectTableSources(
        workspace.project_id,
        fields,
        requiredObjectArray(sourcePlan.sources, 'source_plan.sources'),
        primaryKey,
        'source_plan.sources',
      );
      sourceRefs = [
        ...(existing?.sourceRefs ?? []).filter((item) => item.kind !== 'table'),
        ...parsed.sources,
      ];
      for (const field of fields) field.sourceColumn = parsed.defaultColumns.get(field.key);
    } else if (source) {
      const documentId = text(source.document_id, 'source.document_id');
      const tableName = text(source.table, 'source.table');
      const document = listDocuments(workspace.project_id).find((item) => item.id === documentId);
      const table = document?.tables?.find((item) => item.name === tableName);
      if (!document || document.parseStatus !== 'parsed' || !table) {
        throw new AgentCoreError('TABLE_NOT_FOUND', `资料 ${documentId} 中不存在已解析的数据表《${tableName}》`, ['调用 list_tables 核对资料 ID 和表名'], 404);
      }
      const bindings = requiredObjectArray(source.field_bindings, 'source.field_bindings').map((raw, index) => ({
        fieldKey: text(raw.field_key, `source.field_bindings[${index}].field_key`),
        sourceColumn: text(raw.source_column, `source.field_bindings[${index}].source_column`),
      }));
      assertUniqueDomainKeys(bindings.map((item) => item.fieldKey), '对象字段绑定');
      assertUniqueDomainKeys(bindings.map((item) => item.sourceColumn), '来源字段绑定');
      assertReferences('对象字段', bindings.map((item) => item.fieldKey), fields.map((field) => field.key));
      assertReferences('来源字段', bindings.map((item) => item.sourceColumn), table.fields.map((field) => field.name));
      const missingFields = fields.filter((field) => !bindings.some((binding) => binding.fieldKey === field.key));
      if (missingFields.length) {
        throw new AgentCoreError('INVALID_OBJECT_SOURCE_BINDING', '对象字段与来源表字段没有形成完整的一一绑定', ['为对象全部字段补充 source.field_bindings'], 400, { missingFields: missingFields.map((field) => field.key) });
      }
      primaryKey = requiredStringArray(source.primary_key, 'source.primary_key');
      assertReferences('对象字段', primaryKey, fields.map((field) => field.key));
      const sourceColumnByField = new Map(bindings.map((item) => [item.fieldKey, item.sourceColumn]));
      for (const field of fields) field.sourceColumn = sourceColumnByField.get(field.key);
      sourceRefs = [
        ...(existing?.sourceRefs ?? []).filter((item) => item.kind !== 'table'),
        { kind: 'table', documentId: document.id, table: table.name, columns: bindings.map((item) => item.sourceColumn) },
      ];
    } else if (existing && !removeSource) {
      const existingFieldKeys = new Set(existing.fields.map((field) => field.key));
      const fieldKeysChanged = existingFieldKeys.size !== fields.length
        || fields.some((field) => !existingFieldKeys.has(field.key));
      if (tableSourcesFor(existing).length && fieldKeysChanged) {
        throw new AgentCoreError(
          'OBJECT_SOURCE_REBIND_REQUIRED',
          '对象字段已经变化，不能沿用原数据源绑定',
          ['提交完整 source 重新绑定全部字段，或使用 remove_source=true 显式移除原绑定'],
          409,
          {
            previousFields: existing.fields.map((field) => field.key),
            nextFields: fields.map((field) => field.key),
          },
        );
      }
      const sourceColumnByField = new Map(existing.fields.map((field) => [field.key, field.sourceColumn]));
      for (const field of fields) field.sourceColumn = sourceColumnByField.get(field.key);
      sourceRefs = existing.sourceRefs;
      sourceTable = existing.sourceTable;
      primaryKey = existing.primaryKey;
    } else if (existing && removeSource) {
      const preservedSources = (existing.sourceRefs ?? []).filter((item) => item.kind !== 'table');
      sourceRefs = preservedSources.length ? preservedSources : undefined;
    }
    const nextObject: SpecObject = {
      key,
      title,
      description: text(args.description, 'description'),
      fields,
      ...(aliases.length ? { aliases } : {}),
      ...(optionalInputText(args.query_boundary) ? { queryBoundary: optionalInputText(args.query_boundary) } : {}),
      ...(relations.length ? { relations } : {}),
      ...(sourceRefs?.length ? { sourceRefs } : {}),
      ...(sourceTable ? { sourceTable } : {}),
      ...(primaryKey?.length ? { primaryKey } : {}),
    };
    const objects = [...current.spec.objects];
    const objectIndex = objects.findIndex((item) => item.key === key);
    if (objectIndex >= 0) objects[objectIndex] = nextObject;
    else objects.push(nextObject);
    const nextSpec = { ...current.spec, objects };
    const renderedPaths = [
      `objects/${key}.md`,
      'objects/index.md',
      ...(packageNeedsObjectDataMapping(nextSpec) ? [OBJECT_DATA_MAPPING_PATH] : []),
    ];
    await this.stageCompiledSpec(
      workspace,
      context,
      nextSpec,
      renderedPaths,
      changeGroupId(args.change_group, 'change_group'),
      compiledEvidence,
      `对象 ${key} 的结构化定义${source || sourcePlan ? '和数据源绑定' : removeSource ? '和数据源解绑' : ''}`,
    );
    return {
      ok: true,
      action: existing ? 'updated' : 'created',
      element: { kind: 'object', key },
      renderedPaths,
      deterministic: true,
      display: {
        text: `${existing ? '已更新' : '已创建'}对象《${title}》${source || sourcePlan
          ? '并完成数据绑定'
          : removeSource ? '并移除数据绑定' : ''}`,
      },
    };
  }

  private async upsertRule(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const current = await this.overlay(workspace);
    const key = domainKey(args.key, '规则');
    const title = text(args.title, 'title');
    const appliesTo = object(args.applies_to);
    const appliesToObjects = requiredStringArray(appliesTo.objects, 'applies_to.objects', true);
    const appliesToOperations = requiredStringArray(appliesTo.operations, 'applies_to.operations', true);
    if (!appliesToObjects.length && !appliesToOperations.length) {
      throw new AgentCoreError(
        'INVALID_DOMAIN_PAYLOAD',
        '规则至少要绑定一个对象或操作',
        ['补充 applies_to.objects 或 applies_to.operations 后重试'],
        400,
      );
    }
    assertReferences('对象', appliesToObjects, current.spec.objects.map((item) => item.key));
    assertReferences('操作', appliesToOperations, current.spec.operations.map((item) => item.key));
    const conditions = requiredObjectArray(args.conditions, 'conditions').map((raw, index) => ({
      condition: text(raw.condition, `conditions[${index}].condition`),
      conclusion: text(raw.conclusion, `conditions[${index}].conclusion`),
    }));
    if (!conditions.length) {
      throw new AgentCoreError('INVALID_DOMAIN_PAYLOAD', '规则至少要包含一个判断条件', [], 400);
    }
    const evidence = requiredStringArray(args.evidence, 'evidence');
    const existing = (current.spec.rules ?? []).find((item) => item.key === key);
    const rule: SpecRule = {
      key,
      title,
      ...(optionalInputText(args.description) ? { description: optionalInputText(args.description) } : {}),
      appliesToObjects,
      appliesToOperations,
      content: text(args.conclusion, 'conclusion'),
      conditions,
      ...(stringArray(args.exceptions).length ? { exceptions: stringArray(args.exceptions) } : {}),
      evidence,
    };
    const rules = [...(current.spec.rules ?? [])];
    const index = rules.findIndex((item) => item.key === key);
    if (index >= 0) rules[index] = rule;
    else rules.push(rule);
    const nextSpec = { ...current.spec, rules };
    const renderedPaths = [`rules/${key}.md`, 'rules/index.md'];
    await this.stageCompiledSpec(
      workspace,
      context,
      nextSpec,
      renderedPaths,
      changeGroupId(args.change_group, 'change_group'),
      evidence,
      `规则 ${key} 的结构化定义`,
    );
    return {
      ok: true,
      action: existing ? 'updated' : 'created',
      element: { kind: 'rule', key },
      renderedPaths,
      deterministic: true,
      display: { text: `${existing ? '已更新' : '已创建'}规则《${title}》及其索引` },
    };
  }

  private async upsertOperation(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const current = await this.overlay(workspace);
    const key = domainKey(args.key, '操作');
    const title = text(args.title, 'title');
    const relatedObjects = requiredStringArray(args.related_objects, 'related_objects');
    const usesRules = requiredStringArray(args.uses_rules, 'uses_rules', true);
    const usesMetrics = requiredStringArray(args.uses_metrics, 'uses_metrics', true);
    assertReferences('对象', relatedObjects, current.spec.objects.map((item) => item.key));
    assertReferences('规则', usesRules, (current.spec.rules ?? []).map((item) => item.key));
    assertReferences('指标', usesMetrics, (current.spec.metrics ?? []).map((item) => item.key));
    const inputs = requiredObjectArray(args.inputs, 'inputs').map((raw, index) => {
      if (typeof raw.required !== 'boolean') {
        throw new AgentCoreError(
          'INVALID_DOMAIN_PAYLOAD',
          `inputs[${index}].required 必须是布尔值`,
          [],
          400,
        );
      }
      return {
        param: text(raw.param, `inputs[${index}].param`),
        ...(optionalInputText(raw.cn_name) ? { cnName: optionalInputText(raw.cn_name) } : {}),
        type: text(raw.type, `inputs[${index}].type`),
        required: raw.required,
        ...(optionalInputText(raw.note) ? { note: optionalInputText(raw.note) } : {}),
      };
    });
    assertUniqueDomainKeys(inputs.map((item) => item.param), '输入参数');
    const outputs = requiredObjectArray(args.outputs, 'outputs').map((raw, index) => ({
      field: text(raw.field, `outputs[${index}].field`),
      ...(optionalInputText(raw.cn_name) ? { cnName: optionalInputText(raw.cn_name) } : {}),
      type: text(raw.type, `outputs[${index}].type`),
      ...(optionalInputText(raw.note) ? { note: optionalInputText(raw.note) } : {}),
    }));
    assertUniqueDomainKeys(outputs.map((item) => item.field), '输出字段');
    const flow = requiredStringArray(args.flow, 'flow');
    const evidence = requiredStringArray(args.evidence, 'evidence');
    const existing = current.spec.operations.find((item) => item.key === key);
    const operation: SpecOperation = {
      key,
      title,
      description: text(args.description, 'description'),
      inputs,
      outputs,
      flow,
      relatedObjects,
      ...(usesRules.length ? { usesRules } : {}),
      ...(usesMetrics.length ? { usesMetrics } : {}),
      ...(stringArray(args.notes).length ? { notes: stringArray(args.notes) } : {}),
      ...(existing?.script ? { script: existing.script } : {}),
      ...(existing?.competencyQuestionId
        ? { competencyQuestionId: existing.competencyQuestionId }
        : {}),
      ...(existing?.competencyQuestion
        ? { competencyQuestion: existing.competencyQuestion }
        : {}),
      evidence,
    };
    const operations = [...current.spec.operations];
    const index = operations.findIndex((item) => item.key === key);
    if (index >= 0) operations[index] = operation;
    else operations.push(operation);
    const nextSpec = { ...current.spec, operations };
    const renderedPaths = [`operations/${key}.md`, 'operations/index.md'];
    await this.stageCompiledSpec(
      workspace,
      context,
      nextSpec,
      renderedPaths,
      changeGroupId(args.change_group, 'change_group'),
      evidence,
      `操作 ${key} 的结构化定义`,
    );
    return {
      ok: true,
      action: existing ? 'updated' : 'created',
      element: { kind: 'operation', key },
      renderedPaths,
      deterministic: true,
      display: { text: `${existing ? '已更新' : '已创建'}操作《${title}》及其索引` },
    };
  }

  private async bindObjectSource(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const current = await this.overlay(workspace);
    const key = domainKey(args.key, '对象');
    const objectIndex = current.spec.objects.findIndex((item) => item.key === key);
    if (objectIndex < 0) {
      throw new AgentCoreError('OBJECT_NOT_FOUND', `对象不存在：${key}`, ['先核对对象目录'], 404);
    }
    const documentId = text(args.document_id, 'document_id');
    const tableName = text(args.table, 'table');
    const document = listDocuments(workspace.project_id).find((item) => item.id === documentId);
    const table = document?.tables?.find((item) => item.name === tableName);
    if (!document || document.parseStatus !== 'parsed' || !table) {
      throw new AgentCoreError(
        'TABLE_NOT_FOUND',
        `资料 ${documentId} 中不存在已解析的数据表《${tableName}》`,
        ['调用 list_tables 核对资料 ID 和表名'],
        404,
      );
    }
    const targetObject = current.spec.objects[objectIndex];
    const bindings = requiredObjectArray(args.field_bindings, 'field_bindings').map((raw, index) => ({
      fieldKey: text(raw.field_key, `field_bindings[${index}].field_key`),
      sourceColumn: text(raw.source_column, `field_bindings[${index}].source_column`),
    }));
    assertUniqueDomainKeys(bindings.map((item) => item.fieldKey), '对象字段绑定');
    assertUniqueDomainKeys(bindings.map((item) => item.sourceColumn), '来源字段绑定');
    const objectFieldKeys = new Set(targetObject.fields.map((field) => field.key));
    const unknownFields = bindings.filter((item) => !objectFieldKeys.has(item.fieldKey));
    const missingFields = targetObject.fields.filter((field) => (
      !bindings.some((binding) => binding.fieldKey === field.key)
    ));
    const tableColumns = new Set(table.fields.map((field) => field.name));
    const unknownColumns = bindings.filter((item) => !tableColumns.has(item.sourceColumn));
    if (unknownFields.length || missingFields.length || unknownColumns.length) {
      throw new AgentCoreError(
        'INVALID_OBJECT_SOURCE_BINDING',
        '对象字段与来源表字段没有形成完整的一一绑定',
        ['按对象全部字段重新提交 field_bindings，并使用 describe_table 返回的真实字段名'],
        400,
        {
          unknownFields: unknownFields.map((item) => item.fieldKey),
          missingFields: missingFields.map((item) => item.key),
          unknownColumns: unknownColumns.map((item) => item.sourceColumn),
        },
      );
    }
    const primaryKey = requiredStringArray(args.primary_key, 'primary_key');
    assertReferences('对象字段', primaryKey, targetObject.fields.map((field) => field.key));
    const evidence = requiredStringArray(args.evidence, 'evidence');
    const sourceColumnByField = new Map(bindings.map((item) => [item.fieldKey, item.sourceColumn]));
    const preservedSources = (targetObject.sourceRefs ?? []).filter((source) => source.kind !== 'table');
    const nextObject: SpecObject = {
      ...targetObject,
      sourceTable: undefined,
      sourceRefs: [
        ...preservedSources,
        {
          kind: 'table',
          documentId: document.id,
          table: table.name,
          columns: bindings.map((item) => item.sourceColumn),
        },
      ],
      primaryKey,
      fields: targetObject.fields.map((field) => ({
        ...field,
        sourceColumn: sourceColumnByField.get(field.key),
      })),
    };
    const objects = [...current.spec.objects];
    objects[objectIndex] = nextObject;
    const nextSpec = { ...current.spec, objects };
    const renderedPaths = [
      `objects/${key}.md`,
      'objects/index.md',
      'mappings/object-data-mappings.json',
    ];
    await this.stageCompiledSpec(
      workspace,
      context,
      nextSpec,
      renderedPaths,
      changeGroupId(args.change_group, 'change_group'),
      evidence,
      `对象 ${key} 的数据源绑定`,
    );
    return {
      ok: true,
      element: { kind: 'object', key },
      source: { documentId: document.id, table: table.name },
      boundFields: bindings.length,
      renderedPaths,
      deterministic: true,
      display: { text: `已绑定对象《${targetObject.title}》与数据表《${table.name}》` },
    };
  }

  private async bindObjectSources(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    if (workspace.package_data_source_type !== 'neo4j') {
      throw new AgentCoreError(
        'MULTI_SOURCE_OBJECT_UNSUPPORTED',
        '只有 Neo4j 图导入支持主表与扩展表组成的跨 Sheet 对象',
        ['拆分为逐来源对象，或将知识包数据源类型切换为 Neo4j 后重新建模'],
        400,
      );
    }
    const current = await this.overlay(workspace);
    const key = domainKey(args.key, '对象');
    const objectIndex = current.spec.objects.findIndex((item) => item.key === key);
    if (objectIndex < 0) {
      throw new AgentCoreError('OBJECT_NOT_FOUND', `对象不存在：${key}`, ['先核对对象目录'], 404);
    }
    const targetObject = current.spec.objects[objectIndex];
    const primaryKey = requiredStringArray(args.primary_key, 'primary_key');
    assertReferences('对象字段', primaryKey, targetObject.fields.map((field) => field.key));
    const parsed = this.parseObjectTableSources(
      workspace.project_id,
      targetObject.fields,
      requiredObjectArray(args.sources, 'sources'),
      primaryKey,
      'sources',
    );
    const evidence = requiredStringArray(args.evidence, 'evidence');
    const nextObject: SpecObject = {
      ...targetObject,
      sourceTable: undefined,
      sourceRefs: [
        ...(targetObject.sourceRefs ?? []).filter((source) => source.kind !== 'table'),
        ...parsed.sources,
      ],
      primaryKey,
      fields: targetObject.fields.map((field) => ({
        ...field,
        sourceColumn: parsed.defaultColumns.get(field.key),
      })),
    };
    const objects = [...current.spec.objects];
    objects[objectIndex] = nextObject;
    const renderedPaths = [
      `objects/${key}.md`,
      'objects/index.md',
      'mappings/object-data-mappings.json',
    ];
    await this.stageCompiledSpec(
      workspace,
      context,
      { ...current.spec, objects },
      renderedPaths,
      changeGroupId(args.change_group, 'change_group'),
      evidence,
      `对象 ${key} 的跨 Sheet 数据源绑定`,
    );
    return {
      ok: true,
      element: { kind: 'object', key },
      sources: parsed.sources.map((source) => ({
        documentId: source.documentId,
        table: source.table,
        role: source.role,
      })),
      boundFields: parsed.sources.reduce((sum, source) => sum + (source.fieldBindings?.length ?? 0), 0),
      renderedPaths,
      deterministic: true,
      display: { text: `已绑定对象《${targetObject.title}》的 ${parsed.sources.length} 张来源表` },
    };
  }

  private parseObjectTableSources(
    projectId: string,
    fields: SpecField[],
    rawSources: Record<string, unknown>[],
    primaryKey: string[],
    field: string,
  ): { sources: TableSourceRef[]; defaultColumns: Map<string, string> } {
    if (rawSources.length < 2) {
      throw new AgentCoreError(
        'INVALID_OBJECT_SOURCE_BINDING',
        '跨 Sheet 来源计划至少需要一张主表和一张扩展表',
        ['单表对象使用 source 或 bind_object_source'],
        400,
      );
    }
    const documents = listDocuments(projectId);
    const fieldKeys = new Set(fields.map((item) => item.key));
    const primaryKeys = new Set(primaryKey);
    const seenSources = new Set<string>();
    const boundSourcesByField = new Map<string, string[]>();
    const sources = rawSources.map((raw, index): TableSourceRef => {
      const prefix = `${field}[${index}]`;
      const documentId = text(raw.document_id, `${prefix}.document_id`);
      const tableName = text(raw.table, `${prefix}.table`);
      const role = String(raw.role ?? '');
      if (!['primary', 'extension'].includes(role)) {
        throw new AgentCoreError(
          'INVALID_OBJECT_SOURCE_BINDING',
          `${prefix}.role 必须是 primary 或 extension`,
          [],
          400,
        );
      }
      const sourceIdentity = `${documentId}\u0000${tableName}`;
      if (seenSources.has(sourceIdentity)) {
        throw new AgentCoreError('INVALID_OBJECT_SOURCE_BINDING', '跨 Sheet 来源计划包含重复表', [], 400);
      }
      seenSources.add(sourceIdentity);
      const document = documents.find((item) => item.id === documentId);
      const table = document?.tables?.find((item) => item.name === tableName);
      if (!document || document.parseStatus !== 'parsed' || !table) {
        throw new AgentCoreError(
          'TABLE_NOT_FOUND',
          `资料 ${documentId} 中不存在已解析的数据表《${tableName}》`,
          ['调用 list_tables 核对资料 ID 和表名'],
          404,
        );
      }
      const bindings = requiredObjectArray(raw.field_bindings, `${prefix}.field_bindings`).map((binding, bindingIndex) => ({
        fieldKey: text(binding.field_key, `${prefix}.field_bindings[${bindingIndex}].field_key`),
        sourceColumn: text(binding.source_column, `${prefix}.field_bindings[${bindingIndex}].source_column`),
      }));
      assertUniqueDomainKeys(bindings.map((item) => item.fieldKey), `${tableName} 对象字段绑定`);
      assertUniqueDomainKeys(bindings.map((item) => item.sourceColumn), `${tableName} 来源字段绑定`);
      assertReferences('对象字段', bindings.map((item) => item.fieldKey), [...fieldKeys]);
      assertReferences('来源字段', bindings.map((item) => item.sourceColumn), table.fields.map((item) => item.name));
      const bindingKeys = new Set(bindings.map((item) => item.fieldKey));
      const missingPrimaryKeys = primaryKey.filter((key) => !bindingKeys.has(key));
      if (missingPrimaryKeys.length) {
        throw new AgentCoreError(
          'INVALID_OBJECT_SOURCE_BINDING',
          `来源表《${tableName}》缺少主键字段绑定，无法按对象主键合并`,
          ['为每张主表和扩展表绑定全部对象主键字段'],
          400,
          { missingPrimaryKeys },
        );
      }
      for (const binding of bindings) {
        boundSourcesByField.set(binding.fieldKey, [
          ...(boundSourcesByField.get(binding.fieldKey) ?? []),
          tableName,
        ]);
      }
      return {
        kind: 'table',
        documentId: document.id,
        table: table.name,
        role: role as TableSourceRef['role'],
        columns: bindings.map((item) => item.sourceColumn),
        fieldBindings: bindings,
      };
    });
    const primarySources = sources.filter((source) => source.role === 'primary');
    if (primarySources.length !== 1) {
      throw new AgentCoreError(
        'INVALID_OBJECT_SOURCE_BINDING',
        '跨 Sheet 来源计划必须且只能包含一张 primary 主表',
        ['将决定对象实例范围的表设为 primary，其余表设为 extension'],
        400,
      );
    }
    const missingFields = fields.filter((item) => !boundSourcesByField.has(item.key)).map((item) => item.key);
    const ambiguousFields = fields.filter((item) => (
      !primaryKeys.has(item.key) && (boundSourcesByField.get(item.key)?.length ?? 0) > 1
    )).map((item) => item.key);
    if (missingFields.length || ambiguousFields.length) {
      throw new AgentCoreError(
        'INVALID_OBJECT_SOURCE_BINDING',
        missingFields.length
          ? '跨 Sheet 来源计划没有覆盖对象全部字段'
          : '同一非主键字段不能由多张来源表同时提供',
        ['调整逐来源字段绑定后一次性重新提交完整来源计划'],
        400,
        { missingFields, ambiguousFields },
      );
    }
    const defaultColumns = new Map<string, string>();
    for (const source of [primarySources[0], ...sources.filter((item) => item !== primarySources[0])]) {
      for (const binding of source.fieldBindings ?? []) {
        if (!defaultColumns.has(binding.fieldKey)) defaultColumns.set(binding.fieldKey, binding.sourceColumn);
      }
    }
    return { sources, defaultColumns };
  }

  private getProductLedger(workspace: WorkspaceRow) {
    const row = this.db.prepare(`SELECT source_revision, scope, required,
      ledger_json, validation_json, created_at, updated_at
      FROM product_ledger_snapshots
      WHERE project_id = ? AND source_revision = ?`).get(
      workspace.project_id,
      workspace.base_revision_oid,
    ) as {
      source_revision: string;
      scope: string;
      required: number;
      ledger_json: string;
      validation_json: string;
      created_at: string;
      updated_at: string;
    } | undefined;
    if (!row) {
      return {
        available: false,
        required: false,
        mode: config.productLedger.mode,
        sourceRevision: workspace.base_revision_oid,
        validation: null,
        display: { text: '当前没有产品清单对账台账' },
      };
    }
    let ledger: Record<string, unknown> = {};
    let validation: ProductLedgerValidation | null = null;
    try { ledger = JSON.parse(row.ledger_json) as Record<string, unknown>; } catch { ledger = {}; }
    try { validation = JSON.parse(row.validation_json) as ProductLedgerValidation; } catch { validation = null; }
    return {
      available: true,
      required: row.required === 1,
      mode: config.productLedger.mode,
      sourceRevision: row.source_revision,
      scope: row.scope,
      ledger,
      validation,
      rawCount: validation?.counts.raw ?? 0,
      canonicalCount: validation?.counts.canonical ?? 0,
      mergedGroupCount: validation?.counts.mergedGroups ?? 0,
      completeCount: validation?.counts.complete ?? 0,
      insufficientCount: validation?.counts.insufficient ?? 0,
      createdAt: row.created_at,
      updatedAt: row.updated_at,
      display: {
        text: validation?.pass === true
          ? `产品清单台账已校验：${validation.counts.raw} 条原始项，${validation.counts.canonical} 条去重项`
          : `产品清单台账校验未通过：${validation?.errors?.length ?? 0} 项待处理`,
      },
    };
  }

  private stageProductLedger(workspace: WorkspaceRow, args: Record<string, unknown>) {
    const raw = args.ledger && typeof args.ledger === 'object' && !Array.isArray(args.ledger)
      ? args.ledger as Record<string, unknown>
      : args;
    const normalizeEntry = (value: unknown): ProductLedger['entries'][number] => {
      const item = value && typeof value === 'object' && !Array.isArray(value)
        ? value as Record<string, unknown>
        : {};
      return {
        canonicalItemId: item.canonicalItemId ?? item.canonical_item_id,
        ruleId: item.ruleId ?? item.rule_id,
        sourceRecordIds: item.sourceRecordIds ?? item.source_record_ids,
        sourceLocator: item.sourceLocator ?? item.source_locator,
        mergeGroupId: item.mergeGroupId ?? item.merge_group_id,
        evidenceStatus: item.evidenceStatus ?? item.evidence_status,
        processingStatus: item.processingStatus ?? item.processing_status,
        insufficiencyReason: item.insufficiencyReason ?? item.insufficiency_reason,
      } as ProductLedger['entries'][number];
    };
    const ledger: ProductLedger = {
      schemaVersion: 'product-ledger-v1',
      sourceRevision: String(raw.sourceRevision ?? raw.source_revision ?? '').trim()
        || workspace.base_revision_oid,
      scope: String(raw.scope ?? 'product_catalog').trim(),
      rawCount: Number(raw.rawCount ?? raw.raw_count ?? 0),
      canonicalCount: Number(raw.canonicalCount ?? raw.canonical_count ?? 0),
      mergedGroupCount: Number(raw.mergedGroupCount ?? raw.merged_group_count ?? 0),
      entries: (Array.isArray(raw.entries) ? raw.entries : []).map(normalizeEntry),
      required: raw.required === true,
    };
    const required = ledger.required === true
      || ledger.rawCount >= 100
      || /(?:product|catalog|产品|商品|规则|手册|台账|清单)/iu.test(ledger.scope);
    ledger.required = required;
    const validation = validateProductLedger(ledger);
    const timestamp = nowIso();
    this.db.prepare(`INSERT INTO product_ledger_snapshots
      (project_id, source_revision, scope, required, ledger_json, validation_json, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(project_id, source_revision) DO UPDATE SET
        scope = excluded.scope,
        required = excluded.required,
        ledger_json = excluded.ledger_json,
        validation_json = excluded.validation_json,
        updated_at = excluded.updated_at`).run(
      workspace.project_id,
      ledger.sourceRevision,
      ledger.scope,
      required ? 1 : 0,
      JSON.stringify(ledger),
      JSON.stringify(validation),
      timestamp,
      timestamp,
    );
    if (!validation.pass && config.productLedger.mode === 'enforce' && required) {
      throw new AgentCoreError(
        'PRODUCT_LEDGER_INVALID',
        `产品清单台账校验未通过：${validation.errors.slice(0, 8).join('；')}`,
        ['补齐逐项来源定位、唯一 ID、归并关系和完整/不足状态后重新提交台账'],
        409,
        { validation, ledger },
      );
    }
    return {
      ok: validation.pass,
      required,
      mode: config.productLedger.mode,
      sourceRevision: ledger.sourceRevision,
      validation,
      counts: validation.counts,
      rawCount: validation.counts.raw,
      canonicalCount: validation.counts.canonical,
      mergedGroupCount: validation.counts.mergedGroups,
      completeCount: validation.counts.complete,
      insufficientCount: validation.counts.insufficient,
      display: {
        text: validation.pass
          ? `已记录产品清单台账：${validation.counts.raw} 条原始项，${validation.counts.canonical} 条去重项`
          : `已记录产品清单台账，但还有 ${validation.errors.length} 项待处理`,
      },
    };
  }

  private async schemaMappingPlan(workspace: WorkspaceRow) {
    const current = await this.overlay(workspace);
    const reviews = this.materialPurposeReviews.list(workspace.project_id);
    const problems = findMaterialPurposeReviewProblems(this.db, workspace.project_id, current.spec);
    const sheets = reviews.flatMap((review) => review.sheetDecisions.map((decision) => {
      const mappedObjects = current.spec.objects.filter((item) => tableSourcesFor(item).some((source) => (
        source.documentId === review.documentId && source.table === decision.sheet
      ))).map((item) => item.key);
      const mappingStatus = decision.importDisposition === 'object'
        ? mappedObjects.length === 0 ? 'unmapped' : mappedObjects.length === 1 ? 'mapped' : 'duplicated'
        : mappedObjects.length ? 'disposition_mismatch' : 'not_applicable';
      return {
        documentId: review.documentId,
        sheet: decision.sheet,
        importDisposition: decision.importDisposition,
        mappedObjects,
        mappingStatus,
      };
    }));
    const objectSheets = sheets.filter((item) => item.importDisposition === 'object');
    const mappedObjectSheets = objectSheets.filter((item) => item.mappingStatus === 'mapped');
    return {
      complete: problems.length === 0,
      sheetCount: sheets.length,
      objectSheetCount: objectSheets.length,
      mappedObjectSheetCount: mappedObjectSheets.length,
      unresolvedObjectSheetCount: objectSheets.length - mappedObjectSheets.length,
      sheets,
      problems,
      next: problems.length
        ? ['完成全部实体 Sheet 的对象来源计划后，再发起候选确认']
        : ['来源映射台账完整，可以继续候选预检'],
      display: {
        text: problems.length
          ? `数据 Schema 映射计划还有 ${problems.length} 项未完成`
          : '数据 Schema 与对象来源映射已经对齐',
      },
    };
  }

  private async stageCompiledSpec(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    spec: PackageSpec,
    renderedPaths: string[],
    changeGroup: string,
    evidence: string[],
    reason: string,
    scriptGateContext?: {
      runId: string;
      taskId: string;
      tool: string;
      targetKey: string;
    },
  ) {
    assertMaterialEvidenceReferences(workspace.project_id, evidence);
    const declaredDetailPath = renderedPaths.find((path) => !path.endsWith('/index.md')
      && path !== 'mappings/object-data-mappings.json');
    let compiledSpec: PackageSpec;
    let rendered: Record<string, string>;
    try {
      compiledSpec = validateSpecKeys(canonicalizePackageSpec(spec));
      rendered = renderPackage(compiledSpec);
    } catch (error) {
      throw new AgentCoreError(
        'INVALID_DOMAIN_PAYLOAD',
        error instanceof Error ? error.message : '结构化知识无法编译',
        ['修正领域工具参数中的 key、引用或内容后重试'],
        400,
      );
    }
    const current = await this.overlay(workspace);
    const detailPath = renderedPaths.find((path) => (
      !path.endsWith('/index.md')
      && path !== 'mappings/object-data-mappings.json'
      && rendered[path] !== current.files[path]
    )) ?? declaredDetailPath;
    if (detailPath) await this.assertCandidateStageGate(workspace, detailPath, scriptGateContext);
    const currentFiles = current.files;
    await this.discardFrozenCandidate(workspace, context, '领域结构继续修改，旧确认请求已失效');
    this.db.exec('SAVEPOINT agent_domain_compile');
    try {
      for (const path of renderedPaths) {
        const content = rendered[path];
        if (content === undefined) {
          throw new AgentCoreError(
            'DOMAIN_COMPILER_OUTPUT_MISSING',
            `确定性编译没有生成预期文件：${path}`,
            ['核对结构化资源类型和 key 后重试'],
            500,
          );
        }
        if (currentFiles[path] === content) continue;
        await this.upsertChange(workspace, {
          path,
          kind: 'write',
          content,
          groupId: path === detailPath ? changeGroup : COMPILED_RESOURCES_GROUP,
          evidence,
          reason,
        });
      }
      if (rendered[OBJECT_DATA_MAPPING_PATH] === undefined
        && Object.hasOwn(current.files, OBJECT_DATA_MAPPING_PATH)) {
        await this.upsertChange(workspace, {
          path: OBJECT_DATA_MAPPING_PATH,
          kind: 'delete',
          content: null,
          groupId: COMPILED_RESOURCES_GROUP,
          evidence: [],
          reason: '虚拟对象模式不生成物理数据 mapping',
        });
      }
      await this.upsertChange(workspace, {
        path: INTERNAL_SPEC_CHANGE_PATH,
        kind: 'write',
        content: `${JSON.stringify(compiledSpec, null, 2)}\n`,
        groupId: COMPILED_RESOURCES_GROUP,
        evidence: [],
        reason: '领域工具确定性编译的语义规格',
      });
      this.db.exec('RELEASE SAVEPOINT agent_domain_compile');
    } catch (error) {
      this.db.exec('ROLLBACK TO SAVEPOINT agent_domain_compile');
      this.db.exec('RELEASE SAVEPOINT agent_domain_compile');
      throw error;
    }
  }

  private async writeFile(workspace: WorkspaceRow, context: AgentToolContext, args: Record<string, unknown>) {
    const path = packagePath(text(args.path, 'path'));
    let content = text(args.content, 'content', true);
    const evidence = stringArray(args.evidence);
    let manifestNormalization: { ignoredFields: string[] } | undefined;
    if (path === INTERNAL_SPEC_CHANGE_PATH) {
      throw new AgentCoreError(
        'INTERNAL_FILE_WRITE_FORBIDDEN',
        '内部语义规格只能由领域工具生成',
        ['使用 create_*、patch_*、领域删除或脚本契约工具'],
        400,
      );
    }
    if (path === 'mappings/object-data-mappings.json') {
      const physicalTables = schemaTables(this.tables(context.project.id));
      if (!physicalTables.length) {
        throw new AgentCoreError(
          'PHYSICAL_SCHEMA_REQUIRED',
          '项目没有确定数据 Schema，虚拟对象不能写入物理数据 mapping',
          ['移除 mapping 修改；只保留不绑定物理表的虚拟对象'],
          409,
          {
            managedPath: path,
            object_source_mode: 'virtual',
            retryable: false,
          },
        );
      }
      const active = this.changeControl.activePlan(context.project.id, context.runId);
      const plan = active
        ? this.changeControl.snapshot(context.project.id, context.runId, active.id)
        : null;
      const planTools = new Set(
        plan?.tasks.flatMap((task) => task.allowed_tools) ?? [],
      );
      const requiredTool = planTools.has('patch_object_source_binding')
        && !planTools.has('sync_schema_objects')
        ? 'patch_object_source_binding'
        : 'sync_schema_objects';
      const schemaSync = requiredTool === 'sync_schema_objects';
      const issueId = stableAgentIssueId(
        schemaSync ? 'managed-schema-objects' : 'managed-object-binding',
        path,
      );
      throw new AgentCoreError(
        'MANAGED_FILE_REQUIRES_DOMAIN_TOOL',
        '对象数据映射是受管文件，不能直接写入',
        [schemaSync
          ? '读取当前计划后使用 sync_schema_objects，由核心按全部 Schema Sheet 重新生成对象与 mapping'
          : '读取当前计划、对象资源和来源表后，使用 patch_object_source_binding 由核心重新生成对象与 mapping'],
        409,
        {
          managedPath: path,
          requiredAction: 'redirect_tool',
          requiredTool,
          targetKind: schemaSync ? 'schema_objects' : 'object_binding',
          issueId,
          retryable: false,
          recoveryAction: {
            id: schemaSync ? 'resume_schema_object_sync' : 'resume_object_binding',
            label: schemaSync ? '自动完成 Schema 对象同步' : '自动完成对象绑定',
          },
        },
      );
    }
    if (MANAGED_DOMAIN_MARKDOWN.test(path)) {
      const kind = path.split('/', 1)[0].replace(/s$/u, '');
      const requiredTool = path.endsWith('/index.md') ? `create_${kind} / patch_${kind}` : `patch_${kind}`;
      throw new AgentCoreError(
        'MANAGED_FILE_REQUIRES_DOMAIN_TOOL',
        `${path} 是领域编译器受管文件，不能直接写入`,
        [`先读取结构化资源，再使用 ${requiredTool}；索引由核心自动生成`],
        409,
        {
          managedPath: path,
          requiredTool,
          ...(!path.endsWith('/index.md') ? {
            requiredAction: 'redirect_tool',
            targetKind: `${kind}_resource`,
            issueId: stableAgentIssueId(`managed-${kind}`, path),
          } : {}),
          contractVersion: DOMAIN_TOOL_CONTRACT_VERSION,
          retryable: false,
        },
      );
    }
    if (MANAGED_SCRIPT_PATH.test(path) && this.usesScriptContract(context)) {
      throw new AgentCoreError(
        'MANAGED_FILE_REQUIRES_DOMAIN_TOOL',
        `${path} 是脚本契约受管文件，不能直接写入`,
        ['使用 get_script_edit_context 读取当前符号与哈希，再通过 stage_script_patch 提交最小补丁'],
        409,
        {
          managedPath: path,
          requiredAction: 'redirect_tool',
          requiredTool: 'stage_script_patch',
          targetKind: 'script_source',
          issueId: stableAgentIssueId('managed-script', path),
          contractVersion: 'script-contract-v1',
          retryable: false,
        },
      );
    }
    if (path.startsWith('knowledge/')) {
      throw new AgentCoreError(
        'INVALID_KNOWLEDGE_PATH',
        '知识条目不能写入 knowledge/ 通用目录，consumer 不会读取这些文件',
        ['按内容分别写入 operations/、objects/、rules/ 或 metrics/，索引写入各目录的 index.md'],
        400,
      );
    }
    if (path.startsWith('tests/')) {
      throw new AgentCoreError(
        'EVALUATION_CASES_USER_ONLY',
        '正式评测用例不能由 Agent 写入知识包或从知识内容反推',
        ['请用户在评测界面录入真实场景和参考答案；缺少参考答案时保留为空并等待人工判定'],
        400,
      );
    }
    if (path.endsWith('.mmd') && !path.startsWith('diagrams/')) {
      throw new AgentCoreError(
        'INVALID_DIAGRAM_PATH',
        'Mermaid 流程图必须放在 diagrams/ 目录下',
        [`将文件路径调整为 diagrams/${path.split('/').at(-1)}`],
        400,
      );
    }
    if (content.length > MAX_FILE_CHARS) {
      throw new AgentCoreError('WRITE_UNSAFE', '单个候选文件超过 100 万字符', ['拆分为多个单一职责文件后重试'], 400);
    }
    const mermaidIssues = validateMermaidSource(path, content);
    if (mermaidIssues.length) {
      throw new AgentCoreError(
        'MERMAID_CONTENT_INVALID',
        `Mermaid 候选源码校验未通过：${mermaidIssues.map((issue) => issue.message).join('；')}`,
        ['按 findings 删除 Markdown 代码围栏并修正流程图声明后，重新写入同一候选文件'],
        400,
        {
          retryable: true,
          validatorVersion: MERMAID_SOURCE_VALIDATOR_VERSION,
          findingCount: mermaidIssues.length,
          findings: mermaidIssues,
        },
      );
    }
    await this.assertCandidateStageGate(workspace, path);
    if (path === 'manifest.yaml') {
      const base = await this.artifacts.readPackageFiles(workspace.project_id, workspace.base_revision_oid);
      const current = await this.overlay(workspace);
      try {
        const merged = mergeAgentManifest(
          base.files['manifest.yaml'],
          current.files['manifest.yaml'],
          content,
        );
        content = merged.content;
        manifestNormalization = { ignoredFields: merged.ignoredFields };
      } catch (cause) {
        const message = cause instanceof Error ? cause.message : 'manifest.yaml 不符合知识包生成契约';
        throw new AgentCoreError(
          'MANIFEST_CONTRACT_INVALID',
          message,
          ['读取本轮 packageContract，只提交 description 和 resources 后重试'],
          400,
          {
            retryable: true,
            validatorVersion: PACKAGE_CONTRACT_VERSION,
            findingCount: 1,
            findings: [{
              ruleId: 'manifest.yaml.structured-content',
              path: 'manifest.yaml',
              message,
              suggestion: '保留平台管理字段，只提交合法 YAML 的 description 和 resources',
            }],
          },
        );
      }
    }
    if (/^(?:objects|operations|rules|metrics)\/.+\.md$/u.test(path)) {
      const current = await this.overlay(workspace);
      const candidateFiles = { ...current.files, [path]: content };
      const existingIssues = path in current.files
        ? new Set(validateKnowledgeMarkdownFile(path, current.files[path], current.files))
        : new Set<string>();
      const introducedIssues = validateKnowledgeMarkdownFile(path, content, candidateFiles)
        .filter((issue) => !existingIssues.has(issue));
      if (introducedIssues.length) {
        const findings = introducedIssues.map(knowledgeValidationFinding);
        throw new AgentCoreError(
          'KNOWLEDGE_FILE_INVALID',
          `候选文件快速校验未通过：${introducedIssues.join('；')}`,
          ['完整处理 findings 中当前文件的每一项后重新写入；不要修改无关文件'],
          400,
          {
            retryable: true,
            validatorVersion: KNOWLEDGE_MARKDOWN_VALIDATOR_VERSION,
            findingCount: findings.length,
            findings,
          },
        );
      }
    }
    assertMaterialEvidenceReferences(workspace.project_id, evidence);
    const groupId = path.startsWith('.modeling/') && path !== '.modeling/goal.md'
      ? RUNTIME_GROUP
      : changeGroupId(args.change_group, 'change_group');
    await this.discardFrozenCandidate(workspace, context, '候选内容继续修改，旧确认请求已失效');
    await this.upsertChange(workspace, {
      path, kind: 'write', content, groupId,
      evidence, reason: null,
    });
    const managedWriteWarning = MANAGED_DOMAIN_MARKDOWN.test(path)
      ? {
        code: 'MANAGED_WRITE_DEPRECATED',
        message: `${path} 已进入领域工具灰度管理；请改用 list_domain_resources、get_domain_resource 和对应 create_*/patch_* 工具`,
      }
      : undefined;
    return { ok: true, path, changeGroup: groupId, candidate: true,
      ...(manifestNormalization ? { manifestNormalization } : {}),
      ...(managedWriteWarning ? { warning: managedWriteWarning } : {}),
      display: { text: `已写入候选文件《${path}》` } };
  }

  private async deleteFile(workspace: WorkspaceRow, context: AgentToolContext, args: Record<string, unknown>) {
    const path = packagePath(text(args.path, 'path'));
    if (MANAGED_DOMAIN_MARKDOWN.test(path) || path === OBJECT_DATA_MAPPING_PATH) {
      throw new AgentCoreError(
        'MANAGED_FILE_REQUIRES_DOMAIN_TOOL',
        `${path} 是领域编译器受管文件，不能按路径删除`,
        ['使用 preview_domain_delete 查看引用影响，再用 delete_domain_resource 原子删除'],
        409,
        {
          managedPath: path,
          requiredAction: 'redirect_tool',
          requiredTool: 'preview_domain_delete',
          targetKind: 'domain_delete',
          issueId: stableAgentIssueId('managed-domain-delete', path),
          contractVersion: DOMAIN_TOOL_CONTRACT_VERSION,
          retryable: false,
        },
      );
    }
    if (MANAGED_SCRIPT_PATH.test(path) && this.usesScriptContract(context)) {
      throw new AgentCoreError(
        'MANAGED_FILE_REQUIRES_DOMAIN_TOOL',
        `${path} 是脚本契约受管文件，不能按路径删除`,
        ['脚本删除需要结构化解绑和引用预览；当前版本请保留脚本或放弃整批候选'],
        409,
        {
          managedPath: path,
          requiredAction: 'redirect_tool',
          requiredTool: 'stage_script_patch',
          targetKind: 'script_source',
          issueId: stableAgentIssueId('managed-script-delete', path),
          contractVersion: 'script-contract-v1',
          retryable: false,
        },
      );
    }
    if (path === INTERNAL_SPEC_CHANGE_PATH) {
      throw new AgentCoreError(
        'INTERNAL_FILE_DELETE_FORBIDDEN',
        '内部语义规格不能单独删除',
        ['需要撤销整批候选时使用 discard_rejected_candidate_changes'],
        409,
      );
    }
    const groupId = path.startsWith('.modeling/') && path !== '.modeling/goal.md'
      ? RUNTIME_GROUP
      : changeGroupId(args.change_group, 'change_group');
    const base = await this.artifacts.readPackageFiles(workspace.project_id, workspace.base_revision_oid);
    const existing = this.changes(workspace).find((change) => change.path === path);
    if (!(path in base.files)) {
      if (existing) {
        await this.discardFrozenCandidate(workspace, context, '候选新增文件已撤销，旧确认请求已失效');
        this.db.prepare(`DELETE FROM agent_candidate_changes
          WHERE project_id = ? AND run_id = ? AND path = ?`)
          .run(workspace.project_id, workspace.run_id, path);
        return {
          ok: true,
          path,
          cancelledCandidateAddition: true,
          removedChangeGroup: existing.group_id,
          display: { text: `已撤销候选新增文件《${path}》` },
        };
      }
      throw new AgentCoreError('FILE_NOT_FOUND', `知识包文件不存在：${path}`, ['调用 list_dir 核对路径'], 404);
    }
    if (AGENT_REQUIRED_FILES.has(path)) {
      throw new AgentCoreError(
        'REQUIRED_FILE_DELETE_FORBIDDEN',
        `《${path}》是项目必需入口，不能删除`,
        ['需要调整时用 write_file 覆盖内容，不要删除必需入口'],
        409,
      );
    }
    if (path.endsWith('/index.md')) {
      const { files } = await this.overlay(workspace);
      const prefix = path.slice(0, -'index.md'.length);
      const details = Object.keys(files).filter((candidate) => (
        candidate.startsWith(prefix) && candidate !== path && !candidate.endsWith('/index.md')
      ));
      if (details.length) {
        throw new AgentCoreError(
          'INDEX_NOT_EMPTY',
          `《${path}》仍索引 ${details.length} 个详情文件，不能删除`,
          ['先在同一候选中清理对应详情，或保留并更新索引'],
          409,
          { blockers: details },
        );
      }
    }
    await this.assertCandidateStageGate(workspace, path);
    await this.discardFrozenCandidate(workspace, context, '候选内容继续修改，旧确认请求已失效');
    await this.upsertChange(workspace, {
      path, kind: 'delete', content: null, groupId,
      evidence: [], reason: String(args.reason ?? '').trim() || null,
    });
    return { ok: true, path, changeGroup: groupId, candidate: true,
      display: { text: `已加入候选删除《${path}》` } };
  }

  private async regroupChanges(workspace: WorkspaceRow, context: AgentToolContext, _args: Record<string, unknown>) {
    const changes = this.changes(workspace).filter((change) => !isHiddenCandidateGroup(change.group_id));
    if (!changes.length) {
      throw new AgentCoreError('NO_CANDIDATE', '没有可重组的业务候选', ['先用 write_file 写入业务候选']);
    }
    const groups = suggestChangeGroupAssignments(changes);
    await this.discardFrozenCandidate(workspace, context, '候选确认分组已调整，旧确认请求已失效');
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const update = this.db.prepare(`UPDATE agent_candidate_changes SET group_id = ?
        WHERE project_id = ? AND run_id = ? AND path = ?`);
      for (const group of groups) {
        for (const path of group.paths) update.run(group.id, workspace.project_id, workspace.run_id, path);
      }
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
    return {
      ok: true,
      assignments: groups,
      change_groups: groups.map(({ paths, ...group }) => ({ ...group, impact: paths })),
      display: { text: `已将业务候选整理为 ${groups.length} 个确认组` },
    };
  }

  private async prepareConfirmationGroups(workspace: WorkspaceRow) {
    if (workspace.candidate_id) {
      const candidate = await this.artifacts.getCandidateRevision(workspace.project_id, workspace.candidate_id);
      if (candidate.status === 'pending') {
        return {
          candidate_revision: candidate.id,
          change_groups: this.candidateGroups(candidate.id).filter((group) => !isHiddenCandidateGroup(group.id)),
          frozen: true,
          display: { text: '已恢复原待确认内容' },
        };
      }
    }
    const changes = (await this.effectiveChanges(workspace)).filter((change) => (
      !isHiddenCandidateGroup(change.group_id)
    ));
    if (!changes.length) {
      throw new AgentCoreError(
        'NO_CANDIDATE',
        '没有需要专家确认的业务候选',
        ['仅有内部工作记录时直接结束当前复核'],
        409,
        { retryable: false },
      );
    }
    const groups = confirmationGroupAssignments(changes);
    const currentByPath = new Map(changes.map((change) => [change.path, change.group_id]));
    const normalized = groups.some((group) => group.paths.some((path) => currentByPath.get(path) !== group.id));
    if (normalized) {
      this.db.exec('BEGIN IMMEDIATE');
      try {
        const update = this.db.prepare(`UPDATE agent_candidate_changes SET group_id = ?
          WHERE project_id = ? AND run_id = ? AND path = ?`);
        for (const group of groups) {
          for (const path of group.paths) update.run(group.id, workspace.project_id, workspace.run_id, path);
        }
        this.db.exec('COMMIT');
      } catch (error) {
        if (this.db.isTransaction) this.db.exec('ROLLBACK');
        throw error;
      }
    }
    return {
      normalized,
      change_groups: groups.map(({ paths, ...group }) => ({ ...group, impact: paths })),
      display: { text: normalized ? '已整理待确认内容' : '待确认内容已就绪' },
    };
  }

  private async assertCandidateStageGate(
    workspace: WorkspaceRow,
    path: string,
    scriptGateContext?: {
      runId: string;
      taskId: string;
      tool: string;
      targetKey: string;
    },
  ) {
    if (path.startsWith('.modeling/')) return;
    const registered = path.startsWith('diagrams/') || path.startsWith('scripts/')
      || /^(objects|operations|rules|metrics|mappings)\//u.test(path)
      || path === 'manifest.yaml' || path === 'index.md';
    if (!registered) {
      throw new AgentCoreError(
        'INVALID_CANDIDATE_PATH',
        `候选文件不属于已注册的知识包目录：${path}`,
        ['按内容写入 diagrams/、objects/、operations/、rules/、metrics/、mappings/ 或 scripts/'],
        400,
      );
    }
    const changes = await this.effectiveChanges(workspace);
    const pendingPaths = new Set(changes.map((change) => change.path));
    const confirmed = this.confirmedPaths(workspace.project_id);
    const base = await this.artifacts.readPackageFiles(workspace.project_id, workspace.base_revision_oid);
    const basePaths = Object.keys(base.files);
    const trustedProcessBaseline = basePaths.some((candidate) => (
      /^(objects|operations|rules|metrics|scripts)\/.+/u.test(candidate)
      && !candidate.endsWith('/index.md')
    ));
    const trustedGoalBaseline = trustedProcessBaseline || basePaths.some((candidate) => (
      candidate.startsWith('diagrams/') && candidate.endsWith('.mmd')
    ));
    const goalConfirmed = (confirmed.has('.modeling/goal.md') || trustedGoalBaseline)
      && !pendingPaths.has('.modeling/goal.md');
    if (path.startsWith('diagrams/')) {
      if (goalConfirmed) return;
      throw new AgentCoreError(
        'GATE_UNMET',
        '业务流程必须建立在已经确认的建模目标上',
        ['先提交建模目标确认；专家确认后再形成流程候选'],
        409,
        { gate: 'goal_confirmed', blockers: ['建模目标尚未确认或正在调整'] },
      );
    }
    const { files } = await this.overlay(workspace);
    const diagrams = Object.keys(files).filter((candidate) => (
      candidate.startsWith('diagrams/') && candidate.endsWith('.mmd')
    ));
    const unconfirmed = diagrams.filter((candidate) => (
      !confirmed.has(candidate) || pendingPaths.has(candidate)
    ));
    const processConfirmed = !changes.some((change) => change.path.startsWith('diagrams/'))
      && (trustedProcessBaseline
        || (goalConfirmed && diagrams.length > 0 && unconfirmed.length === 0));
    if (path.startsWith('scripts/')) {
      const knowledgePaths = Object.keys(files).filter((candidate) => (
        /^(objects|operations|rules|metrics)\/.+\.md$/u.test(candidate)
        && !candidate.endsWith('/index.md')
      ));
      const pendingKnowledgeChanges = changes.filter((change) => (
        !isHiddenCandidateGroup(change.group_id)
        && /^(objects|operations|rules|metrics)\//u.test(change.path)
      ));
      const plannedAuthorization = scriptGateContext?.taskId
        ? this.changeControl.scriptCandidateDependencyAuthorization({
          projectId: workspace.project_id,
          runId: scriptGateContext.runId,
          taskId: scriptGateContext.taskId,
          tool: scriptGateContext.tool,
          targetKey: scriptGateContext.targetKey,
        })
        : null;
      // Published baselines can stage a script together with knowledge edits, but only when every
      // visible knowledge candidate is proven to come from the script task's staged dependencies.
      const plannedKnowledgeDependencies = Boolean(
        trustedProcessBaseline
        && plannedAuthorization
        && pendingKnowledgeChanges.length > 0
        && pendingKnowledgeChanges.every((change) => (
          plannedAuthorization.allowedGroupIds.includes(change.group_id)
          && plannedAuthorization.allowedPaths.includes(change.path)
        )),
      );
      const knowledgeConfirmed = (!pendingKnowledgeChanges.length || plannedKnowledgeDependencies)
        && (trustedProcessBaseline
        || (processConfirmed && knowledgePaths.length > 0
          && knowledgePaths.every((candidate) => confirmed.has(candidate))));
      if (knowledgeConfirmed) return;
      throw new AgentCoreError(
        'GATE_UNMET',
        '确定性脚本必须建立在已经确认的知识内容上',
        ['先确认对象、操作、规则和指标候选，再确认脚本契约并生成脚本'],
        409,
        { gate: 'knowledge_confirmed', blockers: ['知识内容尚未确认或正在调整'] },
      );
    }
    if (processConfirmed) return;
    throw new AgentCoreError(
      'GATE_UNMET',
      '知识内容必须建立在已经确认的业务流程上',
      [diagrams.length
        ? `先确认流程：${unconfirmed.join('、')}`
        : '先形成 diagrams/*.mmd 流程候选并请专家确认'],
      409,
      { gate: 'process_confirmed', blockers: unconfirmed.length ? unconfirmed : ['尚无已确认业务流程'] },
    );
  }

  private async updateRuntimeFile(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    path: string,
    args: Record<string, unknown>,
  ) {
    const content = text(args.markdown, 'markdown', true);
    await this.discardFrozenCandidate(workspace, context, '运行锚点已更新，旧确认请求已失效');
    await this.upsertChange(workspace, {
      path, kind: 'write', content, groupId: RUNTIME_GROUP, evidence: [], reason: null,
    });
    return { ok: true, path, candidate: true, display: { text: path.endsWith('plan.md') ? '已更新建模计划' : '已更新项目摘要' } };
  }

  private async upsertChange(
    workspace: WorkspaceRow,
    change: { path: string; kind: 'write' | 'delete'; content: string | null; groupId: string; evidence: string[]; reason: string | null },
  ): Promise<void> {
    await runProjectWriteExclusive(workspace.project_id, async () => {
      this.db.prepare(`INSERT INTO agent_candidate_changes (
        project_id, run_id, path, kind, content, group_id, evidence_json, reason, updated_at
      ) VALUES (?,?,?,?,?,?,?,?,?)
      ON CONFLICT(project_id, run_id, path) DO UPDATE SET
        kind = excluded.kind,
        content = excluded.content,
        group_id = excluded.group_id,
        evidence_json = excluded.evidence_json,
        reason = excluded.reason,
        updated_at = excluded.updated_at`)
        .run(
          workspace.project_id, workspace.run_id, change.path, change.kind, change.content,
          change.groupId, JSON.stringify(change.evidence), change.reason, nowIso(),
        );
    });
  }

  private async effectiveChanges(workspace: WorkspaceRow): Promise<ChangeRow[]> {
    const changes = this.changes(workspace);
    if (!changes.length) return changes;
    const base = await this.artifacts.readPackageFiles(workspace.project_id, workspace.base_revision_oid);
    const noOpPaths = changes.filter((change) => (
      change.kind === 'write'
        ? base.files[change.path] === change.content
        : !Object.hasOwn(base.files, change.path)
    )).map((change) => change.path);
    if (!noOpPaths.length) return changes;

    this.db.exec('BEGIN IMMEDIATE');
    try {
      const remove = this.db.prepare(`DELETE FROM agent_candidate_changes
        WHERE project_id = ? AND run_id = ? AND path = ?`);
      for (const path of noOpPaths) remove.run(workspace.project_id, workspace.run_id, path);
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
    const removed = new Set(noOpPaths);
    return changes.filter((change) => !removed.has(change.path));
  }

  private async grepPackage(workspace: WorkspaceRow, args: Record<string, unknown>) {
    const pattern = text(args.pattern, 'pattern');
    const { files } = await this.overlay(workspace);
    const hits: Array<{ path: string; line: number; text: string }> = [];
    for (const [path, content] of Object.entries(files)) {
      for (const [index, line] of content.split('\n').entries()) {
        if (line.toLocaleLowerCase('zh-CN').includes(pattern.toLocaleLowerCase('zh-CN'))) {
          hits.push({ path, line: index + 1, text: line.slice(0, 500) });
          if (hits.length >= 100) break;
        }
      }
      if (hits.length >= 100) break;
    }
    return { pattern, hits, truncated: hits.length >= 100,
      display: { text: hits.length ? `在知识包中找到 ${hits.length} 处匹配` : `知识包中没有找到“${pattern}”` } };
  }

  private async whoReferences(workspace: WorkspaceRow, args: Record<string, unknown>) {
    const target = text(args.target, 'target');
    const { files } = await this.overlay(workspace);
    const references = Object.entries(files).flatMap(([path, content]) => (
      content.includes(target) ? [{ path, occurrences: content.split(target).length - 1 }] : []
    ));
    return { target, references, display: { text: `找到 ${references.length} 个直接引用条目` } };
  }

  private async analyzeMaterialImpact(
    workspace: WorkspaceRow,
    args: Record<string, unknown>,
  ): Promise<Record<string, any>> {
    const requestedSources = [...new Set(stringArray(args.sources))];
    if (requestedSources.length) {
      if (requestedSources.length > 20 || Object.hasOwn(args, 'direct_resources')) {
        throw new AgentCoreError(
          'INVALID_MATERIAL_IMPACT_BATCH',
          '合并影响视图需要 1 到 20 份已经完成范围界定的资料',
          ['先逐份界定直接影响范围，再按同一批资料生成合并视图'],
          400,
        );
      }
      const results: Array<Record<string, any>> = [];
      for (const requestedSource of requestedSources) {
        const result = await this.analyzeMaterialImpact(workspace, { source: requestedSource });
        if (!results.some((item) => item.impactId === result.impactId)) results.push(result);
      }
      return combineMaterialImpactResults(results);
    }
    const source = String(args.source ?? '').normalize('NFKC').trim();
    const rows = this.db.prepare(`SELECT impacts.id, impacts.source_key, impacts.previous_document_id,
        impacts.current_document_id, impacts.change_kind, impacts.semantic_changed,
        impacts.affected_json, impacts.created_at,
        current_doc.filename AS current_filename,
        previous_doc.filename AS previous_filename
      FROM material_revision_impacts impacts
      LEFT JOIN documents current_doc ON current_doc.id = impacts.current_document_id
      LEFT JOIN documents previous_doc ON previous_doc.id = impacts.previous_document_id
      WHERE impacts.project_id = ?
      ORDER BY impacts.created_at DESC, impacts.rowid DESC`)
      .all(workspace.project_id) as any[];
    const row = rows.find((item) => !source || [
      item.source_key, item.current_document_id, item.previous_document_id,
      item.current_filename, item.previous_filename,
    ].some((value) => String(value ?? '').normalize('NFKC') === source));
    if (!row) {
      throw new AgentCoreError(
        'MATERIAL_IMPACT_NOT_FOUND',
        source ? `没有找到资料“${source}”的修订影响记录` : '项目还没有资料修订影响记录',
        ['先等待新增资料解析完成，再重新分析影响'],
        404,
      );
    }

    let analysis: {
      affectedResources?: Array<{ kind?: string; key?: string }>;
      directResources?: Array<{ kind?: string; key?: string }>;
      scopeResolved?: boolean;
    } = {};
    try { analysis = JSON.parse(String(row.affected_json || '{}')); } catch { /* keep empty */ }
    const { files, spec } = await this.overlay(workspace);
    if (Object.hasOwn(args, 'direct_resources')) {
      if (analysis.scopeResolved !== false) {
        throw new AgentCoreError(
          'MATERIAL_IMPACT_SCOPE_ALREADY_RESOLVED',
          '这份资料的影响范围已经由来源关系确定，不需要重复界定',
          ['继续处理当前已确定的影响项'],
        );
      }
      const direct = impactResources(args.direct_resources);
      const missing = direct.filter((item) => !findKnowledgeResourcePath(files, item.kind, item.key));
      if (missing.length) {
        throw new AgentCoreError(
          'MATERIAL_IMPACT_RESOURCE_NOT_FOUND',
          `知识包中找不到这些直接影响项：${missing.map((item) => `${knowledgeKindLabel(item.kind)}“${item.key}”`).join('、')}`,
          ['先读取知识文件确认 frontmatter key，再重新界定影响范围'],
          400,
        );
      }
      const resolved = resolveMaterialImpactScope(this.db, workspace.project_id, String(row.id), spec, direct);
      applyMaterialInvalidation(this.db, workspace.project_id, resolved);
      analysis = resolved;
    }
    const affectedResources = Array.isArray(analysis.affectedResources)
      ? analysis.affectedResources.filter((item) => item?.kind && item?.key)
      : [];
    const businessPaths = Object.keys(files).filter(isReviewableKnowledgePath).sort();
    const affectedPaths = new Map<string, { kind: string; key: string }>();
    for (const resource of affectedResources) {
      const kind = String(resource.kind);
      const key = String(resource.key);
      const match = findKnowledgeResourcePath(files, kind, key);
      if (match) affectedPaths.set(match, { kind, key });
    }
    const scopeResolved = analysis.scopeResolved !== false;
    if (!scopeResolved) {
      for (const path of businessPaths) {
        if (!affectedPaths.has(path)) affectedPaths.set(path, { kind: 'knowledge', key: path });
      }
    }

    const previousDocument = row.previous_document_id
      ? {
        id: String(row.previous_document_id),
        sourceKey: String(row.source_key),
        filename: String(row.previous_filename ?? ''),
      }
      : null;
    const directPaths = new Set([
      ...(previousDocument ? referringFiles(files, previousDocument).map((item) => item.path) : []),
      ...(analysis.directResources ?? []).flatMap((item) => {
        const match = item.kind && item.key
          ? findKnowledgeResourcePath(files, String(item.kind), String(item.key))
          : undefined;
        return match ? [match] : [];
      }),
    ]);
    const sourceName = String(row.current_filename || row.previous_filename || row.source_key);
    const sourceDocumentId = String(row.current_document_id || row.previous_document_id || '');
    const todoItems = [...affectedPaths].map(([path, resource]) => ({
      t: path,
      s: !scopeResolved
        ? `新增资料《${sourceName}》尚未关联旧版本，需先核对是否改变当前内容`
        : directPaths.has(path)
        ? `直接引用《${sourceName}》的${knowledgeKindLabel(resource.kind)}`
        : `由${knowledgeKindLabel(resource.kind)}“${resource.key}”的显式引用传播`,
      direct: directPaths.has(path),
    }));
    const intactPaths = scopeResolved
      ? businessPaths.filter((path) => !affectedPaths.has(path))
      : [];
    const semanticChanged = Boolean(row.semantic_changed);
    const view = {
      scopeResolved,
      intact: { n: intactPaths.length, items: intactPaths },
      todo: {
        n: todoItems.length,
        decide: 0,
        items: todoItems.map(({ t, s }) => ({ t, s })),
      },
      chains: todoItems.map((item) => ({
        src: `《${sourceName}》`,
        srcDocumentId: sourceDocumentId,
        srcSub: !scopeResolved
          ? '新增资料尚未绑定旧版本，影响范围待助手核对'
          : semanticChanged ? '新旧修订语义不同' : '仅排版或文件字节变化，语义未变',
        dst: item.t,
        dstPath: item.t,
        dstSub: !scopeResolved
          ? '暂不能判断保持有效'
          : item.direct ? '直接引用，需复核' : '间接连带，沿显式引用传播',
      })),
    };
    return {
      impactId: row.id,
      sourceKey: row.source_key,
      source: sourceName,
      sourceDocumentId,
      changeKind: row.change_kind,
      semanticChanged,
      scopeResolved,
      createdAt: row.created_at,
      affectedResources,
      directResources: analysis.directResources ?? [],
      directReferences: [...directPaths].sort(),
      intactPaths,
      view,
      display: {
        text: semanticChanged
          ? scopeResolved
            ? `影响分析完成：${intactPaths.length} 项保持有效，${todoItems.length} 项需要复核`
            : `《${sourceName}》是新增资料，尚不能判断现有内容保持有效；正在核对 ${todoItems.length} 项业务内容`
          : `《${sourceName}》语义未变化，${intactPaths.length} 项保持有效`,
      },
    };
  }

  private listPackageSources(workspace: WorkspaceRow) {
    const sources = listPackageMergeSources(this.db, workspace.project_id).map(packageSourceSummary);
    return {
      sources,
      display: { text: sources.length ? `已找到 ${sources.length} 个只读来源包` : '还没有导入来源包' },
    };
  }

  private async createPackageFusionPlan(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const output = object(args.output);
    const current = await this.artifacts.current(workspace.project_id);
    if (!current || current.revision.oid !== workspace.base_revision_oid) {
      throw new AgentCoreError(
        'FUSION_REVISION_CONFLICT',
        '当前知识包已经变化，请重新盘点来源后建立融合计划',
        ['重新调用 list_package_sources，再建立一次批量融合计划'],
        409,
      );
    }
    const version = String(output.version ?? '').trim() || nextValidationCandidateVersion({
      currentVersion: current.packageVersion,
      currentStatus: current.spec.status ?? 'draft',
    });
    return this.invokePackageFusion(() => this.packageFusion.createPlan(
      workspace.project_id,
      context.username,
      {
        expectedRevision: workspace.base_revision_oid,
        sourceIds: args.source_ids,
        sourceSelections: args.source_selections,
        output: { ...output, version },
        concepts: args.concepts,
        dispositions: args.dispositions,
      },
    ));
  }

  private async compilePackageFusionPlan(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const staged = this.changes(workspace).filter((change) => (
      change.group_id !== RUNTIME_GROUP && change.group_id !== LEGACY_RUNTIME_GROUP
    ));
    if (staged.length || workspace.candidate_id) {
      throw new AgentCoreError(
        'FUSION_WORKSPACE_BUSY',
        '当前还有一批待确认修改，不能同时生成多来源融合候选',
        ['先完成或退回当前确认，再继续批量融合'],
        409,
      );
    }
    const plan = await this.invokePackageFusion(() => this.packageFusion.compilePlan(
      workspace.project_id,
      text(args.plan_id, 'plan_id'),
      context.username,
    ));
    if (!plan.candidateId) return plan;
    const candidate = await this.artifacts.getCandidateRevision(workspace.project_id, plan.candidateId);
    const group = candidate.groups.find((item) => item.id === 'fusion-batch');
    if (!group) {
      throw new AgentCoreError(
        'FUSION_CANDIDATE_INVALID',
        '批量融合候选缺少原子变更组，不能进入确认',
        ['保留融合计划并重新生成候选'],
        409,
      );
    }
    const impact = group.changes.filter((change) => change.path.startsWith('package/'))
      .map((change) => publicPath(change.path));
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.prepare(`INSERT OR IGNORE INTO agent_candidate_group_metadata (
        candidate_id, group_id, tier, title, impact_json, warning
      ) VALUES (?,?,?,?,?,?)`).run(
        candidate.id,
        group.id,
        'caliber',
        `确认 ${plan.sources.length} 个来源知识包的一次性融合`,
        JSON.stringify(impact),
        null,
      );
      const attached = this.db.prepare(`UPDATE agent_candidate_workspaces
        SET candidate_id = ?, updated_at = ?
        WHERE project_id = ? AND run_id = ? AND candidate_id IS NULL`).run(
          candidate.id,
          nowIso(),
          workspace.project_id,
          workspace.run_id,
        );
      if (Number(attached.changes) !== 1) {
        throw new AgentCoreError(
          'FUSION_WORKSPACE_BUSY',
          '当前确认内容已经变化，批量融合候选未挂到本轮对话',
          ['刷新当前待确认内容后继续'],
          409,
        );
      }
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
    workspace.candidate_id = candidate.id;
    return {
      ...plan,
      candidateRevision: candidate.id,
      changeGroups: this.candidateGroups(candidate.id),
      display: { text: `已生成 ${plan.sources.length} 个来源知识包的一次性融合候选，等待整体确认` },
    };
  }

  private async invokePackageFusion<T>(task: () => T | Promise<T>): Promise<T> {
    try {
      return await task();
    } catch (error) {
      if (!(error instanceof PackageFusionServiceError)) throw error;
      const next = error.code === 'FUSION_DECISIONS_REQUIRED'
        ? ['根据未裁决清单逐项登记保留来源视图，再继续生成候选']
        : error.code === 'FUSION_QUALITY_FAILED'
          ? ['查看质量门禁明细，修正来源包或语义计划后重新融合']
          : ['根据错误中的当前事实修正融合计划后重试'];
      throw new AgentCoreError(error.code, error.message, next, error.statusCode, error.details);
    }
  }

  private async analyzePackageMerge(workspace: WorkspaceRow, args: Record<string, unknown>) {
    const source = this.requirePackageSource(workspace.project_id, text(args.source_id, 'source_id'));
    const current = await this.overlay(workspace);
    const analysis = analyzeMergePackage({
      currentFiles: current.files,
      currentSpec: current.spec,
      source,
      selectedScopes: stringArray(args.scopes),
    });
    return {
      ...analysis,
      display: {
        text: `融合范围已核对：选中 ${analysis.selectedPaths.length} 个文件，${analysis.conflicts.length} 项冲突`,
      },
    };
  }

  private readPackageSourceFile(workspace: WorkspaceRow, args: Record<string, unknown>) {
    const source = this.requirePackageSource(workspace.project_id, text(args.source_id, 'source_id'));
    const path = packagePath(text(args.path, 'path'));
    const content = source.files[path];
    if (content === undefined) {
      throw new AgentCoreError('FILE_NOT_FOUND', `来源包文件不存在：${path}`, ['先调用 analyze_package_merge 核对选中范围'], 404);
    }
    return {
      source: packageSourceSummary(source),
      path,
      content,
      provenance: `pkg:${source.name}@${source.version}`,
      display: { text: `正在读取来源包文件《${path}》` },
    };
  }

  private async stagePackageMerge(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const source = this.requirePackageSource(workspace.project_id, text(args.source_id, 'source_id'));
    const current = await this.overlay(workspace);
    const analysis = analyzeMergePackage({
      currentFiles: current.files,
      currentSpec: current.spec,
      source,
      selectedScopes: stringArray(args.scopes),
    });
    if (analysis.validation.brokenReferences.length) {
      throw new AgentCoreError(
        'MERGE_REFERENCE_BROKEN',
        analysis.validation.blocker,
        ['补勾选缺失条目所在范围，或从融合范围剔除引用它的条目'],
        409,
        { analysis },
      );
    }
    const resolutions = new Map(parseObjects(args.resolutions).map((item) => [String(item.conflict_id ?? ''), item]));
    const unresolved = analysis.conflicts.filter((conflict) => !resolutions.has(conflict.id));
    if (unresolved.length) {
      throw new AgentCoreError(
        'MERGE_CONFLICT_UNRESOLVED',
        `还有 ${unresolved.length} 项融合冲突没有裁决`,
        ['逐项说明保留当前、采用来源或重命名共存后重试'],
        409,
        { conflicts: unresolved },
      );
    }
    const actionBySourcePath = new Map<string, { action: string; targetPath?: string }>();
    for (const conflict of analysis.conflicts) {
      const resolution = resolutions.get(conflict.id)!;
      const action = String(resolution.action ?? '');
      if (!['keep_current', 'use_source', 'rename_source'].includes(action)) {
        throw new AgentCoreError('INVALID_INPUT', `冲突 ${conflict.id} 的处置不正确`, ['使用 keep_current、use_source 或 rename_source'], 400);
      }
      const targetPath = action === 'rename_source'
        ? packagePath(text(resolution.target_path, 'target_path'))
        : action === 'use_source' ? conflict.currentPath : undefined;
      const existing = actionBySourcePath.get(conflict.sourcePath);
      if (existing && (existing.action !== action || existing.targetPath !== targetPath)) {
        throw new AgentCoreError(
          'MERGE_RESOLUTION_CONFLICT',
          `来源文件 ${conflict.sourcePath} 收到了互相矛盾的处置`,
          ['对同一来源条目的 key、别名和路径冲突采用一致处置'],
          409,
        );
      }
      actionBySourcePath.set(conflict.sourcePath, { action, targetPath });
    }

    await this.assertCandidateStageGate(workspace, 'objects/__package_merge__.md');
    await this.discardFrozenCandidate(workspace, context, '融合范围或冲突裁决已变化，旧确认请求已失效');
    const provenance = `pkg:${source.name}@${source.version}`;
    const staged: Array<{ sourcePath: string; path: string; groupId: string }> = [];
    for (const sourcePath of analysis.selectedPaths) {
      const resolution = actionBySourcePath.get(sourcePath);
      if (resolution?.action === 'keep_current') continue;
      const targetPath = resolution?.targetPath ?? sourcePath;
      const content = withPackageProvenance(source.files[sourcePath], targetPath, provenance);
      if (current.files[targetPath] === content) continue;
      const tier = expectedTier(targetPath) ?? 'structural';
      const groupId = `merge-${tier}`;
      await this.upsertChange(workspace, {
        path: targetPath,
        kind: 'write',
        content,
        groupId,
        evidence: [provenance],
        reason: null,
      });
      staged.push({ sourcePath, path: targetPath, groupId });
    }
    if (!staged.length) {
      return {
        staged: [],
        change_groups: [],
        provenance,
        display: { text: '所选内容全部保留当前版本，没有生成候选修改' },
      };
    }
    const groups = [...new Set(staged.map((item) => item.groupId))].map((id) => {
      const tier = id.slice('merge-'.length) as Tier;
      return {
        id,
        tier,
        title: `融合${tierLabel(tier)}内容`,
        impact: staged.filter((item) => item.groupId === id).map((item) => item.path),
      };
    });
    return {
      staged,
      change_groups: groups,
      provenance,
      display: { text: `已生成融合候选：${staged.length} 个文件，来源均已标记` },
    };
  }

  private requirePackageSource(projectId: string, sourceId: string): PackageMergeSource {
    const source = getPackageMergeSource(this.db, projectId, sourceId);
    if (!source) {
      throw new AgentCoreError('PACKAGE_SOURCE_NOT_FOUND', '来源知识包不存在', ['先通过工作台导入来源包 ZIP'], 404);
    }
    return source;
  }

  private async diff(workspace: WorkspaceRow, args: Record<string, unknown>) {
    const revA = String(args.rev_a ?? '').trim() || workspace.base_revision_oid;
    const revB = String(args.rev_b ?? '').trim();
    let files: ArtifactFileDiff[];
    if (revB === 'candidate' || revB === workspace.candidate_id) {
      if (workspace.candidate_id) {
        files = (await this.artifacts.previewCandidateRevision(
          workspace.project_id, workspace.candidate_id,
        )).diff;
      } else {
        const base = await this.artifacts.readPackageFiles(workspace.project_id, workspace.base_revision_oid);
        files = candidateDiff(base.files, this.changes(workspace));
      }
    } else {
      if (!revB) throw new AgentCoreError('INVALID_INPUT', 'diff 需要 rev_b', ['传入具体修订号或 candidate'], 400);
      files = (await this.artifacts.diff(workspace.project_id, revA, revB)).files;
    }
    const scope = String(args.scope ?? '').trim();
    const filtered = files.filter((file) => (
      file.path !== 'model/package-spec.json'
      && (!scope || publicPath(file.path).startsWith(scope))
    ));
    return {
      from: revA,
      to: revB || 'candidate',
      files: filtered.map(plainDiff),
      display: { text: `正在对照 ${filtered.length} 个变更文件` },
    };
  }

  private async preflight(workspace: WorkspaceRow, context: AgentToolContext, args: Record<string, unknown>) {
    const requested = changeGroups(args.change_groups);
    this.db.prepare(`UPDATE agent_candidate_changes SET group_id = ?
      WHERE project_id = ? AND run_id = ? AND group_id = ?`)
      .run(RUNTIME_GROUP, workspace.project_id, workspace.run_id, LEGACY_RUNTIME_GROUP);
    const changes = await this.effectiveChanges(workspace);
    const businessIds = [...new Set(changes.map((item) => item.group_id).filter((id) => !isHiddenCandidateGroup(id)))];
    if (!businessIds.length && workspace.candidate_id) {
      const candidate = await this.artifacts.getCandidateRevision(workspace.project_id, workspace.candidate_id);
      const groups = this.candidateGroups(candidate.id).filter((group) => !isHiddenCandidateGroup(group.id));
      if (candidate.status !== 'pending' || !groups.length) {
        throw new AgentCoreError(
          'NO_CANDIDATE',
          '当前融合候选已经失效或缺少确认分组',
          ['重新生成批量融合候选后发起确认'],
          409,
        );
      }
      const requestedIds = new Set(requested.map((group) => group.id));
      const missing = groups.filter((group) => !requestedIds.has(group.id)).map((group) => group.id);
      const extra = requested.filter((group) => !groups.some((item) => item.id === group.id)).map((group) => group.id);
      if (missing.length || extra.length) {
        throw new AgentCoreError(
          'BROKEN_GROUP_SELECTION',
          '确认分组与批量融合候选不一致',
          ['使用 prepare_confirmation_groups 返回的完整分组重新发起确认'],
          409,
          { missing, extra, available_groups: groups },
        );
      }
      const focusPath = candidate.groups.flatMap((group) => group.changes)
        .find((change) => change.path.startsWith('package/'))?.path;
      const candidateChanges = candidate.groups.flatMap((group) => group.changes).map((change) => ({
        path: publicPath(change.path),
        kind: change.kind,
        content: change.kind === 'write' ? change.content : null,
      }));
      this.assertScriptCandidatesTested(workspace, candidateChanges);
      const materialReview = await this.assertFrozenCandidateValid(workspace, candidate);
      const validationReceipt = this.recordCandidateValidationReceipt(candidate);
      return {
        candidate_revision: candidate.id,
        validation_receipt: validationReceipt,
        change_groups: groups.map((group) => ({ ...group, autoApproved: false })),
        auto_approved: false,
        atomic_selection: candidate.groups.some((group) => group.id === COMPILED_RESOURCES_GROUP),
        ...materialPurposeReviewPayload(materialReview),
        ...(focusPath ? { focus: { kind: 'diff', target: publicPath(focusPath) } } : {}),
        display: { text: '多来源融合会整体替换当前知识包，需要确认后才会生效' },
      };
    }
    if (!businessIds.length) {
      throw new AgentCoreError(
        'NO_CANDIDATE',
        '没有需要专家确认的业务候选',
        ['仅有内部工作记录时直接结束当前复核'],
        409,
        { retryable: false },
      );
    }
    const requestedIds = new Set(requested.map((group) => group.id));
    const missing = businessIds.filter((id) => !requestedIds.has(id));
    const extra = requested.filter((group) => !businessIds.includes(group.id)).map((group) => group.id);
    if (missing.length || extra.length) {
      const availableGroups = businessIds.map((id) => ({
        id,
        paths: changes.filter((change) => change.group_id === id).map((change) => change.path),
      }));
      const regroupRequired = businessIds.length > 5;
      throw new AgentCoreError(
        'BROKEN_GROUP_SELECTION',
        regroupRequired
          ? `当前业务候选有 ${businessIds.length} 个分组，超过确认上限 5 组，必须先重组`
          : `确认分组与候选修改不一致${missing.length ? `；缺少 ${missing.join('、')}` : ''}${extra.length ? `；没有对应修改 ${extra.join('、')}` : ''}`,
        regroupRequired
          ? ['调用无需参数的 regroup_changes；成功后用返回的 change_groups 发起 request_confirmation']
          : ['按错误返回的 available_groups 完整 ID 重建本批 change_groups'],
        409,
        {
          missing,
          extra,
          available_groups: availableGroups,
          regroup_required: regroupRequired,
        },
      );
    }
    for (const group of requested) this.assertTier(group, changes.filter((item) => item.group_id === group.id));
    this.assertScriptCandidatesTested(workspace, changes);
    let materialReview: MaterialPurposeReviewValidation | undefined;
    if (changes.some((item) => !item.path.startsWith('.modeling/'))) {
      materialReview = await this.assertCandidateFormatValid(workspace, changes);
    }

    let candidate: PackageCandidateRevision;
    if (workspace.candidate_id) {
      candidate = await this.artifacts.getCandidateRevision(workspace.project_id, workspace.candidate_id);
    } else {
      const byId = new Map(requested.map((group) => [group.id, group]));
      if (changes.some((item) => item.group_id === RUNTIME_GROUP)) {
        byId.set(RUNTIME_GROUP, {
          id: RUNTIME_GROUP,
          tier: 'index',
          title: '同步建模计划与会话锚点',
          impact: [],
        });
      }
      if (changes.some((item) => item.group_id === COMPILED_RESOURCES_GROUP)) {
        byId.set(COMPILED_RESOURCES_GROUP, {
          id: COMPILED_RESOURCES_GROUP,
          tier: 'index',
          title: '同步确定性编译产物',
          impact: [],
        });
      }
      const nonIndexIds = [...byId.values()].filter((item) => item.tier !== 'index').map((item) => item.id);
      candidate = await this.artifacts.createCandidateRevision(workspace.project_id, {
        expectedRevision: workspace.base_revision_oid,
        groups: [...byId.values()].map((group) => ({
          id: group.id,
          summary: group.title,
          dependsOn: group.tier === 'index' ? nonIndexIds : [],
          changes: changes.filter((item) => item.group_id === group.id).map(toArtifactChange),
        })),
        action: {
          cause: 'agent_candidate_created',
          summary: auditSummary(args.summary, '整理待确认候选'),
          actorKind: 'agent',
          actorName: '建模助手',
        },
      });
      this.db.exec('BEGIN IMMEDIATE');
      try {
        const insert = this.db.prepare(`INSERT INTO agent_candidate_group_metadata (
          candidate_id, group_id, tier, title, impact_json, warning
        ) VALUES (?,?,?,?,?,?)`);
        for (const group of byId.values()) {
          insert.run(
            candidate.id, group.id, group.tier, group.title,
            JSON.stringify(group.impact ?? []), group.warning ?? null,
          );
        }
        this.db.prepare(`UPDATE agent_candidate_workspaces SET candidate_id = ?, updated_at = ?
          WHERE project_id = ? AND run_id = ?`)
          .run(candidate.id, nowIso(), workspace.project_id, workspace.run_id);
        this.db.exec('COMMIT');
      } catch (error) {
        if (this.db.isTransaction) this.db.exec('ROLLBACK');
        throw error;
      }
      workspace.candidate_id = candidate.id;
    }

    const publicGroups = this.candidateGroups(candidate.id).filter((group) => !isHiddenCandidateGroup(group.id));
    const askGroups = publicGroups.filter((group) => !this.autoPolicy(workspace.project_id, group.tier));
    const focusPath = candidate.groups
      .filter((group) => !isHiddenCandidateGroup(group.id))
      .flatMap((group) => group.changes)
      .map((change) => publicPath(change.path))[0];
    const validationReceipt = this.recordCandidateValidationReceipt(candidate);
    const atomicSelection = candidate.groups.some((group) => group.id === COMPILED_RESOURCES_GROUP);
    return {
      candidate_revision: candidate.id,
      validation_receipt: validationReceipt,
      change_groups: publicGroups.map((group) => ({ ...group, autoApproved: !askGroups.some((item) => item.id === group.id) })),
      auto_approved: askGroups.length === 0,
      atomic_selection: atomicSelection,
      ...materialPurposeReviewPayload(materialReview),
      ...(focusPath ? { focus: { kind: 'diff', target: focusPath } } : {}),
      display: { text: askGroups.length ? `有 ${askGroups.length} 组变更需要确认` : '候选变更符合项目自动确认策略' },
    };
  }

  private async assertCandidateFormatValid(
    workspace: WorkspaceRow,
    changes: ChangeRow[],
  ): Promise<MaterialPurposeReviewValidation> {
    const candidate = await this.overlay(workspace, changes);
    const base = await this.artifacts.readPackageFiles(workspace.project_id, workspace.base_revision_oid);
    const materialReview = this.assertMaterialPurposeReviews(
      workspace.project_id,
      base.spec,
      candidate.spec,
      {
        requireObjectMappings: candidateRequiresObjectMappings(changes.map((change) => change.path)),
        changedPaths: changes.map((change) => change.path),
      },
    );
    this.assertObjectSourcePolicy(workspace, candidate, changes.map((change) => change.path));
    this.assertSchemaSemantics(workspace, candidate);
    this.assertPackageFilesValid(base, candidate);
    return materialReview;
  }

  private async assertFrozenCandidateValid(
    workspace: WorkspaceRow,
    candidate: PackageCandidateRevision,
  ): Promise<MaterialPurposeReviewValidation> {
    const preview = await this.artifacts.previewCandidateRevision(workspace.project_id, candidate.id);
    const base = await this.artifacts.readPackageFiles(workspace.project_id, candidate.baseRevision);
    const candidatePaths = candidate.groups.flatMap((group) => (
      group.changes.map((change) => publicPath(change.path))
    ));
    const materialReview = this.assertMaterialPurposeReviews(
      workspace.project_id,
      base.spec,
      preview.spec,
      {
        requireObjectMappings: candidateRequiresObjectMappings(candidatePaths),
        changedPaths: candidatePaths,
      },
    );
    this.assertObjectSourcePolicy(
      workspace,
      preview,
      candidatePaths,
    );
    this.assertSchemaSemantics(workspace, preview);
    this.assertPackageFilesValid(base, preview);
    return materialReview;
  }

  private assertObjectSourcePolicy(
    workspace: WorkspaceRow,
    candidate: Pick<PackageArtifactFiles, 'spec' | 'files'>,
    changedPaths: string[],
  ): void {
    const plan = this.changeControl.activePlan(workspace.project_id, workspace.run_id);
    const touchesObjects = changedPaths.some((path) => (
      path === OBJECT_DATA_MAPPING_PATH
      || /^objects\//u.test(path)
    ));
    const physicalTables = schemaTables(this.tables(workspace.project_id));
    if (plan?.mode !== 'generation' && !(physicalTables.length && touchesObjects)) return;
    const issues = schemaObjectPolicyIssues(
      candidate.spec,
      publicKnowledgePackageFiles(candidate.files),
      this.tables(workspace.project_id),
    );
    if (!issues.length) return;
    throw new AgentCoreError(
      'CANDIDATE_OBJECT_SOURCE_POLICY_INVALID',
      `对象来源策略校验未通过：${issues.map((issue) => issue.message).join('；')}`,
      [physicalTables.length
        ? '重新执行 sync_schema_objects，禁止手工创建、合并或遗漏 Sheet 对象'
        : '移除物理来源和 mapping，只保留不绑定物理表的虚拟对象'],
      422,
      {
        retryable: false,
        object_source_mode: physicalTables.length ? 'schema' : 'virtual',
        findingCount: issues.length,
        findings: issues,

      },
    );
  }

  private schemaSemanticEnforcementEnabled(projectId: string): boolean {
    return Boolean(this.db.prepare(`SELECT 1
      FROM agent_change_plan_tasks task
      JOIN agent_change_plans plan ON plan.id = task.plan_id
      WHERE plan.project_id = ? AND plan.status <> 'cancelled'
        AND instr(task.allowed_tools_json, 'stage_schema_semantic_changes') > 0
        AND instr(task.allowed_tools_json, 'confirm_schema_semantics') > 0
      LIMIT 1`).get(projectId));
  }

  private schemaSemanticConfirmationMatches(projectId: string, status: SchemaSemanticStatus): boolean {
    if (!status.available) return true;
    if (!status.pass) return false;
    const rows = this.db.prepare(`SELECT response_json FROM agent_structured_mutation_receipts
      WHERE project_id = ? AND tool = 'confirm_schema_semantics'
      ORDER BY created_at DESC LIMIT 20`).all(projectId) as Array<{ response_json: string }>;
    return rows.some((row) => {
      try {
        const response = JSON.parse(row.response_json) as Record<string, any>;
        const confirmed = object(response.semantic_status);
        return String(confirmed.fingerprint ?? '') === status.fingerprint;
      } catch {
        return false;
      }
    });
  }

  private assertSchemaSemantics(
    workspace: Pick<WorkspaceRow, 'project_id'>,
    candidate: Pick<PackageArtifactFiles, 'spec' | 'files'>,
  ): void {
    if (!this.schemaSemanticEnforcementEnabled(workspace.project_id)) return;
    const status = analyzeSchemaSemantics(
      candidate.spec,
      this.tables(workspace.project_id),
    );
    if (this.schemaSemanticConfirmationMatches(workspace.project_id, status)) return;
    throw new AgentCoreError(
      'CANDIDATE_SCHEMA_SEMANTICS_INVALID',
      status.errorCount
        ? `Schema 对象业务语义校验未通过：还有 ${status.errorCount} 项阻断问题`
        : 'Schema 对象业务语义已变化，当前确认记录不再匹配',
      ['调用 get_schema_semantics 分页读取问题，补全或重新确认当前语义后再预检'],
      422,
      {
        retryable: false,
        ...schemaSemanticSummary(status),
        findingCount: status.errorCount,
        findings: status.issues.filter((item) => item.level === 'error').slice(0, 100),
        confirmation_current: false,
      },
    );
  }

  private assertMaterialPurposeReviews(
    projectId: string,
    baselineSpec: PackageSpec,
    candidateSpec: PackageSpec,
    options: { requireObjectMappings: boolean; changedPaths: string[] },
  ): MaterialPurposeReviewValidation {
    const baseline = findMaterialPurposeReviewProblems(this.db, projectId, baselineSpec);
    const candidate = findMaterialPurposeReviewProblems(this.db, projectId, candidateSpec);
    const baselineFingerprints = new Set(
      baseline.map((problem) => materialPurposeReviewProblemFingerprint(problem)),
    );
    const mappingScopeChanged = options.changedPaths.some((path) => (
      path === OBJECT_DATA_MAPPING_PATH
      || /^objects\//u.test(path)
    ));
    const pending = candidate.filter((problem) => (
      problem.code === 'MATERIAL_PURPOSE_REVIEW_REQUIRED'
    ));
    const introducedMapping = options.requireObjectMappings
      ? candidate.filter((problem) => (
        isMaterialMappingProblem(problem)
        && (!baselineFingerprints.has(materialPurposeReviewProblemFingerprint(problem)) || mappingScopeChanged)
      ))
      : [];
    const introduced = [...pending, ...introducedMapping.filter((problem) => !pending.includes(problem))];
    if (introduced.length) {
      const mappingProblems = introduced.filter(isMaterialMappingProblem);
      const affectedSheetCount = new Set(mappingProblems.flatMap((problem) => (
        problem.sheet ? [`${problem.documentId}\u0000${problem.sheet}`] : []
      ))).size;
      throw new AgentCoreError(
        pending.length ? 'MATERIAL_PURPOSE_REVIEW_REQUIRED' : 'SCHEMA_SHEET_MAPPING_INCOMPLETE',
        pending.length
          ? `建模资料用途尚未确认：${pending.map((problem) => problem.filename).join('、')}`
          : `数据 Schema 与对象映射不一致：还有 ${affectedSheetCount} 张 Sheet 的结构化对象来源尚未闭环`,
        pending.length
          ? ['等待用户在资料页确认文件用途和每张 Sheet 的处置后，再重新发起候选确认']
          : ['调用 get_schema_mapping_plan 对照完整台账，补齐本次涉及的对象来源后再重新发起确认；不要重复提交相同候选'],
        409,
        {
          problems: introduced,
          baselineProblems: baseline,
          baselineFindingCount: baseline.length,
          introducedFindingCount: introduced.length,
          requiresUserConfirmation: pending.length > 0,
          retryable: false,
          validatorVersion: 'material-source-mapping-v3',
          findingCount: introduced.length,
          findings: introduced.map((problem) => ({
            ruleId: problem.code,
            path: 'mappings/object-data-mappings.json',
            message: problem.message,
            suggestion: problem.code === 'MATERIAL_PURPOSE_REVIEW_REQUIRED'
              ? '等待用户完成资料用途确认'
              : '通过对象领域工具补齐本次涉及 Sheet 的结构化来源绑定',
          })),
        },
      );
    }
    return { baseline, candidate, introduced };
  }

  private assertPackageFilesValid(
    base: Pick<PackageArtifactFiles, 'spec' | 'files'>,
    candidate: Pick<PackageArtifactFiles, 'spec' | 'files'>,
  ) {
    const baseFiles = publicKnowledgePackageFiles(base.files);
    const candidateFiles = publicKnowledgePackageFiles(candidate.files);
    const baselineErrors = runFormatChecks(base.spec, baseFiles)
      .filter((finding) => finding.severity === '错误');
    const baselineIdentities = new Set(baselineErrors.map(formatFindingIdentity));
    let candidateSpec = candidate.spec;
    try {
      candidateSpec = {
        ...parseRenderedPackageFiles(candidateFiles),
        dataSourceType: candidate.spec.dataSourceType,
      };
    } catch { /* fallback to current spec */ }
    const introduced = runFormatChecks(candidateSpec, candidateFiles)
      .filter((finding) => finding.severity === '错误')
      .filter((finding) => (
        (finding.category === '必选文件缺失' && PACKAGE_REQUIRED_FILE_SET.has(finding.target))
        || !baselineIdentities.has(formatFindingIdentity(finding))
      ));
    if (introduced.length) {
      const findings = introduced.map(agentValidationFinding);
      throw new AgentCoreError(
        'CANDIDATE_FORMAT_INVALID',
        `候选格式校验未通过：${findings.map((finding) => `${finding.path}：${finding.message}`).join('；')}`,
        ['按 findings 逐项修正对应候选文件，然后重新发起确认；不要修改无关文件'],
        409,
        {
          retryable: true,
          validatorVersion: PACKAGE_FORMAT_VALIDATOR_VERSION,
          findingCount: findings.length,
          findings,
        },
      );
    }

    const baselineKnowledgeIssues = new Set(validateKnowledgeMarkdownContract(baseFiles));
    const introducedKnowledgeIssues = validateKnowledgeMarkdownContract(candidateFiles)
      .filter((issue) => !baselineKnowledgeIssues.has(issue));
    if (!introducedKnowledgeIssues.length) return;
    const findings = introducedKnowledgeIssues.map(knowledgeValidationFinding);
    throw new AgentCoreError(
      'KNOWLEDGE_CONTRACT_INVALID',
      `知识候选新增了 consumer 无法解析的问题：${introducedKnowledgeIssues.slice(0, 8).join('；')}${introducedKnowledgeIssues.length > 8 ? `；另有 ${introducedKnowledgeIssues.length - 8} 项` : ''}`,
      ['按 findings 逐项修正本次实际受影响的文件；不要为导入基线的历史格式问题重写无关文件'],
      409,
      {
        retryable: true,
        validatorVersion: KNOWLEDGE_MARKDOWN_VALIDATOR_VERSION,
        findingCount: findings.length,
        findings,
        issues: introducedKnowledgeIssues,
        baselineIssueCount: baselineKnowledgeIssues.size,
      },
    );
  }

  private candidateValidationHash(candidateId: string): { baseRevision: string; candidateHash: string } {
    const row = this.db.prepare(`SELECT base_revision_oid, groups_json
      FROM artifact_candidate_revisions WHERE id = ?`).get(candidateId) as
      | { base_revision_oid: string; groups_json: string }
      | undefined;
    if (!row) {
      throw new AgentCoreError('NO_CANDIDATE', '确认请求对应的候选已经失效', ['重新生成候选并发起确认'], 409);
    }
    const baseRevision = String(row.base_revision_oid);
    const candidateHash = `sha256:${createHash('sha256')
      .update('agent-candidate-validation-v1\0')
      .update(baseRevision)
      .update('\0')
      .update(String(row.groups_json))
      .digest('hex')}`;
    return { baseRevision, candidateHash };
  }

  private recordCandidateValidationReceipt(candidate: PackageCandidateRevision): CandidateValidationReceipt {
    const { baseRevision, candidateHash } = this.candidateValidationHash(candidate.id);
    const validatedAt = nowIso();
    this.db.prepare(`INSERT INTO agent_candidate_validation_receipts (
      candidate_id, project_id, base_revision_oid, candidate_hash, validator_version, validated_at
    ) VALUES (?,?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET
      project_id = excluded.project_id,
      base_revision_oid = excluded.base_revision_oid,
      candidate_hash = excluded.candidate_hash,
      validator_version = excluded.validator_version,
      validated_at = excluded.validated_at`).run(
      candidate.id,
      candidate.projectId,
      baseRevision,
      candidateHash,
      PACKAGE_CONTRACT_VERSION,
      validatedAt,
    );
    return {
      candidateRevision: candidate.id,
      baseRevision,
      candidateHash,
      validatorVersion: PACKAGE_CONTRACT_VERSION,
      validatedAt,
    };
  }

  private assertCandidateValidationReceipt(
    workspace: WorkspaceRow,
    candidate: PackageCandidateRevision,
    args: Record<string, unknown>,
  ) {
    const receipt = this.db.prepare(`SELECT base_revision_oid, candidate_hash, validator_version, validated_at
      FROM agent_candidate_validation_receipts WHERE candidate_id = ? AND project_id = ?`).get(
      candidate.id,
      workspace.project_id,
    ) as { base_revision_oid: string; candidate_hash: string; validator_version: string; validated_at: string } | undefined;
    const current = this.candidateValidationHash(candidate.id);
    const expectedCandidate = String(args.expected_candidate_revision ?? '').trim();
    const expectedHash = String(args.expected_candidate_hash ?? '').trim();
    const expectedValidator = String(args.expected_validator_version ?? '').trim();
    const valid = Boolean(
      receipt
      && candidate.status === 'pending'
      && candidate.baseRevision === workspace.base_revision_oid
      && receipt.base_revision_oid === current.baseRevision
      && receipt.candidate_hash === current.candidateHash
      && receipt.validator_version === PACKAGE_CONTRACT_VERSION
      && expectedCandidate === candidate.id
      && expectedHash === current.candidateHash
      && expectedValidator === PACKAGE_CONTRACT_VERSION
    );
    if (valid) return;
    throw new AgentCoreError(
      'CANDIDATE_REVALIDATION_REQUIRED',
      '待确认候选或校验规则已经变化，原确认不能继续提交',
      ['重新执行完整候选预检，并基于新的确认卡再次决定'],
      409,
      {
        retryable: false,
        expectedCandidateRevision: expectedCandidate || null,
        currentCandidateRevision: candidate.id,
        expectedCandidateHash: expectedHash || null,
        currentCandidateHash: current.candidateHash,
        expectedValidatorVersion: expectedValidator || null,
        currentValidatorVersion: PACKAGE_CONTRACT_VERSION,
        receipt: receipt ? {
          baseRevision: receipt.base_revision_oid,
          candidateHash: receipt.candidate_hash,
          validatorVersion: receipt.validator_version,
          validatedAt: receipt.validated_at,
        } : null,
      },
    );
  }

  private async confirm(workspace: WorkspaceRow, context: AgentToolContext, args: Record<string, unknown>) {
    this.assertExpectedCandidate(workspace, args);
    const review = object(args.review);
    if (review.approved !== true) {
      throw new AgentCoreError('CONFIRMATION_REQUIRED', '待修改内容尚未获得批准', ['等待专家确认后再提交']);
    }
    if (!workspace.candidate_id) {
      throw new AgentCoreError('NO_CANDIDATE', '确认请求对应的候选已经失效', ['根据当前修改重新发起 request_confirmation']);
    }
    const groups = this.candidateGroups(workspace.candidate_id);
    const requested = stringArray(review.selected_groups);
    const automatic = groups.filter((group) => this.autoPolicy(workspace.project_id, group.tier)).map((group) => group.id);
    const publicGroupIds = groups.filter((group) => !isHiddenCandidateGroup(group.id)).map((group) => group.id);
    const requestedBusinessGroups = new Set([
      ...(requested.length ? requested : publicGroupIds),
      ...automatic,
    ]);
    if (groups.some((group) => group.id === COMPILED_RESOURCES_GROUP)) {
      const missing = publicGroupIds.filter((id) => !requestedBusinessGroups.has(id));
      if (missing.length) {
        throw new AgentCoreError(
          'DOMAIN_CANDIDATE_PARTIAL_SELECTION_FORBIDDEN',
          '领域工具生成的语义规格和派生文件必须与本批业务修改一起确认',
          ['接受全部业务组，或退回整批候选后按需要重新生成'],
          409,
          {
            retryable: false,
            atomicSelection: true,
            missing,
            requiredGroups: publicGroupIds,
          },
        );
      }
    }
    const selected = [...new Set([
      ...(requested.length ? requested : groups.map((group) => group.id)),
      ...automatic,
      RUNTIME_GROUP,
      COMPILED_RESOURCES_GROUP,
    ])]
      .filter((id) => groups.some((group) => group.id === id));
    const reviewMode = normalizeReviewMode(review.review_mode, requested.length === 0 && automatic.length === groups.length);
    const selectedIds = new Set(selected);
    const candidate = await this.artifacts.getCandidateRevision(workspace.project_id, workspace.candidate_id);
    this.assertCandidateValidationReceipt(workspace, candidate, args);
    const selectedPreview = await this.artifacts.previewCandidateRevision(
      workspace.project_id,
      candidate.id,
      selected,
    );
    const base = await this.artifacts.readPackageFiles(workspace.project_id, candidate.baseRevision);
    this.assertPackageFilesValid(base, selectedPreview);
    const selectedChanges = candidate.groups
      .filter((group) => selectedIds.has(group.id))
      .flatMap((group) => group.changes)
      .map((change) => ({
        path: publicPath(change.path),
        kind: change.kind,
        content: change.kind === 'write' ? change.content : null,
      }));
    this.assertScriptCandidatesTested(workspace, selectedChanges);
    const accepted = await this.artifacts.acceptCandidateRevision(workspace.project_id, {
      candidateId: workspace.candidate_id,
      selectedGroupIds: selected,
      packageDataSourceType: normalizePackageDataSourceSelection(workspace.package_data_source_type),
      note: auditNote(review.feedback),
      action: {
        cause: 'agent_candidate_accepted',
        summary: '确认 Agent 提出的修改',
        actorKind: 'user',
        actorName: context.username,
      },
    });
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.prepare(`INSERT INTO agent_confirmations (
        id, project_id, candidate_id, revision_oid, actor_username, review_mode,
        selected_groups_json, sampled_groups_json, created_at
      ) VALUES (?,?,?,?,?,?,?,?,?)`)
        .run(
          `ac_${randomUUID().slice(0, 12)}`,
          workspace.project_id,
          workspace.candidate_id,
          accepted.revision.oid,
          context.username,
          reviewMode,
          JSON.stringify(accepted.selection.selectedGroupIds),
          JSON.stringify(stringArray(review.sampled_groups)),
          nowIso(),
        );
      this.db.prepare('DELETE FROM agent_candidate_changes WHERE project_id = ? AND run_id = ?')
        .run(workspace.project_id, workspace.run_id);
      this.db.prepare(`UPDATE agent_candidate_workspaces SET base_revision_oid = ?, candidate_id = NULL,
        updated_at = ? WHERE project_id = ? AND run_id = ?`)
        .run(accepted.revision.oid, nowIso(), workspace.project_id, workspace.run_id);
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
    this.changeControl.settlePlan(
      workspace.project_id,
      context.runId,
      'accepted',
      accepted.candidate.id,
    );
    return {
      approved: true,
      candidateRevision: accepted.candidate.id,
      revision: accepted.revision.oid,
      selectedGroups: accepted.selection.selectedGroupIds,
      reviewMode,
      display: { text: `已确认 ${accepted.selection.selectedGroupIds.length} 组修改` },
    };
  }

  private async rejectConfirmation(workspace: WorkspaceRow, context: AgentToolContext, args: Record<string, unknown>) {
    this.assertExpectedCandidate(workspace, args);
    if (!workspace.candidate_id) return { rejected: true, replayed: true, display: { text: '候选确认已退回' } };
    const candidateId = workspace.candidate_id;
    const feedback = auditNote(object(args.review).feedback, '专家退回了这批候选');
    await this.artifacts.rejectCandidateRevision(workspace.project_id, {
      candidateId,
      note: feedback,
      action: {
        cause: 'agent_candidate_rejected',
        summary: '退回 Agent 提出的修改',
        actorKind: 'user',
        actorName: context.username,
      },
    });
    this.db.prepare(`UPDATE agent_candidate_workspaces SET candidate_id = NULL, updated_at = ?
      WHERE project_id = ? AND run_id = ?`).run(nowIso(), workspace.project_id, workspace.run_id);
    workspace.candidate_id = null;
    this.changeControl.settlePlan(workspace.project_id, context.runId, 'rejected', candidateId);
    return {
      rejected: true,
      candidateRevision: candidateId,
      feedback,
      display: { text: '候选确认已退回，可继续调整' },
    };
  }

  private discardRejectedCandidateChanges(workspace: WorkspaceRow) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const current = this.db.prepare(`SELECT candidate_id FROM agent_candidate_workspaces
        WHERE project_id = ? AND run_id = ?`).get(workspace.project_id, workspace.run_id) as any;
      if (current?.candidate_id) {
        throw new AgentCoreError(
          'CANDIDATE_DECISION_REQUIRED',
          '当前还有一批修改等待你的决定，助手不能直接撤销',
          ['请先在确认卡中选择“退回调整”；退回后助手才能按你的反馈放弃这些修改'],
          409,
        );
      }
      const rows = this.db.prepare(`SELECT path FROM agent_candidate_changes
        WHERE project_id = ? AND run_id = ? AND group_id NOT IN (?, ?)
        ORDER BY path`).all(
        workspace.project_id,
        workspace.run_id,
        RUNTIME_GROUP,
        LEGACY_RUNTIME_GROUP,
      ) as Array<{ path: string }>;
      const paths = rows.map((row) => row.path);
      if (paths.length) {
        this.db.prepare(`DELETE FROM agent_candidate_changes
          WHERE project_id = ? AND run_id = ? AND group_id NOT IN (?, ?)`).run(
          workspace.project_id,
          workspace.run_id,
          RUNTIME_GROUP,
          LEGACY_RUNTIME_GROUP,
        );
        this.db.prepare(`UPDATE agent_candidate_workspaces SET updated_at = ?
          WHERE project_id = ? AND run_id = ?`).run(
          nowIso(),
          workspace.project_id,
          workspace.run_id,
        );
      }
      this.db.exec('COMMIT');
      return {
        discarded: paths.length > 0,
        idempotent: paths.length === 0,
        discardedCount: paths.length,
        discardedPaths: paths,
        display: {
          text: paths.length
            ? `已放弃 ${paths.length} 项退回修改，当前已确认版本保持不变`
            : '没有需要放弃的退回修改，当前已确认版本保持不变',
        },
      };
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
  }

  private async discardFrozenCandidate(workspace: WorkspaceRow, context: AgentToolContext, reason: string) {
    if (!workspace.candidate_id) return;
    const candidate = await this.artifacts.getCandidateRevision(workspace.project_id, workspace.candidate_id);
    if (candidate.status === 'pending') {
      await this.artifacts.rejectCandidateRevision(workspace.project_id, {
        candidateId: candidate.id,
        note: reason,
        action: {
          cause: 'agent_candidate_superseded',
          summary: '关闭已变化的 Agent 候选',
          actorKind: 'system',
          actorName: context.username,
        },
      });
    }
    this.db.prepare(`UPDATE agent_candidate_workspaces SET candidate_id = NULL, updated_at = ?
      WHERE project_id = ? AND run_id = ?`).run(nowIso(), workspace.project_id, workspace.run_id);
    workspace.candidate_id = null;
  }

  private assertExpectedCandidate(workspace: WorkspaceRow, args: Record<string, unknown>) {
    const expected = String(args.expected_candidate_revision ?? '').trim();
    if (expected && workspace.candidate_id !== expected) {
      throw new AgentCoreError(
        'STALE_CONFIRMATION',
        '待确认内容已经变化，不能把旧卡片的决定用于新候选',
        ['回到消息流查看当前待确认内容后重新决定'],
        409,
        { expected_candidate_revision: expected, current_candidate_revision: workspace.candidate_id },
      );
    }
  }

  private candidateGroups(candidateId: string): Array<ChangeGroupInput> {
    return (this.db.prepare(`SELECT group_id, tier, title, impact_json, warning
      FROM agent_candidate_group_metadata WHERE candidate_id = ? ORDER BY rowid`).all(candidateId) as any[])
      .map((row) => ({
        id: row.group_id,
        tier: row.tier,
        title: row.title,
        impact: parseArray(row.impact_json),
        ...(row.warning ? { warning: row.warning } : {}),
      }));
  }

  private autoPolicy(projectId: string, tier: Tier): boolean {
    if (tier === 'caliber' || tier === 'script') return false;
    if (tier === 'index') return true;
    const row = this.db.prepare(`SELECT mode FROM agent_confirm_policy
      WHERE project_id = ? AND tier = ?`).get(projectId, tier) as { mode?: string } | undefined;
    return row?.mode === 'auto';
  }

  private assertTier(group: ChangeGroupInput, changes: ChangeRow[]) {
    const studioAtomicGroup = group.id.startsWith('studio-')
      && changes.length > 0
      && changes.every((change) => change.reason === '由知识包 Studio 形成的待确认修改');
    if (studioAtomicGroup) {
      const expected: Tier = changes.some((change) => expectedTier(change.path) === 'caliber')
        ? 'caliber'
        : 'structural';
      if (group.tier !== expected) {
        throw new AgentCoreError(
          'TIER_MISMATCH',
          `Studio 原子变更组“${group.title}”应标为 ${expected}，不能标为 ${group.tier}`,
          [`保持整批不拆分，并按 ${expected} 层级重新确认`],
        );
      }
      return;
    }
    for (const change of changes) {
      const expected = expectedTier(change.path);
      if (expected && expected !== group.tier) {
        throw new AgentCoreError(
          'TIER_MISMATCH',
          `变更组“${group.title}”标为 ${group.tier}，但 ${change.path} 属于 ${expected}`,
          [`把 ${change.path} 放入 ${expected} 变更组后重新确认`],
        );
      }
    }
  }

  private async gateStatus(workspace: WorkspaceRow, args: Record<string, unknown>) {
    const gate = text(args.gate, 'gate');
    const gates = await this.computeGates(workspace);
    const result = gates[gate];
    if (!result) {
      throw new AgentCoreError('GATE_NOT_FOUND', `未知门禁：${gate}`, [`可用门禁：${Object.keys(gates).join('、')}`], 400);
    }
    const gateLabel = GATE_LABELS[gate] || '当前条件';
    return { gate, ...result, display: { text: result.pass ? `“${gateLabel}”已通过` : `“${gateLabel}”还有 ${result.blockers.length} 项未完成` } };
  }

  /** 一次算出全部门禁。谓词之间共享同一份候选叠加和确认记录，分开算既慢又可能互相矛盾。 */
  private async computeGates(workspace: WorkspaceRow): Promise<GateResults> {
    const documents = listDocuments(workspace.project_id);
    const parsed = documents.filter((item) => item.parseStatus === 'parsed').length;
    const failed = documents.filter((item) => item.parseStatus === 'failed');
    const confirmedPaths = this.confirmedPaths(workspace.project_id);
    const workspaceChanges = this.changes(workspace);
    const { files, spec: storedWorkingSpec } = await this.overlay(workspace, workspaceChanges);
    const workingSpec = resolveAgentStudioSpec(storedWorkingSpec, files, workspaceChanges);
    const base = await this.artifacts.readPackageFiles(workspace.project_id, workspace.base_revision_oid);
    const pendingPaths = new Set(workspaceChanges.map((change) => change.path));
    const diagramPaths = Object.keys(files).filter((path) => path.startsWith('diagrams/') && path.endsWith('.mmd'));
    const knowledgePaths = Object.keys(files).filter((path) => /^(objects|operations|rules|metrics)\/.+\.md$/.test(path)
      && !path.endsWith('/index.md'));
    const trustedProcessBaseline = Object.keys(base.files).some((path) => (
      /^(objects|operations|rules|metrics|scripts)\/.+/u.test(path) && !path.endsWith('/index.md')
    ));
    const importedPackageBaseline = getProject(workspace.project_id)?.kind === 'package'
      && trustedProcessBaseline;
    const trustedGoalBaseline = trustedProcessBaseline || Object.keys(base.files)
      .some((path) => path.startsWith('diagrams/') && path.endsWith('.mmd'));
    const goalConfirmed = !pendingPaths.has('.modeling/goal.md')
      && (confirmedPaths.has('.modeling/goal.md') || trustedGoalBaseline);
    const processConfirmed = ![...pendingPaths].some((path) => path.startsWith('diagrams/'))
      && (trustedProcessBaseline || (goalConfirmed && diagramPaths.length > 0
        && diagramPaths.every((path) => confirmedPaths.has(path))));
    const knowledgeFilesConfirmed = ![...pendingPaths].some((path) => /^(objects|operations|rules|metrics)\//u.test(path))
      && (trustedProcessBaseline || (processConfirmed && knowledgePaths.length > 0
        && knowledgePaths.every((path) => confirmedPaths.has(path))));
    const schemaSemantics = analyzeSchemaSemantics(
      workingSpec,
      this.tables(workspace.project_id),
    );
    const schemaSemanticsEnforced = this.schemaSemanticEnforcementEnabled(workspace.project_id);
    const schemaSemanticsConfirmed = !schemaSemanticsEnforced
      || this.schemaSemanticConfirmationMatches(workspace.project_id, schemaSemantics);
    const knowledgeConfirmed = knowledgeFilesConfirmed
      && schemaSemanticsConfirmed;
    const pendingBusiness = workspaceChanges.filter((item) => (
      item.group_id !== RUNTIME_GROUP && item.group_id !== LEGACY_RUNTIME_GROUP
    )).length;
    const pendingCandidates = Number((this.db.prepare(`SELECT COUNT(*) AS count FROM artifact_candidate_revisions
      WHERE project_id = ? AND status = 'pending'`).get(workspace.project_id) as any)?.count ?? 0);
    const deterministic = this.deterministicCapabilityState(
      workspace.project_id,
      workingSpec,
    );
    const productLedger = this.getProductLedger(workspace);
    const productLedgerValidation = productLedger.validation;
    const productLedgerEnforced = config.productLedger.mode === 'enforce'
      && productLedger.required === true;
    const manualCases = listTestcases(workspace.project_id).filter((item) => item.source === 'manual');
    const { latestRound: latestEval, results: latestResults } = latestEffectiveEvaluation(
      workspace.project_id,
      workspace.base_revision_oid,
      manualCases.map((item) => item.id),
    );
    const resultByCase = new Map(latestResults.map((result) => [result.testcaseId, result]));
    const evaluationMatchesCurrentFacts = manualCases.length > 0
      && latestEval?.status === 'done'
      && latestEval?.artifactRevision === workspace.base_revision_oid
      && latestResults.length === manualCases.length
      && manualCases.every((testcase) => {
        const result = resultByCase.get(testcase.id);
        return result?.question === testcase.question
          && result?.groundTruth === testcase.groundTruth
          && result?.verdict === 'pass';
      });
    const releasedContent = await this.equivalentReleasedRevision(
      workspace.project_id,
      workspace.base_revision_oid,
    );

    const gates: GateResults = {
      materials_ready: {
        // 单份扫描件或损坏文件应即时告警，但不能让其他可读资料的建模永久停摆。
        pass: parsed > 0 || importedPackageBaseline,
        blockers: [
          ...(parsed || importedPackageBaseline ? [] : ['至少上传并成功解析 1 份资料']),
        ],
        warnings: failed.map((item) => `资料无法解析：${item.filename}`),
      },
      goal_confirmed: {
        pass: goalConfirmed,
        blockers: goalConfirmed ? [] : ['确认当前建模目标'],
      },
      process_confirmed: {
        pass: processConfirmed,
        blockers: processConfirmed ? [] : diagramPaths.length
          ? diagramPaths.filter((path) => !confirmedPaths.has(path) || pendingPaths.has(path))
            .map((path) => `确认流程 ${path}`)
          : ['形成并确认至少 1 条业务流程'],
      },
      knowledge_confirmed: {
        pass: knowledgeConfirmed,
        blockers: [
          ...(knowledgeFilesConfirmed ? [] : knowledgePaths.length
            ? knowledgePaths.filter((path) => !confirmedPaths.has(path) || pendingPaths.has(path))
              .map((path) => `确认知识条目 ${path}`)
            : ['形成并确认对象、操作或规则知识']),
          ...(schemaSemanticsConfirmed ? [] : [
            `补全并确认 Schema 对象业务语义（${schemaSemantics.errorCount} 项阻断问题）`,
          ]),
        ],
      },
      modeling_complete: {
        pass: pendingBusiness === 0 && pendingCandidates === 0,
        blockers: [
          ...(pendingBusiness ? [`还有 ${pendingBusiness} 个候选文件未确认`] : []),
          ...(pendingCandidates ? [`还有 ${pendingCandidates} 个确认请求待处理`] : []),
        ],
      },
      deterministic_confirmed: {
        pass: !['decision_required', 'implementation_required'].includes(deterministic.status),
        blockers: deterministic.status === 'decision_required'
          ? ['确认当前确定性计算候选是否生成或继续使用脚本']
          : [
            ...deterministic.missing_script_bindings
              .map((key) => `完成已确认业务操作“${key}”的脚本契约、实现和绑定`),
            ...deterministic.missing_script_tests
              .map((key) => `让业务操作“${key}”的当前脚本通过已确认契约的隔离测试`),
          ],
        warnings: deterministic.skipped_operation_keys.length
          ? [`专家已明确跳过 ${deterministic.skipped_operation_keys.length} 个确定性计算候选`]
          : [],
      },
      evaluation_passed: {
        pass: evaluationMatchesCurrentFacts,
        blockers: evaluationMatchesCurrentFacts ? [] : [
          '确保当前知识包的每条真实用例都有最新有效结果，且问题、参考答案和结果保持一致并通过',
        ],
      },
    };
    // 只有明确提交过产品大清单台账时才把它显示为独立门禁；普通对象/规则
    // 建模不会因为新增的观测能力多出一条视觉闸位。
    if (productLedger.available === true) {
      gates.product_ledger_verified = {
        pass: !productLedgerEnforced || (
          productLedger.sourceRevision === workspace.base_revision_oid
          && productLedgerValidation?.pass === true
        ),
        blockers: !productLedgerEnforced
          ? []
          : productLedger.sourceRevision !== workspace.base_revision_oid
            ? ['台账对应的资料修订已变化，请重新核对当前清单']
            : (productLedgerValidation?.errors ?? []).slice(0, 8),
        warnings: config.productLedger.mode === 'advisory' && productLedgerValidation?.pass !== true
          ? ['产品清单台账目前处于观测阶段，当前问题不会阻断普通建模']
          : [],
      };
    }
    const modelingReady = combine(gates, [
      'materials_ready',
      'goal_confirmed',
      'process_confirmed',
      'knowledge_confirmed',
      ...(productLedger.available === true ? ['product_ledger_verified'] : []),
      'modeling_complete',
      'deterministic_confirmed',
    ]);
    const dataSource = getProjectDataSource(workspace.project_id);
    const runningImport = getRunningEvaluationDatabaseImportRun(workspace.project_id);
    const readyForEval = {
      pass: modelingReady.pass && manualCases.length > 0 && Boolean(dataSource) && !runningImport,
      blockers: [
        ...modelingReady.blockers,
        ...(manualCases.length ? [] : ['先在评测界面录入至少 1 条用户真实场景用例，Agent 不能从知识包生成']),
        ...(dataSource ? [] : ['先在评测界面完成数据源配置；判断类问题可显式选择“无数据源”']),
        ...(runningImport ? [`评测库数据仍在后台导入：${runningImport.filename}`] : []),
      ],
    };
    gates.ready_for_eval = readyForEval;
    gates.releasable = {
      pass: Boolean(releasedContent) && pendingBusiness === 0 && pendingCandidates === 0,
      blockers: releasedContent && pendingBusiness === 0 && pendingCandidates === 0
        ? []
        : ['由项目负责人确认真实用例覆盖和稳定版本号，并完成正式发布'],
    };
    return gates;
  }

  private confirmedPaths(projectId: string): Set<string> {
    const rows = this.db.prepare(`SELECT c.groups_json, a.selected_groups_json
      FROM agent_confirmations a JOIN artifact_candidate_revisions c ON c.id = a.candidate_id
      WHERE a.project_id = ? ORDER BY a.created_at`).all(projectId) as any[];
    const paths = new Set<string>();
    for (const row of rows) {
      const selected = new Set(parseArray(row.selected_groups_json));
      const groups = parseObjects(row.groups_json);
      for (const group of groups) {
        if (!selected.has(String(group.id))) continue;
        for (const change of Array.isArray(group.changes) ? group.changes : []) {
          if (change && typeof change === 'object' && 'path' in change) paths.add(publicPath(String(change.path)));
        }
      }
    }
    return paths;
  }

  private evaluationStatus(projectId: string) {
    const ready = this.db.prepare(`SELECT id, status, progress_done, progress_total, pass_count,
        created_at, finished_at FROM eval_rounds
      WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1`).get(projectId) as any;
    if (!ready) return this.unavailable(
      'EVALUATION_NOT_STARTED',
      '尚未创建可运行的评测轮次，未伪造评测结果',
      ['先在评测中心配置数据源和用例，再调用 run_evaluation'],
    );
    const status = String(ready.status);
    const terminal = ['done', 'failed', 'cancelled'].includes(status);
    return { available: true, roundId: ready.id, status, terminal,
      done: Number(ready.progress_done || 0), total: Number(ready.progress_total || 0),
      passed: Number(ready.pass_count || 0), createdAt: ready.created_at,
      finishedAt: ready.finished_at || null,
      scope: getRound(String(ready.id))?.scope ?? { kind: 'current' },
      display: { text: status === 'running' ? '评测正在后台运行' : '最近一轮评测已经结束' } };
  }

  private async cancelEvaluation(projectId: string, args: Record<string, unknown> = {}) {
    const requestedRoundId = typeof args.round_id === 'string' ? args.round_id.trim() : '';
    const latest = this.evaluationStatus(projectId);
    if (requestedRoundId) {
      const requested = this.db.prepare(`SELECT id, status, progress_done, progress_total, pass_count,
          created_at, finished_at FROM eval_rounds WHERE project_id = ? AND id = ?`)
        .get(projectId, requestedRoundId) as any;
      if (!requested) {
        throw new AgentCoreError(
          'EVALUATION_NOT_FOUND',
          '指定评测轮次不存在',
          ['重新读取项目状态后，使用其中最近一轮评测编号'],
          404,
        );
      }
      if (requested.status !== 'running') {
        return {
          cancelled: false,
          idempotent: true,
          roundId: requested.id,
          status: requested.status,
          terminal: true,
          done: Number(requested.progress_done || 0),
          total: Number(requested.progress_total || 0),
          passed: Number(requested.pass_count || 0),
          finishedAt: requested.finished_at || null,
          display: { text: '指定评测已经结束，无需重复停止' },
        };
      }
    }
    if (!isEvaluationActive(projectId)) {
      return {
        cancelled: false,
        idempotent: true,
        ...(!('error' in latest) ? latest : {}),
        display: { text: '当前没有运行中的评测' },
      };
    }
    const round = await cancelEvaluation(projectId, requestedRoundId || undefined);
    return {
      cancelled: true,
      roundId: round.id,
      status: round.status,
      terminal: round.status !== 'running',
      done: round.progressDone,
      total: round.progressTotal,
      passed: round.passCount,
      finishedAt: round.finishedAt ?? null,
      display: { text: '当前评测已停止' },
    };
  }

  private evaluationResults(projectId: string, args: Record<string, unknown>) {
    const requestedRoundId = typeof args.round_id === 'string' ? args.round_id.trim() : '';
    const round = requestedRoundId
      ? this.db.prepare(`SELECT id, status, progress_done, progress_total, pass_count, artifact_revision_oid,
          created_at, finished_at FROM eval_rounds WHERE project_id = ? AND id = ?`).get(projectId, requestedRoundId) as any
      : this.db.prepare(`SELECT id, status, progress_done, progress_total, pass_count, artifact_revision_oid,
          created_at, finished_at FROM eval_rounds WHERE project_id = ? ORDER BY created_at DESC LIMIT 1`).get(projectId) as any;
    if (!round) {
      throw new AgentCoreError(
        'EVALUATION_NOT_FOUND',
        requestedRoundId ? '指定评测轮次不存在' : '尚无评测结果',
        ['先在评测界面录入真实用例并运行评测'],
        404,
      );
    }
    const resultRows = listResults(String(round.id));
    const traces = listResultTraces(String(round.id));
    const results = resultRows.map((item) => ({
      testcaseId: item.testcaseId,
      question: item.question,
      groundTruth: item.groundTruth,
      finalAnswer: item.finalAnswer,
      verdict: item.verdict,
      reason: item.reason,
      overridden: item.overridden,
      trace: traces[item.id],
    }));
    const counts = results.reduce<Record<string, number>>((acc, item) => {
      const verdict = item.verdict || 'pending';
      acc[verdict] = (acc[verdict] || 0) + 1;
      return acc;
    }, {});
    return {
      round: {
        id: round.id,
        status: round.status,
        done: round.progress_done,
        total: round.progress_total,
        passed: round.pass_count,
        artifactRevision: round.artifact_revision_oid,
        createdAt: round.created_at,
        finishedAt: round.finished_at,
        scope: getRound(String(round.id))?.scope ?? { kind: 'current' },
      },
      counts,
      results,
      display: { text: `已读取 ${results.length} 条真实评测结果` },
    };
  }

  private async runEvaluation(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const sourceId = typeof args.source_id === 'string' ? args.source_id.trim() : '';
    const gate = sourceId ? null : await this.gateStatus(workspace, { gate: 'ready_for_eval' });
    if (gate && !gate.pass) {
      throw new AgentCoreError(
        'GATE_BLOCKED',
        '建模内容尚未满足评测前置条件',
        gate.blockers,
        409,
        { gate: 'ready_for_eval', blockers: gate.blockers },
      );
    }
    const authSession = this.authSessions.get(context.username);
    if (!authSession) {
      throw new AgentCoreError(
        'AUTH_REQUIRED',
        '启动评测需要当前可续期的登录凭据',
        ['在建模工作台重新登录后再运行评测'],
        401,
      );
    }
    const rawCaseIds = args.case_ids;
    if (rawCaseIds !== undefined && !Array.isArray(rawCaseIds)) {
      throw new AgentCoreError('INVALID_INPUT', 'case_ids 必须是用例编号数组', [], 400);
    }
    const caseIds = (rawCaseIds as unknown[] | undefined)?.map((item) => String(item).trim()).filter(Boolean) ?? [];
    try {
      const source = sourceId
        ? getPackageFusionStoredSource(this.db, context.project.id, sourceId)
        : null;
      if (sourceId && !source) {
        throw new AgentCoreError(
          'FUSION_SOURCE_NOT_FOUND',
          '指定的融合来源不存在',
          ['先读取已上传的来源包列表后再重试'],
          404,
        );
      }
      const evaluationInput = {
        projectId: context.project.id,
        owner: context.username,
        token: await authSession.getAccessToken(),
        authSession,
        caseIds,
      };
      const result = source
        ? startEvaluation({
          ...evaluationInput,
          evaluationScope: {
            kind: 'fusion_source',
            sourceId: source.id,
            sourceName: source.name,
            fingerprint: source.fingerprint,
          },
          packageSnapshot: {
            version: source.version,
            spec: source.spec,
            files: source.files,
            revisionOid: `fusion-source:${source.fingerprint}`,
          },
        })
        : await startCurrentEvaluation(evaluationInput);
      if (!source) advanceStage(context.project.id, 'evaluate');
      return {
        roundId: result.roundId,
        status: 'running',
        pending: true,
        terminal: false,
        scope: {
          ...(source ? {
            kind: 'fusion_source', sourceId: source.id, sourceName: source.name,
          } : { kind: 'current' }),
          ...(caseIds.length ? { caseIds } : { all: true }),
        },
        display: {
          text: '正式评测任务已创建，可在评测界面查看实时进度',
        },
      };
    } catch (error) {
      if (error instanceof AgentCoreError) throw error;
      if (error instanceof EvaluationAuthenticationError) {
        throw new AgentCoreError(
          'AUTH_REQUIRED',
          error.message,
          ['在建模工作台重新登录后明确发起新一轮评测'],
          401,
        );
      }
      throw new AgentCoreError(
        'EVALUATION_START_FAILED',
        error instanceof Error ? error.message : '评测启动失败',
        ['按提示补齐真实用例、数据源或评测库后重试'],
        409,
      );
    }
  }

  private async releasePreflight(workspace: WorkspaceRow) {
    const current = await this.artifacts.current(workspace.project_id);
    if (!current) {
      throw new AgentCoreError('PACKAGE_NOT_FOUND', '当前没有可发布的知识包', ['先完成并确认知识建模'], 409);
    }
    const existing = current.spec.status === 'released'
      ? await this.equivalentReleasedRevision(workspace.project_id, current.revision.oid)
      : null;
    if (existing) {
      const recordedCount = /已确认 (\d+) 条真实用例覆盖/u.exec(String(existing.summary ?? ''));
      return {
        ready: true,
        already_released: true,
        version: existing.version,
        source_revision: existing.source_revision_oid,
        revision: current.revision.oid,
        release_revision: existing.released_revision_oid,
        testcase_count: Number(recordedCount?.[1] ?? 0),
        blockers: [],
        display: { text: `知识包 ${existing.version} 已正式发布` },
      };
    }

    const manualCases = listTestcases(workspace.project_id).filter((item) => item.source === 'manual');
    const { latestRound: latestEval, results } = latestEffectiveEvaluation(
      workspace.project_id,
      current.revision.oid,
      manualCases.map((item) => item.id),
    );
    const evaluatedByCase = new Map(results.map((result) => [result.testcaseId, result]));
    const scopeMatches = manualCases.length > 0
      && results.length === manualCases.length
      && manualCases.every((testcase) => {
        const result = evaluatedByCase.get(testcase.id);
        return result?.question === testcase.question
          && result?.groundTruth === testcase.groundTruth
          && result?.verdict === 'pass';
      });
    const pendingChanges = Number((this.db.prepare(`SELECT COUNT(*) AS count FROM agent_candidate_changes
      WHERE project_id = ? AND run_id = ? AND group_id NOT IN (?, ?)`)
      .get(workspace.project_id, workspace.run_id, RUNTIME_GROUP, LEGACY_RUNTIME_GROUP) as any)?.count ?? 0);
    const pendingCandidates = Number((this.db.prepare(`SELECT COUNT(*) AS count FROM artifact_candidate_revisions
      WHERE project_id = ? AND status = 'pending'`).get(workspace.project_id) as any)?.count ?? 0);
    const releaseCandidate = current.spec.status === 'review'
      && isReleaseCandidateVersion(current.packageVersion);
    const revisionMatches = latestEval?.artifactRevision === current.revision.oid;
    const blockers = [
      ...(releaseCandidate ? [] : ['当前知识包必须是已确认的 RC 发布候选']),
      ...(pendingChanges ? [`还有 ${pendingChanges} 个候选文件未确认`] : []),
      ...(pendingCandidates ? [`还有 ${pendingCandidates} 个确认请求待处理`] : []),
      ...(latestEval?.status === 'done' ? [] : ['先完成当前知识包的评测']),
      ...(revisionMatches ? [] : ['知识包在最近一次评测后有改动，必须重新评测当前版本']),
      ...(scopeMatches ? [] : ['每条当前真实用例都必须执行并通过，问题和参考答案不能在评测后改写']),
    ];
    let version = '';
    if (releaseCandidate) version = promoteReleaseCandidate(current.packageVersion);
    return {
      ready: blockers.length === 0,
      already_released: false,
      version,
      source_revision: current.revision.oid,
      testcase_count: manualCases.length,
      questions: manualCases.map((item) => item.question),
      blockers,
      display: { text: blockers.length ? `正式发布还有 ${blockers.length} 项未完成` : `已可确认发布 ${version}` },
    };
  }

  private async equivalentReleasedRevision(projectId: string, revision: string) {
    const releases = this.db.prepare(`SELECT parent_oid AS source_revision_oid,
        oid AS released_revision_oid, package_version AS version, summary
      FROM artifact_revisions WHERE project_id = ? AND cause = 'agent_release_confirmed'
      ORDER BY sequence DESC`).all(projectId) as any[];
    for (const release of releases) {
      if (release.released_revision_oid === revision) return release;
      const diff = await this.artifacts.diff(projectId, release.released_revision_oid, revision);
      if (diff.files.length === 0) return release;
    }
    return null;
  }

  private async release(
    workspace: WorkspaceRow,
    context: AgentToolContext,
    args: Record<string, unknown>,
  ) {
    const review = object(args.review);
    if (review.approved !== true || review.coverage_confirmed !== true) {
      throw new AgentCoreError(
        'RELEASE_CONFIRMATION_REQUIRED',
        '正式发布必须由项目负责人确认用例覆盖和版本号',
        ['发起发布确认并等待项目负责人明确接受'],
        409,
      );
    }
    const preflight = await this.releasePreflight(workspace);
    if (preflight.already_released) return preflight;
    if (!preflight.ready) {
      throw new AgentCoreError('RELEASE_GATE_BLOCKED', '正式发布条件未满足', preflight.blockers, 409, {
        blockers: preflight.blockers,
      });
    }
    const version = text(args.version, 'version');
    if (version !== preflight.version || String(review.version ?? '') !== preflight.version) {
      throw new AgentCoreError(
        'RELEASE_VERSION_MISMATCH',
        `确认的版本号必须是 ${preflight.version}`,
        ['按最新发布预检重新发起确认'],
        409,
      );
    }
    const current = await this.artifacts.current(workspace.project_id);
    if (!current || current.revision.oid !== preflight.source_revision) {
      throw new AgentCoreError('REVISION_CONFLICT', '知识包在发布确认前已经变化', ['重新预检并确认最新修订'], 409);
    }
    const saved = await this.artifacts.saveCurrentAsPackageVersion(workspace.project_id, {
      expectedRevision: current.revision.oid,
      version,
      status: 'released',
      note: '由项目负责人确认真实用例覆盖后正式发布',
      action: {
        cause: 'agent_release_confirmed',
        summary: `正式发布 ${version}，已确认 ${preflight.testcase_count} 条真实用例覆盖`,
        actorKind: 'user',
        actorName: context.username,
      },
    });
    this.db.prepare(`UPDATE agent_candidate_workspaces
      SET base_revision_oid = ?, updated_at = ? WHERE project_id = ? AND run_id = ?`)
      .run(saved.revision.oid, nowIso(), workspace.project_id, workspace.run_id);
    workspace.base_revision_oid = saved.revision.oid;
    return {
      released: true,
      version,
      source_revision: current.revision.oid,
      revision: saved.revision.oid,
      testcase_count: preflight.testcase_count,
      display: { text: `知识包 ${version} 已正式发布` },
    };
  }

  private async runScriptTest(workspace: WorkspaceRow, args: Record<string, unknown>) {
    const script = packagePath(text(args.script, 'script'));
    if (!script.startsWith('scripts/') || !script.endsWith('.py')) {
      throw new AgentCoreError('INVALID_INPUT', 'script 必须指向 scripts/ 下的 Python 文件', [], 400);
    }
    if (!Array.isArray(args.cases) || !args.cases.length || args.cases.length > 50) {
      throw new AgentCoreError('CONTRACT_REQUIRED', '脚本测试必须提供 1 到 50 个契约用例', ['为每例补充 input 和 expected'], 409);
    }
    const cases = args.cases.map((item, index) => {
      if (!item || typeof item !== 'object' || Array.isArray(item)
        || !Object.hasOwn(item, 'input') || !Object.hasOwn(item, 'expected')) {
        throw new AgentCoreError(
          'CONTRACT_REQUIRED',
          `第 ${index + 1} 个脚本用例缺少 input 或 expected`,
          ['确认输入输出契约后重新测试'],
          409,
        );
      }
      if (Object.hasOwn(item, 'argv') || Object.hasOwn(item, 'function')) {
        throw new AgentCoreError(
          'CONTRACT_REQUIRED',
          `第 ${index + 1} 个脚本用例不能提供 argv 或 function`,
          ['只提供业务 input 和完整 expected；平台会按 Markdown 契约拼装 CLI'],
          409,
        );
      }
      const caseInput = (item as Record<string, unknown>).input;
      if (!caseInput || typeof caseInput !== 'object' || Array.isArray(caseInput)) {
        throw new AgentCoreError(
          'CONTRACT_REQUIRED',
          `第 ${index + 1} 个脚本用例的 input 必须是业务参数对象`,
          ['按脚本 Markdown 契约中的参数 key 提供输入'],
          409,
        );
      }
      return item as { input: Record<string, unknown>; expected: unknown };
    });
    const artifact = await this.scriptArtifactForTest(workspace, script);
    const cliCases = cases.map((item, index) => {
      const built = buildScriptCliArguments(artifact.contract.parameters, item.input);
      if (built.missing.length || built.invalid.length) {
        throw new AgentCoreError(
          'CONTRACT_REQUIRED',
          `第 ${index + 1} 个脚本用例无法按 Markdown 契约拼装 CLI`,
          ['补齐必填参数并移除未知或类型不正确的输入'],
          409,
          { missing: built.missing, invalid: built.invalid },
        );
      }
      return { argv: built.args, expected: item.expected };
    });
    const result = await runScriptCasesInJiuwenbox({
        runId: workspace.run_id,
        source: artifact.source,
        cases: cliCases,
      }).catch((error: unknown) => {
      throw new AgentCoreError(
        'SANDBOX_UNAVAILABLE',
        '脚本隔离环境暂时不可用，当前测试没有执行',
        ['稍后从当前进展重试；不要改用不隔离的环境执行'],
        503,
        { technical: error instanceof Error ? error.message : String(error) },
      );
    });
    const sourceHash = scriptSourceHash(artifact.source);
    if (result.failed === 0) {
      this.db.prepare(`INSERT INTO agent_script_test_receipts (
        project_id, run_id, script_path, source_hash, total, passed, failed, sandbox, created_at
      ) VALUES (?,?,?,?,?,?,?,?,?)
      ON CONFLICT(project_id, run_id, script_path, source_hash) DO UPDATE SET
        total = excluded.total,
        passed = excluded.passed,
        failed = excluded.failed,
        sandbox = excluded.sandbox,
        created_at = excluded.created_at`)
        .run(
          workspace.project_id, workspace.run_id, script, sourceHash,
          result.total, result.passed, result.failed, result.sandbox, nowIso(),
        );
    } else {
      this.db.prepare(`DELETE FROM agent_script_test_receipts
        WHERE project_id = ? AND run_id = ? AND script_path = ? AND source_hash = ?`)
        .run(workspace.project_id, workspace.run_id, script, sourceHash);
    }
    return {
      ...result,
      script,
      display: {
        text: result.failed
          ? `沙箱测试完成：${result.passed}/${result.total} 通过`
          : `沙箱测试全部通过（${result.total}/${result.total}）`,
      },
    };
  }

  private async scriptArtifactForTest(
    workspace: WorkspaceRow,
    script: string,
  ): Promise<{ source: string; contract: SpecScript }> {
    let files: Record<string, string>;
    if (workspace.candidate_id) {
      const candidate = await this.artifacts.getCandidateRevision(
        workspace.project_id,
        workspace.candidate_id,
      );
      if (candidate.status === 'pending') {
        const preview = await this.artifacts.previewCandidateRevision(
          workspace.project_id,
          candidate.id,
        );
        files = preview.files;
      } else {
        files = (await this.overlay(workspace)).files;
      }
    } else {
      files = (await this.overlay(workspace)).files;
    }
    const source = files[script];
    if (source === undefined) {
      throw new AgentCoreError('FILE_NOT_FOUND', `脚本文件不存在：${script}`, ['调用 list_dir 核对脚本路径'], 404);
    }
    const parseErrors: string[] = [];
    const contracts = Object.keys(files)
      .filter((path) => /^scripts\/[^/]+\.md$/u.test(path) && path !== 'scripts/index.md')
      .flatMap((path) => {
        try {
          return [parseRenderedScriptFile(path, files[path], files)];
        } catch (error) {
          parseErrors.push(error instanceof Error ? error.message : `脚本契约无法解析：${path}`);
          return [];
        }
      })
      .filter((contract) => `scripts/${contract.entrypoint}` === script);
    if (contracts.length !== 1) {
      throw new AgentCoreError(
        'INVALID_SCRIPT_CONTRACT',
        contracts.length
          ? `脚本文件绑定了多个 Markdown 契约：${script}`
          : `没有找到脚本文件对应的 Markdown 契约：${script}`,
        ['确认 scripts/*.md 的 entrypoint 与待测脚本一致'],
        409,
        parseErrors.length ? { parseErrors } : undefined,
      );
    }
    return { source, contract: contracts[0] };
  }

  private assertScriptCandidatesTested(
    workspace: WorkspaceRow,
    changes: Array<{ path: string; kind: 'write' | 'delete'; content: string | Uint8Array | null }>,
  ) {
    const blockers = untestedScriptPaths(
      workspace.project_id,
      workspace.run_id,
      changes,
      this.db,
    );
    if (blockers.length) {
      throw new AgentCoreError(
        'SCRIPT_TEST_REQUIRED',
        '脚本候选尚未在隔离环境通过当前版本测试',
        ['对 blockers 中的每个脚本运行隔离测试；全部通过后重新执行候选预检并发起确认'],
        409,
        { blockers },
      );
    }
  }

  private async queryEvaluationDatabase(projectId: string, args: Record<string, unknown>) {
    const sql = text(args.sql, 'sql').normalize('NFKC').trim().replace(/;\s*$/, '');
    if (sql.length > 20_000 || /;|--|#|\/\*/u.test(sql)
      || !/^(?:select|show|describe|desc|explain)\b/iu.test(sql)) {
      throw new AgentCoreError(
        'READ_ONLY_SQL_REQUIRED',
        '评测库工具只接受一条不含注释的只读 SQL',
        ['使用 SELECT、SHOW、DESCRIBE 或 EXPLAIN，并移除多语句和注释'],
        400,
      );
    }
    if (!config.evalDbUrl) {
      throw new AgentCoreError(
        'EVAL_DB_TOOL_UNAVAILABLE',
        '当前没有开启建模助手直连评测库的诊断能力，未执行数据库查询',
        ['运行正式评测并读取评测结果；只有需要诊断 MySQL 评测库镜像数据时才使用本工具'],
        409,
        {
          retryable: false,
          tracePhase: 'rejected',
          problemVisibility: 'activity',
          customerSummary: '可选评测库未配置，已跳过本次查询',
        },
      );
    }
    const target = resolveEvaluationDatabaseTarget(projectEvaluationDatabaseName(projectId), config.evalDbUrl);
    const mysql = await import('mysql2/promise');
    let connection: import('mysql2/promise').Connection | undefined;
    try {
      connection = await mysql.createConnection({
        uri: target.connectionUrl,
        database: target.database,
        connectTimeout: 10_000,
        multipleStatements: false,
      });
      await connection.query('SET SESSION MAX_EXECUTION_TIME = 5000');
      await connection.query('START TRANSACTION READ ONLY');
      const [rawRows, fields] = await connection.query(sql);
      const rows = Array.isArray(rawRows) ? rawRows.slice(0, 200).map(safeDatabaseRow) : [];
      await connection.query('ROLLBACK');
      return {
        columns: Array.isArray(fields) ? fields.slice(0, 100).map((field) => field.name) : [],
        rows,
        rowCount: rows.length,
        truncated: Array.isArray(rawRows) && rawRows.length > rows.length,
        database: target.database,
        display: { text: `已从项目评测库读取 ${rows.length} 行` },
      };
    } catch (error) {
      await connection?.query('ROLLBACK').catch(() => undefined);
      throw new AgentCoreError(
        'EVAL_DB_QUERY_FAILED',
        `评测库查询失败：${redactEvaluationDatabaseError(error, target)}`,
        ['检查表名和字段后重试只读查询'],
        409,
      );
    } finally {
      await connection?.end().catch(() => undefined);
    }
  }

  private unavailable(code: string, reason: string, next: string[]) {
    return { error: code, reason, next, display: { text: reason } };
  }
}

function ontologyObjectPath(key: string): string {
  return `objects/${String(key ?? '').trim()}.md`;
}

function candidateStatusArg(value: unknown): string {
  const normalized = String(value ?? '').trim();
  if (!normalized) return '';
  if (['pending', 'accepted', 'revision_requested', 'rejected', 'superseded'].includes(normalized)) {
    return normalized;
  }
  throw new AgentCoreError(
    'INVALID_INPUT',
    'status 只支持 pending、accepted、revision_requested、rejected、superseded',
    [],
    400,
  );
}

function auditCursorArg(value: unknown): { createdAt: string; candidateId: string } | null {
  const normalized = String(value ?? '').trim();
  if (!normalized) return null;
  try {
    const decoded = JSON.parse(Buffer.from(normalized, 'base64url').toString('utf8')) as Record<string, unknown>;
    const createdAt = String(decoded.createdAt ?? '').trim();
    const candidateId = String(decoded.candidateId ?? '').trim();
    if (!createdAt || !candidateId) throw new Error('cursor fields missing');
    return { createdAt, candidateId };
  } catch {
    throw new AgentCoreError('INVALID_INPUT', 'cursor 不合法', ['使用上一次返回的 next_cursor'], 400);
  }
}

function encodeAuditCursor(value: { createdAt: string; candidateId: string }): string {
  return Buffer.from(JSON.stringify(value), 'utf8').toString('base64url');
}

function safeJsonParse(value: unknown): unknown {
  try { return JSON.parse(String(value ?? 'null')); } catch { return null; }
}

function candidateGroupChangedPaths(group: Record<string, unknown>): string[] {
  const changes = Array.isArray(group.changes) ? group.changes : [];
  return changes
    .filter((item) => item && typeof item === 'object' && 'path' in item)
    .map((item) => publicPath(String((item as Record<string, unknown>).path ?? '')))
    .filter(Boolean);
}

function workspaceObjectChangeGroups(
  changes: ChangeRow[],
  objectPath: string,
): Array<{ id: string; tier: Tier; title: string; impact: string[] }> {
  const groupIds = [...new Set(changes
    .filter((change) => change.path === objectPath)
    .map((change) => change.group_id))];
  return groupIds.map((groupId) => {
    const impact = [...new Set(changes
      .filter((change) => change.group_id === groupId)
      .map((change) => change.path)
      .filter(Boolean))]
      .sort();
    return {
      id: groupId,
      tier: workspaceGroupTier(impact),
      title: workspaceGroupTitle(groupId, impact),
      impact,
    };
  });
}

function candidateGroupTouchesPath(group: Record<string, unknown>, path: string): boolean {
  return candidateGroupChangedPaths(group).includes(path);
}

function candidateOtherBusinessPaths(groupsValue: unknown, objectPath: string): string[] {
  return [...new Set(parseObjects(groupsValue)
    .flatMap(candidateGroupChangedPaths)
    .filter((path) => isBusinessDetailPath(path) && path !== objectPath))]
    .sort();
}

function otherWorkspaceBusinessPaths(changes: ChangeRow[], objectPath: string): string[] {
  return [...new Set(changes
    .map((change) => change.path)
    .filter((path) => isBusinessDetailPath(path) && path !== objectPath))]
    .sort();
}

function isBusinessDetailPath(path: string): boolean {
  return /^(?:objects|operations|rules|metrics|scripts)\/(?!index\.md$).+/u.test(path);
}

function workspaceGroupTier(paths: string[]): Tier {
  if (paths.some((path) => path.startsWith('rules/'))) return 'caliber';
  if (paths.some((path) => path.startsWith('scripts/'))) return 'script';
  if (paths.every((path) => path.startsWith('mappings/'))) return 'index';
  return 'structural';
}

function workspaceGroupTitle(groupId: string, paths: string[]): string {
  if (groupId.startsWith('studio-')) {
    if (paths.some((path) => path.startsWith('rules/'))) return 'Studio 规则草稿';
    if (paths.some((path) => path.startsWith('objects/'))) return 'Studio 对象草稿';
    if (paths.some((path) => path.startsWith('operations/'))) return 'Studio 操作草稿';
    if (paths.some((path) => path.startsWith('metrics/'))) return 'Studio 指标草稿';
    return 'Studio 草稿';
  }
  return `草稿变更 ${groupId}`;
}

function ontologyReviewStageLabel(stage: string): string {
  const normalized = String(stage ?? '').trim();
  if (normalized === 'draft') return '草稿待提交';
  if (normalized === 'review_requested') return '已提交复核';
  if (normalized === 'draft_locked') return '他人草稿占用';
  if (normalized === 'awaiting_confirmation' || normalized === 'pending') return '待确认';
  if (normalized === 'accepted') return '已确认';
  if (normalized === 'rejected') return '已退回';
  if (normalized === 'revision_requested') return '要求调整';
  if (normalized === 'superseded') return '已失效';
  return '空闲';
}

function impactResources(value: unknown): AffectedKnowledgeResource[] {
  if (!Array.isArray(value)) {
    throw new AgentCoreError(
      'INVALID_MATERIAL_IMPACT_RESOURCES',
      '直接影响项必须是列表',
      ['按业务对象、对象关系、业务操作、规则、指标或脚本逐项提供 key'],
      400,
    );
  }
  const allowed = new Set<AffectedKnowledgeKind>([
    'object', 'relation', 'operation', 'rule', 'metric', 'script',
  ]);
  return value.map((item) => {
    const record = item && typeof item === 'object' ? item as Record<string, unknown> : {};
    const kind = String(record.kind ?? '') as AffectedKnowledgeKind;
    const key = String(record.key ?? '').normalize('NFKC').trim();
    if (!allowed.has(kind) || !key) {
      throw new AgentCoreError(
        'INVALID_MATERIAL_IMPACT_RESOURCE',
        '直接影响项缺少有效的类型或 key',
        ['读取对应知识文件的 frontmatter 后重试'],
        400,
      );
    }
    return { kind, key };
  });
}

function combineMaterialImpactResults(results: any[]) {
  const sources = results.map((result) => ({
    impactId: result.impactId,
    sourceKey: result.sourceKey,
    source: result.source,
    sourceDocumentId: result.sourceDocumentId,
  }));
  const allPaths = [...new Set(results.flatMap((result) => [
    ...(result.intactPaths ?? []),
    ...(result.view?.todo?.items ?? []).map((item: any) => String(item.t ?? '')).filter(Boolean),
  ]))].sort();
  const scopeResolved = results.every((result) => result.scopeResolved !== false);
  if (!scopeResolved) {
    return {
      sources,
      semanticChanged: results.some((result) => result.semanticChanged),
      scopeResolved: false,
      affectedResources: [],
      directReferences: [],
      intactPaths: [],
      view: {
        scopeResolved: false,
        intact: { n: 0, items: [] },
        todo: { n: allPaths.length, decide: 0, items: [] },
        chains: [],
      },
      display: { text: `${sources.length} 份资料的影响范围仍在界定，暂不判断现有内容是否保持有效` },
    };
  }

  const todoByPath = new Map<string, { t: string; reasons: string[] }>();
  for (const result of results) {
    for (const item of result.view?.todo?.items ?? []) {
      const path = String(item.t ?? '');
      if (!path) continue;
      const current = todoByPath.get(path) ?? { t: path, reasons: [] };
      const reason = String(item.s ?? '').trim();
      if (reason && !current.reasons.includes(reason)) current.reasons.push(reason);
      todoByPath.set(path, current);
    }
  }
  const todoItems = [...todoByPath.values()]
    .sort((left, right) => left.t.localeCompare(right.t, 'zh-CN'))
    .map((item) => ({ t: item.t, s: item.reasons.join('；') }));
  const affectedPaths = new Set(todoItems.map((item) => item.t));
  const intactPaths = allPaths.filter((path) => !affectedPaths.has(path));
  const chainKeys = new Set<string>();
  const chains = results.flatMap((result) => result.view?.chains ?? []).filter((chain: any) => {
    const key = `${chain.srcDocumentId ?? ''}:${chain.dstPath ?? chain.dst ?? ''}`;
    if (chainKeys.has(key)) return false;
    chainKeys.add(key);
    return true;
  });
  const resourceKeys = new Set<string>();
  const affectedResources = results.flatMap((result) => result.affectedResources ?? [])
    .filter((resource: any) => {
      const key = `${resource.kind ?? ''}:${resource.key ?? ''}`;
      if (resourceKeys.has(key)) return false;
      resourceKeys.add(key);
      return true;
    });
  const directReferences = [...new Set(results.flatMap((result) => result.directReferences ?? []))].sort();
  return {
    sources,
    semanticChanged: results.some((result) => result.semanticChanged),
    scopeResolved: true,
    affectedResources,
    directReferences,
    intactPaths,
    view: {
      scopeResolved: true,
      intact: { n: intactPaths.length, items: intactPaths },
      todo: {
        n: todoItems.length,
        decide: results.reduce((total, result) => total + Number(result.view?.todo?.decide ?? 0), 0),
        items: todoItems,
      },
      chains,
    },
    display: { text: `合并影响分析完成：${intactPaths.length} 项保持有效，${todoItems.length} 项需要复核` },
  };
}

function packagePath(value: string): string {
  const path = value.normalize('NFKC').trim().replace(/^package\//, '');
  const parts = path.split('/');
  if (!path || path.startsWith('/') || path.includes('\\') || parts.some((part) => !part || part === '..')
    || [...path].some((char) => char.charCodeAt(0) < 32)) {
    throw new AgentCoreError('WRITE_UNSAFE', '知识包路径必须是安全的包内相对路径', ['改用不含空段、反斜杠或 .. 的相对路径'], 400);
  }
  return path;
}

function stableAgentIssueId(kind: string, target: string): string {
  return `agent-issue-${createHash('sha256').update(`${kind}\0${target}`).digest('hex').slice(0, 20)}`;
}

function domainTargetKey(value: unknown): string {
  const resourceId = domainResourceIdArg(value);
  return resourceId.slice(resourceId.indexOf(':') + 1);
}

function singleDomainTargetKey(value: unknown): string {
  const resourceIds = requiredStringArray(value, 'resource_ids');
  if (resourceIds.length !== 1) {
    throw new AgentCoreError(
      'CHANGE_SET_OUT_OF_SCOPE',
      '一个受控独立计划任务只能删除一个目标资源',
      ['把每个删除目标拆成独立计划任务'],
      409,
      { retryable: false },
    );
  }
  return domainTargetKey(resourceIds[0]);
}

function parseWorkingPackageSpec(files: Record<string, string>): PackageSpec {
  const internal = files[INTERNAL_SPEC_CHANGE_PATH];
  return internal ? JSON.parse(internal) as PackageSpec : parseRenderedPackageFiles(files);
}

function resolveAgentStudioSpec(
  storedSpec: PackageSpec,
  files: Record<string, string>,
  changes: ChangeRow[],
): PackageSpec {
  const internalSpecChange = changes.find((change) => change.path === INTERNAL_SPEC_CHANGE_PATH);
  return internalSpecChange?.kind === 'write'
    ? storedSpec
    : resolveStudioWorkingSpec(storedSpec, files);
}

function workspacePackageDataSourceType(
  projectId: string,
  currentSpec: PackageSpec,
): 'nl2sql' | 'neo4j' {
  const selection = getProjectPackageDataSourceSelection(projectId);
  if (selection.createdAt) return selection.dataSourceType;
  return currentSpec.dataSourceType === 'nl2sql' || currentSpec.dataSourceType === 'neo4j'
    ? currentSpec.dataSourceType
    : selection.dataSourceType;
}

function scriptContractFromArgs(
  args: Record<string, unknown>,
  operationKey: string,
): Omit<SpecScript, 'sourceCode'> {
  const key = domainKey(args.script_key, '脚本');
  const entrypoint = text(args.entrypoint, 'entrypoint');
  if (!/^[A-Za-z0-9_.-]+\.py$/u.test(entrypoint) || entrypoint.includes('..')) {
    throw new AgentCoreError(
      'INVALID_SCRIPT_CONTRACT',
      'entrypoint 必须是安全且唯一的 Python 文件名',
      ['使用不含路径分隔符和 .. 的 *.py 文件名'],
      400,
    );
  }
  const timeoutSeconds = Number(args.timeout_seconds);
  if (!Number.isInteger(timeoutSeconds) || timeoutSeconds < 1 || timeoutSeconds > 300) {
    throw new AgentCoreError(
      'INVALID_SCRIPT_CONTRACT',
      'timeout_seconds 必须是 1 到 300 之间的整数',
      [],
      400,
    );
  }
  const parameters = requiredObjectArray(args.parameters ?? [], 'parameters').map((parameter, index) => {
    if (typeof parameter.required !== 'boolean') {
      throw new AgentCoreError(
        'INVALID_SCRIPT_CONTRACT',
        `parameters[${index}].required 必须是布尔值`,
        [],
        400,
      );
    }
    const flag = text(parameter.flag, `parameters[${index}].flag`);
    if (!/^--[a-z][a-z0-9-]*$/u.test(flag)) {
      throw new AgentCoreError(
        'INVALID_SCRIPT_CONTRACT',
        `parameters[${index}].flag 必须是 -- 开头的小写 CLI 参数`,
        [],
        400,
      );
    }
    const defaultValue = parameter.default_value;
    if (defaultValue !== undefined
      && !['string', 'number', 'boolean'].includes(typeof defaultValue)) {
      throw new AgentCoreError(
        'INVALID_SCRIPT_CONTRACT',
        `parameters[${index}].default_value 只能是字符串、数字或布尔值`,
        [],
        400,
      );
    }
    const parameterType = text(parameter.type, `parameters[${index}].type`);
    const rawAction = optionalInputText(parameter.action)?.toLowerCase();
    if (rawAction && rawAction !== 'store_true' && rawAction !== 'store_false') {
      throw new AgentCoreError(
        'INVALID_SCRIPT_CONTRACT',
        `parameters[${index}].action 只能是 store_true 或 store_false`,
        [],
        400,
      );
    }
    const action: SpecScript['parameters'][number]['action'] =
      rawAction === 'store_true' || rawAction === 'store_false' ? rawAction : undefined;
    if (action && !['bool', 'boolean'].includes(parameterType.toLowerCase())) {
      throw new AgentCoreError(
        'INVALID_SCRIPT_CONTRACT',
        `parameters[${index}].action 只适用于 boolean 参数`,
        [],
        400,
      );
    }
    if (action && parameter.required) {
      throw new AgentCoreError(
        'INVALID_SCRIPT_CONTRACT',
        `parameters[${index}] 使用 ${action} 时不能设为必填`,
        [],
        400,
      );
    }
    if (action && parameter.multiple === true) {
      throw new AgentCoreError(
        'INVALID_SCRIPT_CONTRACT',
        `parameters[${index}] 使用 ${action} 时不能同时设为多值`,
        [],
        400,
      );
    }
    const actionDefault = action === 'store_true' ? false : action === 'store_false' ? true : undefined;
    if (actionDefault !== undefined && defaultValue !== undefined && defaultValue !== actionDefault) {
      throw new AgentCoreError(
        'INVALID_SCRIPT_CONTRACT',
        `parameters[${index}] 的默认值与 ${action} 语义不一致`,
        [`${action} 的默认值必须为 ${String(actionDefault)}`],
        400,
      );
    }
    const normalizedDefaultValue = defaultValue ?? actionDefault;
    if (parameter.multiple !== undefined && typeof parameter.multiple !== 'boolean') {
      throw new AgentCoreError(
        'INVALID_SCRIPT_CONTRACT',
        `parameters[${index}].multiple 必须是布尔值`,
        [],
        400,
      );
    }
    return {
      name: domainKey(parameter.name, `脚本参数 parameters[${index}]`),
      flag,
      type: parameterType,
      required: parameter.required,
      ...(normalizedDefaultValue !== undefined ? { defaultValue: normalizedDefaultValue } : {}),
      ...(optionalInputText(parameter.description)
        ? { description: optionalInputText(parameter.description) }
        : {}),
      ...(parameter.multiple === true ? { multiple: true } : {}),
      ...(action ? { action } : {}),
    };
  });
  assertUniqueDomainKeys(parameters.map((parameter) => parameter.name), '脚本参数 key');
  assertUniqueDomainKeys(parameters.map((parameter) => parameter.flag), '脚本参数 flag');
  const outputs = requiredObjectArray(args.outputs, 'outputs').map((output, index) => ({
    key: domainKey(output.key, `脚本输出 outputs[${index}]`),
    type: text(output.type, `outputs[${index}].type`),
    ...(optionalInputText(output.description)
      ? { description: optionalInputText(output.description) }
      : {}),
  }));
  assertUniqueDomainKeys(outputs.map((output) => output.key), '脚本输出 key');
  if (!outputs.length) {
    throw new AgentCoreError('INVALID_SCRIPT_CONTRACT', '脚本至少要声明一个结构化输出', [], 400);
  }
  return {
    key,
    title: text(args.title, 'title'),
    description: text(args.description, 'description'),
    boundOperation: operationKey,
    runtime: 'python',
    entrypoint,
    timeoutSeconds,
    parameters,
    outputs,
    boundaries: requiredStringArray(args.boundaries, 'boundaries'),
    evidence: requiredStringArray(args.evidence, 'evidence'),
  };
}

function scriptContractView(contract: Omit<SpecScript, 'sourceCode'>) {
  return {
    key: contract.key,
    title: contract.title,
    description: contract.description,
    bound_operation: `operation:${contract.boundOperation}`,
    runtime: contract.runtime,
    entrypoint: contract.entrypoint,
    timeout_seconds: contract.timeoutSeconds,
    parameters: contract.parameters.map((parameter) => ({
      name: parameter.name,
      flag: parameter.flag,
      type: parameter.type,
      required: parameter.required,
      ...(parameter.defaultValue !== undefined ? { default_value: parameter.defaultValue } : {}),
      ...(parameter.description ? { description: parameter.description } : {}),
      ...(parameter.multiple ? { multiple: true } : {}),
      ...(parameter.action ? { action: parameter.action } : {}),
    })),
    outputs: contract.outputs,
    boundaries: contract.boundaries,
    evidence: contract.evidence ?? [],
  };
}

function operationResourceIdArg(value: unknown): string {
  const resourceId = domainResourceIdArg(value);
  if (!resourceId.startsWith('operation:')) {
    throw new AgentCoreError(
      'INVALID_INPUT',
      'operation_id 必须是 operation:key',
      ['使用 get_domain_resource 返回的操作 resource_id'],
      400,
    );
  }
  return resourceId;
}

function scriptOperationContractHash(element: StudioElement): string {
  if (element.kind !== 'operation') {
    throw new AgentCoreError('DOMAIN_KIND_MISMATCH', '脚本只能绑定操作资源', [], 400);
  }
  const values = structuredClone(element.values);
  delete values.script;
  return hashStructuredValue('script-operation-contract-v1', {
    key: element.key,
    values,
    relations: element.relations
      .filter((relation) => !relation.targetId.startsWith('script:'))
      .map((relation) => ({
        kind: relation.kind,
        targetId: relation.targetId,
        label: relation.label,
      })),
  });
}

function hashStructuredValue(namespace: string, value: unknown): string {
  return `sha256:${createHash('sha256')
    .update(namespace)
    .update('\0')
    .update(stableDomainJson(value))
    .digest('hex')}`;
}

function domainCreateValues(kind: DomainKind, args: Record<string, unknown>): Record<string, unknown> {
  if (kind === 'object') {
    const fields = requiredObjectArray(args.fields, 'fields').map((field, index) => {
      if (typeof field.required !== 'boolean') {
        throw new AgentCoreError(
          'INVALID_DOMAIN_PAYLOAD',
          `fields[${index}].required 必须是布尔值`,
          [],
          400,
        );
      }
      return {
        key: text(field.key, `fields[${index}].key`),
        ...(optionalInputText(field.cn_name) ? { cnName: optionalInputText(field.cn_name) } : {}),
        ...(optionalInputText(field.type) ? { type: optionalInputText(field.type) } : {}),
        required: field.required,
        ...(optionalInputText(field.note) ? { note: optionalInputText(field.note) } : {}),
        ...(optionalInputText(field.alias) ? { alias: optionalInputText(field.alias) } : {}),
      };
    });
    return {
      title: text(args.title, 'title'),
      description: text(args.description, 'description'),
      fields,
      aliases: stringArray(args.aliases),
      queryBoundary: String(args.query_boundary ?? '').trim(),
      relations: requiredObjectArray(args.relations ?? [], 'relations').map((relation, index) => ({
        kind: 'object_relation',
        targetId: text(relation.target_id, `relations[${index}].target_id`),
        ...(optionalInputText(relation.label) ? { label: optionalInputText(relation.label) } : {}),
        ...(optionalInputText(relation.description)
          ? { description: optionalInputText(relation.description) }
          : {}),
      })),
    };
  }
  if (kind === 'rule') {
    return {
      title: text(args.title, 'title'),
      description: String(args.description ?? '').trim(),
      content: text(args.content, 'content'),
      conditions: requiredObjectArray(args.conditions ?? [], 'conditions').map((condition, index) => ({
        condition: text(condition.condition, `conditions[${index}].condition`),
        conclusion: text(condition.conclusion, `conditions[${index}].conclusion`),
      })),
      exceptions: stringArray(args.exceptions),
      evidence: requiredStringArray(args.evidence, 'evidence'),
      relations: domainReferenceRelations('rule', requiredStringArray(args.targets ?? [], 'targets', true)),
    };
  }
  if (kind === 'operation') {
    return {
      title: text(args.title, 'title'),
      description: text(args.description, 'description'),
      inputs: requiredObjectArray(args.inputs ?? [], 'inputs').map((input, index) => {
        if (typeof input.required !== 'boolean') {
          throw new AgentCoreError(
            'INVALID_DOMAIN_PAYLOAD',
            `inputs[${index}].required 必须是布尔值`,
            [],
            400,
          );
        }
        return {
          param: text(input.param, `inputs[${index}].param`),
          ...(optionalInputText(input.cn_name) ? { cnName: optionalInputText(input.cn_name) } : {}),
          type: text(input.type, `inputs[${index}].type`),
          required: input.required,
          ...(optionalInputText(input.note) ? { note: optionalInputText(input.note) } : {}),
        };
      }),
      outputs: requiredObjectArray(args.outputs ?? [], 'outputs').map((output, index) => ({
        field: text(output.field, `outputs[${index}].field`),
        ...(optionalInputText(output.cn_name) ? { cnName: optionalInputText(output.cn_name) } : {}),
        type: text(output.type, `outputs[${index}].type`),
        ...(optionalInputText(output.note) ? { note: optionalInputText(output.note) } : {}),
      })),
      flow: requiredStringArray(args.flow, 'flow'),
      notes: stringArray(args.notes),
      evidence: requiredStringArray(args.evidence, 'evidence'),
      relations: domainReferenceRelations(
        'operation',
        requiredStringArray(args.references ?? [], 'references', true),
      ),
    };
  }
  return {
    title: text(args.title, 'title'),
    description: String(args.description ?? '').trim(),
    unit: String(args.unit ?? '').trim(),
    grain: String(args.grain ?? '').trim(),
    caliber: text(args.caliber, 'caliber'),
    boundaries: requiredStringArray(args.boundaries, 'boundaries'),
    nullPolicy: requiredObjectArray(args.null_policy ?? [], 'null_policy').map((item, index) => ({
      field: text(item.field, `null_policy[${index}].field`),
      handling: text(item.handling, `null_policy[${index}].handling`),
    })),
    dimensions: requiredObjectArray(args.dimensions ?? [], 'dimensions').map((item, index) => ({
      key: text(item.key, `dimensions[${index}].key`),
      note: text(item.note, `dimensions[${index}].note`),
    })),
    evidence: requiredStringArray(args.evidence, 'evidence'),
    relations: domainReferenceRelations(
      'metric',
      requiredStringArray(args.object_references ?? [], 'object_references', true),
    ),
  };
}

function domainReferenceRelations(
  sourceKind: Exclude<DomainKind, 'object'>,
  targets: string[],
): Array<{ kind: string; targetId: string }> {
  const legal: Record<typeof sourceKind, Record<string, string>> = {
    rule: { object: 'applies_to_object', operation: 'applies_to_operation' },
    operation: { object: 'uses_object', rule: 'uses_rule', metric: 'uses_metric' },
    metric: { object: 'uses_object' },
  };
  return targets.map((targetId, index) => {
    const targetKind = targetId.split(':', 1)[0];
    const relationKind = legal[sourceKind][targetKind];
    if (!relationKind) {
      throw new AgentCoreError(
        'DOMAIN_REFERENCE_KIND_INVALID',
        `${sourceKind} 不能引用 ${targetKind || '未知'} 类型`,
        ['使用 get_domain_resource 返回的 resource_id'],
        400,
        { path: `references[${index}]`, target_id: targetId },
      );
    }
    return { kind: relationKind, targetId };
  });
}

function domainMutationResponse(
  response: StudioMutationResponse,
  summary: { action_count: number; operations: string[] },
) {
  return {
    working_revision: response.workingRevision,
    mutation_id: response.clientMutationId,
    ...(response.editSessionId ? { edit_session_id: response.editSessionId } : {}),
    candidate: response.candidate,
    ...(response.replayed ? { replayed: true } : {}),
    change_group: response.changeGroup,
    changed_paths: response.changedPaths,
    ...(response.element ? { resource: domainResourcePayload(response.element) } : {}),
    resources: response.elements.map((element) => domainResourcePayload(element)),
    ...(response.deletedElementIds?.length ? { deleted_resource_ids: response.deletedElementIds } : {}),
    warnings: response.warnings,
    historical_problem_count: response.historicalProblemCount,
    mutation_summary: summary,
    display: response.display,
  };
}

function domainMutationRequestHash(operation: string, args: Record<string, unknown>): string {
  return createHash('sha256')
    .update('agent-domain-mutation-v2\0')
    .update(operation)
    .update('\0')
    .update(stableDomainJson(args))
    .digest('hex');
}

function domainDependencyRevision(projectId: string): string {
  const dependencies = listDocuments(projectId).map((document) => ({
    id: document.id,
    sourceKey: document.sourceKey,
    parseStatus: document.parseStatus,
    revisionId: (document as any).revisionId ?? (document as any).revision_id ?? '',
    updatedAt: (document as any).updatedAt ?? (document as any).updated_at ?? '',
    tables: (document.tables ?? []).map((table) => ({
      name: table.name,
      fields: table.fields.map((field) => field.name),
    })),
  }));
  return createHash('sha256')
    .update('agent-domain-dependencies-v1\0')
    .update(stableDomainJson(dependencies))
    .digest('hex');
}

function domainMutationSummary(
  args: Record<string, unknown>,
  fallbackOperation = 'patch_resource',
): { action_count: number; operations: string[] } {
  const operations = Array.isArray(args.actions)
    ? args.actions.map((action) => (
      action && typeof action === 'object' && !Array.isArray(action)
        ? String((action as Record<string, unknown>).op ?? fallbackOperation)
        : fallbackOperation
    ))
    : requiredStringArray(args.resource_ids ?? [], 'resource_ids', true).map(() => fallbackOperation);
  return { action_count: operations.length || 1, operations: operations.length ? operations : [fallbackOperation] };
}

function stableDomainJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableDomainJson).join(',')}]`;
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => left.localeCompare(right));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stableDomainJson(item)}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

function assertDomainResourceHash(
  element: StudioElement,
  expectedValue: unknown,
  actualRevision: string,
): void {
  const expected = text(expectedValue, 'expected_resource_hash');
  const actual = domainResourceHash(element);
  if (expected === actual) return;
  throw new AgentCoreError(
    'RESOURCE_HASH_CONFLICT',
    '目标资源已经变化，请重新读取后生成增量修改',
    ['重新调用 get_domain_resource，并基于新的 resource_hash 生成 Patch'],
    409,
    {
      error_class: 'conflict',
      retryable: false,
      repairable: true,
      expected_resource_hash: expected,
      actual_resource_hash: actual,
      actual_revision: actualRevision,
    },
  );
}

function domainKindArg(value: unknown): DomainKind {
  const normalized = String(value ?? '').trim();
  if (['object', 'rule', 'operation', 'metric'].includes(normalized)) return normalized as DomainKind;
  throw new AgentCoreError(
    'INVALID_INPUT',
    'kind 必须是 object、rule、operation 或 metric',
    [],
    400,
  );
}

function domainResourceIdArg(value: unknown): string {
  const normalized = String(value ?? '').trim();
  const separator = normalized.indexOf(':');
  if (separator > 0) {
    const kind = normalized.slice(0, separator);
    const key = normalized.slice(separator + 1);
    if (['object', 'rule', 'operation', 'metric'].includes(kind)) {
      try {
        return `${kind}:${validateKey(key, '知识资源')}`;
      } catch {
        // Normalize invalid IDs to the stable public error below.
      }
    }
  }
  throw new AgentCoreError(
    'INVALID_INPUT',
    'resource_id 必须是 kind:key',
    ['使用 list_domain_resources 或 get_domain_resource 返回的 resource_id'],
    400,
  );
}

function domainKindLabel(kind: DomainKind): string {
  return ({ object: '对象', rule: '规则', operation: '操作', metric: '指标' } as const)[kind];
}

function normalizeChangeControlError(error: unknown): AgentCoreError {
  if (error instanceof AgentCoreError) return error;
  if (error instanceof ChangeControlError) {
    return new AgentCoreError(
      error.code,
      error.message,
      error.details.requires_user_decision
        ? ['先通过问题卡获得用户决定，再修订原建模计划']
        : [],
      error.statusCode,
      {
        error_class: error.statusCode === 409 ? 'conflict' : 'deterministic',
        retryable: false,
        repairable: error.statusCode < 500,
        ...error.details,
      },
    );
  }
  return normalizeDomainAgentError(error);
}

function changeSetStatusLabel(status: string): string {
  return ({
    staging: '正在结算',
    staged: '已形成候选',
    validated: '校验通过',
    waiting_user: '等待确认',
    accepted: '已接受',
    rejected: '已退回',
    failed: '未生效',
    discarded: '已放弃',
    cancelled: '已取消',
  } as Record<string, string>)[status] || '状态未知';
}

function normalizeDomainAgentError(error: unknown): AgentCoreError {
  if (error instanceof DomainPatchError) {
    const fingerprint = createHash('sha256')
      .update(`${error.code}\0${error.path}\0${error.message}`)
      .digest('hex');
    return new AgentCoreError(
      error.code,
      error.message,
      ['只修正错误 path 对应的动作后，使用同一业务语义重新提交'],
      error.code.endsWith('_CONFLICT') ? 409 : 400,
      {
        error_class: error.code.endsWith('_CONFLICT') ? 'conflict' : 'deterministic',
        retryable: false,
        repairable: true,
        failure_fingerprint: `sha256:${fingerprint}`,
        path: error.path,
        ...error.details,
      },
    );
  }
  if (error instanceof AgentCoreError) return error;
  if (error instanceof StudioReadError || error instanceof StudioWriteError) {
    const conflict = ['REVISION_CONFLICT', 'WORKING_REVISION_CHANGED', 'DELETE_PREVIEW_STALE'].includes(error.code);
    const precondition = ['ELEMENT_NOT_FOUND', 'STUDIO_WRITE_LOCKED'].includes(error.code);
    const fingerprint = createHash('sha256')
      .update(`${error.code}\0${error.message}\0${JSON.stringify(error.details)}`)
      .digest('hex');
    return new AgentCoreError(
      error.code,
      error.message,
      [],
      error.statusCode,
      {
        error_class: conflict ? 'conflict' : precondition ? 'precondition' : 'deterministic',
        retryable: false,
        repairable: error.statusCode < 500,
        failure_fingerprint: `sha256:${fingerprint}`,
        ...error.details,
      },
    );
  }
  return new AgentCoreError(
    'INTERNAL_ERROR',
    error instanceof Error ? error.message : String(error),
    [],
    500,
    { error_class: 'transient', retryable: true, repairable: false },
  );
}

function ontologyObjectPayload(element: StudioElement) {
  const values = object(element.values);
  return {
    id: element.id,
    key: element.key,
    title: element.title,
    description: element.description,
    source_path: element.sourcePath,
    relation_count: element.relationCount,
    metadata: {
      title: element.title,
      description: element.description,
      aliases: Array.isArray(values.aliases) ? values.aliases.map(String) : [],
      queryBoundary: String(values.queryBoundary ?? ''),
    },
    fields: Array.isArray(values.fields) ? values.fields : [],
    sources: Array.isArray(values.sources) ? values.sources : [],
    relations: element.relations.map((relation) => ({
      id: relation.id,
      kind: relation.kind,
      label: relation.label,
      target_id: relation.targetId,
      target_kind: relation.targetKind,
      target_key: relation.targetKey,
      target_exists: relation.targetExists,
      ...(relation.description ? { description: relation.description } : {}),
    })),
  };
}

function objectIdArg(value: unknown): string {
  const normalized = String(value ?? '').trim();
  if (normalized.startsWith('object:')) {
    try {
      return `object:${validateKey(normalized.slice('object:'.length), '对象')}`;
    } catch {
      // Normalize all invalid IDs to the stable public error below.
    }
  }
  throw new AgentCoreError(
    'INVALID_INPUT',
    'object_id 必须是 object:key',
    ['使用 list_ontology_objects 或 get_ontology_object 返回的 object_id'],
    400,
  );
}

function normalizeStudioAgentError(error: unknown): AgentCoreError {
  if (error instanceof AgentCoreError) return error;
  if (error instanceof StudioReadError || error instanceof StudioWriteError) {
    return new AgentCoreError(error.code, error.message, [], error.statusCode, error.details);
  }
  return new AgentCoreError(
    'INTERNAL_ERROR',
    error instanceof Error ? error.message : String(error),
    [],
    500,
  );
}

function text(value: unknown, field: string, allowEmpty = false): string {
  if (typeof value !== 'string' || (!allowEmpty && !value.trim())) {
    throw new AgentCoreError('INVALID_INPUT', `${field} 必填`, [`补充 ${field} 后重试`], 400);
  }
  return allowEmpty ? value : value.trim();
}

function auditSummary(value: unknown, fallback: string): string {
  const normalized = String(value ?? '')
    .replace(/[\u0000-\u001f\u007f]+/gu, ' ')
    .replace(/\s+/gu, ' ')
    .trim() || fallback;
  return normalized.length <= 500 ? normalized : `${normalized.slice(0, 499)}…`;
}

function auditNote(value: unknown, fallback = ''): string | undefined {
  const normalized = String(value ?? '')
    .replace(/[\u0000-\u001f\u007f]+/gu, ' ')
    .replace(/\s+/gu, ' ')
    .trim() || fallback;
  if (!normalized) return undefined;
  return normalized.length <= 1_000 ? normalized : `${normalized.slice(0, 999)}…`;
}

function integer(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = value === undefined ? fallback : Number(value);
  if (!Number.isSafeInteger(parsed)) return fallback;
  return Math.min(max, Math.max(min, parsed));
}

function safeDatabaseRow(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return { value: safeDatabaseValue(value) };
  return Object.fromEntries(Object.entries(value as Record<string, unknown>).slice(0, 100)
    .map(([key, item]) => [key, safeDatabaseValue(item)]));
}

function safeDatabaseValue(value: unknown): unknown {
  if (Buffer.isBuffer(value)) return `<binary ${value.byteLength} bytes>`;
  if (value instanceof Date) return value.toISOString();
  if (typeof value === 'bigint') return value.toString();
  if (typeof value === 'string') return value.slice(0, 4_000);
  if (value === null || ['number', 'boolean'].includes(typeof value)) return value;
  const rendered = JSON.stringify(value);
  return rendered === undefined ? String(value) : rendered.slice(0, 4_000);
}

function object(value: unknown): Record<string, any> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, any> : {};
}

function optionalInputText(value: unknown): string | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'string') {
    throw new AgentCoreError('INVALID_DOMAIN_PAYLOAD', '可选文本字段必须是字符串', [], 400);
  }
  return value.trim() || undefined;
}

function requiredObjectArray(value: unknown, field: string): Array<Record<string, any>> {
  if (!Array.isArray(value) || value.some((item) => !item || typeof item !== 'object' || Array.isArray(item))) {
    throw new AgentCoreError('INVALID_DOMAIN_PAYLOAD', `${field} 必须是对象数组`, [], 400);
  }
  return value as Array<Record<string, any>>;
}

function boundedInteger(
  value: unknown,
  minimum: number,
  maximum: number,
  fallback: number,
): number {
  if (value === undefined || value === null || value === '') return fallback;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new AgentCoreError(
      'INVALID_INPUT',
      `分页参数必须是 ${minimum} 到 ${maximum} 之间的整数`,
      [],
      400,
    );
  }
  return parsed;
}

function schemaSemanticSummary(status: SchemaSemanticStatus) {
  return {
    available: status.available,
    pass: status.pass,
    validator_version: status.validatorVersion,
    fingerprint: status.fingerprint,
    object_count: status.objectCount,
    field_count: status.fieldCount,
    error_count: status.errorCount,
    warning_count: status.warningCount,
  };
}

function assertSchemaSemanticPatchActions(value: unknown, objectIndex: number): void {
  if (!Array.isArray(value) || value.length < 1 || value.length > 500) {
    throw new AgentCoreError(
      'INVALID_DOMAIN_PAYLOAD',
      `objects[${objectIndex}].actions 必须包含 1 到 500 项语义修改`,
      [],
      400,
    );
  }
  const allowed = new Set(['set_title', 'set_description', 'set_aliases', 'update_field']);
  value.forEach((rawAction, actionIndex) => {
    const action = object(rawAction);
    const op = String(action.op ?? '').trim();
    const path = `objects[${objectIndex}].actions[${actionIndex}]`;
    if (!allowed.has(op)) {
      throw new AgentCoreError(
        'SCHEMA_SEMANTIC_ACTION_FORBIDDEN',
        `${path} 只能修改对象与字段的业务语义`,
        ['仅使用 set_title、set_description、set_aliases 或 update_field'],
        400,
        { path, op, retryable: false },
      );
    }
    if (op !== 'update_field') return;
    const changes = object(action.changes);
    const keys = Object.keys(changes);
    const allowedChanges = new Set(['cn_name', 'cnName', 'note', 'alias', 'required']);
    if (!keys.length) {
      throw new AgentCoreError(
        'SCHEMA_SEMANTIC_ACTION_FORBIDDEN',
        `${path}.changes 必须包含至少一项中文名、业务说明、别名或必填决定`,
        ['update_field 使用 field_key、expected_field_hash 和 changes；不要使用 field 重传完整字段'],
        400,
        { path: `${path}.changes`, retryable: false },
      );
    }
    const unexpectedKeys = keys.filter((key) => !allowedChanges.has(key));
    if (unexpectedKeys.length) {
      throw new AgentCoreError(
        'SCHEMA_SEMANTIC_ACTION_FORBIDDEN',
        `${path}.changes 包含不允许的字段属性`,
        ['changes 只能包含 cn_name、note、alias 或 required；不要修改字段 key、来源列或类型'],
        400,
        { path: `${path}.changes`, unexpected_keys: unexpectedKeys, retryable: false },
      );
    }
    if ('required' in changes && typeof changes.required !== 'boolean') {
      throw new AgentCoreError(
        'INVALID_DOMAIN_PAYLOAD',
        `${path}.changes.required 必须明确选择 true 或 false`,
        [],
        400,
      );
    }
  });
}

function mappingWithoutRequired(value: string): unknown {
  if (!value.trim()) return null;
  const parsed = JSON.parse(value) as Record<string, any>;
  const clone = structuredClone(parsed);
  for (const objectMapping of Object.values<Record<string, any>>(clone.objects ?? {})) {
    for (const fieldMapping of Object.values<Record<string, any>>(objectMapping.fields ?? {})) {
      delete fieldMapping.required;
    }
  }
  return clone;
}

function assertOnlyRequiredMappingChanged(before: string, after: string): void {
  try {
    if (JSON.stringify(mappingWithoutRequired(before)) === JSON.stringify(mappingWithoutRequired(after))) return;
  } catch { /* The normal package compiler always produces JSON; report a controlled invariant error below. */ }
  throw new AgentCoreError(
    'SCHEMA_SEMANTIC_MAPPING_DRIFT',
    '业务语义修改意外改变了字段身份、来源、类型或其他数据映射内容',
    ['放弃本批修改并检查 Schema 语义 Patch 编译器'],
    500,
    { retryable: false },
  );
}

function optionalStringArray(value: unknown): string[] | undefined {
  if (value === undefined || value === null) return undefined;
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
    throw new AgentCoreError('INVALID_DOMAIN_PAYLOAD', 'optional string array must contain strings only', [], 400);
  }
  const result = value.map((item) => item.trim()).filter(Boolean);
  return result.length ? [...new Set(result)] : undefined;
}

function optionalPreviousObjectRelations(value: unknown): PreviousObjectRelation[] | undefined {
  if (value === undefined || value === null) return undefined;
  if (!Array.isArray(value) || value.some((item) => !item || typeof item !== 'object' || Array.isArray(item))) {
    throw new AgentCoreError('INVALID_DOMAIN_PAYLOAD', 'previousRelations must be an object array', [], 400);
  }
  const result = (value as Array<Record<string, unknown>>).flatMap((item) => {
    const sourceObject = String(item.sourceObject ?? item.source_object ?? '').trim();
    const targetObject = String(item.targetObject ?? item.target_object ?? '').trim();
    const relation = String(item.relation ?? '').trim();
    if (!sourceObject || !targetObject || !relation) return [];
    const statusValue = String(item.status ?? 'previous').trim();
    const status = ['previous', 'discarded', 'needs_review'].includes(statusValue)
      ? statusValue as PreviousObjectRelation['status']
      : 'needs_review';
    const note = optionalInputText(item.note);
    const evidence = optionalStringArray(item.evidence);
    return [{
      sourceObject,
      targetObject,
      relation,
      ...(note ? { note } : {}),
      ...(evidence ? { evidence } : {}),
      status,
    } satisfies PreviousObjectRelation];
  });
  return result.length ? result : undefined;
}

function optionalSuggestionScope(value: unknown): { affectedObjects?: string[]; affectedOperations?: string[] } | undefined {
  if (value === undefined || value === null) return undefined;
  const input = object(value);
  const affectedObjects = optionalStringArray(input.affectedObjects ?? input.affected_objects);
  const affectedOperations = optionalStringArray(input.affectedOperations ?? input.affected_operations);
  if (!affectedObjects && !affectedOperations) return undefined;
  return {
    ...(affectedObjects ? { affectedObjects } : {}),
    ...(affectedOperations ? { affectedOperations } : {}),
  };
}

function requiredStringArray(value: unknown, field: string, allowEmpty = false): string[] {
  if (value === undefined && allowEmpty) return [];
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string' || !item.trim())) {
    throw new AgentCoreError('INVALID_DOMAIN_PAYLOAD', `${field} 必须是非空字符串数组`, [], 400);
  }
  const result = value.map((item) => item.trim());
  if (!allowEmpty && !result.length) {
    throw new AgentCoreError('INVALID_DOMAIN_PAYLOAD', `${field} 至少包含一项`, [], 400);
  }
  assertUniqueDomainKeys(result, field);
  return result;
}

function domainKey(value: unknown, label: string): string {
  try {
    return validateKey(text(value, 'key'), label);
  } catch (error) {
    if (error instanceof AgentCoreError) throw error;
    throw new AgentCoreError(
      'INVALID_DOMAIN_PAYLOAD',
      error instanceof Error ? error.message : `${label} key 不合法`,
      ['使用稳定且不含路径字符的资源 key'],
      400,
    );
  }
}

function assertUniqueDomainKeys(values: string[], label: string): void {
  const seen = new Set<string>();
  for (const value of values) {
    if (seen.has(value)) {
      throw new AgentCoreError(
        'INVALID_DOMAIN_PAYLOAD',
        `${label}包含重复值：${value}`,
        ['删除重复项后重试'],
        400,
      );
    }
    seen.add(value);
  }
}

function assertReferences(label: string, requested: string[], available: string[]): void {
  const existing = new Set(available);
  const missing = requested.filter((key) => !existing.has(key));
  if (!missing.length) return;
  throw new AgentCoreError(
    'DOMAIN_REFERENCE_NOT_FOUND',
    `${label}引用不存在：${missing.join('、')}`,
    [`使用当前知识包中已存在的${label} key，或先创建被引用资源`],
    400,
    { missing, available: available.slice(0, 100), availableCount: available.length },
  );
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String).map((item) => item.trim()).filter(Boolean) : [];
}

function parseArray(value: unknown): string[] {
  try { return stringArray(JSON.parse(String(value ?? '[]'))); } catch { return []; }
}

function parseObjects(value: unknown): any[] {
  if (Array.isArray(value)) return value.filter((item) => item && typeof item === 'object');
  try {
    const parsed = JSON.parse(String(value ?? '[]'));
    return Array.isArray(parsed) ? parsed.filter((item) => item && typeof item === 'object') : [];
  } catch { return []; }
}

function changeGroups(value: unknown): ChangeGroupInput[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 5) {
    throw new AgentCoreError('TOO_MANY_GROUPS', '每批确认必须包含 1 到 5 个变更组', ['按业务决定拆成不超过 5 组后重试'], 400);
  }
  const seen = new Set<string>();
  return value.map((raw) => {
    const item = object(raw);
    const id = changeGroupId(item.id, 'change_groups.id');
    const tier = String(item.tier ?? '') as Tier;
    if (!['caliber', 'structural', 'index', 'script'].includes(tier)) {
      throw new AgentCoreError('INVALID_INPUT', `变更组 ${id} 的 tier 不正确`, ['使用 caliber、structural、index 或 script'], 400);
    }
    if (seen.has(id)) throw new AgentCoreError('INVALID_INPUT', `变更组 ID 重复：${id}`, ['为每组使用唯一 ID'], 400);
    seen.add(id);
    return {
      id,
      tier,
      title: text(item.title, 'change_groups.title'),
      impact: stringArray(item.impact),
      ...(String(item.warning ?? '').trim() ? { warning: String(item.warning).trim() } : {}),
    };
  });
}

function changeGroupId(value: unknown, field: string): string {
  const id = text(value, field);
  if (!CHANGE_GROUP_ID_PATTERN.test(id)) {
    throw new AgentCoreError(
      'INVALID_CHANGE_GROUP_ID',
      `${field} 必须是小写英文开头、只含小写英文、数字、点、横线或下划线的标识`,
      ['使用 goal、process_main 或 knowledge_objects 这类简短标识'],
      400,
    );
  }
  if (isHiddenCandidateGroup(id)) {
    throw new AgentCoreError(
      'RESERVED_CHANGE_GROUP_ID',
      `${field} 使用了平台保留标识`,
      ['换一个描述业务修改范围的简短英文标识'],
      400,
    );
  }
  return id;
}

function expectedTier(path: string): Tier | null {
  if (path === 'manifest.yaml' || path === 'index.md' || path.includes('/index.') || path.startsWith('mappings/')) return 'index';
  if (path === '.modeling/goal.md' || path.startsWith('diagrams/') || path.startsWith('rules/')) return 'caliber';
  if (path.startsWith('scripts/')) return 'script';
  if (path.startsWith('objects/') || path.startsWith('operations/') || path.startsWith('metrics/')) return 'structural';
  return null;
}

function suggestChangeGroupAssignments(changes: ChangeRow[]) {
  const titleFor = (tier: Tier, category: string) => {
    if (category === 'goal') return '建模目标确认';
    if (category === 'diagrams') return '业务流程确认';
    if (category === 'rules') return '业务规则确认';
    if (tier === 'caliber') return '业务口径确认';
    if (tier === 'index') return '知识索引同步';
    if (tier === 'script') return '脚本实现';
    if (category === 'operations') return '操作结构';
    if (category === 'objects') return '对象结构';
    if (category === 'metrics') return '指标结构';
    return '知识结构';
  };
  const buckets = new Map<string, { tier: Tier; title: string; paths: string[] }>();
  for (const change of changes) {
    const tier = expectedTier(change.path) ?? 'structural';
    const prefix = change.path.split('/', 1)[0];
    const category = change.path === '.modeling/goal.md'
      ? 'goal'
      : (tier === 'caliber' && ['diagrams', 'rules'].includes(prefix))
          ? prefix
          : tier === 'structural' && ['operations', 'objects', 'metrics'].includes(prefix)
              ? prefix
              : tier;
    const key = `${tier}:${category}`;
    const bucket = buckets.get(key) ?? { tier, title: titleFor(tier, category), paths: [] };
    bucket.paths.push(change.path);
    buckets.set(key, bucket);
  }
  const groups = [...buckets.values()].sort((left, right) =>
    left.tier.localeCompare(right.tier) || left.title.localeCompare(right.title));
  while (groups.length > 5) {
    const mergeTier = groups.find((group) => groups.filter((candidate) => candidate.tier === group.tier).length > 1)?.tier;
    if (!mergeTier) break;
    const indexes = groups.map((group, index) => ({ group, index })).filter((item) => item.group.tier === mergeTier);
    const first = indexes[0];
    const second = indexes[1];
    first.group.paths.push(...second.group.paths);
    first.group.title = `${first.group.title}与${second.group.title}`;
    groups.splice(second.index, 1);
  }
  return groups.map((group, index) => ({
    id: `regroup_${group.tier}_${index + 1}`,
    tier: group.tier,
    title: group.title,
    paths: group.paths.sort(),
  }));
}

function confirmationGroupAssignments(changes: ChangeRow[]) {
  const byId = new Map<string, ChangeRow[]>();
  for (const change of changes) {
    const group = byId.get(change.group_id) ?? [];
    group.push(change);
    byId.set(change.group_id, group);
  }
  const canPreserve = byId.size <= 5 && [...byId.entries()].every(([id, group]) => {
    const tiers = new Set(group.map((change) => expectedTier(change.path) ?? 'structural'));
    return CHANGE_GROUP_ID_PATTERN.test(id) && tiers.size === 1;
  });
  if (!canPreserve) return suggestChangeGroupAssignments(changes);
  return [...byId.entries()].map(([id, group]) => {
    const suggestions = suggestChangeGroupAssignments(group);
    const tier = (expectedTier(group[0].path) ?? 'structural') as Tier;
    return {
      id,
      tier,
      title: suggestions.length === 1 ? suggestions[0].title : confirmationTierTitle(tier),
      paths: group.map((change) => change.path).sort(),
    };
  });
}

function confirmationTierTitle(tier: Tier) {
  if (tier === 'caliber') return '业务口径确认';
  if (tier === 'index') return '知识索引同步';
  if (tier === 'script') return '脚本实现';
  return '知识结构';
}

function toArtifactChange(change: ChangeRow): ArtifactChange {
  const path = change.path === INTERNAL_SPEC_CHANGE_PATH
    ? 'model/package-spec.json'
    : `package/${change.path}`;
  return change.kind === 'delete'
    ? { kind: 'delete', path }
    : { kind: 'write', path, content: change.content ?? '' };
}

function publicPath(path: string): string {
  return path.startsWith('package/') ? path.slice('package/'.length) : path;
}

function candidateRequiresObjectMappings(paths: Iterable<string>): boolean {
  return [...paths].some((path) => (
    path === OBJECT_DATA_MAPPING_PATH
      || /^objects\//u.test(path)
  ));
}

function isMaterialMappingProblem(problem: MaterialPurposeReviewProblem): boolean {
  return problem.code !== 'MATERIAL_PURPOSE_REVIEW_REQUIRED';
}

function materialPurposeReviewPayload(
  validation: MaterialPurposeReviewValidation | undefined,
) {
  if (!validation || !validation.baseline.length) return {};
  const baselineWarnings = validation.baseline.filter((problem) => (
    !validation.introduced.some((item) => (
      materialPurposeReviewProblemFingerprint(item)
        === materialPurposeReviewProblemFingerprint(problem)
    ))
  ));
  return {
    validation_summary: {
      validator_version: 'material-source-mapping-v3',
      baseline_finding_count: validation.baseline.length,
      baseline_warning_count: baselineWarnings.length,
      introduced_finding_count: validation.introduced.length,
      baseline_warnings: baselineWarnings.map((problem) => ({
        ruleId: problem.code,
        documentId: problem.documentId,
        filename: problem.filename,
        ...(problem.sheet ? { sheet: problem.sheet } : {}),
        ...(problem.affectedObjectKeys?.length ? { affectedObjectKeys: problem.affectedObjectKeys } : {}),
        message: problem.message,
      })),
    },
  };
}

function candidateDiff(base: Record<string, string>, changes: ChangeRow[]): ArtifactFileDiff[] {
  const encoder = new TextEncoder();
  const result: ArtifactFileDiff[] = [];
  for (const change of changes) {
    if (change.path === INTERNAL_SPEC_CHANGE_PATH) continue;
    const before = base[change.path];
    if (change.kind === 'delete') {
      if (before !== undefined) {
        result.push({ path: `package/${change.path}`, kind: 'deleted', before: encoder.encode(before) });
      }
      continue;
    }
    if (before === change.content) continue;
    result.push({
      path: `package/${change.path}`,
      kind: before === undefined ? 'added' : 'modified',
      ...(before === undefined ? {} : { before: encoder.encode(before) }),
      after: encoder.encode(change.content ?? ''),
    });
  }
  return result;
}

function plainDiff(file: ArtifactFileDiff) {
  const decoder = new TextDecoder();
  const before = file.before ? decoder.decode(file.before) : '';
  const after = file.after ? decoder.decode(file.after) : '';
  const beforeLines = before ? before.split('\n') : [];
  const afterLines = after ? after.split('\n') : [];
  let prefix = 0;
  while (prefix < beforeLines.length && prefix < afterLines.length
    && beforeLines[prefix] === afterLines[prefix]) prefix += 1;
  let suffix = 0;
  while (suffix < beforeLines.length - prefix && suffix < afterLines.length - prefix
    && beforeLines[beforeLines.length - 1 - suffix] === afterLines[afterLines.length - 1 - suffix]) suffix += 1;
  return {
    path: publicPath(file.path),
    status: file.kind,
    before,
    after,
    additions: Math.max(0, afterLines.length - prefix - suffix),
    deletions: Math.max(0, beforeLines.length - prefix - suffix),
  };
}

function normalizeReviewMode(value: unknown, automatic: boolean): 'full' | 'sequential' | 'sampled' | 'automatic' {
  if (automatic) return 'automatic';
  return ['full', 'sequential', 'sampled'].includes(String(value))
    ? String(value) as 'full' | 'sequential' | 'sampled'
    : 'full';
}

function combine(
  gates: Record<string, { pass: boolean; blockers: string[] }>,
  names: string[],
): { pass: boolean; blockers: string[] } {
  const blockers = names.flatMap((name) => gates[name]?.pass ? [] : gates[name]?.blockers ?? [`门禁 ${name} 未通过`]);
  return { pass: blockers.length === 0, blockers };
}

function locateAnchor(content: string, anchor: string): number {
  if (!anchor || !content) return 0;
  const offsetMatch = anchor.match(/^(?:offset:|@)(\d+)$/i);
  if (offsetMatch) return Math.min(content.length, Number(offsetMatch[1]));
  for (const candidate of [anchor, anchor.replace(/^§/, '')]) {
    const offset = content.indexOf(candidate);
    if (offset >= 0) return offset;
  }
  return 0;
}

function assertMaterialEvidenceReferences(projectId: string, evidence: string[]) {
  const documents = listDocuments(projectId);
  for (const reference of evidence) {
    const normalized = reference.normalize('NFKC').trim();
    if (!normalized.startsWith('material:')) continue;
    const body = normalized.slice('material:'.length);
    const hash = body.indexOf('#');
    const source = (hash >= 0 ? body.slice(0, hash) : body).trim();
    const exists = documents.some((document) => (
      document.id === source || document.sourceKey === source || document.filename === source
    ));
    if (!exists) {
      throw new AgentCoreError(
        'EVIDENCE_NOT_FOUND',
        `证据引用对应的资料不存在：${source || reference}`,
        ['调用 list_documents，并原样复制资料 id、sourceKey 或完整文件名后重试'],
        400,
        { reference },
      );
    }
  }
}

function tabularDatasetCsv(tables: DataDictTable[]): string {
  const lines = ['\uFEFFtable_name,row_json'];
  for (const table of tables) {
    const fields = table.fields.map((field) => field.name.trim());
    for (const row of table.rows ?? []) {
      const record = Object.fromEntries(fields.map((field, index) => {
        const value = row[index];
        return [field, value == null || value === '' ? null : value];
      }));
      lines.push(`${csvCell(table.name)},${csvCell(JSON.stringify(record))}`);
    }
  }
  return `${lines.join('\n')}\n`;
}

function csvCell(value: string): string {
  return `"${value.replaceAll('"', '""')}"`;
}

function referringFiles(
  files: Record<string, string>,
  document: { id: string; sourceKey?: string; filename: string },
  anchor = '',
) {
  const sources = [document.id, document.sourceKey, document.filename].filter(Boolean) as string[];
  const references = sources.flatMap((source) => [source, `material:${source}`]);
  return Object.entries(files).flatMap(([path, content]) => {
    const lines = content.split('\n');
    const indexes = lines.flatMap((line, index) => {
      const sourceHit = references.some((token) => line.includes(token));
      return sourceHit && (!anchor || line.includes(anchor)) ? [index] : [];
    });
    return indexes.length ? [{
      path,
      occurrences: indexes.length,
      line: indexes[0] + 1,
      why: lines[indexes[0]].trim().slice(0, 300),
    }] : [];
  });
}

function isReviewableKnowledgePath(path: string): boolean {
  return /^(?:diagrams|objects|operations|rules|metrics|scripts)\//u.test(path)
    && !path.endsWith('/index.md');
}

function findKnowledgeResourcePath(
  files: Record<string, string>,
  kind: string,
  rawKey: string,
): string | undefined {
  const key = kind === 'relation' ? rawKey.split('.')[0] : rawKey;
  const directory = {
    object: 'objects/', relation: 'objects/', operation: 'operations/',
    rule: 'rules/', metric: 'metrics/', script: 'scripts/',
  }[kind];
  if (!directory) return undefined;
  const candidates = Object.entries(files).filter(([path]) => (
    path.startsWith(directory) && !path.endsWith('/index.md')
  ));
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const frontmatterKey = new RegExp(`^key:\\s*["']?${escaped}["']?\\s*$`, 'mu');
  return candidates.find(([, content]) => frontmatterKey.test(content))?.[0]
    ?? candidates.find(([path]) => path.split('/').at(-1)?.replace(/\.(?:md|py)$/u, '') === key)?.[0];
}

function knowledgeKindLabel(kind: string): string {
  return {
    object: '业务对象', relation: '对象关系', operation: '业务操作',
    rule: '业务规则', metric: '业务指标', script: '确定性脚本',
  }[kind] ?? '知识条目';
}

function packageSourceSummary(source: PackageMergeSource) {
  const scopes = [...new Set(Object.keys(source.files).map((path) => path.split('/')[0]))].sort();
  return {
    id: source.id,
    filename: source.filename,
    packageId: source.packageId,
    name: source.name,
    version: source.version,
    fileCount: Object.keys(source.files).length,
    scopes,
    counts: {
      objects: source.spec.objects.length,
      operations: source.spec.operations.length,
      rules: source.spec.rules?.length ?? 0,
      metrics: source.spec.metrics?.length ?? 0,
      scripts: source.spec.scripts?.length ?? 0,
      diagrams: Object.keys(source.files).filter((path) => path.startsWith('diagrams/') && path.endsWith('.mmd')).length,
    },
    scopeDependencies: packageFusionScopeDependencyGraph(source),
    createdAt: source.createdAt,
  };
}

function withPackageProvenance(content: string, path: string, provenance: string): string {
  if (content.includes(provenance)) return content;
  if (path.endsWith('.md') && content.startsWith('---\n')) {
    const closing = content.indexOf('\n---', 4);
    if (closing >= 0) return `${content.slice(0, closing)}\nprovenance: ${provenance}${content.slice(closing)}`;
  }
  if (path.endsWith('.mmd')) return `%% provenance: ${provenance}\n${content}`;
  if (path.endsWith('.py')) return `# provenance: ${provenance}\n${content}`;
  if (/\.(?:ya?ml|toml)$/u.test(path)) return `# provenance: ${provenance}\n${content}`;
  return content;
}

function tierLabel(tier: Tier): string {
  return { caliber: '业务口径', structural: '知识结构', index: '索引', script: '脚本' }[tier];
}

function formatFindingIdentity(finding: CheckFinding): string {
  return JSON.stringify([
    finding.source,
    finding.severity,
    finding.category.normalize('NFKC').trim(),
    finding.target.normalize('NFKC').trim(),
    finding.message.normalize('NFKC').trim().replace(/\s+/gu, ' '),
  ]);
}

function agentValidationFinding(finding: CheckFinding) {
  const missingManifestField = finding.target === 'manifest.yaml'
    ? /manifest 缺少必选字段 ([A-Za-z_][\w-]*)/u.exec(finding.message)?.[1]
    : undefined;
  const ruleId = finding.code ?? (missingManifestField
    ? `manifest.required.${missingManifestField}`
    : finding.target === 'manifest.yaml' && /kind 必须为 ontology-package/u.test(finding.message)
      ? 'manifest.kind.ontology-package'
      : finding.target === 'manifest.yaml' && /status 不正确/u.test(finding.message)
        ? 'manifest.status.allowed-value'
        : `format.${String(finding.fingerprint ?? finding.id).replace(/^check:/u, '').slice(0, 16)}`);
  return {
    ruleId,
    fingerprint: finding.fingerprint ?? finding.id,
    source: finding.source,
    category: finding.category,
    path: finding.path ?? finding.target,
    message: finding.message,
    suggestion: finding.suggestion ?? `修正《${finding.target}》后重新执行候选预检`,
  };
}

function knowledgeValidationFinding(issue: string) {
  const path = /^([^\s]+\.md)\b/u.exec(issue)?.[1] ?? 'knowledge-package';
  const digest = createHash('sha256').update(issue.normalize('NFKC')).digest('hex').slice(0, 16);
  return {
    ruleId: `knowledge-markdown.${digest}`,
    fingerprint: `knowledge:${digest}`,
    source: 'knowledge-markdown',
    category: 'Markdown 消费契约',
    path,
    message: issue,
    suggestion: `只修正《${path}》中该问题对应的 frontmatter、章节、表格或引用后重新预检`,
  };
}

function materialPurposeLabel(purpose: string | undefined): string {
  return ({
    auto: '由助手判断',
    goal: '建模目标依据',
    schema: '数据 Schema',
    term: '术语表',
    evidence: '通用业务依据',
  })[purpose ?? 'auto'] ?? '由助手判断';
}

function documentRevisionFingerprint(document: {
  id: string; sourceKey?: string; revisionNo?: number; contentHash?: string; semanticHash?: string;
}): string {
  return [
    document.id,
    document.sourceKey ?? '',
    document.revisionNo ?? 0,
    document.contentHash ?? '',
    document.semanticHash ?? '',
  ].join(':');
}

function schemaTableFact(document: { id: string; filename: string }, table: DataDictTable) {
  const duplicateNames = new Set(table.fields
    .filter((field, index, fields) => field.name
      && fields.some((candidate, other) => other !== index && candidate.name === field.name))
    .map((field) => field.name));
  const warnings = table.fields.flatMap((field) => {
    if (!field.name) return [`${field.sourceColumn ?? '未知列'} 没有字段名，需要专家确认`];
    if (duplicateNames.has(field.name)) return [`字段“${field.name}”重复，必须结合 Excel 列号区分`];
    return [];
  });
  return {
    table: table.name,
    documentId: document.id,
    filename: document.filename,
    rowCount: table.rowCount,
    fields: table.fields.map((field) => ({
      name: field.name,
      sourceColumn: field.sourceColumn,
      inferredType: field.inferredType,
      typeInferred: true,
      sampleValues: field.sampleValues,
      distinctRatio: field.distinctRatio,
      nonEmptyRatio: field.nonEmptyRatio ?? 0,
      primaryKeyCandidate: table.rowCount > 0
        && (field.nonEmptyRatio ?? 0) === 1
        && field.distinctRatio >= 0.98,
      primaryKeyInferred: true,
      status: field.name && !duplicateNames.has(field.name) ? 'source_fact' : 'needs_clarification',
    })),
    warnings,
  };
}

function coveredCharacters(chunks: Array<{
  document_key: string; start_offset: number; end_offset: number;
}>): number {
  const byDocument = new Map<string, Array<[number, number]>>();
  for (const chunk of chunks) {
    const ranges = byDocument.get(String(chunk.document_key)) ?? [];
    ranges.push([Number(chunk.start_offset) || 0, Number(chunk.end_offset) || 0]);
    byDocument.set(String(chunk.document_key), ranges);
  }
  let total = 0;
  for (const ranges of byDocument.values()) {
    ranges.sort(([left], [right]) => left - right);
    let start = -1;
    let end = -1;
    for (const [nextStart, nextEnd] of ranges) {
      if (start < 0) {
        start = nextStart;
        end = nextEnd;
      } else if (nextStart <= end) {
        end = Math.max(end, nextEnd);
      } else {
        total += Math.max(0, end - start);
        start = nextStart;
        end = nextEnd;
      }
    }
    if (start >= 0) total += Math.max(0, end - start);
  }
  return total;
}

async function parallelMap<T, R>(items: T[], concurrency: number, mapper: (item: T) => Promise<R>): Promise<R[]> {
  const results = new Array<R>(items.length);
  let cursor = 0;
  const workers = Array.from({ length: Math.min(Math.max(1, concurrency), items.length) }, async () => {
    while (cursor < items.length) {
      const index = cursor;
      cursor += 1;
      results[index] = await mapper(items[index]);
    }
  });
  await Promise.all(workers);
  return results;
}

function workingContentRevision(baseRevision: string, files: Record<string, string>): string {
  const hash = createHash('sha256').update('studio-working-v1\0').update(baseRevision);
  for (const path of Object.keys(files).filter((path) => path !== INTERNAL_SPEC_CHANGE_PATH).sort()) {
    const content = files[path];
    hash.update('\0').update(String(Buffer.byteLength(path))).update(':').update(path);
    hash.update('\0').update(String(Buffer.byteLength(content))).update(':').update(content);
  }
  return `sha256:${hash.digest('hex')}`;
}

export function scriptSourceHash(source: string | Uint8Array): string {
  return scriptTestSourceHash(source);
}

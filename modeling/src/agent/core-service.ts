import { createHash, randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { getPackageArtifactStore } from '../artifacts/runtime.js';
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
import { parseFullTableDocumentIsolated } from '../documents/parse-service.js';
import { config } from '../config.js';
import { getProjectDataSource } from '../evaluation/data-source-repo.js';
import { getRunningEvaluationDatabaseImportRun } from '../evaluation/eval-db-import-repo.js';
import { redactEvaluationDatabaseError, resolveEvaluationDatabaseTarget } from '../evaluation/evaluation-database-target.js';
import { startCurrentEvaluation, startEvaluation, waitForEvaluation } from '../evaluation/engine.js';
import { projectEvaluationDatabaseName } from '../evaluation/project-evaluation-database.js';
import { advanceStage, getProject } from '../projects/repo.js';
import type { DataDictTable, Project } from '../types.js';
import {
  KNOWLEDGE_MARKDOWN_VALIDATOR_VERSION,
  validateKnowledgeMarkdownFile,
  validateKnowledgeMarkdownContract,
} from '../workflow/knowledge-markdown-contract.js';
import {
  PACKAGE_CONTRACT_VERSION,
  PACKAGE_FORMAT_VALIDATOR_VERSION,
  mergeAgentManifest,
  packageGenerationContract,
} from '../workflow/package-contract.js';
import { runFormatChecks, type CheckFinding } from '../workflow/package-check.js';
import {
  MERMAID_SOURCE_VALIDATOR_VERSION,
  validateMermaidSource,
} from '../workflow/mermaid-source-contract.js';
import {
  isReleaseCandidateVersion,
  nextValidationCandidateVersion,
  promoteReleaseCandidate,
} from '../workflow/package-version-lifecycle.js';
import { ensureFullDocumentSummaries } from '../workflow/material-analysis-map-reduce.js';
import { buildTaskLlm } from '../llm/provider.js';
import {
  getRound,
  latestEffectiveEvaluation,
  listResults,
  listResultTraces,
  listTestcases,
} from '../workflow/m3-repo.js';
import { issueAgentRunCredential } from './run-credential.js';
import { runScriptCasesInJiuwenbox } from './sandbox.js';
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

const WORKSPACE_LEASE_MS = 30 * 60 * 1000;
const RUNTIME_GROUP = 'runtime_context';
const LEGACY_RUNTIME_GROUP = '__runtime_context';
const MAX_FILE_CHARS = 1_000_000;
const CHANGE_GROUP_ID_PATTERN = /^[a-z][a-z0-9_.-]{0,63}$/u;
const GATE_LABELS: Record<string, string> = {
  materials_ready: '资料准备',
  goal_confirmed: '建模目标确认',
  process_confirmed: '业务流程确认',
  knowledge_confirmed: '知识内容确认',
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
  token?: string;
}

export class AgentCoreService {
  private readonly db: DatabaseSync;
  private readonly artifacts = getPackageArtifactStore();
  private readonly packageFusion: PackageFusionService;

  constructor(db: DatabaseSync = getDb()) {
    this.db = db;
    this.packageFusion = new PackageFusionService(db, this.artifacts);
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
      files: Object.keys(packageFiles.files).sort(),
      binaryFiles: packageFiles.binaryFiles,
      materials,
      confirmPolicy: this.workbenchConfirmPolicy(project.id),
    };
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
    return {
      workingRevision: workspaceChanges.length
        ? workingContentRevision(current.revision.oid, packageFiles.files)
        : current.revision.oid,
      baseRevision: current.revision.oid,
      candidateRevision: readableWorkspace?.candidate_id ?? null,
      files: packageFiles.files,
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
    if (tool === 'claim_candidate_workspace') return this.claimCandidateWorkspace(context);
    if (tool === 'get_package_contract') return packageGenerationContract();
    if (tool === 'get_material_analysis_progress') return this.materialAnalysisProgress(context.project.id, args);
    const workspace = await this.ensureWorkspace(context);
    switch (tool) {
      case 'list_documents': return this.listDocuments(context.project.id);
      case 'read_document': return this.readDocument(context.project.id, args);
      case 'set_material_directive': return this.setMaterialDirective(context.project.id, args);
      case 'read_documents_fully': return this.readDocumentsFully(context, args);
      case 'search_documents': return this.searchDocuments(context.project.id, args);
      case 'list_tables': return this.listTables(context.project.id);
      case 'describe_table': return this.describeTable(context.project.id, args);
      case 'read_table_rows': return this.readTableRows(context.project.id, args);
      case 'stage_tabular_dataset': return this.stageTabularDataset(workspace, context, args);
      case 'list_dir': return this.listDir(workspace, args);
      case 'read_file': return this.readFile(workspace, args);
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
      case 'update_plan': return this.updateRuntimeFile(workspace, context, '.modeling/plan.md', args);
      case 'request_confirmation_preflight': return this.preflight(workspace, context, args);
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
      const workspace = this.resetWorkspaceToCurrent(context, current.revision.oid);
      return {
        projectId: workspace.project_id,
        runId: workspace.run_id,
        previousRunId: existing.run_id,
        resumed: false,
        supersededChanges,
        runCredential: issueAgentRunCredential(workspace.project_id, workspace.run_id, workspace.owner),
        packageContract: packageGenerationContract(),
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
      return this.resetWorkspaceToCurrent(context, current.revision.oid);
    }

    if (!existing || existing.run_id !== context.runId) {
      this.db.exec('BEGIN IMMEDIATE');
      try {
        this.db.prepare('DELETE FROM agent_candidate_changes WHERE project_id = ?').run(context.project.id);
        this.db.prepare(`INSERT INTO agent_candidate_workspaces (
          project_id, run_id, owner, base_revision_oid, candidate_id,
          lease_expires_at, created_at, updated_at
        ) VALUES (?,?,?,?,NULL,?,?,?)
        ON CONFLICT(project_id) DO UPDATE SET
          run_id = excluded.run_id,
          owner = excluded.owner,
          base_revision_oid = excluded.base_revision_oid,
          candidate_id = NULL,
          lease_expires_at = excluded.lease_expires_at,
          created_at = excluded.created_at,
          updated_at = excluded.updated_at`)
          .run(
            context.project.id,
            context.runId,
            context.username,
            current.revision.oid,
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

  private resetWorkspaceToCurrent(context: AgentToolContext, currentRevision: string): WorkspaceRow {
    const now = new Date();
    const expiresAt = new Date(now.getTime() + WORKSPACE_LEASE_MS).toISOString();
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.prepare('DELETE FROM agent_candidate_changes WHERE project_id = ?').run(context.project.id);
      this.db.prepare(`UPDATE agent_candidate_workspaces SET
        run_id = ?, owner = ?, base_revision_oid = ?, candidate_id = NULL,
        lease_expires_at = ?, created_at = ?, updated_at = ?
        WHERE project_id = ?`)
        .run(
          context.runId,
          context.username,
          currentRevision,
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
      candidate_id, lease_expires_at FROM agent_candidate_workspaces WHERE project_id = ?`)
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
    for (const change of changes) {
      if (change.kind === 'delete') delete files[change.path];
      else files[change.path] = change.content ?? '';
    }
    return { ...base, files };
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
    if (!['auto', 'goal', 'schema', 'evidence'].includes(purpose)) {
      throw new AgentCoreError('INVALID_MATERIAL_PURPOSE', '资料用途不正确', ['选择由助手判断、建模目标依据、数据 Schema 或通用业务依据'], 400);
    }
    if (!['on_demand', 'full'].includes(readingMode)) {
      throw new AgentCoreError('INVALID_READING_MODE', '阅读方式不正确', ['选择按需读取或全文精读'], 400);
    }
    try {
      const updated = updateDocumentDirectives(document.id, {
        purpose: purpose as NonNullable<typeof document.purpose>,
        readingMode: readingMode as NonNullable<typeof document.readingMode>,
      });
      return {
        document: {
          id: updated.id, filename: updated.filename, kind: updated.kind,
          purpose: updated.purpose, readingMode: updated.readingMode,
        },
        ...(updated.purpose === 'schema' ? {
          schema: (updated.tables ?? []).map((table) => schemaTableFact(updated, table)),
          objects: (updated.tables ?? []).map((table) => ({
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
        recommendedPhase: purpose === 'goal' ? 'goal' : purpose === 'schema' ? 'knowledge' : null,
        display: {
          text: `已将《${updated.filename}》设为${materialPurposeLabel(updated.purpose)}，${updated.readingMode === 'full' ? '需要全文精读' : '默认按需读取'}`,
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
    return listDocuments(projectId).flatMap((document) => (document.tables ?? []).map((table) => ({
      ...table,
      documentId: document.id,
    })));
  }

  private listTables(projectId: string) {
    const documents = listDocuments(projectId);
    const tables = this.tables(projectId).map((table) => ({
      name: table.name,
      documentId: table.documentId,
      filename: documents.find((document) => document.id === table.documentId)?.filename,
      rowCount: table.rowCount,
      fields: table.fields.length,
    }));
    return { tables, display: { text: `已查看 ${tables.length} 张数据表` } };
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
    const limit = integer(args.limit, 20, 1, 50);
    const rows = (table.rows ?? []).slice(offset, offset + limit);
    return {
      table: table.name,
      columns: table.fields.map((field) => field.name),
      rows,
      offset,
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
    this.upsertChange(workspace, {
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

  private async readFile(workspace: WorkspaceRow, args: Record<string, unknown>) {
    const path = packagePath(text(args.path, 'path'));
    const { files } = await this.overlay(workspace);
    if (!(path in files)) {
      throw new AgentCoreError('FILE_NOT_FOUND', `知识包文件不存在：${path}`, ['调用 list_dir 核对路径'], 404);
    }
    return { path, content: files[path], revision: workspace.base_revision_oid, candidate: true,
      display: { text: `正在读取《${path}》` } };
  }

  private async writeFile(workspace: WorkspaceRow, context: AgentToolContext, args: Record<string, unknown>) {
    const path = packagePath(text(args.path, 'path'));
    let content = text(args.content, 'content', true);
    const evidence = stringArray(args.evidence);
    let manifestNormalization: { ignoredFields: string[] } | undefined;
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
    this.upsertChange(workspace, {
      path, kind: 'write', content, groupId,
      evidence, reason: null,
    });
    return { ok: true, path, changeGroup: groupId, candidate: true,
      ...(manifestNormalization ? { manifestNormalization } : {}),
      display: { text: `已写入候选文件《${path}》` } };
  }

  private async deleteFile(workspace: WorkspaceRow, context: AgentToolContext, args: Record<string, unknown>) {
    const path = packagePath(text(args.path, 'path'));
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
    if (['.modeling/goal.md', 'manifest.yaml', 'index.md', 'objects/index.md', 'operations/index.md'].includes(path)) {
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
    this.upsertChange(workspace, {
      path, kind: 'delete', content: null, groupId,
      evidence: [], reason: String(args.reason ?? '').trim() || null,
    });
    return { ok: true, path, changeGroup: groupId, candidate: true,
      display: { text: `已加入候选删除《${path}》` } };
  }

  private async regroupChanges(workspace: WorkspaceRow, context: AgentToolContext, _args: Record<string, unknown>) {
    const changes = this.changes(workspace).filter((change) => change.group_id !== RUNTIME_GROUP);
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
          change_groups: this.candidateGroups(candidate.id).filter((group) => group.id !== RUNTIME_GROUP),
          frozen: true,
          display: { text: '已恢复原待确认内容' },
        };
      }
    }
    const changes = this.changes(workspace).filter((change) => (
      change.group_id !== RUNTIME_GROUP && change.group_id !== LEGACY_RUNTIME_GROUP
    ));
    if (!changes.length) {
      throw new AgentCoreError('NO_CANDIDATE', '没有需要专家确认的业务候选', ['先完成候选内容，再发起确认']);
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

  private async assertCandidateStageGate(workspace: WorkspaceRow, path: string) {
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
    const changes = this.changes(workspace);
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
      const knowledgePending = changes.some((change) => (
        /^(objects|operations|rules|metrics)\//u.test(change.path)
      ));
      const knowledgeConfirmed = !knowledgePending && (trustedProcessBaseline
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
    this.upsertChange(workspace, {
      path, kind: 'write', content, groupId: RUNTIME_GROUP, evidence: [], reason: null,
    });
    return { ok: true, path, candidate: true, display: { text: path.endsWith('plan.md') ? '已更新建模计划' : '已更新项目摘要' } };
  }

  private upsertChange(
    workspace: WorkspaceRow,
    change: { path: string; kind: 'write' | 'delete'; content: string | null; groupId: string; evidence: string[]; reason: string | null },
  ) {
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
      this.upsertChange(workspace, {
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
    const filtered = files.filter((file) => !scope || publicPath(file.path).startsWith(scope));
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
    const changes = this.changes(workspace);
    const businessIds = [...new Set(changes.map((item) => item.group_id).filter((id) => id !== RUNTIME_GROUP))];
    if (!businessIds.length && workspace.candidate_id) {
      const candidate = await this.artifacts.getCandidateRevision(workspace.project_id, workspace.candidate_id);
      const groups = this.candidateGroups(candidate.id).filter((group) => group.id !== RUNTIME_GROUP);
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
      await this.assertFrozenCandidateValid(workspace, candidate);
      const validationReceipt = this.recordCandidateValidationReceipt(candidate);
      return {
        candidate_revision: candidate.id,
        validation_receipt: validationReceipt,
        change_groups: groups.map((group) => ({ ...group, autoApproved: false })),
        auto_approved: false,
        ...(focusPath ? { focus: { kind: 'diff', target: publicPath(focusPath) } } : {}),
        display: { text: '多来源融合会整体替换当前知识包，需要确认后才会生效' },
      };
    }
    if (!businessIds.length) {
      throw new AgentCoreError('NO_CANDIDATE', '没有需要专家确认的业务候选', ['先用 write_file 写入业务候选']);
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
    if (changes.some((item) => !item.path.startsWith('.modeling/'))) {
      await this.assertCandidateFormatValid(workspace, changes);
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

    const publicGroups = this.candidateGroups(candidate.id).filter((group) => group.id !== RUNTIME_GROUP);
    const askGroups = publicGroups.filter((group) => !this.autoPolicy(workspace.project_id, group.tier));
    const focusPath = candidate.groups
      .filter((group) => group.id !== RUNTIME_GROUP)
      .flatMap((group) => group.changes)
      .map((change) => publicPath(change.path))[0];
    const validationReceipt = this.recordCandidateValidationReceipt(candidate);
    return {
      candidate_revision: candidate.id,
      validation_receipt: validationReceipt,
      change_groups: publicGroups.map((group) => ({ ...group, autoApproved: !askGroups.some((item) => item.id === group.id) })),
      auto_approved: askGroups.length === 0,
      ...(focusPath ? { focus: { kind: 'diff', target: focusPath } } : {}),
      display: { text: askGroups.length ? `有 ${askGroups.length} 组变更需要确认` : '候选变更符合项目自动确认策略' },
    };
  }

  private async assertCandidateFormatValid(workspace: WorkspaceRow, changes: ChangeRow[]) {
    const candidate = await this.overlay(workspace, changes);
    const base = await this.artifacts.readPackageFiles(workspace.project_id, workspace.base_revision_oid);
    this.assertPackageFilesValid(base, candidate);
  }

  private async assertFrozenCandidateValid(workspace: WorkspaceRow, candidate: PackageCandidateRevision) {
    const preview = await this.artifacts.previewCandidateRevision(workspace.project_id, candidate.id);
    const base = await this.artifacts.readPackageFiles(workspace.project_id, candidate.baseRevision);
    this.assertPackageFilesValid(base, preview);
  }

  private assertPackageFilesValid(
    base: Pick<PackageArtifactFiles, 'spec' | 'files'>,
    candidate: Pick<PackageArtifactFiles, 'spec' | 'files'>,
  ) {
    const baselineErrors = runFormatChecks(base.spec, base.files)
      .filter((finding) => finding.severity === '错误');
    const baselineIdentities = new Set(baselineErrors.map(formatFindingIdentity));
    const introduced = runFormatChecks(candidate.spec, candidate.files)
      .filter((finding) => finding.severity === '错误')
      .filter((finding) => !baselineIdentities.has(formatFindingIdentity(finding)));
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

    const baselineKnowledgeIssues = new Set(validateKnowledgeMarkdownContract(base.files));
    const introducedKnowledgeIssues = validateKnowledgeMarkdownContract(candidate.files)
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
    const selected = [...new Set([...(requested.length ? requested : groups.map((group) => group.id)), ...automatic, RUNTIME_GROUP])]
      .filter((id) => groups.some((group) => group.id === id));
    const reviewMode = normalizeReviewMode(review.review_mode, requested.length === 0 && automatic.length === groups.length);
    const selectedIds = new Set(selected);
    const candidate = await this.artifacts.getCandidateRevision(workspace.project_id, workspace.candidate_id);
    this.assertCandidateValidationReceipt(workspace, candidate, args);
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
    await this.artifacts.rejectCandidateRevision(workspace.project_id, {
      candidateId,
      note: auditNote(object(args.review).feedback, '专家退回了这批候选'),
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
    return { rejected: true, candidateRevision: candidateId, display: { text: '候选确认已退回，可继续调整' } };
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
    const documents = listDocuments(workspace.project_id);
    const parsed = documents.filter((item) => item.parseStatus === 'parsed').length;
    const failed = documents.filter((item) => item.parseStatus === 'failed');
    const confirmedPaths = this.confirmedPaths(workspace.project_id);
    const { files } = await this.overlay(workspace);
    const base = await this.artifacts.readPackageFiles(workspace.project_id, workspace.base_revision_oid);
    const pendingPaths = new Set(this.changes(workspace).map((change) => change.path));
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
    const knowledgeConfirmed = ![...pendingPaths].some((path) => /^(objects|operations|rules|metrics)\//u.test(path))
      && (trustedProcessBaseline || (processConfirmed && knowledgePaths.length > 0
        && knowledgePaths.every((path) => confirmedPaths.has(path))));
    const pendingBusiness = this.changes(workspace).filter((item) => (
      item.group_id !== RUNTIME_GROUP && item.group_id !== LEGACY_RUNTIME_GROUP
    )).length;
    const pendingCandidates = Number((this.db.prepare(`SELECT COUNT(*) AS count FROM artifact_candidate_revisions
      WHERE project_id = ? AND status = 'pending'`).get(workspace.project_id) as any)?.count ?? 0);
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
    const releasedContent = gate === 'releasable'
      ? await this.equivalentReleasedRevision(workspace.project_id, workspace.base_revision_oid)
      : null;

    const gates: Record<string, { pass: boolean; blockers: string[]; warnings?: string[] }> = {
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
        blockers: knowledgeConfirmed ? [] : knowledgePaths.length
          ? knowledgePaths.filter((path) => !confirmedPaths.has(path) || pendingPaths.has(path))
            .map((path) => `确认知识条目 ${path}`)
          : ['形成并确认对象、操作或规则知识'],
      },
      modeling_complete: {
        pass: pendingBusiness === 0 && pendingCandidates === 0,
        blockers: [
          ...(pendingBusiness ? [`还有 ${pendingBusiness} 个候选文件未确认`] : []),
          ...(pendingCandidates ? [`还有 ${pendingCandidates} 个确认请求待处理`] : []),
        ],
      },
      evaluation_passed: {
        pass: evaluationMatchesCurrentFacts,
        blockers: evaluationMatchesCurrentFacts ? [] : [
          '确保当前知识包的每条真实用例都有最新有效结果，且问题、参考答案和结果保持一致并通过',
        ],
      },
    };
    const modelingReady = combine(gates, ['materials_ready', 'goal_confirmed', 'process_confirmed', 'knowledge_confirmed', 'modeling_complete']);
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
    const result = gates[gate];
    if (!result) {
      throw new AgentCoreError('GATE_NOT_FOUND', `未知门禁：${gate}`, [`可用门禁：${Object.keys(gates).join('、')}`], 400);
    }
    const gateLabel = GATE_LABELS[gate] || '当前条件';
    return { gate, ...result, display: { text: result.pass ? `“${gateLabel}”已通过` : `“${gateLabel}”还有 ${result.blockers.length} 项未完成` } };
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
    const ready = this.db.prepare(`SELECT status, progress_total, pass_count, id FROM eval_rounds
      WHERE project_id = ? ORDER BY created_at DESC LIMIT 1`).get(projectId) as any;
    if (!ready) return this.unavailable(
      'EVALUATION_NOT_STARTED',
      '尚未创建可运行的评测轮次，未伪造评测结果',
      ['先在评测中心配置数据源和用例，再调用 run_evaluation'],
    );
    return { roundId: ready.id, status: ready.status, total: ready.progress_total, passed: ready.pass_count,
      scope: getRound(String(ready.id))?.scope ?? { kind: 'current' },
      display: { text: ready.status === 'running' ? '评测正在后台运行' : '已读取最近一轮真实评测结果' } };
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
    if (!context.token) {
      throw new AgentCoreError('AUTH_REQUIRED', '启动评测需要当前登录凭据', ['重新登录后再运行评测'], 401);
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
        token: context.token,
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
      const round = await waitForEvaluation(context.project.id, result.roundId);
      const results = listResults(round.id);
      return {
        roundId: result.roundId,
        status: round.status,
        passed: round.passCount,
        total: round.progressTotal,
        failed: results.filter((item) => item.verdict !== 'pass').length,
        scope: {
          ...(source ? {
            kind: 'fusion_source', sourceId: source.id, sourceName: source.name,
          } : { kind: 'current' }),
          ...(caseIds.length ? { caseIds } : { all: true }),
        },
        display: {
          text: round.status === 'done'
            ? `评测完成：${round.passCount}/${round.progressTotal} 通过`
            : '评测运行失败，请先查看运行结果和失败原因',
        },
      };
    } catch (error) {
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
      return item as Record<string, unknown>;
    });
    const source = (await this.readFile(workspace, { path: script })).content;
    const result = await runScriptCasesInJiuwenbox({
        runId: workspace.run_id,
        source,
        cases,
      }).catch((error: unknown) => {
      throw new AgentCoreError(
        'SANDBOX_UNAVAILABLE',
        '脚本隔离环境暂时不可用，当前测试没有执行',
        ['稍后从当前进展重试；不要改用不隔离的环境执行'],
        503,
        { technical: error instanceof Error ? error.message : String(error) },
      );
    });
    const sourceHash = scriptSourceHash(source);
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

  private assertScriptCandidatesTested(
    workspace: WorkspaceRow,
    changes: Array<{ path: string; kind: 'write' | 'delete'; content: string | Uint8Array | null }>,
  ) {
    const scriptWrites = changes.filter((change) => (
      change.kind === 'write' && publicPath(change.path).startsWith('scripts/')
    ));
    if (!scriptWrites.length) return;
    const executableWrites = scriptWrites.filter((change) => publicPath(change.path).endsWith('.py'));
    const missing = executableWrites.filter((change) => {
      const path = publicPath(change.path);
      const sourceHash = scriptSourceHash(change.content ?? '');
      return !this.db.prepare(`SELECT 1 FROM agent_script_test_receipts
        WHERE project_id = ? AND run_id = ? AND script_path = ? AND source_hash = ?`)
        .get(workspace.project_id, workspace.run_id, path, sourceHash);
    }).map((change) => publicPath(change.path));
    if (!executableWrites.length || missing.length) {
      const blockers = missing.length ? missing : scriptWrites.map((change) => publicPath(change.path));
      throw new AgentCoreError(
        'SCRIPT_TEST_REQUIRED',
        '脚本候选尚未在隔离环境通过当前版本测试',
        ['隔离环境恢复后重新运行脚本测试，再发起确认'],
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
        '评测库尚未配置，未执行查询',
        ['配置 EVAL_DB_URL 并导入项目评测数据后重试'],
        409,
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
  const path = `package/${change.path}`;
  return change.kind === 'delete'
    ? { kind: 'delete', path }
    : { kind: 'write', path, content: change.content ?? '' };
}

function publicPath(path: string): string {
  return path.startsWith('package/') ? path.slice('package/'.length) : path;
}

function candidateDiff(base: Record<string, string>, changes: ChangeRow[]): ArtifactFileDiff[] {
  const encoder = new TextEncoder();
  const result: ArtifactFileDiff[] = [];
  for (const change of changes) {
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
  for (const path of Object.keys(files).sort()) {
    const content = files[path];
    hash.update('\0').update(String(Buffer.byteLength(path))).update(':').update(path);
    hash.update('\0').update(String(Buffer.byteLength(content))).update(':').update(content);
  }
  return `sha256:${hash.digest('hex')}`;
}

function scriptSourceHash(source: string | Uint8Array): string {
  return `sha256:${createHash('sha256').update('agent-script-test-v1\0').update(source).digest('hex')}`;
}

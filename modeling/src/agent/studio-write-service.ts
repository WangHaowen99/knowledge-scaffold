import { createHash } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import type { PackageArtifactStore } from '../artifacts/package-store.js';
import { getPackageArtifactStore } from '../artifacts/runtime.js';
import { getDb, nowIso } from '../db/index.js';
import { listDocuments } from '../documents/repo.js';
import type {
  PackageSpec,
  Project,
  SpecMetric,
  SpecObject,
  SpecOperation,
  SpecRelation,
  SpecRule,
} from '../types.js';
import { validateKey } from '../workflow/keys.js';
import {
  applyValidationIssues,
  introducedSpecIntegrityProblems,
  listSpecIntegrityProblems,
  normalizeObjectFieldsTable,
  normalizeStudioRelations,
  toSpecObjectRelations,
  validateOntologyRelationTargets,
  type NormalizedOntologyRelation,
  type OntologyStudioKind,
} from '../workflow/ontology-model-validation.js';
import { parseRenderedPackageFiles } from '../workflow/package-import.js';
import { renderPackage } from '../workflow/package-render.js';
import {
  STUDIO_SCHEMA,
  StudioReadService,
  type StudioElement,
  type StudioKind,
} from './studio-read-service.js';

const WORKSPACE_LEASE_MS = 30 * 60 * 1000;
const MAX_BATCH_ACTIONS = 100;
const MAX_TEXT_LENGTH = 50_000;
const MAX_LIST_ITEMS = 500;
const MAX_VALIDATION_WARNINGS = 20;
const MUTATION_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/u;
const KINDS = new Set<StudioKind>(['object', 'operation', 'rule', 'metric']);
const STUDIO_CHANGE_REASON = '由知识包 Studio 形成的待确认修改';
const INTERNAL_SPEC_CHANGE_PATH = '.modeling/__compiled-package-spec.json';
const COMPILED_RESOURCES_GROUP = 'compiled_resources';

interface WorkspaceRow {
  project_id: string;
  run_id: string;
  owner: string;
  base_revision_oid: string;
  candidate_id: string | null;
  lease_expires_at: string;
}

interface ChangeRow {
  path: string;
  kind: 'write' | 'delete';
  content: string | null;
}

interface MutationEnvelope {
  expectedRevision: string;
  clientMutationId: string;
  editSessionId?: string;
}

interface CreateAction {
  kind: StudioKind;
  key: string;
  values: Record<string, unknown>;
  path: string;
}

interface PatchAction {
  elementId: string;
  changes: Record<string, unknown>;
  path: string;
}

interface MutationPlan {
  envelope: MutationEnvelope;
  creates: CreateAction[];
  patches: PatchAction[];
  deletes: string[];
  deletePreviewToken?: string;
  cleanupReferences: boolean;
  isBatch: boolean;
}

interface MutationOptions {
  allowObjectFieldWrite?: boolean;
  allowEvidenceWrite?: boolean;
}

interface PreparedCreate extends CreateAction {
  normalizedValues: Record<string, unknown>;
}

interface PreparedPatch extends PatchAction {
  kind: StudioKind;
  key: string;
  normalizedChanges: Record<string, unknown>;
}

export interface StudioMutationResponse {
  workingRevision: string;
  clientMutationId: string;
  editSessionId?: string;
  candidate: true;
  replayed?: boolean;
  changeGroup: {
    id: string;
    tier: 'caliber' | 'structural';
    title: string;
    paths: string[];
  } | null;
  changedPaths: string[];
  element?: StudioElement;
  elements: StudioElement[];
  deletedElementIds?: string[];
  display: { text: string };
  warnings: string[];
  historicalProblemCount: number;
}

export class StudioWriteError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly statusCode: number,
    public readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = 'StudioWriteError';
  }
}

export class StudioWriteService {
  private readonly queues = new Map<string, Promise<void>>();

  constructor(
    private readonly db: DatabaseSync = getDb(),
    private readonly artifacts: PackageArtifactStore = getPackageArtifactStore(),
  ) {}

  leaseStatus(projectId: string, username: string): {
    mode: 'write' | 'readonly' | 'takeover' | 'review';
    owner?: string;
    expiresAt?: string;
  } {
    const workspace = this.workspace(projectId);
    if (workspace?.candidate_id) {
      return {
        mode: 'review',
        owner: workspace.owner,
        expiresAt: workspace.lease_expires_at,
      };
    }
    if (!workspace || workspace.owner === username) return { mode: 'write' };
    const expired = Date.parse(workspace.lease_expires_at) <= Date.now();
    return {
      mode: expired ? 'takeover' : 'readonly',
      owner: workspace.owner,
      expiresAt: workspace.lease_expires_at,
    };
  }

  create(project: Project, username: string, rawBody: unknown): Promise<StudioMutationResponse> {
    return this.enqueue(project.id, () => this.mutate(
      project,
      username,
      parseCreatePlan(rawBody),
      requestHash('create', rawBody),
    ));
  }

  createObjectModel(project: Project, username: string, rawBody: unknown): Promise<StudioMutationResponse> {
    return this.enqueue(project.id, () => this.mutate(
      project,
      username,
      parseCreatePlan(rawBody),
      requestHash('object-model-create', rawBody),
      { allowObjectFieldWrite: true },
    ));
  }

  createDomainModel(
    project: Project,
    username: string,
    rawBody: unknown,
    idempotencyRequestHash: string,
    options: MutationOptions = {},
  ): Promise<StudioMutationResponse> {
    return this.enqueue(project.id, () => this.mutate(
      project,
      username,
      parseCreatePlan(rawBody),
      idempotencyRequestHash,
      { ...options, allowEvidenceWrite: true },
    ));
  }

  patch(
    project: Project,
    username: string,
    elementId: string,
    rawBody: unknown,
  ): Promise<StudioMutationResponse> {
    return this.enqueue(project.id, () => this.mutate(
      project,
      username,
      parsePatchPlan(elementId, rawBody),
      requestHash(`patch:${elementId}`, rawBody),
      {},
    ));
  }

  patchObjectModel(
    project: Project,
    username: string,
    elementId: string,
    rawBody: unknown,
  ): Promise<StudioMutationResponse> {
    return this.enqueue(project.id, () => this.mutate(
      project,
      username,
      parsePatchPlan(elementId, rawBody),
      requestHash(`object-model-patch:${elementId}`, rawBody),
      { allowObjectFieldWrite: true },
    ));
  }

  patchDomainModel(
    project: Project,
    username: string,
    elementId: string,
    rawBody: unknown,
    idempotencyRequestHash?: string,
    options: MutationOptions = {},
  ): Promise<StudioMutationResponse> {
    return this.enqueue(project.id, () => this.mutate(
      project,
      username,
      parsePatchPlan(elementId, rawBody),
      idempotencyRequestHash ?? requestHash(`domain-model-patch:${elementId}`, rawBody),
      { ...options, allowEvidenceWrite: true },
    ));
  }

  deleteDomainModel(
    project: Project,
    username: string,
    rawBody: unknown,
    idempotencyRequestHash: string,
  ): Promise<StudioMutationResponse> {
    return this.enqueue(project.id, () => this.mutate(
      project,
      username,
      parseDeletePlan(rawBody),
      idempotencyRequestHash,
    ));
  }

  replayDomainMutation(
    projectId: string,
    clientMutationId: string,
    idempotencyRequestHash: string,
  ): StudioMutationResponse | null {
    const receipt = this.db.prepare(`SELECT request_hash, response_json
      FROM studio_mutation_receipts WHERE project_id = ? AND client_mutation_id = ?`)
      .get(projectId, clientMutationId) as { request_hash: string; response_json: string } | undefined;
    if (!receipt) return null;
    if (receipt.request_hash !== idempotencyRequestHash) {
      throw new StudioWriteError(
        'MUTATION_ID_REUSED',
        '这个操作标识已经用于另一项修改，请生成新的操作标识后重试',
        409,
        { clientMutationId },
      );
    }
    return { ...(JSON.parse(receipt.response_json) as StudioMutationResponse), replayed: true };
  }

  batch(project: Project, username: string, rawBody: unknown): Promise<StudioMutationResponse> {
    return this.enqueue(project.id, () => this.mutate(
      project,
      username,
      parseBatchPlan(rawBody),
      requestHash('batch', rawBody),
    ));
  }

  previewDelete(project: Project, username: string, rawBody: unknown): Promise<StudioDeletePreview> {
    return this.enqueue(project.id, async () => {
      const body = requireBody(rawBody);
      const expectedRevision = String(body.expectedRevision ?? '').trim();
      const elementIds = parseDeleteIds(body.elementIds);
      const context = await this.workingContext(project, username);
      if (expectedRevision !== context.workingRevision) {
        throw revisionConflict(expectedRevision, context.workingRevision);
      }
      const spec = parseWorkingSpec(context.files);
      assertDeleteElementsExist(spec, elementIds);
      const references = incomingReferences(spec, new Set(elementIds));
      return {
        workingRevision: context.workingRevision,
        elementIds,
        references,
        affectedElementIds: [...new Set(references.map((item) => item.sourceId))],
        previewToken: deletePreviewToken(project.id, context.workingRevision, elementIds, references),
        cleanupRequired: references.length > 0,
        display: {
          text: references.length
            ? `将删除 ${elementIds.length} 个条目，并清理 ${references.length} 处关系引用`
            : `将删除 ${elementIds.length} 个条目，没有其他条目引用它们`,
        },
      };
    });
  }

  delete(project: Project, username: string, rawBody: unknown, elementId = ''): Promise<StudioMutationResponse> {
    return this.enqueue(project.id, () => this.mutate(
      project,
      username,
      parseDeletePlan(rawBody, elementId),
      requestHash(elementId ? `delete:${elementId}` : 'delete-batch', rawBody),
    ));
  }

  private enqueue<T>(projectId: string, task: () => Promise<T>): Promise<T> {
    const previous = this.queues.get(projectId) ?? Promise.resolve();
    let release!: () => void;
    const current = new Promise<void>((resolve) => { release = resolve; });
    const queued = previous.then(() => current);
    this.queues.set(projectId, queued);
    return previous.then(task).finally(() => {
      release();
      if (this.queues.get(projectId) === queued) this.queues.delete(projectId);
    });
  }

  private async mutate(
    project: Project,
    username: string,
    plan: MutationPlan,
    hash: string,
    options: MutationOptions = {},
  ): Promise<StudioMutationResponse> {
    await this.artifacts.ensureProject(project.id);
    let current = await this.artifacts.current(project.id);
    if (!current) {
      current = await this.artifacts.initializeDraftPackage(project.id, {
        id: `project-${project.id.replace(/^pj_/, '')}`,
        name: project.name,
        description: project.background || `${project.name}知识包草稿`,
      });
    }
    const baseRevision = current.revision.oid;
    const baseFiles = (await this.artifacts.readPackageFiles(project.id, baseRevision)).files;

    this.db.exec('BEGIN IMMEDIATE');
    try {
      const receipt = this.db.prepare(`SELECT request_hash, response_json
        FROM studio_mutation_receipts
        WHERE project_id = ? AND client_mutation_id = ?`)
        .get(project.id, plan.envelope.clientMutationId) as {
          request_hash: string;
          response_json: string;
        } | undefined;
      if (receipt) {
        if (receipt.request_hash !== hash) {
          throw new StudioWriteError(
            'MUTATION_ID_REUSED',
            '这个操作标识已经用于另一项修改，请生成新的操作标识后重试',
            409,
            { clientMutationId: plan.envelope.clientMutationId },
          );
        }
        const replayed = JSON.parse(receipt.response_json) as StudioMutationResponse;
        this.db.exec('COMMIT');
        return { ...replayed, replayed: true };
      }

      let workspace = this.workspace(project.id);
      if (workspace?.owner !== undefined && workspace.owner !== username) {
        if (Date.parse(workspace.lease_expires_at) > Date.now()) {
          throw new StudioWriteError(
            'STUDIO_WRITE_LOCKED',
            `${workspace.owner} 正在建模，当前以只读方式打开`,
            409,
            { owner: workspace.owner, expiresAt: workspace.lease_expires_at },
          );
        }
        this.db.prepare(`UPDATE agent_candidate_workspaces SET
          owner = ?, credential_hash = NULL, credential_expires_at = NULL,
          lease_expires_at = ?, updated_at = ? WHERE project_id = ?`)
          .run(username, leaseExpiry(), nowIso(), project.id);
        workspace = this.workspace(project.id);
      }
      if (workspace && workspace.base_revision_oid !== baseRevision) {
        throw revisionConflict(plan.envelope.expectedRevision, baseRevision);
      }
      if (workspace?.candidate_id) {
        throw new StudioWriteError(
          'STUDIO_WRITE_LOCKED',
          '当前修改已经进入确认，完成或退回确认后才能继续编辑',
          409,
          { candidateRevision: workspace.candidate_id },
        );
      }

      const existingChanges = workspace ? this.changes(workspace) : [];
      const workingFiles = applyChanges(baseFiles, existingChanges);
      const actualRevision = existingChanges.length
        ? workingContentRevision(baseRevision, workingFiles)
        : baseRevision;
      if (plan.envelope.expectedRevision !== actualRevision) {
        throw revisionConflict(plan.envelope.expectedRevision, actualRevision);
      }

      const mutation = applyMutation(workingFiles, plan, project.id, options);
      const changedPaths = [...new Set([...Object.keys(workingFiles), ...Object.keys(mutation.files)])]
        .filter((path) => mutation.files[path] !== workingFiles[path])
        .sort();
      if (!changedPaths.length) {
        throw new StudioWriteError('NO_CHANGES', '提交内容与当前待确认内容一致', 400);
      }

      if (!workspace) {
        const createdAt = nowIso();
        const runId = studioRunId(project.id, username);
        this.db.prepare(`INSERT INTO agent_candidate_workspaces (
          project_id, run_id, owner, base_revision_oid, candidate_id,
          lease_expires_at, created_at, updated_at
        ) VALUES (?,?,?,?,NULL,?,?,?)`).run(
          project.id,
          runId,
          username,
          baseRevision,
          leaseExpiry(),
          createdAt,
          createdAt,
        );
        workspace = this.workspace(project.id)!;
      } else {
        this.db.prepare(`UPDATE agent_candidate_workspaces
          SET lease_expires_at = ?, updated_at = ? WHERE project_id = ? AND run_id = ?`)
          .run(leaseExpiry(), nowIso(), workspace.project_id, workspace.run_id);
      }

      const groupId = studioGroupId(plan.envelope.editSessionId ?? plan.envelope.clientMutationId);
      const stagedPaths: string[] = [];
      for (const path of changedPaths) {
        const content = mutation.files[path];
        if (content === baseFiles[path]) {
          this.db.prepare(`DELETE FROM agent_candidate_changes
            WHERE project_id = ? AND run_id = ? AND path = ?`)
            .run(project.id, workspace.run_id, path);
          continue;
        }
        const deleted = !(path in mutation.files);
        const pathGroupId = path === INTERNAL_SPEC_CHANGE_PATH ? COMPILED_RESOURCES_GROUP : groupId;
        this.db.prepare(`INSERT INTO agent_candidate_changes (
          project_id, run_id, path, kind, content, group_id, evidence_json, reason, updated_at
        ) VALUES (?,?,?,?,?,?,'[]',?,?)
        ON CONFLICT(project_id, run_id, path) DO UPDATE SET
          kind = excluded.kind,
          content = excluded.content,
          group_id = excluded.group_id,
          evidence_json = excluded.evidence_json,
          reason = excluded.reason,
          updated_at = excluded.updated_at`)
          .run(
            project.id,
            workspace.run_id,
            path,
            deleted ? 'delete' : 'write',
            deleted ? null : content,
            pathGroupId,
            STUDIO_CHANGE_REASON,
            nowIso(),
          );
        stagedPaths.push(path);
      }

      const finalChanges = this.changes(workspace);
      const nextFiles = applyChanges(baseFiles, finalChanges);
      const workingRevision = finalChanges.length
        ? workingContentRevision(baseRevision, nextFiles)
        : baseRevision;
      const reader = new StudioReadService({
        workingRevision,
        baseRevision,
        candidateRevision: null,
        files: nextFiles,
      });
      const elements = mutation.elementIds.map((id) => reader.detail(id).element);
      const uniqueElements = [...new Map(elements.map((element) => [element.id, element])).values()];
      const noun = plan.deletes.length
        ? `${plan.deletes.length} 个删除`
        : plan.creates.length && plan.patches.length
        ? `${plan.creates.length} 个新增和 ${plan.patches.length} 个编辑`
        : plan.creates.length
          ? `${plan.creates.length} 个新增`
          : `${plan.patches.length} 个编辑`;
      const historicalProblemNote = mutation.historicalProblemCount
        ? `；知识包仍有 ${mutation.historicalProblemCount} 项历史问题，本次未增加历史问题`
        : '';
      const publicChangedPaths = changedPaths.filter((path) => path !== INTERNAL_SPEC_CHANGE_PATH);
      const publicStagedPaths = stagedPaths.filter((path) => path !== INTERNAL_SPEC_CHANGE_PATH);
      const response: StudioMutationResponse = {
        workingRevision,
        clientMutationId: plan.envelope.clientMutationId,
        ...(plan.envelope.editSessionId ? { editSessionId: plan.envelope.editSessionId } : {}),
        candidate: true,
        changeGroup: publicStagedPaths.length ? {
          id: groupId,
          tier: studioGroupTier(publicStagedPaths),
          title: `确认 Studio 的${noun}`,
          paths: publicStagedPaths,
        } : null,
        changedPaths: publicChangedPaths,
        ...(uniqueElements.length === 1 ? { element: uniqueElements[0] } : {}),
        elements: uniqueElements,
        ...(mutation.deletedElementIds.length
          ? { deletedElementIds: mutation.deletedElementIds }
          : {}),
        display: {
          text: publicStagedPaths.length
            ? `已形成${noun}的待确认修改，确认后才会保存为新版本${historicalProblemNote}`
            : `已撤销对应的待确认修改，当前版本尚未改变${historicalProblemNote}`,
        },
        warnings: mutation.warnings,
        historicalProblemCount: mutation.historicalProblemCount,
      };
      this.db.prepare(`INSERT INTO studio_mutation_receipts (
        project_id, client_mutation_id, request_hash, response_json, created_at
      ) VALUES (?,?,?,?,?)`).run(
        project.id,
        plan.envelope.clientMutationId,
        hash,
        JSON.stringify(response),
        nowIso(),
      );
      this.db.exec('COMMIT');
      return response;
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
  }

  private workspace(projectId: string): WorkspaceRow | null {
    return (this.db.prepare(`SELECT project_id, run_id, owner, base_revision_oid,
      candidate_id, lease_expires_at FROM agent_candidate_workspaces WHERE project_id = ?`)
      .get(projectId) as WorkspaceRow | undefined) ?? null;
  }

  private changes(workspace: WorkspaceRow): ChangeRow[] {
    return this.db.prepare(`SELECT path, kind, content FROM agent_candidate_changes
      WHERE project_id = ? AND run_id = ? ORDER BY path`)
      .all(workspace.project_id, workspace.run_id) as unknown as ChangeRow[];
  }

  private async workingContext(project: Project, username: string): Promise<{
    files: Record<string, string>;
    workingRevision: string;
  }> {
    await this.artifacts.ensureProject(project.id);
    let current = await this.artifacts.current(project.id);
    if (!current) {
      current = await this.artifacts.initializeDraftPackage(project.id, {
        id: `project-${project.id.replace(/^pj_/, '')}`,
        name: project.name,
        description: project.background || `${project.name}知识包草稿`,
      });
    }
    const baseRevision = current.revision.oid;
    const baseFiles = (await this.artifacts.readPackageFiles(project.id, baseRevision)).files;
    const workspace = this.workspace(project.id);
    if (workspace && workspace.owner !== username) {
      if (Date.parse(workspace.lease_expires_at) > Date.now()) {
        throw new StudioWriteError(
          'STUDIO_WRITE_LOCKED',
          `${workspace.owner} 正在建模，当前以只读方式打开`,
          409,
          { owner: workspace.owner, expiresAt: workspace.lease_expires_at },
        );
      }
    }
    if (workspace && workspace.base_revision_oid !== baseRevision) {
      throw revisionConflict(baseRevision, baseRevision);
    }
    const changes = workspace ? this.changes(workspace) : [];
    const files = applyChanges(baseFiles, changes);
    return {
      files,
      workingRevision: changes.length ? workingContentRevision(baseRevision, files) : baseRevision,
    };
  }
}

export interface StudioDeleteReference {
  sourceId: string;
  sourceTitle: string;
  field: string;
  targetId: string;
}

export interface StudioDeletePreview {
  workingRevision: string;
  elementIds: string[];
  references: StudioDeleteReference[];
  affectedElementIds: string[];
  previewToken: string;
  cleanupRequired: boolean;
  display: { text: string };
}

function parseCreatePlan(rawBody: unknown): MutationPlan {
  const body = requireBody(rawBody);
  return {
    envelope: parseEnvelope(body),
    creates: [parseCreate(body, '')],
    patches: [],
    deletes: [],
    cleanupReferences: false,
    isBatch: false,
  };
}

function parsePatchPlan(elementId: string, rawBody: unknown): MutationPlan {
  const body = requireBody(rawBody);
  return {
    envelope: parseEnvelope(body),
    creates: [],
    patches: [parsePatch({ elementId, changes: body.changes }, '')],
    deletes: [],
    cleanupReferences: false,
    isBatch: false,
  };
}

function parseBatchPlan(rawBody: unknown): MutationPlan {
  const body = requireBody(rawBody);
  const errors: Record<string, string> = {};
  const createsValue = body.creates === undefined ? [] : body.creates;
  const patchesValue = body.patches === undefined ? [] : body.patches;
  if (!Array.isArray(createsValue)) errors.creates = 'creates 必须是数组';
  if (!Array.isArray(patchesValue)) errors.patches = 'patches 必须是数组';
  if (Object.keys(errors).length) throw fieldError(errors);
  const rawCreates = createsValue as unknown[];
  const rawPatches = patchesValue as unknown[];
  const count = rawCreates.length + rawPatches.length;
  if (count < 1 || count > MAX_BATCH_ACTIONS) {
    throw new StudioWriteError(
      'INVALID_STUDIO_INPUT',
      `批量操作数量必须是 1 到 ${MAX_BATCH_ACTIONS}`,
      400,
      { limit: MAX_BATCH_ACTIONS },
    );
  }
  const creates: CreateAction[] = [];
  const patches: PatchAction[] = [];
  rawCreates.forEach((item, index) => {
    try {
      creates.push(parseCreate(requireRecord(item, `creates[${index}]`), `creates[${index}].`));
    } catch (error) {
      mergeFieldErrors(errors, error);
    }
  });
  rawPatches.forEach((item, index) => {
    try {
      patches.push(parsePatch(requireRecord(item, `patches[${index}]`), `patches[${index}].`));
    } catch (error) {
      mergeFieldErrors(errors, error);
    }
  });
  if (Object.keys(errors).length) throw fieldError(errors);
  return {
    envelope: parseEnvelope(body), creates, patches, deletes: [],
    cleanupReferences: false, isBatch: true,
  };
}

function parseDeletePlan(rawBody: unknown, forcedElementId = ''): MutationPlan {
  const body = requireBody(rawBody);
  const deletes = parseDeleteIds(forcedElementId ? [forcedElementId] : body.elementIds);
  const previewToken = String(body.previewToken ?? '').trim();
  if (!previewToken) throw fieldError({ previewToken: '请先预览删除影响' });
  if (body.cleanupReferences !== true) {
    throw fieldError({ cleanupReferences: '请确认同时清理这些关系引用' });
  }
  return {
    envelope: parseEnvelope(body),
    creates: [],
    patches: [],
    deletes,
    deletePreviewToken: previewToken,
    cleanupReferences: true,
    isBatch: deletes.length > 1,
  };
}

function parseDeleteIds(value: unknown): string[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > MAX_BATCH_ACTIONS) {
    throw fieldError({ elementIds: `请选择 1 到 ${MAX_BATCH_ACTIONS} 个知识条目` });
  }
  const ids = value.map((item) => String(item ?? '').trim());
  const errors: Record<string, string> = {};
  ids.forEach((id, index) => {
    if (!parseElementId(id)) errors[`elementIds[${index}]`] = '知识条目标识无效';
    if (ids.indexOf(id) !== index) errors[`elementIds[${index}]`] = '不能重复选择同一条目';
  });
  if (Object.keys(errors).length) throw fieldError(errors);
  return ids;
}

function requireBody(raw: unknown): Record<string, unknown> {
  if (!isRecord(raw)) {
    throw new StudioWriteError('INVALID_STUDIO_INPUT', '请求体必须是 JSON 对象', 400);
  }
  return raw;
}

function requireRecord(raw: unknown, path: string): Record<string, unknown> {
  if (!isRecord(raw)) throw fieldError({ [path]: '必须是对象' });
  return raw;
}

function parseEnvelope(body: Record<string, unknown>): MutationEnvelope {
  const expectedRevision = String(body.expectedRevision ?? '').trim();
  const clientMutationId = String(body.clientMutationId ?? '').trim();
  const editSessionId = String(body.editSessionId ?? '').trim();
  const errors: Record<string, string> = {};
  if (!expectedRevision || expectedRevision.length > 200) {
    errors.expectedRevision = 'expectedRevision 不能为空且不能超过 200 字符';
  }
  if (!MUTATION_ID_PATTERN.test(clientMutationId)) {
    errors.clientMutationId = 'clientMutationId 必须是 1 到 128 位字母、数字或 . _ : -';
  }
  if (editSessionId && !MUTATION_ID_PATTERN.test(editSessionId)) {
    errors.editSessionId = 'editSessionId 必须是 1 到 128 位字母、数字或 . _ : -';
  }
  if (Object.keys(errors).length) throw fieldError(errors);
  return { expectedRevision, clientMutationId, ...(editSessionId ? { editSessionId } : {}) };
}

function parseCreate(body: Record<string, unknown>, prefix: string): CreateAction {
  const errors: Record<string, string> = {};
  const rawKind = String(body.kind ?? '').trim();
  if (!KINDS.has(rawKind as StudioKind)) errors[`${prefix}kind`] = '不支持的知识类型';
  let key = '';
  try {
    key = validateKey(String(body.key ?? ''), '知识条目');
  } catch (error) {
    errors[`${prefix}key`] = errorMessage(error);
  }
  if (body.values !== undefined && !isRecord(body.values)) {
    errors[`${prefix}values`] = 'values 必须是对象';
  }
  if (Object.keys(errors).length) throw fieldError(errors);
  return {
    kind: rawKind as StudioKind,
    key,
    values: (body.values as Record<string, unknown> | undefined) ?? {},
    path: prefix.slice(0, -1),
  };
}

function parsePatch(body: Record<string, unknown>, prefix: string): PatchAction {
  const elementId = String(body.elementId ?? '').trim();
  const errors: Record<string, string> = {};
  if (!parseElementId(elementId)) errors[`${prefix}elementId`] = 'elementId 必须是 kind:key';
  if (!isRecord(body.changes)) errors[`${prefix}changes`] = 'changes 必须是对象';
  else if (!Object.keys(body.changes).length) errors[`${prefix}changes`] = '至少提供一个修改字段';
  if (Object.keys(errors).length) throw fieldError(errors);
  return {
    elementId,
    changes: body.changes as Record<string, unknown>,
    path: prefix.slice(0, -1),
  };
}

function applyMutation(
  workingFiles: Record<string, string>,
  plan: MutationPlan,
  projectId: string,
  options: MutationOptions = {},
): {
  files: Record<string, string>;
  elementIds: string[];
  warnings: string[];
  historicalProblemCount: number;
  deletedElementIds: string[];
} {
  let spec: PackageSpec;
  try {
    spec = structuredClone(parseRenderedPackageFiles(workingFiles));
  } catch (error) {
    throw new StudioWriteError(
      'INVALID_WORKING_PACKAGE',
      `当前工作修订无法解析：${errorMessage(error)}`,
      409,
    );
  }
  const previousIntegrityProblems = listSpecIntegrityProblems(spec);

  const errors: Record<string, string> = {};
  const deletedPaths: string[] = [];
  if (plan.deletes.length) {
    assertDeleteElementsExist(spec, plan.deletes);
    const references = incomingReferences(spec, new Set(plan.deletes));
    const expectedToken = deletePreviewToken(projectId, plan.envelope.expectedRevision, plan.deletes, references);
    if (plan.deletePreviewToken !== expectedToken) {
      throw new StudioWriteError(
        'DELETE_PREVIEW_STALE',
        '删除影响已经变化，请重新预览后确认',
        409,
      );
    }
    if (references.length && !plan.cleanupReferences) {
      throw fieldError({ cleanupReferences: '这些条目仍被引用，请确认同时清理关系' });
    }
    const beforeReader = new StudioReadService({
      workingRevision: plan.envelope.expectedRevision,
      baseRevision: plan.envelope.expectedRevision,
      candidateRevision: null,
      files: workingFiles,
    });
    deletedPaths.push(...plan.deletes.map((id) => beforeReader.detail(id).element.sourcePath));
    removeIncomingReferences(spec, new Set(plan.deletes));
    for (const id of plan.deletes) {
      const parsed = parseElementId(id)!;
      const items = collection(spec, parsed.kind) as unknown as Array<{ key: string }>;
      const index = items.findIndex((item) => item.key === parsed.key);
      items.splice(index, 1);
    }
  }
  const existingIds = specElementIds(spec);
  const createIds = new Set<string>();
  const preparedCreates: PreparedCreate[] = [];
  for (const action of plan.creates) {
    const id = elementId(action.kind, action.key);
    if (!plan.isBatch && existingIds.has(id)) {
      throw new StudioWriteError(
        'ELEMENT_EXISTS',
        '同类型的知识条目 key 已存在',
        409,
        { elementId: id },
      );
    }
    if (existingIds.has(id) || createIds.has(id)) {
      errors[fieldPath(action.path, 'key')] = '同类型的 key 已存在';
    } else {
      createIds.add(id);
    }
    const valuesPath = fieldPath(action.path, 'values');
    const normalizedValues = normalizeValues(
      action.kind,
      action.values,
      valuesPath,
      errors,
      { allowEvidence: true, projectId, allowObjectFieldWrite: options.allowObjectFieldWrite === true },
    );
    const requiredEvidence = schemaFor(action.kind).fields.some((field) => (
      field.type === 'evidence_list' && field.required
    ));
    if (requiredEvidence && !Object.hasOwn(normalizedValues, 'evidence')) {
      errors[`${valuesPath}.evidence`] = '至少选择一份项目资料并填写原文位置';
    }
    preparedCreates.push({ ...action, normalizedValues });
  }

  const allIds = new Set([...existingIds, ...createIds]);
  const patchedIds = new Set<string>();
  const preparedPatches: PreparedPatch[] = [];
  const patchKinds = new Set<StudioKind>();
  for (const action of plan.patches) {
    const parsed = parseElementId(action.elementId)!;
    if (!plan.isBatch && !allIds.has(action.elementId)) {
      throw new StudioWriteError(
        'ELEMENT_NOT_FOUND',
        '知识条目不存在',
        404,
        { elementId: action.elementId },
      );
    }
    if (!allIds.has(action.elementId)) {
      errors[fieldPath(action.path, 'elementId')] = '知识条目不存在';
    }
    if (patchedIds.has(action.elementId)) {
      errors[fieldPath(action.path, 'elementId')] = '同一批操作不能重复编辑同一条目';
    }
    patchedIds.add(action.elementId);
    patchKinds.add(parsed.kind);
    const normalizedChanges = normalizeValues(
      parsed.kind,
      action.changes,
      fieldPath(action.path, 'changes'),
      errors,
      {
        allowEvidence: options.allowEvidenceWrite === true,
        projectId,
        allowObjectFieldWrite: options.allowObjectFieldWrite === true,
      },
    );
    preparedPatches.push({ ...action, ...parsed, normalizedChanges });
  }

  if (plan.isBatch && preparedPatches.length > 1) {
    if (patchKinds.size > 1) {
      for (const action of preparedPatches) {
        errors[fieldPath(action.path, 'elementId')] = '批量编辑只支持同一种知识类型';
      }
    } else {
      const kind = preparedPatches[0].kind;
      const allowed = new Set(schemaFor(kind).bulkEditableFields as readonly string[]);
      for (const action of preparedPatches) {
        for (const field of Object.keys(action.normalizedChanges)) {
          if (!allowed.has(field)) {
            errors[fieldPath(action.path, `changes.${field}`)] = '该字段不支持批量编辑';
          }
        }
      }
    }
  }
  if (Object.keys(errors).length) throw fieldError(errors);

  for (const action of preparedCreates) {
    const item = minimalSpec(action.kind, action.key, action.normalizedValues);
    collection(spec, action.kind).push(item as never);
  }
  for (const action of preparedPatches) {
    const items = collection(spec, action.kind) as unknown as Array<Record<string, unknown>>;
    const item = items.find((candidate) => candidate.key === action.key)!;
    applyValues(action.kind, item, action.normalizedChanges);
  }

  validateRelations(spec, errors, preparedCreates, preparedPatches);
  if (Object.keys(errors).length) throw fieldError(errors);

  const currentIntegrityProblems = listSpecIntegrityProblems(spec);
  const addedIntegrityProblems = introducedSpecIntegrityProblems(spec, previousIntegrityProblems);
  if (addedIntegrityProblems.length) {
    throw fieldError({
      _batch: `本次修改新增或恶化了知识包完整性问题：${addedIntegrityProblems.join('；')}`,
    });
  }

  let rendered: Record<string, string>;
  try {
    rendered = renderPackage(spec, { validateScripts: false });
  } catch (error) {
    throw fieldError({ _batch: errorMessage(error) });
  }
  const nextFiles = { ...workingFiles, ...rendered };
  nextFiles[INTERNAL_SPEC_CHANGE_PATH] = `${JSON.stringify(spec, null, 2)}\n`;
  for (const path of deletedPaths) delete nextFiles[path];
  return {
    files: nextFiles,
    elementIds: [
      ...preparedCreates.map((action) => elementId(action.kind, action.key)),
      ...preparedPatches.map((action) => action.elementId),
    ],
    warnings: limitValidationWarnings(currentIntegrityProblems),
    historicalProblemCount: currentIntegrityProblems.length,
    deletedElementIds: plan.deletes,
  };
}

function limitValidationWarnings(problems: string[]): string[] {
  return problems.slice(0, MAX_VALIDATION_WARNINGS);
}

function normalizeValues(
  kind: StudioKind,
  raw: Record<string, unknown>,
  path: string,
  errors: Record<string, string>,
  options: { allowEvidence: boolean; projectId: string; allowObjectFieldWrite?: boolean },
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  const schema = schemaFor(kind);
  const fields = new Map<string, { key: string; type: string; required: boolean; readOnly?: boolean }>(
    schema.fields.map((field) => [field.key, field]),
  );
  for (const [key, value] of Object.entries(raw)) {
    if (key === 'key') {
      errors[`${path}.key`] = 'key 是稳定标识，创建后不能修改';
      continue;
    }
    if (key === 'relations') {
      const normalized = normalizeStudioRelations(
        kind as OntologyStudioKind,
        value,
        `${path}.relations`,
        schemaFor(kind).legalRelations,
        { maxListItems: MAX_LIST_ITEMS },
      );
      applyValidationIssues(errors, normalized.issues);
      if (normalized.relations) result.relations = normalized.relations;
      continue;
    }
    if (kind === 'object' && key === 'fields' && options.allowObjectFieldWrite === true) {
      const normalized = normalizeObjectFieldsTable(value, `${path}.fields`, { maxListItems: MAX_LIST_ITEMS });
      applyValidationIssues(errors, normalized.issues);
      if (normalized.fields) result.fields = normalized.fields;
      continue;
    }
    const field = fields.get(key);
    if (!field) {
      errors[`${path}.${key}`] = '未知字段';
      continue;
    }
    if ('readOnly' in field && field.readOnly && !(options.allowEvidence && field.type === 'evidence_list')) {
      errors[`${path}.${key}`] = '该字段由建模流程维护，不能在 Studio 中直接编辑';
      continue;
    }
    if (field.type === 'evidence_list') {
      const normalized = normalizeCreateEvidence(value, `${path}.${key}`, errors, options.projectId);
      if (normalized !== INVALID) result[key] = normalized;
      continue;
    }
    const normalized = normalizeFieldValue(
      key,
      field.type,
      field.required,
      value,
      `${path}.${key}`,
      errors,
    );
    if (normalized !== INVALID) result[key] = normalized;
  }
  return result;
}

function normalizeCreateEvidence(
  value: unknown,
  path: string,
  errors: Record<string, string>,
  projectId: string,
): string[] | typeof INVALID {
  const normalized = normalizeStringArray(value, path, errors);
  if (normalized === INVALID) return INVALID;
  if (!normalized.length) {
    errors[path] = '至少选择一份项目资料并填写原文位置';
    return normalized;
  }
  const documents = listDocuments(projectId);
  normalized.forEach((reference, index) => {
    if (!reference.startsWith('material:')) {
      errors[`${path}[${index}]`] = 'Studio 新建知识必须引用项目资料';
      return;
    }
    const body = reference.slice('material:'.length);
    const hash = body.indexOf('#');
    const source = (hash >= 0 ? body.slice(0, hash) : body).trim();
    const anchor = (hash >= 0 ? body.slice(hash + 1) : '').trim();
    if (!source || !documents.some((document) => (
      document.id === source || document.sourceKey === source || document.filename === source
    ))) {
      errors[`${path}[${index}]`] = '所选资料不存在或已经停用，请刷新资料清单后重试';
    } else if (!anchor) {
      errors[`${path}[${index}]`] = '请填写资料中的原文位置';
    }
  });
  return normalized;
}

const INVALID = Symbol('invalid');

function normalizeFieldValue(
  field: string,
  type: string,
  required: boolean,
  value: unknown,
  path: string,
  errors: Record<string, string>,
): unknown {
  if (type === 'text' || type === 'long_text') {
    if (typeof value !== 'string') {
      errors[path] = '必须是文本';
      return INVALID;
    }
    const text = value.trim();
    if (required && !text) errors[path] = '必填文本不能为空';
    if (text.length > MAX_TEXT_LENGTH) errors[path] = `不能超过 ${MAX_TEXT_LENGTH} 字符`;
    return text;
  }
  if (type === 'text_list' || type === 'ordered_text_list') {
    const normalized = normalizeStringArray(value, path, errors);
    if (normalized !== INVALID && required && ['flow', 'boundaries'].includes(field) && !normalized.length) {
      errors[path] = '至少需要一项';
    }
    return normalized;
  }
  if (!Array.isArray(value)) {
    errors[path] = '必须是数组';
    return INVALID;
  }
  if (value.length > MAX_LIST_ITEMS) {
    errors[path] = `最多包含 ${MAX_LIST_ITEMS} 项`;
    return INVALID;
  }
  const shapes: Record<string, Record<string, 'string' | 'boolean' | 'optional-string'>> = {
    inputs: { param: 'string', cnName: 'optional-string', type: 'string', required: 'boolean', note: 'optional-string' },
    outputs: { field: 'string', cnName: 'optional-string', type: 'string', note: 'optional-string' },
    conditions: { condition: 'string', conclusion: 'string' },
    nullPolicy: { field: 'string', handling: 'string' },
    dimensions: { key: 'string', note: 'string' },
  };
  const shape = shapes[field];
  if (!shape) {
    errors[path] = '该结构字段暂不支持编辑';
    return INVALID;
  }
  const normalized: Record<string, unknown>[] = [];
  value.forEach((item, index) => {
    if (!isRecord(item)) {
      errors[`${path}[${index}]`] = '必须是对象';
      return;
    }
    const row: Record<string, unknown> = {};
    for (const [key, expected] of Object.entries(shape)) {
      const cell = item[key];
      if (expected === 'boolean') {
        if (typeof cell !== 'boolean') errors[`${path}[${index}].${key}`] = '必须是布尔值';
        else row[key] = cell;
      } else if (expected === 'string') {
        if (typeof cell !== 'string' || !cell.trim()) errors[`${path}[${index}].${key}`] = '不能为空';
        else row[key] = cell.trim();
      } else if (cell !== undefined && cell !== null && cell !== '') {
        if (typeof cell !== 'string') errors[`${path}[${index}].${key}`] = '必须是文本';
        else row[key] = cell.trim();
      }
    }
    for (const key of Object.keys(item)) {
      if (!(key in shape)) errors[`${path}[${index}].${key}`] = '未知字段';
    }
    normalized.push(row);
  });
  return normalized;
}

function normalizeStringArray(
  value: unknown,
  path: string,
  errors: Record<string, string>,
): string[] | typeof INVALID {
  if (!Array.isArray(value)) {
    errors[path] = '必须是文本数组';
    return INVALID;
  }
  if (value.length > MAX_LIST_ITEMS) errors[path] = `最多包含 ${MAX_LIST_ITEMS} 项`;
  const result: string[] = [];
  value.forEach((item, index) => {
    if (typeof item !== 'string' || !item.trim()) errors[`${path}[${index}]`] = '不能为空';
    else if (item.length > MAX_TEXT_LENGTH) errors[`${path}[${index}]`] = `不能超过 ${MAX_TEXT_LENGTH} 字符`;
    else result.push(item.trim());
  });
  return result;
}

function minimalSpec(
  kind: StudioKind,
  key: string,
  values: Record<string, unknown>,
): SpecObject | SpecOperation | SpecRule | SpecMetric {
  const title = typeof values.title === 'string' && values.title ? values.title : key;
  let item: SpecObject | SpecOperation | SpecRule | SpecMetric;
  if (kind === 'object') {
    item = { key, title, description: title, fields: [] };
  } else if (kind === 'operation') {
    item = {
      key,
      title,
      description: title,
      inputs: [],
      outputs: [],
      flow: ['待补充流程步骤'],
      relatedObjects: [],
    };
  } else if (kind === 'rule') {
    item = { key, title, description: '', content: title, evidence: [] };
  } else {
    item = {
      key,
      title,
      description: '',
      caliber: title,
      boundaries: ['待补充计算边界'],
      evidence: [],
    };
  }
  applyValues(kind, item as unknown as Record<string, unknown>, values);
  return item;
}

function applyValues(
  kind: StudioKind,
  item: Record<string, unknown>,
  values: Record<string, unknown>,
): void {
  for (const [field, value] of Object.entries(values)) {
    if (field === 'relations') {
      applyRelations(kind, item, value as NormalizedOntologyRelation[]);
    } else if (kind === 'operation' && field === 'intentExamples') {
      item.competencyQuestion = (value as string[])[0] || undefined;
    } else {
      item[field] = value;
    }
  }
}

function applyRelations(
  kind: StudioKind,
  item: Record<string, unknown>,
  relations: NormalizedOntologyRelation[],
): void {
  if (kind === 'object') {
    const existing = Array.isArray(item.relations) ? item.relations as SpecRelation[] : [];
    item.relations = toSpecObjectRelations(relations).map((relation) => {
      const preserved = existing.find((current) => (
        current.target === relation.target || `object:${current.target}` === `object:${relation.target}`
      ));
      return preserved?.evidence?.length ? { ...relation, evidence: preserved.evidence } : relation;
    });
  } else if (kind === 'operation') {
    item.relatedObjects = relationTargets(relations, 'uses_object');
    item.usesRules = relationTargets(relations, 'uses_rule');
    item.usesMetrics = relationTargets(relations, 'uses_metric');
  } else if (kind === 'rule') {
    item.appliesToObjects = relationTargets(relations, 'applies_to_object');
    item.appliesToOperations = relationTargets(relations, 'applies_to_operation');
  } else {
    item.usesObjects = relationTargets(relations, 'uses_object');
  }
}

function relationTargets(relations: NormalizedOntologyRelation[], kind: string): string[] {
  return [...new Set(relations.filter((relation) => relation.kind === kind).map((relation) => relation.targetKey))];
}

function validateRelations(
  spec: PackageSpec,
  errors: Record<string, string>,
  creates: PreparedCreate[],
  patches: PreparedPatch[],
): void {
  const pending = [];
  for (const action of [...creates, ...patches]) {
    const values = 'normalizedValues' in action ? action.normalizedValues : action.normalizedChanges;
    const relations = values.relations as NormalizedOntologyRelation[] | undefined;
    if (!relations) continue;
    const base = 'normalizedValues' in action
      ? fieldPath(action.path, 'values.relations')
      : fieldPath(action.path, 'changes.relations');
    pending.push({ path: base, relations });
  }
  applyValidationIssues(errors, validateOntologyRelationTargets(spec, pending));
}

function schemaFor(kind: StudioKind) {
  return STUDIO_SCHEMA.kinds.find((item) => item.kind === kind)!;
}

function collection(spec: PackageSpec, kind: StudioKind) {
  if (kind === 'object') return spec.objects;
  if (kind === 'operation') return spec.operations;
  if (kind === 'rule') return spec.rules ?? (spec.rules = []);
  return spec.metrics ?? (spec.metrics = []);
}

function specElementIds(spec: PackageSpec): Set<string> {
  return new Set([
    ...spec.objects.map((item) => elementId('object', item.key)),
    ...spec.operations.map((item) => elementId('operation', item.key)),
    ...(spec.rules ?? []).map((item) => elementId('rule', item.key)),
    ...(spec.metrics ?? []).map((item) => elementId('metric', item.key)),
  ]);
}

function parseWorkingSpec(files: Record<string, string>): PackageSpec {
  try {
    return structuredClone(parseRenderedPackageFiles(files));
  } catch (error) {
    throw new StudioWriteError(
      'INVALID_WORKING_PACKAGE',
      `当前工作修订无法解析：${errorMessage(error)}`,
      409,
    );
  }
}

function assertDeleteElementsExist(spec: PackageSpec, elementIds: string[]): void {
  const existing = specElementIds(spec);
  const errors: Record<string, string> = {};
  elementIds.forEach((id, index) => {
    if (!existing.has(id)) errors[`elementIds[${index}]`] = '知识条目不存在或已经删除';
  });
  if (Object.keys(errors).length) throw fieldError(errors);
}

function incomingReferences(spec: PackageSpec, deleted: Set<string>): StudioDeleteReference[] {
  const references: StudioDeleteReference[] = [];
  const add = (
    sourceKind: StudioKind,
    source: { key: string; title?: string },
    field: string,
    targetKind: StudioKind,
    targets: unknown,
  ) => {
    if (deleted.has(elementId(sourceKind, source.key)) || !Array.isArray(targets)) return;
    for (const target of targets) {
      const targetKey = typeof target === 'string'
        ? target
        : isRecord(target) ? String(target.target ?? '') : '';
      const targetId = elementId(targetKind, targetKey);
      if (targetKey && deleted.has(targetId)) {
        references.push({
          sourceId: elementId(sourceKind, source.key),
          sourceTitle: source.title || source.key,
          field,
          targetId,
        });
      }
    }
  };
  for (const item of spec.objects) add('object', item, '对象关系', 'object', item.relations);
  for (const item of spec.operations) {
    add('operation', item, '相关对象', 'object', item.relatedObjects);
    add('operation', item, '使用规则', 'rule', item.usesRules);
    add('operation', item, '使用指标', 'metric', item.usesMetrics);
  }
  for (const item of spec.rules ?? []) {
    add('rule', item, '适用对象', 'object', item.appliesToObjects);
    add('rule', item, '适用操作', 'operation', item.appliesToOperations);
  }
  for (const item of spec.metrics ?? []) add('metric', item, '使用对象', 'object', item.usesObjects);
  return references.sort((a, b) => (
    `${a.sourceId}\0${a.field}\0${a.targetId}`.localeCompare(`${b.sourceId}\0${b.field}\0${b.targetId}`)
  ));
}

function removeIncomingReferences(spec: PackageSpec, deleted: Set<string>): void {
  const kept = (kind: StudioKind, values: string[] | undefined): string[] => (
    (values ?? []).filter((key) => !deleted.has(elementId(kind, key)))
  );
  for (const item of spec.objects) {
    item.relations = (item.relations ?? []).filter((relation) => (
      !deleted.has(elementId('object', relation.target))
    ));
  }
  for (const item of spec.operations) {
    item.relatedObjects = kept('object', item.relatedObjects);
    item.usesRules = kept('rule', item.usesRules);
    item.usesMetrics = kept('metric', item.usesMetrics);
  }
  for (const item of spec.rules ?? []) {
    item.appliesToObjects = kept('object', item.appliesToObjects);
    item.appliesToOperations = kept('operation', item.appliesToOperations);
  }
  for (const item of spec.metrics ?? []) item.usesObjects = kept('object', item.usesObjects);
}

function deletePreviewToken(
  projectId: string,
  workingRevision: string,
  elementIds: string[],
  references: StudioDeleteReference[],
): string {
  return `delete_${createHash('sha256').update(stableJson({
    projectId,
    workingRevision,
    elementIds: [...elementIds].sort(),
    references,
  })).digest('hex')}`;
}

function parseElementId(value: string): { kind: StudioKind; key: string } | null {
  const separator = value.indexOf(':');
  if (separator <= 0) return null;
  const kind = value.slice(0, separator) as StudioKind;
  const key = value.slice(separator + 1);
  if (!KINDS.has(kind) || !key) return null;
  try {
    return { kind, key: validateKey(key, '知识条目') };
  } catch {
    return null;
  }
}

function elementId(kind: StudioKind, key: string): string {
  return `${kind}:${key}`;
}

function applyChanges(base: Record<string, string>, changes: ChangeRow[]): Record<string, string> {
  const files = { ...base };
  for (const change of changes) {
    if (change.kind === 'delete') delete files[change.path];
    else files[change.path] = change.content ?? '';
  }
  return files;
}

function revisionConflict(expectedRevision: string, actualRevision: string): StudioWriteError {
  return new StudioWriteError(
    'REVISION_CONFLICT',
    '知识包工作修订已经变化，请刷新后重试',
    409,
    { expectedRevision, actualRevision },
  );
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

function studioRunId(projectId: string, username: string): string {
  return `studio_${createHash('sha256').update(`${projectId}\0${username}`).digest('hex').slice(0, 24)}`;
}

function studioGroupId(seed: string): string {
  return `studio-${createHash('sha256').update(seed).digest('hex').slice(0, 24)}`;
}

function studioGroupTier(paths: string[]): 'caliber' | 'structural' {
  return paths.some((path) => path.startsWith('rules/')) ? 'caliber' : 'structural';
}

function leaseExpiry(): string {
  return new Date(Date.now() + WORKSPACE_LEASE_MS).toISOString();
}

function requestHash(operation: string, body: unknown): string {
  return createHash('sha256').update(operation).update('\0').update(stableJson(body)).digest('hex');
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function fieldError(fieldErrors: Record<string, string>): StudioWriteError {
  return new StudioWriteError(
    'STUDIO_FIELD_INVALID',
    '部分字段不符合知识包约束，请修正后重试',
    422,
    { fieldErrors },
  );
}

function mergeFieldErrors(target: Record<string, string>, error: unknown): void {
  if (error instanceof StudioWriteError && isRecord(error.details.fieldErrors)) {
    Object.assign(target, error.details.fieldErrors);
    return;
  }
  throw error;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function fieldPath(base: string, suffix: string): string {
  return base ? `${base}.${suffix}` : suffix;
}

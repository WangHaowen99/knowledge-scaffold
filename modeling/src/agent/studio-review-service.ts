import { createHash } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import type { PackageArtifactStore } from '../artifacts/package-store.js';
import { getPackageArtifactStore } from '../artifacts/runtime.js';
import { ArtifactRepositoryError } from '../artifacts/repository.js';
import { getDb, nowIso } from '../db/index.js';
import type { Project } from '../types.js';
import { runFormatChecks } from '../workflow/package-check.js';
import { parseRenderedPackageFiles } from '../workflow/package-import.js';
import {
  introducedSpecIntegrityProblems,
  listSpecIntegrityProblems,
} from '../workflow/ontology-model-validation.js';
import {
  enqueueStudioReviewProjectEvent,
  type StudioReviewProjectEvent,
} from './project-event-outbox.js';
import { StudioWriteError } from './studio-write-service.js';
import { StudioReadService } from './studio-read-service.js';

const MUTATION_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/u;
const INTERNAL_SPEC_CHANGE_PATH = '.modeling/__compiled-package-spec.json';

interface WorkspaceRow {
  project_id: string;
  run_id: string;
  owner: string;
  base_revision_oid: string;
  candidate_id: string | null;
}

interface ChangeRow {
  path: string;
  kind: 'write' | 'delete';
  content: string | null;
}

interface ReviewInput {
  expectedRevision: string;
  clientMutationId: string;
}

export interface StudioReviewResponse {
  workingRevision: string;
  clientMutationId: string;
  reviewRequested: boolean;
  mode: 'deterministic' | 'agent';
  applied: boolean;
  replayed?: boolean;
  currentRevision?: string;
  packageVersion?: string;
  impacts?: {
    changedElementIds: string[];
    affectedElementIds: string[];
    relationCount: number;
  };
  changedPaths: string[];
  elementIds: string[];
  display: { text: string };
}

interface DeterministicReviewContext {
  project: Project;
  username: string;
  input: ReviewInput;
  hash: string;
  workspace: WorkspaceRow;
  baseRevision: string;
  baseFiles: Record<string, string>;
  workingFiles: Record<string, string>;
  workingRevision: string;
  changes: ChangeRow[];
  changedPaths: string[];
  elementIds: string[];
}

export class StudioReviewService {
  constructor(
    private readonly db: DatabaseSync = getDb(),
    private readonly artifacts: PackageArtifactStore = getPackageArtifactStore(),
  ) {}

  reviewRequested(projectId: string, workingRevision: string): boolean {
    const rows = this.db.prepare(`SELECT payload_json FROM agent_project_event_outbox
      WHERE project_id = ? AND event_type = 'studio_review' ORDER BY created_at DESC`)
      .all(projectId) as Array<{ payload_json: string }>;
    return rows.some((row) => {
      try {
        return (JSON.parse(row.payload_json) as StudioReviewProjectEvent).workingRevision === workingRevision;
      } catch {
        return false;
      }
    });
  }

  async requestReview(
    project: Project,
    username: string,
    rawBody: unknown,
  ): Promise<StudioReviewResponse> {
    const input = parseReviewInput(rawBody);
    const hash = requestHash(input);
    const eventId = `studio-review:${project.id}:${input.clientMutationId}`;

    const directReceipt = this.db.prepare(`SELECT request_hash, response_json
      FROM studio_mutation_receipts WHERE project_id = ? AND client_mutation_id = ?`)
      .get(project.id, input.clientMutationId) as { request_hash: string; response_json: string } | undefined;
    if (directReceipt) {
      if (directReceipt.request_hash !== hash) {
        throw new StudioWriteError(
          'MUTATION_ID_REUSED',
          '这个复核标识已经用于另一批修改，请生成新的操作标识后重试',
          409,
          { clientMutationId: input.clientMutationId },
        );
      }
      return { ...(JSON.parse(directReceipt.response_json) as StudioReviewResponse), replayed: true };
    }

    await this.artifacts.ensureProject(project.id);
    const current = await this.artifacts.current(project.id);
    if (!current) {
      throw new StudioWriteError('NO_CANDIDATE', '当前没有待复核修改', 409);
    }
    const baseRevision = current.revision.oid;
    const baseFiles = (await this.artifacts.readPackageFiles(project.id, baseRevision)).files;

    this.db.exec('BEGIN IMMEDIATE');
    try {
      const existing = this.db.prepare(`SELECT payload_json FROM agent_project_event_outbox
        WHERE id = ? AND project_id = ? AND event_type = 'studio_review'`)
        .get(eventId, project.id) as { payload_json: string } | undefined;
      if (existing) {
        const event = JSON.parse(existing.payload_json) as StudioReviewProjectEvent;
        if (event.requestHash !== hash) {
          throw new StudioWriteError(
            'MUTATION_ID_REUSED',
            '这个复核标识已经用于另一批修改，请生成新的操作标识后重试',
            409,
            { clientMutationId: input.clientMutationId },
          );
        }
        this.db.exec('COMMIT');
        return responseFromEvent(event, true);
      }

      const workspace = this.db.prepare(`SELECT project_id, run_id, owner, base_revision_oid,
        candidate_id FROM agent_candidate_workspaces WHERE project_id = ?`)
        .get(project.id) as WorkspaceRow | undefined;
      if (!workspace) throw new StudioWriteError('NO_CANDIDATE', '当前没有待复核修改', 409);
      if (workspace.owner !== username) {
        throw new StudioWriteError(
          'STUDIO_WRITE_LOCKED',
          '当前项目已有其他用户的待确认修改，不能提交复核',
          409,
        );
      }
      if (workspace.base_revision_oid !== baseRevision) {
        throw revisionConflict(input.expectedRevision, baseRevision);
      }
      if (workspace.candidate_id) {
        throw new StudioWriteError(
          'STUDIO_WRITE_LOCKED',
          '当前修改已经进入确认，请先处理现有确认',
          409,
          { candidateRevision: workspace.candidate_id },
        );
      }
      const changes = this.db.prepare(`SELECT path, kind, content FROM agent_candidate_changes
        WHERE project_id = ? AND run_id = ? ORDER BY path`)
        .all(project.id, workspace.run_id) as unknown as ChangeRow[];
      if (!changes.length) throw new StudioWriteError('NO_CANDIDATE', '当前没有待复核修改', 409);

      const workingFiles = applyChanges(baseFiles, changes);
      const actualRevision = workingContentRevision(baseRevision, workingFiles);
      if (input.expectedRevision !== actualRevision) {
        throw revisionConflict(input.expectedRevision, actualRevision);
      }

      const changedPaths = changes.map((change) => change.path).sort();
      const elementIds = elementIdsFromPaths(changedPaths);
      if (isDeterministicStudioScope(elementIds)) {
        this.db.exec('COMMIT');
        return await this.applyDeterministicReview({
          project,
          username,
          input,
          hash,
          workspace,
          baseRevision,
          baseFiles,
          workingFiles,
          workingRevision: actualRevision,
          changes,
          changedPaths,
          elementIds,
        });
      }
      const priorEvent = this.findReviewEvent(project.id, actualRevision);
      if (priorEvent) {
        this.db.exec('COMMIT');
        return responseFromEvent(priorEvent, true);
      }
      const summary = elementIds.length
        ? `已将 ${elementIds.length} 个知识条目的待确认修改交给助手整体复核`
        : `已将 ${changedPaths.length} 个文件的待确认修改交给助手整体复核`;
      const event: StudioReviewProjectEvent = {
        eventId,
        clientMutationId: input.clientMutationId,
        requestHash: hash,
        workingRevision: actualRevision,
        changedPaths,
        elementIds,
        summary,
      };
      enqueueStudioReviewProjectEvent(project.id, event, this.db);
      this.db.exec('COMMIT');
      return responseFromEvent(event, false);
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
  }

  private async applyDeterministicReview(
    context: DeterministicReviewContext,
  ): Promise<StudioReviewResponse> {
    let candidateId = '';
    try {
      const current = await this.artifacts.current(context.project.id);
      if (!current || current.revision.oid !== context.baseRevision) {
        throw revisionConflict(context.baseRevision, current?.revision.oid ?? '');
      }
      const proposed = parseRenderedPackageFiles(context.workingFiles);
      assertNoIntroducedIntegrityErrors(current.spec, proposed);
      assertNoIntroducedFormatErrors(current.spec, context.baseFiles, proposed, context.workingFiles);
      const impacts = deterministicImpacts(
        context.baseRevision,
        context.workingRevision,
        context.workingFiles,
        context.elementIds,
        proposed,
      );
      const groupId = `studio-direct-${createHash('sha256')
        .update(context.workingRevision).digest('hex').slice(0, 16)}`;
      const candidate = await this.artifacts.createCandidateRevision(context.project.id, {
        expectedRevision: context.baseRevision,
        groups: [{
          id: groupId,
          summary: `Studio 的 ${context.elementIds.length} 个知识条目修改`,
          changes: context.changes.map((change) => change.kind === 'delete'
            ? { kind: 'delete' as const, path: `package/${change.path}` }
            : { kind: 'write' as const, path: `package/${change.path}`, content: change.content ?? '' }),
        }],
        action: {
          cause: 'studio_deterministic_candidate',
          summary: `确定性检查 Studio 的 ${context.elementIds.length} 个知识条目修改`,
          actorKind: 'user',
          actorName: context.username,
        },
      });
      candidateId = candidate.id;
      this.db.exec('BEGIN IMMEDIATE');
      try {
        const latestChanges = this.db.prepare(`SELECT path, kind, content FROM agent_candidate_changes
          WHERE project_id = ? AND run_id = ? ORDER BY path`)
          .all(context.project.id, context.workspace.run_id) as unknown as ChangeRow[];
        const latestWorkingRevision = workingContentRevision(
          context.baseRevision,
          applyChanges(context.baseFiles, latestChanges),
        );
        if (latestWorkingRevision !== context.workingRevision) {
          throw revisionConflict(context.workingRevision, latestWorkingRevision);
        }
        const locked = this.db.prepare(`UPDATE agent_candidate_workspaces
          SET candidate_id = ?, updated_at = ?
          WHERE project_id = ? AND run_id = ? AND base_revision_oid = ? AND candidate_id IS NULL`)
          .run(candidateId, nowIso(), context.project.id, context.workspace.run_id, context.baseRevision);
        if (Number(locked.changes) !== 1) {
          throw new StudioWriteError('STUDIO_WRITE_LOCKED', '当前修改正在处理，请稍后重试', 409);
        }
        this.db.exec('COMMIT');
      } catch (error) {
        if (this.db.isTransaction) this.db.exec('ROLLBACK');
        throw error;
      }
      const accepted = await this.artifacts.acceptCandidateRevision(context.project.id, {
        candidateId,
        selectedGroupIds: [groupId],
        note: `Studio 确定性检查确认 ${context.elementIds.length} 个知识条目`,
        action: {
          cause: 'studio_deterministic_commit',
          summary: `确认 Studio 的 ${context.elementIds.length} 个知识条目修改`,
          actorKind: 'user',
          actorName: context.username,
        },
      });
      const response: StudioReviewResponse = {
        workingRevision: accepted.revision.oid,
        clientMutationId: context.input.clientMutationId,
        reviewRequested: false,
        mode: 'deterministic',
        applied: true,
        currentRevision: accepted.revision.oid,
        packageVersion: accepted.packageVersion,
        impacts,
        changedPaths: context.changedPaths,
        elementIds: context.elementIds,
        display: {
          text: `确定性检查通过，已检查 ${impacts.affectedElementIds.length} 个关联条目并生成 v${accepted.packageVersion}`,
        },
      };
      this.db.exec('BEGIN IMMEDIATE');
      try {
        this.db.prepare(`DELETE FROM agent_candidate_changes
          WHERE project_id = ? AND run_id = ?`).run(context.project.id, context.workspace.run_id);
        const updated = this.db.prepare(`UPDATE agent_candidate_workspaces
          SET base_revision_oid = ?, candidate_id = NULL, lease_expires_at = ?, updated_at = ?
          WHERE project_id = ? AND run_id = ? AND candidate_id = ?`)
          .run(
            accepted.revision.oid,
            nowIso(),
            nowIso(),
            context.project.id,
            context.workspace.run_id,
            candidateId,
          );
        if (Number(updated.changes) !== 1) {
          throw new StudioWriteError('STUDIO_WRITE_LOCKED', 'Studio 工作区状态已经变化，请刷新后重试', 409);
        }
        this.db.prepare(`INSERT INTO studio_mutation_receipts (
          project_id, client_mutation_id, request_hash, response_json, created_at
        ) VALUES (?,?,?,?,?)`).run(
          context.project.id,
          context.input.clientMutationId,
          context.hash,
          JSON.stringify(response),
          nowIso(),
        );
        this.db.exec('COMMIT');
      } catch (error) {
        if (this.db.isTransaction) this.db.exec('ROLLBACK');
        throw error;
      }
      return response;
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      this.db.prepare(`UPDATE agent_candidate_workspaces SET candidate_id = NULL, updated_at = ?
        WHERE project_id = ? AND run_id = ? AND candidate_id = ?`)
        .run(nowIso(), context.project.id, context.workspace.run_id, candidateId);
      if (candidateId) {
        try {
          const candidate = await this.artifacts.getCandidateRevision(context.project.id, candidateId);
          if (candidate.status === 'pending') {
            await this.artifacts.rejectCandidateRevision(context.project.id, {
              candidateId,
              note: 'Studio 确定性提交未完成',
              action: {
                cause: 'studio_deterministic_aborted',
                summary: '关闭未完成的 Studio 确定性候选',
                actorKind: 'system',
                actorName: '建模系统',
              },
            });
          }
        } catch { /* 保留原始错误 */ }
      }
      if (error instanceof ArtifactRepositoryError && error.code === 'revision_conflict') {
        throw revisionConflict(context.baseRevision, String(error.details?.actualRevision ?? ''));
      }
      if (error instanceof Error && /版本|废弃/u.test(error.message)) {
        throw new StudioWriteError('PACKAGE_NOT_EDITABLE', error.message, 409);
      }
      throw error;
    }
  }

  private findReviewEvent(projectId: string, workingRevision: string): StudioReviewProjectEvent | null {
    const rows = this.db.prepare(`SELECT payload_json FROM agent_project_event_outbox
      WHERE project_id = ? AND event_type = 'studio_review' ORDER BY created_at DESC`)
      .all(projectId) as Array<{ payload_json: string }>;
    for (const row of rows) {
      try {
        const event = JSON.parse(row.payload_json) as StudioReviewProjectEvent;
        if (event.workingRevision === workingRevision) return event;
      } catch {
        // Ignore malformed historical outbox rows and continue with a fresh event.
      }
    }
    return null;
  }
}

function parseReviewInput(rawBody: unknown): ReviewInput {
  if (!rawBody || typeof rawBody !== 'object' || Array.isArray(rawBody)) {
    throw new StudioWriteError('INVALID_STUDIO_INPUT', '请求体必须是 JSON 对象', 400);
  }
  const body = rawBody as Record<string, unknown>;
  const expectedRevision = String(body.expectedRevision ?? '').trim();
  const clientMutationId = String(body.clientMutationId ?? '').trim();
  const fieldErrors: Record<string, string> = {};
  if (!expectedRevision || expectedRevision.length > 200) {
    fieldErrors.expectedRevision = 'expectedRevision 不能为空且不能超过 200 字符';
  }
  if (!MUTATION_ID_PATTERN.test(clientMutationId)) {
    fieldErrors.clientMutationId = 'clientMutationId 必须是 1 到 128 位字母、数字或 . _ : -';
  }
  if (Object.keys(fieldErrors).length) {
    throw new StudioWriteError(
      'STUDIO_FIELD_INVALID',
      '复核请求不完整，请修正后重试',
      422,
      { fieldErrors },
    );
  }
  return { expectedRevision, clientMutationId };
}

function responseFromEvent(
  event: StudioReviewProjectEvent,
  replayed: boolean,
): StudioReviewResponse {
  return {
    workingRevision: event.workingRevision,
    clientMutationId: event.clientMutationId,
    reviewRequested: true,
    mode: 'agent',
    applied: false,
    ...(replayed ? { replayed: true } : {}),
    changedPaths: event.changedPaths,
    elementIds: event.elementIds,
    display: { text: `${event.summary}；确认前不会改变当前版本` },
  };
}

function isDeterministicStudioScope(elementIds: string[]): boolean {
  return elementIds.length > 0 && elementIds.every((id) => /^(?:object|operation|rule):/u.test(id));
}

function assertNoIntroducedFormatErrors(
  baseSpec: ReturnType<typeof parseRenderedPackageFiles>,
  baseFiles: Record<string, string>,
  proposedSpec: ReturnType<typeof parseRenderedPackageFiles>,
  proposedFiles: Record<string, string>,
): void {
  const identity = (finding: ReturnType<typeof runFormatChecks>[number]) => JSON.stringify([
    finding.category,
    finding.target,
    finding.message,
  ]);
  const baseline = new Set(runFormatChecks(baseSpec, baseFiles)
    .filter((finding) => finding.severity === '错误')
    .map(identity));
  const introduced = runFormatChecks(proposedSpec, proposedFiles)
    .filter((finding) => finding.severity === '错误' && !baseline.has(identity(finding)));
  if (!introduced.length) return;
  throw new StudioWriteError(
    'DETERMINISTIC_CHECK_FAILED',
    `确定性检查未通过：${introduced.map((finding) => finding.message).join('；')}`,
    422,
    {
      findings: introduced.map((finding) => ({
        category: finding.category,
        target: finding.target,
        message: finding.message,
        suggestion: finding.suggestion,
      })),
    },
  );
}

function assertNoIntroducedIntegrityErrors(
  baseSpec: ReturnType<typeof parseRenderedPackageFiles>,
  proposedSpec: ReturnType<typeof parseRenderedPackageFiles>,
): void {
  const introduced = introducedSpecIntegrityProblems(
    proposedSpec,
    listSpecIntegrityProblems(baseSpec),
  );
  if (!introduced.length) return;
  throw new StudioWriteError(
    'DETERMINISTIC_CHECK_FAILED',
    `确定性完整性检查未通过：${introduced.join('；')}`,
    422,
    { findings: introduced.map((message) => ({ category: '知识包完整性', message })) },
  );
}

function deterministicImpacts(
  baseRevision: string,
  workingRevision: string,
  files: Record<string, string>,
  changedElementIds: string[],
  spec: ReturnType<typeof parseRenderedPackageFiles>,
) {
  const graph = new StudioReadService({
    workingRevision,
    baseRevision,
    candidateRevision: null,
    files,
  }).graph();
  const changed = new Set(changedElementIds);
  const affected = new Set<string>();
  let relationCount = 0;
  for (const edge of graph.edges) {
    const touchesSource = changed.has(edge.source);
    const touchesTarget = changed.has(edge.target);
    if (!touchesSource && !touchesTarget) continue;
    relationCount += 1;
    if (touchesSource && !changed.has(edge.target)) affected.add(edge.target);
    if (touchesTarget && !changed.has(edge.source)) affected.add(edge.source);
  }
  for (const script of spec.scripts ?? []) {
    if (!changed.has(`operation:${script.boundOperation}`)) continue;
    affected.add(`script:${script.key}`);
    relationCount += 1;
  }
  return {
    changedElementIds: [...changed].sort(),
    affectedElementIds: [...affected].sort(),
    relationCount,
  };
}

function requestHash(input: ReviewInput): string {
  return createHash('sha256')
    .update('studio-review\0')
    .update(input.expectedRevision)
    .update('\0')
    .update(input.clientMutationId)
    .digest('hex');
}

function revisionConflict(expectedRevision: string, actualRevision: string): StudioWriteError {
  return new StudioWriteError(
    'REVISION_CONFLICT',
    '知识包工作修订已经变化，请刷新后重试',
    409,
    { expectedRevision, actualRevision },
  );
}

function applyChanges(base: Record<string, string>, changes: ChangeRow[]): Record<string, string> {
  const files = { ...base };
  for (const change of changes) {
    if (change.kind === 'delete') delete files[change.path];
    else files[change.path] = change.content ?? '';
  }
  return files;
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

function elementIdsFromPaths(paths: string[]): string[] {
  const directories: Record<string, string> = {
    objects: 'object',
    operations: 'operation',
    rules: 'rule',
    metrics: 'metric',
  };
  return [...new Set(paths.flatMap((path) => {
    const match = /^(objects|operations|rules|metrics)\/(.+)\.md$/u.exec(path);
    if (!match || match[2] === 'index') return [];
    return [`${directories[match[1]]}:${match[2]}`];
  }))].sort();
}

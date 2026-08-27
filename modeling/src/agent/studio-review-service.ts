import { createHash } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import type { PackageArtifactStore } from '../artifacts/package-store.js';
import { getPackageArtifactStore } from '../artifacts/runtime.js';
import { getDb } from '../db/index.js';
import type { Project } from '../types.js';
import {
  enqueueStudioReviewProjectEvent,
  type StudioReviewProjectEvent,
} from './project-event-outbox.js';
import { StudioWriteError } from './studio-write-service.js';

const MUTATION_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/u;

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
  reviewRequested: true;
  replayed?: boolean;
  changedPaths: string[];
  elementIds: string[];
  display: { text: string };
}

export class StudioReviewService {
  constructor(
    private readonly db: DatabaseSync = getDb(),
    private readonly artifacts: PackageArtifactStore = getPackageArtifactStore(),
  ) {}

  async requestReview(
    project: Project,
    username: string,
    rawBody: unknown,
  ): Promise<StudioReviewResponse> {
    const input = parseReviewInput(rawBody);
    const hash = requestHash(input);
    const eventId = `studio-review:${project.id}:${input.clientMutationId}`;

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
    ...(replayed ? { replayed: true } : {}),
    changedPaths: event.changedPaths,
    elementIds: event.elementIds,
    display: { text: `${event.summary}；确认前不会改变当前版本` },
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
  for (const path of Object.keys(files).sort()) {
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

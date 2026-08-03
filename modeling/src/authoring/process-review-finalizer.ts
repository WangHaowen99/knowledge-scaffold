import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import type { AiConstructionOrchestrator } from '../ai-construction/orchestrator.js';
import { getDb, nowIso } from '../db/index.js';
import type { FlowchartDraftStore } from '../flowchart/draft-store.js';
import type { FlowchartDocument } from '../flowchart/types.js';
import type { PiAuthoringAdapter } from './pi-adapter.js';
import { AuthoringStateError, type JsonObject } from './types.js';

export interface ProcessReviewFinalizationInput {
  projectId: string;
  interactionId: string;
  draftRevision: number;
  requestId: string;
  response: JsonObject;
  actorUsername: string;
  packageRevisionOid: string;
  taskId: string;
  document: FlowchartDocument;
}

export interface ProcessReviewFinalizationAttempt {
  projectId: string;
  interactionId: string;
  draftRevision: number;
  requestId: string;
  responseJson: string;
  actorUsername: string;
  packageRevisionOid?: string;
  status: 'pending' | 'completed';
}

interface ProcessReviewFinalizerDependencies {
  drafts: FlowchartDraftStore;
  adapter: PiAuthoringAdapter;
  orchestrator: Pick<AiConstructionOrchestrator, 'completeProcessReview'>;
  db?: DatabaseSync;
}

/**
 * Git 修订无法参加 SQLite 事务，因此先记录提交意图，再在 Git 写入后收敛交互和任务状态。
 * 服务中断时，同一 requestId 可从 pending 记录继续，不会重复产生知识包修订。
 */
export class ProcessReviewFinalizer {
  private readonly db: DatabaseSync;

  constructor(private readonly deps: ProcessReviewFinalizerDependencies) {
    this.db = deps.db ?? getDb();
  }

  begin(input: Omit<ProcessReviewFinalizationInput, 'packageRevisionOid' | 'taskId' | 'document'>) {
    const responseJson = stableJson(input.response);
    return this.transaction(() => {
      const existing = this.get(input.projectId, input.interactionId, input.draftRevision);
      if (existing) {
        this.assertSameAttempt(existing, input.requestId, responseJson, input.actorUsername);
        return existing;
      }
      const requestOwner = this.db.prepare(`SELECT interaction_id, draft_revision
        FROM flowchart_review_finalizations WHERE project_id = ? AND request_id = ?`)
        .get(input.projectId, input.requestId) as
        { interaction_id: string; draft_revision: number } | undefined;
      if (requestOwner) {
        throw new AuthoringStateError('IDEMPOTENCY_CONFLICT', '请求编号已用于其他流程图提交');
      }
      this.db.prepare(`INSERT INTO flowchart_review_finalizations
        (project_id, interaction_id, draft_revision, request_id, response_json,
          actor_username, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)`)
        .run(
          input.projectId,
          input.interactionId,
          input.draftRevision,
          input.requestId,
          responseJson,
          input.actorUsername,
          nowIso(),
        );
      return this.get(input.projectId, input.interactionId, input.draftRevision)!;
    });
  }

  get(projectId: string, interactionId: string, draftRevision: number): ProcessReviewFinalizationAttempt | null {
    const row = this.db.prepare(`SELECT * FROM flowchart_review_finalizations
      WHERE project_id = ? AND interaction_id = ? AND draft_revision = ?`)
      .get(projectId, interactionId, draftRevision) as any;
    return row ? rowToAttempt(row) : null;
  }

  recordPackageSaved(
    projectId: string,
    interactionId: string,
    draftRevision: number,
    packageRevisionOid: string,
  ): ProcessReviewFinalizationAttempt {
    return this.transaction(() => {
      const attempt = this.get(projectId, interactionId, draftRevision);
      if (!attempt) throw new AuthoringStateError('INVALID_TRANSITION', '流程图提交意图不存在');
      if (attempt.packageRevisionOid && attempt.packageRevisionOid !== packageRevisionOid) {
        throw new AuthoringStateError('IDEMPOTENCY_CONFLICT', '流程图提交已绑定其他版本的知识包');
      }
      this.db.prepare(`UPDATE flowchart_review_finalizations SET package_revision_oid = ?
        WHERE project_id = ? AND interaction_id = ? AND draft_revision = ?`)
        .run(packageRevisionOid, projectId, interactionId, draftRevision);
      return this.get(projectId, interactionId, draftRevision)!;
    });
  }

  finalize(input: ProcessReviewFinalizationInput) {
    return this.transaction(() => {
      const attempt = this.get(input.projectId, input.interactionId, input.draftRevision);
      if (!attempt) throw new AuthoringStateError('INVALID_TRANSITION', '流程图提交意图不存在');
      this.assertSameAttempt(
        attempt,
        input.requestId,
        stableJson(input.response),
        input.actorUsername,
      );
      if (attempt.packageRevisionOid !== input.packageRevisionOid) {
        throw new AuthoringStateError('IDEMPOTENCY_CONFLICT', '流程图提交绑定的知识包内容不一致');
      }

      const draft = this.deps.drafts.get(input.projectId, input.interactionId);
      if (!draft) throw new AuthoringStateError('INVALID_TRANSITION', '流程图草稿不存在');
      if (draft.submittedRevision !== input.draftRevision) {
        this.deps.drafts.markSubmitted({
          projectId: input.projectId,
          interactionId: input.interactionId,
          expectedRevision: input.draftRevision,
          actorUsername: input.actorUsername,
        });
      }
      const answered = this.deps.adapter.respond(
        input.projectId,
        input.interactionId,
        input.requestId,
        input.response,
      );
      const completedRun = answered.run.status === 'completed'
        ? answered.run
        : this.deps.adapter.complete(input.projectId, answered.run.id, {
          accepted: true,
          revision: input.packageRevisionOid,
        }).run;
      const construction = this.deps.orchestrator.completeProcessReview(input.projectId, {
        taskId: input.taskId,
        packageRevisionOid: input.packageRevisionOid,
        messageKey: `process.review.${input.interactionId}`,
      });
      this.db.prepare(`UPDATE flowchart_review_finalizations
        SET status = 'completed', completed_at = ?
        WHERE project_id = ? AND interaction_id = ? AND draft_revision = ?`)
        .run(nowIso(), input.projectId, input.interactionId, input.draftRevision);
      return { answered, completedRun, construction };
    });
  }

  private assertSameAttempt(
    attempt: ProcessReviewFinalizationAttempt,
    requestId: string,
    responseJson: string,
    actorUsername: string,
  ): void {
    if (attempt.requestId !== requestId
      || attempt.responseJson !== responseJson
      || attempt.actorUsername !== actorUsername) {
      throw new AuthoringStateError('IDEMPOTENCY_CONFLICT', '流程图提交请求与已有请求不一致');
    }
  }

  private transaction<T>(work: () => T): T {
    if (!this.db.isTransaction) {
      this.db.exec('BEGIN IMMEDIATE');
      try {
        const result = work();
        this.db.exec('COMMIT');
        return result;
      } catch (error) {
        if (this.db.isTransaction) this.db.exec('ROLLBACK');
        throw error;
      }
    }
    const savepoint = `process_review_${randomUUID().replaceAll('-', '')}`;
    this.db.exec(`SAVEPOINT ${savepoint}`);
    try {
      const result = work();
      this.db.exec(`RELEASE SAVEPOINT ${savepoint}`);
      return result;
    } catch (error) {
      this.db.exec(`ROLLBACK TO SAVEPOINT ${savepoint}`);
      this.db.exec(`RELEASE SAVEPOINT ${savepoint}`);
      throw error;
    }
  }
}

function rowToAttempt(row: any): ProcessReviewFinalizationAttempt {
  return {
    projectId: row.project_id,
    interactionId: row.interaction_id,
    draftRevision: Number(row.draft_revision),
    requestId: row.request_id,
    responseJson: row.response_json,
    actorUsername: row.actor_username,
    packageRevisionOid: row.package_revision_oid ?? undefined,
    status: row.status,
  };
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`)
      .join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { getDb, nowIso } from '../db/index.js';
import { normalizeFlowchartDocument, validateFlowchartDocument } from './document.js';
import type { FlowchartDocument, FlowchartValidationIssue } from './types.js';

export type FlowchartDraftErrorCode =
  | 'already_exists'
  | 'conflict'
  | 'invalid_document'
  | 'not_found'
  | 'scope_mismatch';

export class FlowchartDraftError extends Error {
  constructor(
    public readonly code: FlowchartDraftErrorCode,
    message: string,
    public readonly issues: FlowchartValidationIssue[] = [],
  ) {
    super(message);
    this.name = 'FlowchartDraftError';
  }
}

export interface FlowchartDraftRevisionRecord {
  revision: number;
  document: FlowchartDocument;
  warnings: FlowchartValidationIssue[];
  actorUsername: string;
  createdAt: string;
}

export interface FlowchartDraftSnapshot {
  projectId: string;
  interactionId: string;
  revision: number;
  document: FlowchartDocument;
  warnings: FlowchartValidationIssue[];
  revisionRecord: FlowchartDraftRevisionRecord;
  submittedRevision?: number;
  updatedAt: string;
}

export interface CreateFlowchartDraftInput {
  projectId: string;
  interactionId: string;
  document: unknown;
  actorUsername: string;
}

export interface SaveFlowchartDraftInput extends CreateFlowchartDraftInput {
  expectedRevision: number;
}

export interface SubmitFlowchartDraftInput {
  projectId: string;
  interactionId: string;
  expectedRevision: number;
  actorUsername: string;
}

export interface FlowchartDraftSubmissionRecord {
  revision: number;
  actorUsername: string;
  createdAt: string;
}

export class FlowchartDraftStore {
  constructor(private readonly db: DatabaseSync = getDb()) {}

  create(input: CreateFlowchartDraftInput): FlowchartDraftSnapshot {
    const projectId = requiredText(input.projectId, '项目编号');
    const interactionId = requiredText(input.interactionId, '交互编号');
    const actorUsername = requiredText(input.actorUsername, '操作人');
    const document = validDocument(input.document);
    const warnings = validateFlowchartDocument(document).filter((issue) => issue.severity === 'warning');
    return this.transaction(() => {
      this.assertInteraction(projectId, interactionId);
      if (this.db.prepare('SELECT 1 FROM flowchart_drafts WHERE interaction_id = ?').get(interactionId)) {
        throw new FlowchartDraftError('already_exists', '流程图草稿已经存在');
      }
      const createdAt = nowIso();
      this.db.prepare(`INSERT INTO flowchart_drafts
        (interaction_id, project_id, current_revision, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?)`)
        .run(interactionId, projectId, createdAt, createdAt);
      this.insertRevision(projectId, interactionId, 1, document, warnings, actorUsername, createdAt);
      return this.get(projectId, interactionId)!;
    });
  }

  save(input: SaveFlowchartDraftInput): FlowchartDraftSnapshot {
    const projectId = requiredText(input.projectId, '项目编号');
    const interactionId = requiredText(input.interactionId, '交互编号');
    const actorUsername = requiredText(input.actorUsername, '操作人');
    const expectedRevision = validRevision(input.expectedRevision);
    const document = validDocument(input.document);
    const warnings = validateFlowchartDocument(document).filter((issue) => issue.severity === 'warning');
    return this.transaction(() => {
      this.assertInteraction(projectId, interactionId);
      const head = this.db.prepare(`SELECT current_revision FROM flowchart_drafts
        WHERE project_id = ? AND interaction_id = ?`).get(projectId, interactionId) as
        { current_revision: number } | undefined;
      if (!head) throw new FlowchartDraftError('not_found', '流程图草稿不存在');
      if (Number(head.current_revision) !== expectedRevision) {
        throw new FlowchartDraftError('conflict', '流程图草稿已被其他修改更新');
      }
      const revision = expectedRevision + 1;
      const createdAt = nowIso();
      this.insertRevision(projectId, interactionId, revision, document, warnings, actorUsername, createdAt);
      const result = this.db.prepare(`UPDATE flowchart_drafts
        SET current_revision = ?, updated_at = ?
        WHERE interaction_id = ? AND project_id = ? AND current_revision = ?`)
        .run(revision, createdAt, interactionId, projectId, expectedRevision);
      if (Number(result.changes) !== 1) {
        throw new FlowchartDraftError('conflict', '流程图草稿已被其他修改更新');
      }
      return this.get(projectId, interactionId)!;
    });
  }

  markSubmitted(input: SubmitFlowchartDraftInput): FlowchartDraftSnapshot {
    const projectId = requiredText(input.projectId, '项目编号');
    const interactionId = requiredText(input.interactionId, '交互编号');
    const actorUsername = requiredText(input.actorUsername, '操作人');
    const expectedRevision = validRevision(input.expectedRevision);
    return this.transaction(() => {
      this.assertInteraction(projectId, interactionId);
      const head = this.db.prepare(`SELECT current_revision FROM flowchart_drafts
        WHERE project_id = ? AND interaction_id = ?`).get(projectId, interactionId) as
        { current_revision: number } | undefined;
      if (!head) throw new FlowchartDraftError('not_found', '流程图草稿不存在');
      if (Number(head.current_revision) !== expectedRevision) {
        throw new FlowchartDraftError('conflict', '只能提交当前已保存的流程图草稿');
      }
      this.db.prepare(`INSERT INTO flowchart_draft_submissions
        (project_id, interaction_id, revision, actor_username, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(interaction_id, revision) DO NOTHING`)
        .run(projectId, interactionId, expectedRevision, actorUsername, nowIso());
      return this.get(projectId, interactionId)!;
    });
  }

  listSubmissions(projectId: string, interactionId: string): FlowchartDraftSubmissionRecord[] {
    return (this.db.prepare(`SELECT s.* FROM flowchart_draft_submissions s
      JOIN flowchart_drafts d ON d.interaction_id = s.interaction_id
      WHERE d.project_id = ? AND s.interaction_id = ?
      ORDER BY s.revision`).all(
      requiredText(projectId, '项目编号'),
      requiredText(interactionId, '交互编号'),
    ) as any[]).map((row) => ({
      revision: Number(row.revision),
      actorUsername: row.actor_username,
      createdAt: row.created_at,
    }));
  }

  get(projectId: string, interactionId: string): FlowchartDraftSnapshot | null {
    const row = this.db.prepare(`SELECT d.project_id, d.interaction_id, d.current_revision, d.updated_at,
        r.document_json, r.warnings_json, r.actor_username, r.created_at,
        (SELECT MAX(s.revision) FROM flowchart_draft_submissions s
          WHERE s.interaction_id = d.interaction_id) AS submitted_revision
      FROM flowchart_drafts d
      JOIN flowchart_draft_revisions r
        ON r.interaction_id = d.interaction_id AND r.revision = d.current_revision
      WHERE d.project_id = ? AND d.interaction_id = ?`)
      .get(requiredText(projectId, '项目编号'), requiredText(interactionId, '交互编号')) as any;
    if (!row) return null;
    const revisionRecord = rowToRevision(row, Number(row.current_revision));
    return {
      projectId: row.project_id,
      interactionId: row.interaction_id,
      revision: revisionRecord.revision,
      document: revisionRecord.document,
      warnings: revisionRecord.warnings,
      revisionRecord,
      ...(row.submitted_revision == null ? {} : { submittedRevision: Number(row.submitted_revision) }),
      updatedAt: row.updated_at,
    };
  }

  getRevision(
    projectId: string,
    interactionId: string,
    revision: number,
  ): FlowchartDraftRevisionRecord | null {
    const row = this.db.prepare(`SELECT r.* FROM flowchart_draft_revisions r
      JOIN flowchart_drafts d ON d.interaction_id = r.interaction_id
      WHERE d.project_id = ? AND r.interaction_id = ? AND r.revision = ?`)
      .get(
        requiredText(projectId, '项目编号'),
        requiredText(interactionId, '交互编号'),
        validRevision(revision),
      ) as any;
    return row ? rowToRevision(row, Number(row.revision)) : null;
  }

  private insertRevision(
    projectId: string,
    interactionId: string,
    revision: number,
    document: FlowchartDocument,
    warnings: FlowchartValidationIssue[],
    actorUsername: string,
    createdAt: string,
  ): void {
    this.db.prepare(`INSERT INTO flowchart_draft_revisions
      (project_id, interaction_id, revision, document_json, warnings_json, actor_username, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?)`)
      .run(
        projectId,
        interactionId,
        revision,
        JSON.stringify(document),
        JSON.stringify(warnings),
        actorUsername,
        createdAt,
      );
  }

  private assertInteraction(projectId: string, interactionId: string): void {
    const row = this.db.prepare(`SELECT project_id, type, status, payload_json
      FROM authoring_interactions WHERE id = ?`)
      .get(interactionId) as {
        project_id: string;
        type: string;
        status: string;
        payload_json: string;
      } | undefined;
    if (!row) throw new FlowchartDraftError('not_found', '流程图确认交互不存在');
    let purpose = '';
    try { purpose = String(JSON.parse(row.payload_json)?.purpose ?? ''); } catch { /* 按作用域不匹配处理 */ }
    if (row.project_id !== projectId || row.type !== 'flowchart_review' || purpose !== 'process_review') {
      throw new FlowchartDraftError('scope_mismatch', '流程图确认交互不属于当前项目');
    }
    if (row.status !== 'pending') {
      throw new FlowchartDraftError('conflict', '流程图确认交互已经结束，不能修改草稿');
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
    const savepoint = `flowchart_draft_${randomUUID().replaceAll('-', '')}`;
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

function validDocument(value: unknown): FlowchartDocument {
  let document: FlowchartDocument;
  try {
    document = normalizeFlowchartDocument(value);
  } catch (error) {
    throw new FlowchartDraftError(
      'invalid_document',
      error instanceof Error ? error.message : '流程图文档格式不正确',
    );
  }
  const issues = validateFlowchartDocument(document);
  const errors = issues.filter((issue) => issue.severity === 'error');
  if (errors.length) throw new FlowchartDraftError('invalid_document', '流程图结构不正确', errors);
  return document;
}

function rowToRevision(row: any, revision: number): FlowchartDraftRevisionRecord {
  const warnings = JSON.parse(row.warnings_json ?? '[]') as FlowchartValidationIssue[];
  return {
    revision,
    document: normalizeFlowchartDocument(JSON.parse(row.document_json)),
    warnings,
    actorUsername: row.actor_username,
    createdAt: row.created_at,
  };
}

function requiredText(value: unknown, label: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new FlowchartDraftError('scope_mismatch', `${label}不能为空`);
  }
  if (/[\u0000-\u001f\u007f]/u.test(value)) {
    throw new FlowchartDraftError('scope_mismatch', `${label}不正确`);
  }
  return value.trim();
}

function validRevision(value: number): number {
  if (!Number.isInteger(value) || value < 1) {
    throw new FlowchartDraftError('conflict', '流程图草稿版本不正确');
  }
  return value;
}

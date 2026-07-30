export interface ArtifactAuthor {
  name: string;
  email: string;
}

export interface ArtifactRevision {
  oid: string;
  parentOid: string | null;
  message: string;
  author: ArtifactAuthor;
  createdAt: string;
}

export interface ArtifactFile {
  path: string;
  content: Uint8Array;
}

export interface ArtifactSnapshot {
  revision: ArtifactRevision;
  files: ArtifactFile[];
}

export type ArtifactChange =
  | { kind: 'write'; path: string; content: string | Uint8Array }
  | { kind: 'delete'; path: string };

export interface ArtifactCommitMetadata {
  message: string;
  author?: ArtifactAuthor;
  createdAt?: Date;
}

export interface ArtifactFileDiff {
  path: string;
  kind: 'added' | 'modified' | 'deleted';
  before?: Uint8Array;
  after?: Uint8Array;
}

export interface ArtifactDiff {
  from: ArtifactRevision;
  to: ArtifactRevision;
  files: ArtifactFileDiff[];
}

export interface ArtifactRepository {
  initialize(projectId: string): Promise<ArtifactRevision>;
  /** 创建或删除项目失败时清理对应的工件仓库。 */
  removeProject?(projectId: string): Promise<void>;
  current(projectId: string): Promise<ArtifactRevision>;
  snapshot(projectId: string, revision?: string): Promise<ArtifactSnapshot>;
  commit(
    projectId: string,
    expectedRevision: string,
    changes: readonly ArtifactChange[],
    metadata: ArtifactCommitMetadata,
  ): Promise<ArtifactRevision>;
  /**
   * 仅用于跨存储提交失败时的补偿：当 expectedRevision 仍是 HEAD 时，
   * 丢弃这个刚刚形成的修订并恢复到它的直接父修订。
   */
  rollbackCommit(projectId: string, expectedRevision: string): Promise<ArtifactRevision>;
  history(projectId: string, limit?: number): Promise<ArtifactRevision[]>;
  diff(projectId: string, fromRevision: string, toRevision: string): Promise<ArtifactDiff>;
  restore(
    projectId: string,
    targetRevision: string,
    expectedRevision: string,
    metadata?: ArtifactCommitMetadata,
  ): Promise<ArtifactRevision>;
}

export type ArtifactRepositoryErrorCode =
  | 'invalid_project'
  | 'invalid_path'
  | 'invalid_input'
  | 'not_initialized'
  | 'revision_not_found'
  | 'revision_conflict'
  | 'empty_change'
  | 'no_change'
  | 'workspace_not_empty'
  | 'invalid_repository';

export class ArtifactRepositoryError extends Error {
  constructor(
    public readonly code: ArtifactRepositoryErrorCode,
    message: string,
    public readonly details?: Readonly<Record<string, string>>,
  ) {
    super(message);
    this.name = 'ArtifactRepositoryError';
  }
}

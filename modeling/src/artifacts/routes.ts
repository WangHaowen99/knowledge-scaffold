import type { FastifyInstance, FastifyReply } from 'fastify';

import {
  ProjectLeaseError,
  ProjectOperationLease,
  runWithProjectLease,
  SHORT_WRITE_LEASE_TTL_MS,
} from '../authoring/lease.js';
import { getAuthoringState } from '../authoring/state.js';
import { getProject } from '../projects/repo.js';
import { ArtifactRepositoryError, type ArtifactFileDiff } from './repository.js';
import { PackageArtifactStoreError } from './package-store.js';
import { getPackageArtifactStore } from './runtime.js';

const decoder = new TextDecoder('utf-8', { fatal: true });

function ownedProject(id: string, username: string) {
  const project = getProject(id);
  return project?.owner === username ? project : null;
}

function publicPath(path: string): string | null {
  return path.startsWith('package/') ? path.slice('package/'.length) : null;
}

function publicRevision(revision: any) {
  return {
    id: revision.oid,
    parentRevision: revision.parentOid ?? null,
    sequence: revision.sequence,
    packageVersion: revision.packageVersion,
    cause: revision.cause,
    summary: revision.summary,
    actor: { kind: revision.actorKind, displayName: revision.actorName },
    changedPaths: (revision.changedPaths ?? []).map(publicPath).filter(Boolean),
    createdAt: revision.createdAt,
  };
}

function sendArtifactError(reply: FastifyReply, error: unknown) {
  if (error instanceof ProjectLeaseError) {
    const status = error.code === 'project_not_found' ? 404
      : error.code === 'busy' || error.code === 'expired' || error.code === 'not_owner' ? 409 : 400;
    return reply.code(status).send({ error: error.message, code: error.code });
  }
  if (error instanceof ArtifactRepositoryError) {
    const status = error.code === 'revision_conflict' ? 409
      : error.code === 'revision_not_found' || error.code === 'not_initialized' ? 404 : 400;
    return reply.code(status).send({ error: error.message, code: error.code });
  }
  if (error instanceof PackageArtifactStoreError) {
    const status = error.code === 'project_not_found'
      || error.code === 'package_not_found'
      || error.code === 'file_not_found'
      || error.code === 'revision_has_no_package'
      ? 404
      : error.code === 'commit_in_progress' ? 409 : 400;
    return reply.code(status).send({ error: error.message, code: error.code });
  }
  throw error;
}

function decode(value: Uint8Array | undefined): string | null {
  if (!value) return '';
  try { return decoder.decode(value); } catch { return null; }
}

function changedMiddle(before: string[], after: string[]) {
  let prefix = 0;
  while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) prefix += 1;
  let suffix = 0;
  while (
    suffix < before.length - prefix
    && suffix < after.length - prefix
    && before[before.length - 1 - suffix] === after[after.length - 1 - suffix]
  ) suffix += 1;
  return {
    prefix,
    suffix,
    removed: before.slice(prefix, before.length - suffix),
    added: after.slice(prefix, after.length - suffix),
  };
}

function publicDiff(file: ArtifactFileDiff) {
  const path = publicPath(file.path);
  if (!path) return null;
  const beforeText = decode(file.before);
  const afterText = decode(file.after);
  if (beforeText === null || afterText === null) {
    return { path, status: file.kind, additions: 0, deletions: 0, binary: true, hunks: [] };
  }
  const before = file.before === undefined ? [] : beforeText.split('\n');
  const after = file.after === undefined ? [] : afterText.split('\n');
  const middle = changedMiddle(before, after);
  const contextStart = Math.max(0, middle.prefix - 2);
  const contextEndBefore = Math.min(before.length, before.length - middle.suffix + 2);
  const lines = [
    ...before.slice(contextStart, middle.prefix).map((content) => ({ type: 'context', content })),
    ...middle.removed.map((content) => ({ type: 'remove', content })),
    ...middle.added.map((content) => ({ type: 'add', content })),
    ...before.slice(before.length - middle.suffix, contextEndBefore)
      .map((content) => ({ type: 'context', content })),
  ];
  return {
    path,
    status: file.kind,
    additions: middle.added.length,
    deletions: middle.removed.length,
    binary: false,
    hunks: lines.length ? [{ oldStart: contextStart + 1, newStart: contextStart + 1, lines }] : [],
  };
}

export function registerArtifactRoutes(app: FastifyInstance): void {
  const store = getPackageArtifactStore();
  const leases = new ProjectOperationLease();

  app.get('/api/projects/:id/revisions', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    try {
      const limit = Math.min(200, Math.max(1, Number((req.query as any)?.limit) || 50));
      const history = await store.history(project.id, limit);
      const versions = history
        .filter((revision) => revision.cause !== 'project_created')
        .map(publicRevision);
      return { currentRevision: versions[0]?.id ?? null, versions };
    } catch (error) {
      return sendArtifactError(reply, error);
    }
  });

  app.get('/api/projects/:id/revisions/diff', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const from = String((req.query as any)?.from ?? '');
    const to = String((req.query as any)?.to ?? '');
    if (!from || !to) return reply.code(400).send({ error: '请选择两个版本' });
    try {
      const diff = await store.diff(project.id, from, to);
      return {
        fromRevision: diff.from.oid,
        toRevision: diff.to.oid,
        files: diff.files.map(publicDiff).filter(Boolean),
      };
    } catch (error) {
      return sendArtifactError(reply, error);
    }
  });

  app.post('/api/projects/:id/revisions/restore', async (req, reply) => {
    const project = ownedProject(String((req.params as any).id), req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const { targetRevision, expectedRevision } = (req.body ?? {}) as {
      targetRevision?: string;
      expectedRevision?: string;
    };
    if (!targetRevision || !expectedRevision) {
      return reply.code(400).send({ error: '请选择要恢复的版本' });
    }
    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '恢复知识包版本',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: SHORT_WRITE_LEASE_TTL_MS,
      }, async () => {
        const restored = await store.restore(project.id, {
          targetRevision,
          expectedRevision,
          action: {
            cause: 'restore',
            summary: '恢复历史内容',
            actorKind: 'user',
            actorName: req.user!.username,
          },
        });
        getAuthoringState().apply({
          kind: 'publish_event',
          projectId: project.id,
          type: 'package.revision.created',
          payload: { revisionId: restored.revision.oid, cause: 'restore' },
        });
        return {
          currentRevision: restored.revision.oid,
          packageVersion: restored.packageVersion,
        };
      });
    } catch (error) {
      return sendArtifactError(reply, error);
    }
  });
}

import type { FastifyInstance, FastifyReply } from 'fastify';

import {
  ProjectLeaseError,
  ProjectOperationLease,
  runWithProjectLease,
  SHORT_WRITE_LEASE_TTL_MS,
} from '../authoring/lease.js';
import { getAuthoringState } from '../authoring/state.js';
import { optimizationCandidateDiagnosisLinkState } from '../evaluation/optimization-candidate.js';
import { getProject } from '../projects/repo.js';
import * as m4repo from '../workflow/m4-repo.js';
import { runFormatChecks } from '../workflow/package-check.js';
import { ArtifactRepositoryError, type ArtifactFileDiff } from './repository.js';
import {
  PackageArtifactStoreError,
  type PackageCandidateRevision,
  type PackageCandidateRevisionStatus,
} from './package-store.js';
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
      || error.code === 'candidate_not_found'
      ? 404
      : error.code === 'commit_in_progress'
        || error.code === 'candidate_not_pending'
        || error.code === 'candidate_validation_failed'
        ? 409 : 400;
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
  return formattedDiff(file, path);
}

function publicCandidateDiff(file: ArtifactFileDiff) {
  const path = publicPath(file.path) ?? file.path;
  return formattedDiff(file, path);
}

function formattedDiff(file: ArtifactFileDiff, path: string) {
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

function publicCandidate(candidate: PackageCandidateRevision) {
  return {
    id: candidate.id,
    baseRevision: candidate.baseRevision,
    status: candidate.status,
    cause: candidate.cause,
    summary: candidate.summary,
    actor: { kind: candidate.actorKind, displayName: candidate.actorName },
    groups: candidate.groups.map((group) => ({
      id: group.id,
      summary: group.summary,
      dependsOn: group.dependsOn,
      changedPaths: group.changes.map((change) => publicPath(change.path) ?? change.path),
      validation: group.validation,
    })),
    selectedGroupIds: candidate.selectedGroupIds,
    acceptedRevision: candidate.acceptedRevision ?? null,
    decision: candidate.decision ? {
      cause: candidate.decision.cause,
      summary: candidate.decision.summary,
      actor: {
        kind: candidate.decision.actorKind,
        displayName: candidate.decision.actorName,
      },
      note: candidate.decision.note ?? null,
      decidedAt: candidate.decision.decidedAt,
    } : null,
    createdAt: candidate.createdAt,
  };
}

function candidateManagedPath(value: unknown): string {
  const path = String(value ?? '');
  if (path.startsWith('model/')) {
    throw new PackageArtifactStoreError('invalid_input', '接口只能提交知识包文件修改');
  }
  return path.startsWith('package/') ? path : `package/${path}`;
}

export function registerArtifactRoutes(app: FastifyInstance): void {
  const store = getPackageArtifactStore();
  const leases = new ProjectOperationLease();

  app.get('/api/projects/:id/candidate-revisions', async (req, reply) => {
    const project = ownedProject((req.params as any).id, req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const rawStatus = (req.query as any)?.status;
    const status = rawStatus === undefined ? undefined : String(rawStatus) as PackageCandidateRevisionStatus;
    try {
      const overview = await store.candidateRevisionOverview(
        project.id,
        status,
        Math.min(200, Number((req.query as any)?.limit) || 100),
      );
      return {
        pendingCount: overview.pendingCount,
        hasPending: overview.pendingCount > 0,
        candidates: overview.candidates.map(publicCandidate),
      };
    } catch (error) {
      return sendArtifactError(reply, error);
    }
  });

  app.post('/api/projects/:id/candidate-revisions', async (req, reply) => {
    const project = ownedProject((req.params as any).id, req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const { expectedRevision, summary, groups } = (req.body ?? {}) as any;
    if (!expectedRevision || !summary || !Array.isArray(groups)) {
      return reply.code(400).send({ error: '候选修订内容不完整' });
    }
    try {
      const candidate = await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '创建候选修订',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: SHORT_WRITE_LEASE_TTL_MS,
      }, () => store.createCandidateRevision(project.id, {
        expectedRevision: String(expectedRevision),
        groups: groups.map((group: any) => ({
          id: group?.id,
          summary: group?.summary,
          dependsOn: group?.dependsOn,
          validation: group?.validation,
          changes: Array.isArray(group?.changes) ? group.changes.map((change: any) => (
            change?.kind === 'delete'
              ? { kind: 'delete' as const, path: candidateManagedPath(change?.path) }
              : {
                kind: change?.kind,
                path: candidateManagedPath(change?.path),
                content: change?.content,
              }
          )) : [],
        })),
        action: {
          cause: 'candidate_created',
          summary: String(summary),
          actorKind: 'user',
          actorName: req.user!.username,
        },
      }));
      return reply.code(201).send({ candidate: publicCandidate(candidate) });
    } catch (error) {
      return sendArtifactError(reply, error);
    }
  });

  app.get('/api/projects/:id/candidate-revisions/:candidateId/preview', async (req, reply) => {
    const project = ownedProject((req.params as any).id, req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const rawGroups = String((req.query as any)?.groups ?? '').trim();
    const selectedGroupIds = rawGroups
      ? rawGroups.split(',').map((id) => id.trim()).filter(Boolean)
      : undefined;
    try {
      const preview = await store.previewCandidateRevision(
        project.id,
        String((req.params as any).candidateId),
        selectedGroupIds,
      );
      return {
        candidate: publicCandidate(preview.candidate),
        selection: preview.selection,
        package: {
          spec: preview.spec,
          coverage: preview.coverage,
          files: preview.files,
          binaryPaths: preview.binaryFiles,
        },
        files: preview.diff.map(publicCandidateDiff),
      };
    } catch (error) {
      return sendArtifactError(reply, error);
    }
  });

  app.post('/api/projects/:id/candidate-revisions/:candidateId/respond', async (req, reply) => {
    const project = ownedProject((req.params as any).id, req.user!.username);
    if (!project) return reply.code(404).send({ error: '项目不存在' });
    const candidateId = String((req.params as any).candidateId);
    const { action, selectedGroupIds, note } = (req.body ?? {}) as {
      action?: 'accept' | 'revise' | 'reject';
      selectedGroupIds?: string[];
      note?: string;
    };
    if (!['accept', 'revise', 'reject'].includes(String(action))) {
      return reply.code(400).send({ error: '候选修订处理动作不正确' });
    }
    try {
      return await runWithProjectLease(leases, {
        projectId: project.id,
        operation: '处理候选修订',
        owner: `${req.user!.username}:${req.id}`,
        ttlMs: SHORT_WRITE_LEASE_TTL_MS,
      }, async () => {
        const existing = await store.getCandidateRevision(project.id, candidateId);
        if (existing.status === 'superseded') {
          m4repo.rejectCandidateRevision(project.id, candidateId);
          return reply.code(409).send({
            code: 'revision_conflict',
            error: '候选修订基线已经变化，请基于当前修订重新生成候选',
          });
        }
        if (action === 'accept') {
          if (existing.status === 'accepted') {
            if (existing.acceptedRevision) {
              const acceptedSnapshot = await store.readPackageFiles(project.id, existing.acceptedRevision);
              m4repo.markCandidateRevisionApplied(
                project.id, candidateId, acceptedSnapshot.spec.version, existing.acceptedRevision,
              );
            }
            return {
              candidate: publicCandidate(existing),
              selection: {
                requestedGroupIds: existing.selectedGroupIds,
                selectedGroupIds: existing.selectedGroupIds,
                autoIncludedGroupIds: [],
              },
              currentRevision: existing.acceptedRevision,
              replayed: true,
            };
          }
          const preview = await store.previewCandidateRevision(project.id, candidateId, selectedGroupIds);
          const current = await store.current(project.id);
          if (current?.revision.oid === existing.baseRevision) {
            const diagnosisLink = optimizationCandidateDiagnosisLinkState({
              projectId: project.id,
              candidateId,
              baseRevisionOid: existing.baseRevision,
            });
            if (diagnosisLink.linked && !diagnosisLink.current) {
              const rejected = await store.rejectCandidateRevision(project.id, {
                candidateId,
                note: diagnosisLink.reason,
                action: {
                  cause: 'optimization_diagnosis_stale',
                  summary: '关闭失效的优化候选',
                  actorKind: 'system',
                  actorName: '建模系统',
                },
              });
              m4repo.rejectCandidateRevision(project.id, candidateId);
              return reply.code(409).send({
                code: 'optimization_diagnosis_stale',
                error: `${diagnosisLink.reason}，请基于当前诊断重新生成优化建议`,
                candidate: publicCandidate(rejected),
              });
            }
          }
          const formatErrors = runFormatChecks(preview.spec, preview.files)
            .filter((finding) => finding.severity === '错误');
          if (formatErrors.length > 0) {
            return reply.code(409).send({
              error: `候选修订格式检查未通过：${formatErrors.map((finding) => finding.message).join('；')}`,
              code: 'candidate_validation_failed',
              findings: formatErrors,
            });
          }
          const accepted = await store.acceptCandidateRevision(project.id, {
            candidateId,
            selectedGroupIds,
            note,
            action: {
              cause: 'candidate_accepted',
              summary: '接受候选修订',
              actorKind: 'user',
              actorName: req.user!.username,
            },
          });
          m4repo.markCandidateRevisionApplied(
            project.id, candidateId, preview.spec.version, accepted.revision.oid,
          );
          getAuthoringState().apply({
            kind: 'publish_event',
            projectId: project.id,
            type: 'package.revision.created',
            payload: { revisionId: accepted.revision.oid, cause: 'candidate_accepted', candidateId },
          });
          return {
            candidate: publicCandidate(accepted.candidate),
            selection: accepted.selection,
            currentRevision: accepted.revision.oid,
          };
        }
        if (action === 'revise') {
          if (existing.status === 'revision_requested') {
            m4repo.returnCandidateRevision(
              project.id, candidateId, existing.decision?.note ?? String(note ?? ''),
            );
            return { candidate: publicCandidate(existing), replayed: true };
          }
          const candidate = await store.returnCandidateRevision(project.id, {
            candidateId,
            selectedGroupIds,
            note,
            action: {
              cause: 'candidate_revision_requested',
              summary: '退回候选修订',
              actorKind: 'user',
              actorName: req.user!.username,
            },
          });
          m4repo.returnCandidateRevision(project.id, candidateId, String(note ?? ''));
          return { candidate: publicCandidate(candidate) };
        }
        if (existing.status === 'rejected') {
          m4repo.rejectCandidateRevision(project.id, candidateId);
          return { candidate: publicCandidate(existing), replayed: true };
        }
        const candidate = await store.rejectCandidateRevision(project.id, {
          candidateId,
          note,
          action: {
            cause: 'candidate_rejected',
            summary: '拒绝候选修订',
            actorKind: 'user',
            actorName: req.user!.username,
          },
        });
        m4repo.rejectCandidateRevision(project.id, candidateId);
        return { candidate: publicCandidate(candidate) };
      });
    } catch (error) {
      if (error instanceof ArtifactRepositoryError && error.code === 'revision_conflict') {
        try {
          const candidate = await store.getCandidateRevision(project.id, candidateId);
          if (candidate.status === 'superseded') {
            m4repo.rejectCandidateRevision(project.id, candidateId);
          }
        } catch { /* 原始 revision_conflict 优先返回 */ }
      }
      return sendArtifactError(reply, error);
    }
  });

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

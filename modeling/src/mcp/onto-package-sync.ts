import type { FastifyInstance } from 'fastify';
import { getPackageArtifactStore } from '../artifacts/runtime.js';
import { sendHttpError } from '../http-error.js';
import { getProject } from '../projects/repo.js';
import { assertNoActivePackageDraft } from '../workflow/package-draft.js';
import { parsePackageArchive } from '../workflow/package-import.js';

// Onto owns the local workspace; revision checks protect subsequent MCP edits.
export function registerOntoPackageSyncRoute(app: FastifyInstance): void {
  app.post('/api/projects/:id/mcp/package', { bodyLimit: 70 * 1024 * 1024 }, async (request, reply) => {
    const project = getProject(String((request.params as { id: string }).id));
    if (!project || (request.ontoAdminProjectId !== project.id && project.owner !== request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    if (!project.externalId?.startsWith('onto:') || request.headers['x-onto-project-id'] !== project.externalId) {
      return reply.code(403).send({ error: '知识包同步必须绑定到对应的 Onto 项目' });
    }
    const body = (request.body ?? {}) as Record<string, unknown>;
    if (typeof body.archive !== 'string' || !body.archive.length
      || body.archive.length > 68 * 1024 * 1024
      || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/u.test(body.archive)
      || (body.expected_revision !== undefined && typeof body.expected_revision !== 'string')) {
      return reply.code(400).send({ error: '知识包请求格式无效' });
    }
    let imported: ReturnType<typeof parsePackageArchive>;
    try {
      imported = parsePackageArchive(Buffer.from(body.archive, 'base64'));
      if (!imported.spec.objects.length) throw new Error('empty package');
    } catch {
      return reply.code(400).send({ error: '知识包格式无效或没有对象', code: 'INVALID_PACKAGE' });
    }
    try {
      const store = getPackageArtifactStore();
      const initial = await store.ensureProject(project.id);
      const current = await store.current(project.id);
      if (current) {
        const snapshot = await store.readPackageArchiveFiles(project.id, current.revision.oid);
        const paths = Object.keys(imported.rawFiles);
        // Handles a lost successful response without duplicating a version.
        if (paths.length === Object.keys(snapshot.files).length && paths.every((path) =>
          snapshot.files[path] && Buffer.from(snapshot.files[path]).equals(Buffer.from(imported.rawFiles[path]!)))) {
          return { revision: current.revision.oid, synced: true, changed: false };
        }
        if (body.expected_revision !== current.revision.oid) {
          return reply.code(409).send({ error: 'MCP 知识包已修改，请先核对版本差异再同步', code: 'REVISION_CONFLICT' });
        }
      } else if (body.expected_revision !== undefined && body.expected_revision !== initial.oid) {
        return reply.code(409).send({ error: '知识包版本不匹配', code: 'REVISION_CONFLICT' });
      }
      assertNoActivePackageDraft(project.id, 'onto-package-sync');
      const saved = await store.importPackageVersion(project.id, {
        expectedRevision: current?.revision.oid ?? initial.oid,
        beforeCommit: async () => assertNoActivePackageDraft(project.id, 'onto-package-sync'),
        spec: imported.spec, coverage: imported.coverage, files: imported.rawFiles,
        note: '同步 Onto 工作区知识包',
        action: { cause: 'onto_package_sync', summary: '同步 Onto 工作区知识包',
          actorKind: 'user', actorName: request.user!.username },
      });
      return { revision: saved.revision.oid, synced: true, changed: true };
    } catch (error) {
      return sendHttpError(reply, error);
    }
  });
}
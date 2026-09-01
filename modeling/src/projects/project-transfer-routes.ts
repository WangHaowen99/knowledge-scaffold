import { createReadStream } from 'node:fs';

import type { FastifyInstance } from 'fastify';

import { config } from '../config.js';
import { getDb } from '../db/index.js';
import { getProject } from './repo.js';
import {
  HttpAgentProjectTransferClient,
  ProjectTransferJobManager,
  ProjectTransferJobNotFoundError,
} from './project-transfer-jobs.js';

let defaultJobs: ProjectTransferJobManager | undefined;

function getDefaultJobs(): ProjectTransferJobManager {
  defaultJobs ??= new ProjectTransferJobManager({
    db: getDb(),
    dataDir: config.dataDir,
    agentTransfer: new HttpAgentProjectTransferClient(config.agentBaseUrl, config.modelingServiceToken),
  });
  return defaultJobs;
}

export function registerProjectTransferRoutes(
  app: FastifyInstance,
  jobs: ProjectTransferJobManager = getDefaultJobs(),
): void {
  app.post('/api/projects/:id/transfer-exports', async (request, reply) => {
    const project = getProject(String((request.params as { id?: string }).id ?? ''));
    if (!project || project.owner !== request.user!.username) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    const job = jobs.startExport({
      projectId: project.id,
      projectName: project.name,
      owner: request.user!.username,
      runtime: project.runtime,
      authorization: String(request.headers.authorization ?? ''),
    });
    return reply.code(202).send({ job });
  });

  app.post('/api/project-transfers/imports', async (request, reply) => {
    const file = await (request as any).file({ limits: { fileSize: 512 * 1024 * 1024 } });
    if (!file) return reply.code(400).send({ error: '请选择项目迁移包' });
    if (!String(file.filename || '').toLowerCase().endsWith('.zip')) {
      return reply.code(400).send({ error: '仅支持 .zip 项目迁移包' });
    }
    try {
      const archive = new Uint8Array(await file.toBuffer());
      if (file.file?.truncated) return reply.code(413).send({ error: '项目迁移包超过大小限制' });
      const job = await jobs.startImport({
        archive,
        owner: request.user!.username,
        authorization: String(request.headers.authorization ?? ''),
      });
      return reply.code(202).send({ job });
    } catch (error) {
      if (file.file?.truncated) return reply.code(413).send({ error: '项目迁移包超过大小限制' });
      throw error;
    }
  });

  app.get('/api/project-transfers/jobs/:jobId', async (request, reply) => {
    try {
      const job = jobs.get(String((request.params as { jobId?: string }).jobId ?? ''), request.user!.username);
      return { job };
    } catch (error) {
      if (error instanceof ProjectTransferJobNotFoundError) {
        return reply.code(404).send({ error: error.message });
      }
      throw error;
    }
  });

  app.get('/api/project-transfers/jobs/:jobId/download', async (request, reply) => {
    try {
      const download = jobs.download(
        String((request.params as { jobId?: string }).jobId ?? ''),
        request.user!.username,
      );
      const encoded = encodeURIComponent(download.filename);
      return reply
        .type('application/zip')
        .header('Content-Disposition', `attachment; filename="project-transfer.zip"; filename*=UTF-8''${encoded}`)
        .send(createReadStream(download.path));
    } catch (error) {
      if (error instanceof ProjectTransferJobNotFoundError) {
        return reply.code(404).send({ error: error.message });
      }
      throw error;
    }
  });
}

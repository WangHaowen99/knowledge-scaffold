import { randomUUID } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import type { DatabaseSync } from 'node:sqlite';

import {
  buildProjectTransferArchive,
  importProjectTransferArchive,
  projectTransferFilename,
  type ImportedProjectTransfer,
  type ProjectTransferProgress,
} from './project-transfer.js';

const JOB_TTL_MS = 60 * 60_000;
const JOB_FILE_MAX_AGE_MS = 24 * 60 * 60_000;

export type ProjectTransferJobType = 'export' | 'import';
export type ProjectTransferJobStatus = 'queued' | 'running' | 'completed' | 'failed';

interface ProjectTransferJob {
  id: string;
  type: ProjectTransferJobType;
  owner: string;
  status: ProjectTransferJobStatus;
  progress: ProjectTransferProgress;
  createdAt: string;
  updatedAt: string;
  expiresAt: number;
  inputPath?: string;
  downloadPath?: string;
  filename?: string;
  project?: ImportedProjectTransfer;
  error?: string;
}

export interface ProjectTransferJobView {
  id: string;
  type: ProjectTransferJobType;
  status: ProjectTransferJobStatus;
  progress: ProjectTransferProgress;
  createdAt: string;
  updatedAt: string;
  filename?: string;
  project?: ImportedProjectTransfer;
  error?: string;
}

export interface ProjectTransferJobManagerOptions {
  db: DatabaseSync;
  dataDir: string;
  agentTransfer?: AgentProjectTransferClient;
}

export interface AgentProjectTransferClient {
  exportProject(projectId: string, authorization: string): Promise<Record<string, unknown>>;
  importProject(
    projectId: string,
    payload: Record<string, unknown>,
    authorization: string,
  ): Promise<Record<string, string>>;
  removeProject(projectId: string, authorization: string): Promise<void>;
}

export class HttpAgentProjectTransferClient implements AgentProjectTransferClient {
  constructor(
    private readonly baseUrl: string,
    private readonly serviceToken = '',
  ) {}

  async exportProject(projectId: string, authorization: string): Promise<Record<string, unknown>> {
    const response = await this.request(projectId, 'GET', authorization);
    const payload = await response.json() as unknown;
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      throw new Error('建模助手返回的迁移数据格式不正确');
    }
    return payload as Record<string, unknown>;
  }

  async importProject(
    projectId: string,
    payload: Record<string, unknown>,
    authorization: string,
  ): Promise<Record<string, string>> {
    const response = await this.request(projectId, 'POST', authorization, payload);
    const body = await response.json() as { idMap?: unknown };
    if (!body.idMap || typeof body.idMap !== 'object' || Array.isArray(body.idMap)
      || !Object.entries(body.idMap).every(([source, target]) => source && typeof target === 'string' && target)) {
      throw new Error('建模助手返回的 ID 映射不正确');
    }
    return body.idMap as Record<string, string>;
  }

  async removeProject(projectId: string, authorization: string): Promise<void> {
    await this.request(projectId, 'DELETE', authorization);
  }

  private async request(
    projectId: string,
    method: 'GET' | 'POST' | 'DELETE',
    authorization: string,
    payload?: Record<string, unknown>,
  ): Promise<Response> {
    let response: Response;
    try {
      response = await fetch(
        `${this.baseUrl.replace(/\/+$/, '')}/v1/internal/projects/${encodeURIComponent(projectId)}/transfer`,
        {
          method,
          headers: {
            ...(authorization ? { authorization } : {}),
            ...(this.serviceToken ? { 'x-modeling-service-token': this.serviceToken } : {}),
            ...(payload ? { 'content-type': 'application/json' } : {}),
          },
          ...(payload ? { body: JSON.stringify({ payload }) } : {}),
          signal: AbortSignal.timeout(5 * 60_000),
        },
      );
    } catch {
      throw new Error('建模助手迁移服务暂时不可用');
    }
    if (!response.ok) {
      const error = await response.json().catch(() => ({})) as { error?: string };
      throw new Error(error.error || '建模助手项目数据迁移失败');
    }
    return response;
  }
}

export class ProjectTransferJobNotFoundError extends Error {
  constructor() {
    super('迁移任务不存在或已经过期');
    this.name = 'ProjectTransferJobNotFoundError';
  }
}

export class ProjectTransferJobManager {
  private readonly jobs = new Map<string, ProjectTransferJob>();
  private readonly db: DatabaseSync;
  private readonly dataDir: string;
  private readonly jobDir: string;
  private readonly agentTransfer?: AgentProjectTransferClient;
  private executionTail: Promise<void> = Promise.resolve();

  constructor(options: ProjectTransferJobManagerOptions) {
    this.db = options.db;
    this.dataDir = resolve(options.dataDir);
    this.jobDir = join(this.dataDir, 'tmp', 'project-transfer-jobs');
    this.agentTransfer = options.agentTransfer;
    void this.cleanupOldJobFiles();
  }

  startExport(input: {
    projectId: string;
    owner: string;
    projectName: string;
    runtime: string;
    authorization: string;
  }): ProjectTransferJobView {
    const job = this.createJob('export', input.owner);
    this.jobs.set(job.id, job);
    setImmediate(() => this.enqueue(() => this.runExport(job, input)));
    return publicJob(job);
  }

  async startImport(input: {
    archive: Uint8Array;
    owner: string;
    authorization: string;
    projectName?: string;
    assertNameAvailable?: () => void;
  }): Promise<ProjectTransferJobView> {
    const job = this.createJob('import', input.owner);
    await mkdir(this.jobDir, { recursive: true, mode: 0o700 });
    job.inputPath = join(this.jobDir, `${job.id}.upload`);
    await writeFile(job.inputPath, input.archive, { mode: 0o600, flag: 'wx' });
    this.jobs.set(job.id, job);
    setImmediate(() => this.enqueue(() => this.runImport(job, input)));
    return publicJob(job);
  }

  get(jobId: string, owner: string): ProjectTransferJobView {
    this.cleanupExpiredJobs();
    return publicJob(this.ownedJob(jobId, owner));
  }

  download(jobId: string, owner: string): { path: string; filename: string } {
    this.cleanupExpiredJobs();
    const job = this.ownedJob(jobId, owner);
    if (job.type !== 'export' || job.status !== 'completed' || !job.downloadPath || !job.filename
      || !existsSync(job.downloadPath)) {
      throw new ProjectTransferJobNotFoundError();
    }
    return { path: job.downloadPath, filename: job.filename };
  }

  private createJob(type: ProjectTransferJobType, owner: string): ProjectTransferJob {
    this.cleanupExpiredJobs();
    const now = new Date();
    return {
      id: `ptj_${randomUUID().replaceAll('-', '').slice(0, 20)}`,
      type,
      owner,
      status: 'queued',
      progress: { percent: 0, message: type === 'export' ? '等待导出' : '等待导入' },
      createdAt: now.toISOString(),
      updatedAt: now.toISOString(),
      expiresAt: now.getTime() + JOB_TTL_MS,
    };
  }

  private async runExport(job: ProjectTransferJob, input: {
    projectId: string;
    projectName: string;
    runtime: string;
    authorization: string;
  }): Promise<void> {
    this.markRunning(job);
    try {
      let agentRuntime: Record<string, unknown> | undefined;
      if (input.runtime === 'agent') {
        if (!this.agentTransfer) throw new Error('目标环境未配置建模助手迁移服务');
        this.markProgress(job, { percent: 4, message: '正在收集建模助手记录' });
        agentRuntime = await this.agentTransfer.exportProject(input.projectId, input.authorization);
      }
      const archive = await buildProjectTransferArchive({
        db: this.db,
        dataDir: this.dataDir,
        projectId: input.projectId,
        owner: job.owner,
        agentRuntime,
        onProgress: (progress) => this.markProgress(job, progress),
      });
      await mkdir(this.jobDir, { recursive: true, mode: 0o700 });
      const downloadPath = join(this.jobDir, `${job.id}.zip`);
      await writeFile(downloadPath, archive, { mode: 0o600, flag: 'wx' });
      job.downloadPath = downloadPath;
      job.filename = projectTransferFilename(input.projectName);
      this.markCompleted(job, { percent: 100, message: '项目迁移包已生成' });
    } catch (error) {
      this.markFailed(job, error);
    }
  }

  private async runImport(
    job: ProjectTransferJob,
    input: { authorization: string; projectName?: string; assertNameAvailable?: () => void },
  ): Promise<void> {
    this.markRunning(job);
    try {
      if (!job.inputPath) throw new Error('项目迁移包临时文件不存在');
      // 任务串行执行，入队后到真正执行前可能出现新的同名项目，执行前再校验一次
      input.assertNameAvailable?.();
      const archive = new Uint8Array(await readFile(job.inputPath));
      job.project = await importProjectTransferArchive({
        db: this.db,
        dataDir: this.dataDir,
        archive,
        owner: job.owner,
        ...(input.projectName ? { projectName: input.projectName } : {}),
        ...(this.agentTransfer ? {
          importAgentRuntime: (projectId, payload) => this.agentTransfer!.importProject(
            projectId,
            payload,
            input.authorization,
          ),
          removeAgentRuntime: (projectId) => this.agentTransfer!.removeProject(projectId, input.authorization),
        } : {}),
        onProgress: (progress) => this.markProgress(job, progress),
      });
      this.markCompleted(job, { percent: 100, message: '项目导入完成' });
    } catch (error) {
      this.markFailed(job, error);
    } finally {
      if (job.inputPath) await rm(job.inputPath, { force: true }).catch(() => undefined);
      job.inputPath = undefined;
    }
  }

  private markRunning(job: ProjectTransferJob): void {
    job.status = 'running';
    this.markProgress(job, { percent: 1, message: job.type === 'export' ? '正在导出项目' : '正在导入项目' });
  }

  private markProgress(job: ProjectTransferJob, progress: ProjectTransferProgress): void {
    job.progress = {
      percent: Math.min(100, Math.max(0, Math.round(progress.percent))),
      message: progress.message,
    };
    job.updatedAt = new Date().toISOString();
  }

  private markCompleted(job: ProjectTransferJob, progress: ProjectTransferProgress): void {
    job.status = 'completed';
    this.markProgress(job, progress);
  }

  private markFailed(job: ProjectTransferJob, error: unknown): void {
    job.status = 'failed';
    job.error = error instanceof Error ? error.message : '项目迁移失败';
    this.markProgress(job, { percent: job.progress.percent, message: '项目迁移失败' });
  }

  private ownedJob(jobId: string, owner: string): ProjectTransferJob {
    const job = this.jobs.get(jobId);
    if (!job || job.owner !== owner) throw new ProjectTransferJobNotFoundError();
    return job;
  }

  private enqueue(task: () => Promise<void>): void {
    const current = this.executionTail.catch(() => undefined).then(task);
    this.executionTail = current.catch(() => undefined);
  }

  private cleanupExpiredJobs(): void {
    const now = Date.now();
    for (const [id, job] of this.jobs) {
      if (job.expiresAt > now) continue;
      this.jobs.delete(id);
      for (const path of [job.inputPath, job.downloadPath]) {
        if (path) void rm(path, { force: true }).catch(() => undefined);
      }
    }
  }

  private async cleanupOldJobFiles(): Promise<void> {
    await mkdir(this.jobDir, { recursive: true, mode: 0o700 });
    const now = Date.now();
    for (const entry of await readdir(this.jobDir)) {
      const path = join(this.jobDir, entry);
      try {
        const metadata = await stat(path);
        if (now - metadata.mtimeMs > JOB_FILE_MAX_AGE_MS) await rm(path, { recursive: true, force: true });
      } catch {
        // 并发任务可能刚好完成并移走文件，下一次清理会继续处理。
      }
    }
  }
}

function publicJob(job: ProjectTransferJob): ProjectTransferJobView {
  return {
    id: job.id,
    type: job.type,
    status: job.status,
    progress: { ...job.progress },
    createdAt: job.createdAt,
    updatedAt: job.updatedAt,
    ...(job.filename ? { filename: job.filename } : {}),
    ...(job.project ? { project: { ...job.project, excluded: [...job.project.excluded] } } : {}),
    ...(job.error ? { error: job.error } : {}),
  };
}

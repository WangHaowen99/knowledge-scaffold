import type { FastifyInstance } from 'fastify';

import { backgroundAuthSessions } from '../auth/background-session.js';
import { sendHttpError } from '../http-error.js';
import { getProject } from '../projects/repo.js';
import { parsePackageArchive } from '../workflow/package-import.js';
import type { EvaluationAuthSession } from './auth-session.js';
import { getProjectDataSource } from './data-source-repo.js';
import { isEvaluationActive } from './engine.js';
import {
  parseSolverValidationCases,
  relayAuthSession,
  runSolverContractValidation,
  SolverContractValidationError,
  type SolverValidationCase,
} from './solver-contract-validation.js';

export interface SolverContractValidationRouteDependencies {
  run?: typeof runSolverContractValidation;
  isEvaluationActive?: (projectId: string) => boolean;
  authSessions?: { get(username: string): EvaluationAuthSession | undefined };
}

function fieldValue(fields: any, name: string): string {
  return String(fields?.[name]?.value ?? '').trim();
}

/**
 * 求解契约端到端验证入口（供 onto-modeling 编排调用）。同步执行到所有用例终态，
 * 只返回公开摘要；请求体只有知识包 ZIP 与验证用例，不接受 Solver 地址或凭据。
 */
export function registerSolverContractValidationRoutes(
  app: FastifyInstance,
  dependencies: SolverContractValidationRouteDependencies = {},
): void {
  const run = dependencies.run ?? runSolverContractValidation;
  const evaluationActive = dependencies.isEvaluationActive ?? isEvaluationActive;
  const authSessions = dependencies.authSessions ?? backgroundAuthSessions;
  const activeProjects = new Set<string>();

  app.post('/api/projects/:id/solver-contract-validations', async (req, reply) => {
    const username = req.user!.username;
    const project = getProject(String((req.params as any).id ?? ''));
    if (!project || project.owner !== username) return reply.code(404).send({ error: '项目不存在' });
    const dataSource = getProjectDataSource(project.id);
    if (!dataSource) {
      return reply.code(409).send({
        code: 'EVALUATION_DATA_SOURCE_REQUIRED',
        error: '尚未配置数据源，请先完成“数据源配置”步骤',
      });
    }
    if (evaluationActive(project.id) || activeProjects.has(project.id)) {
      return reply.code(409).send({
        code: 'SOLVER_VALIDATION_BUSY',
        error: '该项目已有评测或求解契约验证在运行，请稍后重试',
      });
    }

    let file: any;
    let zip: Buffer;
    try {
      file = await (req as any).file();
      if (!file) return reply.code(400).send({ code: 'SOLVER_PACKAGE_REQUIRED', error: '缺少知识包 ZIP' });
      zip = await file.toBuffer();
    } catch (error) {
      return sendHttpError(reply, error, { logMessage: '读取求解验证知识包失败' });
    }
    let cases: SolverValidationCase[];
    try {
      cases = parseSolverValidationCases(fieldValue(file.fields, 'cases'));
    } catch (error) {
      const failure = error as SolverContractValidationError;
      return reply.code(failure.statusCode).send({ code: failure.code, error: failure.message });
    }
    let ontologyId: string;
    let version: string;
    try {
      const archive = parsePackageArchive(new Uint8Array(zip));
      ontologyId = String(archive.spec.id ?? '').trim();
      version = String(archive.spec.version ?? '').trim();
    } catch {
      return reply.code(422).send({ code: 'SOLVER_PACKAGE_INVALID', error: '知识包 ZIP 内容不正确' });
    }

    const token = String(req.headers.authorization ?? '').replace(/^Bearer\s+/i, '');
    const controller = new AbortController();
    reply.raw.on('close', () => {
      if (!reply.raw.writableFinished) controller.abort(new Error('求解契约验证请求已断开'));
    });
    activeProjects.add(project.id);
    try {
      const report = await run({
        projectId: project.id,
        owner: username,
        auth: relayAuthSession(token, authSessions.get(username)),
        dataSource,
        ontologyId,
        version,
        zip: new Uint8Array(zip),
        cases,
        idempotencyPrefix: fieldValue(file.fields, 'run_id'),
        signal: controller.signal,
      });
      return { validation: report };
    } catch (error) {
      if (error instanceof SolverContractValidationError) {
        return reply.code(error.statusCode).send({ code: error.code, error: error.message, stage: error.stage });
      }
      return sendHttpError(reply, error, { logMessage: '求解契约端到端验证失败' });
    } finally {
      activeProjects.delete(project.id);
    }
  });
}

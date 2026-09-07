import type { FastifyInstance } from 'fastify';

import { sendHttpError } from '../http-error.js';
import { canWriteProject, getProject } from '../projects/repo.js';
import {
  acceptanceModeEnabled,
  armAcceptanceFault,
  closeAllAcceptanceFaults,
  closeAcceptanceFault,
  isAcceptanceFaultId,
  listAcceptanceFaults,
  takeAcceptanceFault,
} from './faults.js';
import {
  ACCEPTANCE_SCENARIOS,
  createAcceptanceScenario,
  isAcceptanceScenarioId,
} from './scenarios.js';

export function registerAcceptanceRoutes(app: FastifyInstance): void {
  app.get('/api/acceptance', async (request, reply) => {
    if (!acceptanceModeEnabled()) return reply.code(404).send({ error: 'Not Found' });
    return {
      enabled: true,
      scenarios: ACCEPTANCE_SCENARIOS.map(([id, title, description]) => ({ id, title, description })),
      faults: listAcceptanceFaults(request.user!.username),
    };
  });

  app.post('/api/acceptance/scenarios/:scenarioId/create', async (request, reply) => {
    if (!acceptanceModeEnabled()) return reply.code(404).send({ error: 'Not Found' });
    const scenarioId = String((request.params as any).scenarioId ?? '');
    if (!isAcceptanceScenarioId(scenarioId)) return reply.code(404).send({ error: '验收场景不存在' });
    try {
      const result = await createAcceptanceScenario(request.user!.username, scenarioId);
      return reply.code(201).send(result);
    } catch (error) {
      return sendHttpError(reply, error, {
        code: 'ACCEPTANCE_SCENARIO_CREATE_FAILED',
        message: '创建验收场景失败，请稍后重试',
        logMessage: `创建合成验收场景失败（${scenarioId}）`,
      });
    }
  });

  app.post('/api/acceptance/faults/:faultId/arm', async (request, reply) => {
    if (!acceptanceModeEnabled()) return reply.code(404).send({ error: 'Not Found' });
    const faultId = String((request.params as any).faultId ?? '');
    if (!isAcceptanceFaultId(faultId)) return reply.code(404).send({ error: '验收故障编号不存在' });
    try {
      return { fault: armAcceptanceFault(request.user!.username, faultId) };
    } catch (error) {
      return sendHttpError(reply, error, { logMessage: '启用验收故障失败' });
    }
  });

  app.post('/api/acceptance/faults/:faultId/close', async (request, reply) => {
    if (!acceptanceModeEnabled()) return reply.code(404).send({ error: 'Not Found' });
    const faultId = String((request.params as any).faultId ?? '');
    if (!isAcceptanceFaultId(faultId)) return reply.code(404).send({ error: '验收故障编号不存在' });
    try {
      return { fault: closeAcceptanceFault(request.user!.username, faultId) };
    } catch (error) {
      return sendHttpError(reply, error, { logMessage: '关闭验收故障失败' });
    }
  });

  app.post('/api/acceptance/faults/close-all', async (request, reply) => {
    if (!acceptanceModeEnabled()) return reply.code(404).send({ error: 'Not Found' });
    return { faults: closeAllAcceptanceFaults(request.user!.username) };
  });

  app.post('/internal/agent/acceptance/faults/:faultId/consume', async (request, reply) => {
    if (!acceptanceModeEnabled()) return reply.code(404).send({ error: 'Not Found' });
    const projectId = String(request.headers['x-modeling-project'] ?? '').trim();
    const runId = String(request.headers['x-modeling-run'] ?? '').trim();
    const project = getProject(projectId);
    if (!projectId || !runId || !project || !canWriteProject(projectId, request.user!.username)) {
      return reply.code(404).send({ error: '项目不存在' });
    }
    const effect = takeAcceptanceFault(
      request.user!.username,
      String((request.params as any).faultId ?? ''),
      runId,
      { stickyRun: Boolean((request.body as any)?.stickyRun) },
    );
    return { active: Boolean(effect), effect };
  });
}

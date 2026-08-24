import assert from 'node:assert/strict';
import test from 'node:test';

import {
  inspectGeneratedKnowledge,
  KnowledgeStructureRunner,
} from '../src/ai-construction/knowledge-structure-runner.js';
import { GenerationWaitingForExpert } from '../src/workflow/generation-gate.js';
import type { FlowchartOutput } from '../src/authoring/workflow-extras.js';
import type { PackageSpec, Project } from '../src/types.js';

const project: Project = {
  id: 'pj_runner', owner: 'alice', name: '流程知识生成', domain: '审批', background: '',
  stage: 'generate', kind: 'modeling', sensitivityLevel: 'standard',
  workflowProfile: {
    enabledSteps: [], requiredSteps: [], releaseRequested: false, externalResearch: false,
  },
  createdAt: '2026-07-23T00:00:00.000Z', updatedAt: '2026-07-23T00:00:00.000Z',
};

const flowchart: FlowchartOutput = {
  diagrams: [{
    id: 'approval', title: '申请审批',
    processMeta: {
      trigger: '收到申请', actors: ['经办人'], inputs: ['申请单'],
      exceptions: [], outputs: ['审批结果'], handoffs: [],
    },
    nodes: [
      { id: 'start', label: '收到申请', kind: 'start' },
      { id: 'end', label: '审批结果', kind: 'end' },
    ],
    edges: [{ id: 'edge-start-end', from: 'start', to: 'end', kind: 'sequence' }],
  }],
};

const spec: PackageSpec = {
  id: 'approval-agent', name: '审批', description: '审批', version: '1.0.0',
  language: 'zh-CN', defaultProviderAlias: 'data',
  objects: [{
    key: 'Application', title: '申请单', description: '业务申请单对象', fields: [],
    relations: [{ target: 'Decision', relation: 'produces', note: '形成审批结果' }],
  }],
  operations: [{
    key: 'Approve', title: '审批申请', description: '按流程审批', inputs: [], outputs: [],
    flow: ['受理申请', '形成审批结果'], relatedObjects: ['Application'],
  }],
  rules: [{ key: 'Complete', title: '完整性规则', content: '资料完整才能进入审批。' }],
  metrics: [],
};

test('默认知识检查只执行确定性格式和规则校验', async () => {
  const validation = await inspectGeneratedKnowledge(project, {
    spec,
    coverage: { covered: [], uncovered: [] },
    files: {},
    version: '1.0.0',
    revisionId: 'revision-deterministic-check',
  }, {
    tables: [],
    documents: [],
  });

  assert.ok(validation.findings.length > 0);
  assert.ok(validation.findings.every((finding) => (
    finding.source === 'format' || finding.source === 'rule'
  )));
});

test('runner 依次启动生成、执行校验并提交结构化完成摘要', async () => {
  const calls: Array<[string, unknown?]> = [];
  const orchestrator = {
    startKnowledgeStructure: (projectId: string) => {
      calls.push(['start', projectId]);
      return { aiConstruction: {}, message: {} };
    },
    startKnowledgeStructureValidation: (projectId: string, input: unknown) => {
      calls.push(['validation', { projectId, input }]);
      return { aiConstruction: {}, message: {} };
    },
    completeKnowledgeStructure: (projectId: string, input: unknown) => {
      calls.push(['complete', { projectId, input }]);
      return { aiConstruction: { revision: 9 }, message: { id: 'done' } };
    },
    failKnowledgeStructure: (projectId: string, reason: string) => {
      calls.push(['fail', { projectId, reason }]);
      return { aiConstruction: {}, message: {} };
    },
  };
  const runner = new KnowledgeStructureRunner({
    orchestrator: orchestrator as any,
    generator: async (_project, context, _llm, confirmed) => {
      assert.equal(confirmed, flowchart);
      assert.deepEqual(context.documents, [{
        id: 'doc-policy', filename: '审批制度.md', textContent: '审批规则正文',
      }]);
      return {
        spec, coverage: { covered: [], uncovered: [] }, files: {}, version: '1.0.0',
        revisionId: 'revision-generated',
      };
    },
    validator: async () => ({ errors: 0, warnings: 1, hints: 2, findings: [] }),
  });

  const result = await runner.run(project, {
    tables: [],
    documents: [{ id: 'doc-policy', filename: '审批制度.md', textContent: '审批规则正文' }],
  }, flowchart);

  assert.deepEqual(calls.map(([name]) => name), ['start', 'validation', 'complete']);
  const completed = (calls[2][1] as any).input;
  assert.equal(completed.packageRevisionOid, 'revision-generated');
  assert.deepEqual(completed.counts, {
    operations: 1, objects: 1, relations: 1, rules: 1, metrics: 0,
  });
  assert.deepEqual(completed.validation, {
    errors: 0, warnings: 1, hints: 2, findings: [],
  });
  assert.equal(result.message.id, 'done');
});

test('runner 在产物生成后先切换到知识包校验任务再执行耗时校验', async () => {
  const calls: string[] = [];
  const orchestrator = {
    startKnowledgeStructure: () => {
      calls.push('start_generation');
      return { aiConstruction: {}, message: {} };
    },
    startKnowledgeStructureValidation: (_projectId: string, input: unknown) => {
      calls.push('start_validation');
      assert.deepEqual(input, {
        packageRevisionOid: 'revision-generated',
        messageKey: 'knowledge.validating.1.0.0',
        version: '1.0.0',
      });
      return { aiConstruction: {}, message: {} };
    },
    completeKnowledgeStructure: () => {
      calls.push('complete');
      return { aiConstruction: { revision: 10 }, message: { id: 'done' } };
    },
    failKnowledgeStructure: () => {
      calls.push('fail');
      return { aiConstruction: {}, message: {} };
    },
  };
  const runner = new KnowledgeStructureRunner({
    orchestrator: orchestrator as any,
    generator: async () => {
      calls.push('generated');
      return {
        spec, coverage: { covered: [], uncovered: [] }, files: {}, version: '1.0.0',
        revisionId: 'revision-generated',
      };
    },
    validator: async () => {
      assert.deepEqual(calls, ['start_generation', 'generated', 'start_validation']);
      calls.push('validated');
      return { errors: 0, warnings: 0, hints: 0, findings: [] };
    },
  });

  await runner.run(project, { tables: [], documents: [] }, flowchart);

  assert.deepEqual(calls, [
    'start_generation', 'generated', 'start_validation', 'validated', 'complete',
  ]);
});

test('runner 在生成失败时把当前任务保留为可重试失败态', async () => {
  const calls: Array<[string, unknown?]> = [];
  const orchestrator = {
    startKnowledgeStructure: () => {
      calls.push(['start']);
      return { aiConstruction: {}, message: {} };
    },
    completeKnowledgeStructure: () => {
      calls.push(['complete']);
      return { aiConstruction: {}, message: {} };
    },
    failKnowledgeStructure: (_projectId: string, reason: string) => {
      calls.push(['fail', reason]);
      return { aiConstruction: { revision: 5 }, message: { id: 'failed' } };
    },
  };
  const runner = new KnowledgeStructureRunner({
    orchestrator: orchestrator as any,
    generator: async () => { throw new Error('模型响应中断'); },
    validator: async () => ({ errors: 0, warnings: 0, hints: 0, findings: [] }),
  });

  await assert.rejects(
    runner.run(project, { tables: [], documents: [] }, flowchart),
    /模型响应中断/,
  );
  assert.deepEqual(calls, [['start'], ['fail', '模型响应中断']]);
});

test('模型两次自动补交仍失败时转为专家决策，不直接标记失败', async () => {
  const calls: string[] = [];
  const failure = {
    phase: 'object_batch_1', batchKind: 'object' as const, batchIndex: 1,
    title: '第 1 批对象仍未通过', issues: ['字段不存在'],
  };
  const interaction = {
    id: 'ai_generation_gate', projectId: project.id, runId: 'ar_generation',
    type: 'decision' as const, status: 'pending' as const,
    payload: { purpose: 'generation_gate_skip' }, createdAt: '2026-07-25T00:00:00.000Z',
  };
  const runner = new KnowledgeStructureRunner({
    orchestrator: {
      startKnowledgeStructure: () => { calls.push('start'); return { aiConstruction: {}, message: {} }; },
      waitForKnowledgeStructureDecision: (_projectId: string, input: any) => {
        calls.push('wait');
        assert.equal(input.failure.phase, 'object_batch_1');
        return { aiConstruction: { revision: 7 }, message: { id: 'waiting' } };
      },
      failKnowledgeStructure: () => { calls.push('fail'); return { aiConstruction: {}, message: {} }; },
    } as any,
    generator: async () => { throw new GenerationWaitingForExpert(failure, interaction); },
  });

  const result = await runner.run(project, { tables: [], documents: [] }, flowchart);

  assert.equal(result.status, 'waiting_user');
  assert.equal(result.interaction.id, interaction.id);
  assert.deepEqual(calls, ['start', 'wait']);
});

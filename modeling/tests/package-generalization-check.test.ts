import assert from 'node:assert/strict';
import test from 'node:test';

import { buildSemanticCheckPrompt } from '../src/workflow/package-check.js';
import type { PackageSpec, Project } from '../src/types.js';

const project: Project = {
  id: 'pj-generalization', owner: 'owner', kind: 'modeling', name: '通用知识建模',
  domain: '业务处理', background: '支持不同输入组合和异常分支', stage: 'generate',
  sensitivityLevel: 'standard',
  workflowProfile: { enabledSteps: [], requiredSteps: [], releaseRequested: true, externalResearch: false },
  createdAt: '2026-07-24T00:00:00.000Z', updatedAt: '2026-07-24T00:00:00.000Z',
};

const spec: PackageSpec = {
  id: 'generic-agent', name: '通用知识建模', description: '通用处理知识', version: '1.0.0',
  language: 'zh-CN', defaultProviderAlias: 'data',
  objects: [{ key: 'Request', title: '请求', description: '需要按规则处理的请求对象', fields: [] }],
  operations: [{
    key: 'HandleRequest', title: '处理请求', description: '按输入和规则处理请求',
    inputs: [{ param: 'request', type: 'object', required: true }], outputs: [],
    flow: ['读取 object:Request', '按 rule:Eligibility 判断'], relatedObjects: ['Request'],
    usesRules: ['Eligibility'], usesMetrics: ['Score'], script: { key: 'CalculateScore' },
  }],
  rules: [{ key: 'Eligibility', title: '适用判断', content: '按已确认条件判断是否适用。' }],
  metrics: [{ key: 'Score', title: '评分', caliber: '按输入参数计算', boundaries: ['仅使用当前请求'] }],
  scripts: [{
    key: 'CalculateScore', title: '计算评分', description: '参数化计算评分', boundOperation: 'HandleRequest',
    runtime: 'python', entrypoint: 'calculate_score.py', timeoutSeconds: 30,
    parameters: [{ name: 'request', flag: '--request', type: 'string', required: true }],
    outputs: [{ key: 'score', type: 'number' }], boundaries: ['不内置样例答案'],
    sourceCode: 'import argparse\nimport json\np=argparse.ArgumentParser()\np.add_argument("--request",required=True)\na=p.parse_args()\nprint(json.dumps({"score":0}))',
  }],
};

test('语义体检覆盖完整知识结构、证据边界和样例外泛化风险', () => {
  const prompt = buildSemanticCheckPrompt(project, spec);

  assert.match(prompt.system, /参数化与泛化/);
  assert.match(prompt.system, /不得把示例中的具体输入或答案写成通用规则/);
  assert.match(prompt.system, /证据与假设边界/);
  assert.match(prompt.system, /边界、异常和未见过的输入组合/);
  assert.match(prompt.body, /Eligibility/);
  assert.match(prompt.body, /Score/);
  assert.match(prompt.body, /CalculateScore/);
});
